"""STOP 요청·취소 ACK·실제 정지 확인 계약 (md/개발플랜.md 1-08).

forstick의 `taskplan_bridge.py`에서 6라운드에 걸쳐 잡은 동시성 결함들의
"규칙"만 가져와 다시 썼다. ROS·rclpy·특정 로봇에 의존하지 않는다 — 전송
계층은 호출자가 콜백으로 주입한다.

이관한 규칙 (각 항목은 실제로 오보고를 일으켰던 경로다):

R1. 전송 직전 정지 확인과 pending 등록은 하나의 임계구역이다.
    (전송 전 STOP이 들어와도 그 뒤 goal이 등록되는 경합 차단)
R2. pending 해제와 active 등록도 하나의 임계구역이다.
R3. 전송 응답이 타임아웃돼도 재전송하지 않는다. 원래 요청을 계속 추적한다.
    (재전송 후 첫 응답이 늦게 도착해 추적을 잃던 문제)
R4. 늦게 수락된 goal은, 정지 상태이거나 다른 goal이 활성이면 기존 것을
    덮어쓰지 않고 orphan으로 별도 보존한다. 취소를 요청하되 추적은 유지한다.
R5. 결과 대기 타임아웃은 handle을 지우지 않는다. 종료가 확인되지 않은 goal은
    계속 추적한다.
R6. active handle은 동일성(identity) 확인으로만 해제한다. 그 사이 새 goal이
    자리를 차지했으면 지우지 않는다.
R7. orphan은 확정적인 결과를 받았을 때만 목록에서 제거한다.
R8. 정지 판정: active와 orphan 전부에 취소+확인을 시도한다. 그와 별개로
    pending이 하나라도 있으면 handle 확인 결과와 무관하게 미확인이다.
    handle도 pending도 없으면 실측 정지 확인으로 판정한다.
R9. 취소 ACK만으로 정지를 확정하지 않는다.
R10. 실측 정지 확인은 표본 누락·NaN/Inf·오래된 표본을 정지로 처리하지 않고
     연속 정지 시간을 초기화한다.

채널("arm"/"gripper" 같은 독립 명령 경로)의 이름은 Adapter가 선언한다.
이 모듈은 채널 이름을 해석하지 않는다(계획.md 27장).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Callable, Hashable, Iterable, Mapping, Sequence

from core.policy import StopPolicy
from core.reason_codes import ReasonCode


class StopContractError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class JointSample:
    """정지 확인에 쓰는 관절 관측 표본. 시각은 주입값이다(실제 시간을 안 기다린다)."""

    at: float
    positions: Mapping[str, float]
    #: 관측 자체가 실패했으면 False. 이때 positions는 신뢰할 수 없다.
    valid: bool = True


@dataclass
class ChannelTracking:
    """한 채널의 goal 추적 상태. 직접 조작하지 않고 GoalTracker를 통해 바꾼다."""

    pending: set[Hashable] = field(default_factory=set)
    active: Hashable | None = None
    orphans: list[Hashable] = field(default_factory=list)


def confirm_motion_stopped(
    samples: Sequence[JointSample],
    joint_names: Sequence[str],
    tolerance: float,
    hold_sec: float,
    max_sample_gap_sec: float,
) -> bool:
    """실측 정지 확인 (R10).

    허용치 안에 `hold_sec` 이상 연속으로 머물렀는지 본다. 아래는 모두 정지로
    처리하지 않고 연속 시간을 초기화한다:
      - 표본 무효(valid=False)
      - 요청한 관절이 표본에 없음
      - 값이 NaN/Inf
      - 직전 표본과의 간격이 max_sample_gap_sec 초과(수신 공백)
      - 같은 시각의 표본이 반복 도착(새 관측이 아니므로 누적하지 않음)
    """
    if not joint_names:
        raise StopContractError(
            ReasonCode.CONFIG_MISSING, "확인할 관절 이름이 비어 있다"
        )
    anchor: dict[str, float] | None = None
    stable_since: float | None = None
    last_at: float | None = None

    for s in samples:
        if last_at is not None and s.at <= last_at:
            # 같은/역행 시각은 새 관측이 아니다. 누적하지 않고 건너뛴다.
            continue
        gap_ok = last_at is None or (s.at - last_at) <= max_sample_gap_sec
        last_at = s.at

        if not s.valid or not gap_ok:
            anchor, stable_since = None, None
            continue
        try:
            values = {n: float(s.positions[n]) for n in joint_names}
        except KeyError:
            anchor, stable_since = None, None
            continue
        if any(not math.isfinite(v) for v in values.values()):
            anchor, stable_since = None, None
            continue

        if anchor is None:
            anchor, stable_since = values, s.at
            continue
        if all(abs(values[n] - anchor[n]) <= tolerance for n in joint_names):
            if stable_since is not None and (s.at - stable_since) >= hold_sec:
                return True
        else:
            anchor, stable_since = values, s.at
    return False


class GoalTracker:
    """채널별 goal 수명 추적. 모든 상태 변경은 하나의 락 아래에서 일어난다."""

    def __init__(self, channels: Iterable[str], policy: StopPolicy):
        chans = tuple(channels)
        if not chans:
            raise StopContractError(ReasonCode.CONFIG_MISSING, "채널이 비어 있다")
        for ch in chans:
            policy.tolerance_for(ch)  # 허용치 없는 채널을 조기에 차단
        self._policy = policy
        self._lock = threading.RLock()
        self._ch: dict[str, ChannelTracking] = {c: ChannelTracking() for c in chans}
        self._stopped = False

    # ── 조회 ────────────────────────────────────────────────────────────
    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped

    def snapshot(self, channel: str) -> ChannelTracking:
        with self._lock:
            t = self._require(channel)
            return ChannelTracking(set(t.pending), t.active, list(t.orphans))

    def _require(self, channel: str) -> ChannelTracking:
        if channel not in self._ch:
            raise StopContractError(
                ReasonCode.CONFIG_MISSING, f"등록되지 않은 채널: {channel!r}"
            )
        return self._ch[channel]

    # ── 수명 ────────────────────────────────────────────────────────────
    def begin_send(self, channel: str, request_id: Hashable) -> bool:
        """R1 — 정지 확인과 pending 등록을 한 임계구역에서 한다.
        정지 상태면 등록하지 않고 False를 돌려준다(전송하면 안 된다)."""
        with self._lock:
            t = self._require(channel)
            if self._stopped:
                return False
            t.pending.add(request_id)
            return True

    def on_accept(self, channel: str, request_id: Hashable, goal_ref: Hashable) -> str:
        """R2, R4 — pending 해제와 active/orphan 등록을 한 임계구역에서 한다.
        반환값: "active" | "orphan"."""
        with self._lock:
            t = self._require(channel)
            t.pending.discard(request_id)
            if self._stopped or t.active is not None:
                # 기존 active를 덮어쓰지 않는다. 취소 대상으로 별도 보존한다.
                t.orphans.append(goal_ref)
                return "orphan"
            t.active = goal_ref
            return "active"

    def on_send_timeout(self, channel: str, request_id: Hashable) -> None:
        """R3 — 재전송하지 않는다. pending을 유지해 추적을 잃지 않는다."""
        with self._lock:
            t = self._require(channel)
            if request_id not in t.pending:
                raise StopContractError(
                    ReasonCode.EXEC_SEND_TIMEOUT,
                    f"추적되지 않는 요청의 전송 타임아웃: {request_id!r}",
                )
            # 의도적으로 아무것도 지우지 않는다.

    def on_result_timeout(self, channel: str, goal_ref: Hashable) -> None:
        """R5 — 결과 타임아웃은 handle을 지우지 않는다."""
        with self._lock:
            self._require(channel)
            # 의도적으로 아무것도 지우지 않는다.

    def on_result(self, channel: str, goal_ref: Hashable) -> None:
        """R6, R7 — 확정 결과를 받았을 때만 정리한다. active는 동일성 확인."""
        with self._lock:
            t = self._require(channel)
            if t.active is goal_ref:
                t.active = None
            t.orphans = [o for o in t.orphans if o is not goal_ref]

    # ── 정지 ────────────────────────────────────────────────────────────
    def request_stop(self) -> None:
        """정지를 즉시 래치한다. 이후 begin_send는 모두 거부된다."""
        with self._lock:
            self._stopped = True

    def reset_for_new_plan(self) -> None:
        """새 계획을 수락할 때 정지 래치를 푼다.

        forstick에서 이 초기화가 실행 허가 단계보다 뒤에 있어서, 한 번 STOP이
        걸린 뒤 모든 실행 요청이 이유 없이 거부되던 문제가 있었다. 계약에서는
        이 호출을 명시적으로 분리해 둔다. 추적 중인 goal이 남아 있으면 초기화를
        거부한다 — 이전 goal이 살아 있는데 새 계획을 시작하지 않는다."""
        with self._lock:
            busy = {
                c: (len(t.pending), t.active is not None, len(t.orphans))
                for c, t in self._ch.items()
                if t.pending or t.active is not None or t.orphans
            }
            if busy:
                raise StopContractError(
                    ReasonCode.EXEC_STOP_UNCONFIRMED,
                    f"추적 중인 goal이 남아 새 계획을 시작할 수 없다: {busy}",
                )
            self._stopped = False

    def confirm_stop(
        self,
        channel: str,
        joint_names: Sequence[str],
        cancel: Callable[[Hashable], bool],
        collect_samples: Callable[[], Sequence[JointSample]],
    ) -> bool:
        """R8, R9 — 채널 하나의 정지를 판정한다.

        `cancel(goal_ref)`는 취소 요청 후 ACK 수신 여부를 돌려준다. ACK만으로
        정지를 확정하지 않고, 항상 실측 확인을 함께 본다.
        `collect_samples()`는 호출 시점의 관측 표본을 돌려준다(주입).
        """
        with self._lock:
            t = self._require(channel)
            handles = ([t.active] if t.active is not None else []) + list(t.orphans)
            pending_count = len(t.pending)
        tolerance = self._policy.tolerance_for(channel)

        handles_ok = True
        for h in handles:
            acked = bool(cancel(h))
            measured = confirm_motion_stopped(
                collect_samples(),
                joint_names,
                tolerance,
                self._policy.hold_sec,
                self._policy.max_sample_gap_sec,
            )
            # R9: ACK만으로는 부족하다. 둘 다 필요하다.
            handles_ok = (acked and measured) and handles_ok

        if pending_count:
            # R8: handle 확인 결과와 무관하게 미확인이다.
            return False
        if handles:
            return handles_ok
        return confirm_motion_stopped(
            collect_samples(),
            joint_names,
            tolerance,
            self._policy.hold_sec,
            self._policy.max_sample_gap_sec,
        )
