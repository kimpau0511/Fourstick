"""Fake Robot Adapter (md/개발플랜.md 2-02~2-05).

실제 로봇·시뮬레이터 문제와 플랫폼 문제를 분리하기 위한 Adapter다. 실제 시간을
기다리지 않고 정상·실패·STOP 흐름을 결정적으로 재현한다.

이 Adapter가 지켜야 하는 전송 계층 규칙 5개
(md/STOP테스트_이관대조표.md "전송 계층으로 분류한 항목"):

  T1. 전송 함수를 정확히 1번만 호출한다(자동 재전송 금지).
  T2. 전송 응답이 늦으면 원래 future에 결과 콜백을 건다.
  T3. 결과 대기가 타임아웃되면 스스로 취소를 시도한다.
  T4. orphan과 늦게 채택된 goal의 결과도 실제로 구독한다.
  T5. 결과 콜백이 예외를 던져도 추적을 유지한다.

goal 수명 관리는 직접 구현하지 않고 core/stop_contract.py의 GoalTracker에
위임한다 — 계약이 한 곳에만 있어야 Adapter가 늘어날 때 규칙이 갈라지지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from core.capability_profile import CapabilityProfile
from core.constants import SKILL_HOME, SKILL_MOVE, SKILL_PICK, SKILL_PLACE
from core.execution_result import ExecutionResult, rejected, success, unverifiable
from core.execution_state import ExecutionState
from core.policy import StopPolicy
from core.reason_codes import ReasonCode
from core.stop_contract import GoalTracker, JointSample
from robots.base.robot_adapter import RobotAdapter, RobotStateSnapshot
from robots.fake.transport import FakeTransport, GoalStatus, Scenario

#: 채널 이름은 Adapter가 선언한다. 공통 코드는 이 값을 해석하지 않는다.
CHANNEL_MOTION = "motion"
CHANNEL_GRIPPER = "gripper"


@dataclass
class FakeWorld:
    """관측되는 세계. 테스트가 직접 조작해 시나리오를 만든다."""

    now: float = 0.0
    joint_positions: dict[str, float] = field(default_factory=dict)
    joint_velocities: dict[str, float] = field(default_factory=dict)
    held_object: str | None = None
    #: 파지 상태를 관측할 수 있는가.
    hold_observed: bool = True
    #: 관절 관측 자체가 유효한가.
    state_valid: bool = True
    #: 상태 관측 시각. now보다 오래되면 stale이다.
    observed_at: float = 0.0
    #: 정지 확인에 쓸 표본. 비어 있으면 "계측 불가"다.
    samples: list[JointSample] = field(default_factory=list)
    connected: bool = False


class FakeRobotAdapter(RobotAdapter):
    def __init__(
        self,
        robot_id: str,
        profile: CapabilityProfile,
        *,
        stop_policy: StopPolicy,
        transport: FakeTransport | None = None,
        world: FakeWorld | None = None,
    ):
        super().__init__(robot_id, profile)
        self.transport = transport or FakeTransport()
        self.world = world or FakeWorld()
        self.tracker = GoalTracker([CHANNEL_MOTION, CHANNEL_GRIPPER], stop_policy)
        self._joint_names = tuple(j.name for j in profile.joint_limits)
        #: T5 검증용 — 콜백에서 예외가 난 사실을 기록한다.
        self.callback_errors: list[str] = []
        self._request_seq = 0

    # ── 연결 ────────────────────────────────────────────────────────────
    def connect(self, timeout_sec: float) -> ExecutionResult:
        if not all(
            self.transport.scenario(c).connected for c in (CHANNEL_MOTION, CHANNEL_GRIPPER)
        ):
            return rejected(ReasonCode.ROBOT_CONNECTION_LOST, {"robot_id": self.robot_id})
        self.world.connected = True
        # 연결은 "작업"이 아니므로 task_succeeded를 주장하지 않고, 실패도 아니므로
        # 이유 코드도 붙이지 않는다(core/execution_result.py 불변식 5 참고).
        return ExecutionResult(
            state=ExecutionState.IDLE,
            request_accepted=True,
            evidence={"connected": True},
        )

    def disconnect(self) -> None:
        self.world.connected = False

    # ── 상태 ────────────────────────────────────────────────────────────
    def state(self) -> RobotStateSnapshot:
        return RobotStateSnapshot(
            state=ExecutionState.STOPPED if self.tracker.stopped else ExecutionState.IDLE,
            observed_at=self.world.observed_at,
            joint_positions=dict(self.world.joint_positions),
            joint_velocities=dict(self.world.joint_velocities),
            valid=self.world.state_valid,
            held_object=self.world.held_object,
            hold_observed=self.world.hold_observed,
        )

    def check(self) -> ExecutionResult:
        if not self.world.connected:
            return rejected(ReasonCode.ROBOT_NOT_CONNECTED, {})
        snap = self.state()
        if not snap.valid:
            return rejected(ReasonCode.ROBOT_STATE_UNAVAILABLE, {})
        return ExecutionResult(
            state=ExecutionState.IDLE,
            request_accepted=True,
            evidence={"observed_at": snap.observed_at},
        )

    # ── 공통 전송 경로 ──────────────────────────────────────────────────
    def _dispatch(
        self, channel: str, skill: str, request_id: str, timeout_sec: float
    ) -> ExecutionResult:
        """T1~T5를 지키는 단일 전송 경로. 모든 스킬이 이 경로를 쓴다.

        `skill`은 분기용 계약 값이고 `request_id`는 goal 추적용 식별자다. 둘을
        섞지 않는다 — 식별자 문자열을 파싱해 동작을 결정하면 식별자 형식이
        계약이 되어 버린다.
        """
        if not self.world.connected:
            return rejected(ReasonCode.ROBOT_NOT_CONNECTED, {})

        # R1: 정지 확인과 pending 등록이 하나의 임계구역(GoalTracker가 보장)
        if not self.tracker.begin_send(channel, request_id):
            return rejected(ReasonCode.EXEC_STOPPED, {"channel": channel})

        # T1: 전송은 정확히 1번. 실패해도 재전송하지 않는다.
        outcome, accept_future = self.transport.send(channel, request_id)

        if outcome == "connection_lost":
            self.tracker.on_send_timeout(channel, request_id)  # 추적 유지
            return rejected(ReasonCode.ROBOT_CONNECTION_LOST, {"channel": channel})
        if outcome == "rejected":
            self.tracker.on_send_timeout(channel, request_id)  # 추적 유지
            return rejected(ReasonCode.EXEC_GOAL_REJECTED, {"channel": channel})

        if outcome == "send_timeout":
            # R3/T2: 재전송하지 않고 원래 future에 콜백을 건다.
            self.tracker.on_send_timeout(channel, request_id)
            if accept_future is not None:
                accept_future.add_done_callback(
                    lambda fut, ch=channel, rid=request_id: self._on_late_accept(ch, rid, fut.value)
                )
            return unverifiable(ReasonCode.EXEC_SEND_TIMEOUT, {"channel": channel})

        goal_ref = self.transport.accepted_goal_ref(channel)
        role = self.tracker.on_accept(channel, request_id, goal_ref)
        # T4: active로 채택되든 orphan이 되든 결과를 구독한다.
        self._subscribe_result(channel, goal_ref)

        if role == "orphan":
            self.transport.cancel(channel, goal_ref)
            return unverifiable(ReasonCode.EXEC_STOPPED, {"channel": channel, "role": role})

        if not self.transport.result_arrives_in_time(channel):
            # R5/T3: handle을 지우지 않고, 스스로 취소를 시도한다.
            self.tracker.on_result_timeout(channel, goal_ref)
            self.transport.cancel(channel, goal_ref)
            return unverifiable(ReasonCode.EXEC_RESULT_TIMEOUT, {"channel": channel})

        # T5: 결과 전달 중 콜백이 예외를 던져도 추적 상태를 깨지 않는다.
        try:
            self.transport.deliver_result(goal_ref)
        except Exception as exc:  # noqa: BLE001 — 콜백 예외를 삼키고 기록만 한다
            self.callback_errors.append(f"{goal_ref}: {exc}")
        status = self.transport.scenario(channel).result_status
        if status is GoalStatus.CANCELED:
            return ExecutionResult(
                state=ExecutionState.FAILED, request_accepted=True, motion_completed=True,
                reason=ReasonCode.EXEC_CANCELED, evidence={"channel": channel},
            )
        if status is GoalStatus.ABORTED:
            return ExecutionResult(
                state=ExecutionState.FAILED, request_accepted=True, motion_completed=True,
                reason=ReasonCode.EXEC_ABORTED, evidence={"channel": channel},
            )
        return ExecutionResult(
            state=ExecutionState.EXECUTING, request_accepted=True, motion_completed=True,
            target_reached=True,
            evidence={"channel": channel, "goal": goal_ref},
        )

    def _subscribe_result(self, channel: str, goal_ref: str) -> None:
        fut = self.transport.subscribe_result(channel, goal_ref)
        def on_result(f, ch=channel, ref=goal_ref):
            self.tracker.on_result(ch, ref)
        try:
            fut.add_done_callback(on_result)
        except RuntimeError as exc:
            # T5: 콜백이 예외를 던져도 추적을 유지한다(정리하지 않는다).
            self.callback_errors.append(f"{goal_ref}: {exc}")

    def _on_late_accept(self, channel: str, request_id: str, goal_ref: str | None) -> None:
        if goal_ref is None:
            return
        role = self.tracker.on_accept(channel, request_id, goal_ref)
        # T4: 늦게 채택된 goal도, orphan이 된 goal도 결과를 구독한다.
        self._subscribe_result(channel, goal_ref)
        if role == "orphan":
            self.transport.cancel(channel, goal_ref)

    # ── 스킬 ────────────────────────────────────────────────────────────
    def home(self, timeout_sec: float) -> ExecutionResult:
        return self._run(CHANNEL_MOTION, SKILL_HOME, timeout_sec)

    def move(self, target: str, timeout_sec: float) -> ExecutionResult:
        return self._run(CHANNEL_MOTION, SKILL_MOVE, timeout_sec, target=target)

    def pick(self, obj: str, source: str, timeout_sec: float) -> ExecutionResult:
        res = self._run(CHANNEL_GRIPPER, SKILL_PICK, timeout_sec, obj=obj, source=source)
        if res.motion_completed and res.target_reached and self.world.hold_observed:
            self.world.held_object = obj
        return res

    def place(self, obj: str, destination: str, timeout_sec: float) -> ExecutionResult:
        res = self._run(
            CHANNEL_GRIPPER, SKILL_PLACE, timeout_sec, obj=obj, destination=destination
        )
        if res.motion_completed and res.target_reached and self.world.hold_observed:
            self.world.held_object = None
        return res

    def _run(self, channel: str, skill: str, timeout_sec: float, **args: str) -> ExecutionResult:
        """스킬 지원 여부를 명시적인 스킬 값으로 확인하고 전송 경로로 넘긴다."""
        if not self.supports(skill):
            return rejected(
                ReasonCode.ROBOT_SKILL_UNSUPPORTED, {"skill": skill, "robot_id": self.robot_id}
            )
        return self._dispatch(channel, skill, self._new_request_id(skill, args), timeout_sec)

    def _new_request_id(self, skill: str, args: Mapping[str, str]) -> str:
        """goal 추적용 식별자. 동작 분기에 쓰지 않는다. 같은 스킬을 연달아
        보내도 겹치지 않도록 일련번호를 붙인다."""
        self._request_seq += 1
        detail = ",".join(f"{k}={v}" for k, v in sorted(args.items()))
        return f"req{self._request_seq}:{skill}" + (f"({detail})" if detail else "")

    # ── 정지 ────────────────────────────────────────────────────────────
    def stop(self, timeout_sec: float) -> ExecutionResult:
        """정지를 요청만 한다. 반환값으로 정지를 확정하지 않는다."""
        self.tracker.request_stop()
        return ExecutionResult(
            state=ExecutionState.STOPPING, request_accepted=True,
            evidence={"note": "요청 접수 — 확인은 confirm_stopped"},
        )

    def cancel(self, timeout_sec: float) -> ExecutionResult:
        acked = True
        for ch in (CHANNEL_MOTION, CHANNEL_GRIPPER):
            snap = self.tracker.snapshot(ch)
            for ref in ([snap.active] if snap.active else []) + list(snap.orphans):
                acked = self.transport.cancel(ch, ref) and acked
        if not acked:
            return unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, {})
        return ExecutionResult(
            state=ExecutionState.STOPPING, request_accepted=True,
            evidence={"cancel_ack": True},
        )

    def confirm_stopped(self, timeout_sec: float) -> ExecutionResult:
        """모든 채널의 실제 정지를 계측으로 확인한다."""
        per_channel = {}
        for ch in (CHANNEL_MOTION, CHANNEL_GRIPPER):
            per_channel[ch] = self.tracker.confirm_stop(
                ch,
                self._joint_names,
                lambda ref, c=ch: self.transport.cancel(c, ref),
                lambda: list(self.world.samples),
            )
        if all(per_channel.values()):
            return ExecutionResult(
                state=ExecutionState.STOPPED, request_accepted=True, evidence=per_channel,
            )
        return unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, per_channel)
