"""해석 결과를 **사람이 누를 때까지** 붙잡아 두는 확인 대기함.

Qwen 분류기(`server/sim_demo_intent.py`)가 통과시킨 해석은 작업이 아니다.
여기 한 건이 만들어지고, 화면은 확인 카드를 보여준다. `confirm`을 부른 뒤에만
기존 시연 작업(`server/sim_demo_jobs.py`)이 만들어진다.

확인이 **거부되는** 경우 — 어느 쪽도 작업을 만들지 않는다.

| 사유 | 뜻 |
|---|---|
| `not_found` | 토큰이 없다(이미 쓰였거나 취소됐다) |
| `expired` | 확인 만료(기본 60초)를 넘겼다 |
| `state_changed` | 해석할 때 본 시연 상태와 지금이 다르다 |
| `busy` | 그 사이 다른 시연 작업이 돌기 시작했다 |
| `not_allowed` | 지금 상태에서 그 동작을 할 수 없다 |

상태 지문(`fingerprint`)은 자재별 기록 상태·배정 슬롯·체크포인트·실행 중 작업으로 만든다.
만든 시점과 누른 시점이 다르면 **그 해석은 더 이상 그 상태의 것이 아니다.**
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: 확인 만료(초). 사용자 요구값이다 — 임의로 바꾸지 않는다.
DEFAULT_TTL_SEC = 60.0
#: 동시에 들고 있는 확인 대기의 최대 개수. 넘으면 오래된 것부터 버린다.
MAX_PENDING = 8


_MATERIAL_FIELDS = ("state", "slot", "pose_m", "recorded_at")
_MATERIAL_OPTIONAL_FIELDS = {
    "scenario", "version", "record_version", "schema", "schema_version",
}
_CHECKPOINT_DISPLAY_ONLY_FIELDS = {"note"}


def _canonical(value: Any) -> Any:
    """Return JSON-shaped data with recursively deterministic mappings.

    Lists retain their order: pose coordinates and resume stages are ordered
    values, so sorting them would erase a real state change.  Tuples normalize
    to lists so equivalent in-memory and JSON-loaded state hashes identically.
    """
    if isinstance(value, Mapping):
        return {str(key): _canonical(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def fingerprint(status: Mapping[str, Any]) -> str:
    """시연 상태의 지문. 실행 안전에 영향을 주는 저장 상태만 포함한다."""
    state = status.get("state") or {}
    objects = {}
    for name, raw_row in (state.get("objects") or {}).items():
        row = raw_row or {}
        record = {key: row.get(key) for key in _MATERIAL_FIELDS}
        record.update({key: value for key, value in row.items()
                       if key in _MATERIAL_OPTIONAL_FIELDS})
        objects[name] = record
    checkpoint = state.get("checkpoint") or {}
    running = status.get("running_job") or {}
    payload = {
        "objects": objects,
        "checkpoints": sorted(state.get("checkpoints") or []),
        # Checkpoint pose, scene, stages, joints, gripper and stop latch all
        # affect whether resumption remains safe.  Keep the complete durable
        # record so newly-added safety fields are covered automatically; omit
        # only text that exists for display.
        "checkpoint": {key: value for key, value in checkpoint.items()
                       if key not in _CHECKPOINT_DISPLAY_ONLY_FIELDS},
        "running_job": running.get("job_id"),
    }
    blob = json.dumps(_canonical(payload), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass
class Pending:
    """확인을 기다리는 해석 한 건. **작업이 아니다.**"""

    token: str
    job_spec: dict
    fingerprint: str
    created_at: float
    expires_at: float
    #: 화면이 그대로 읽는 확인 문장. 예: "A 자재를 컨베이어로 옮기겠습니다."
    summary: str
    #: 해석 근거(규칙 판단·모델 출력·후보). 화면의 "해석 근거"에 나간다.
    evidence: dict = field(default_factory=dict)
    utterance: str = ""
    source: str = ""

    def remaining(self, now: float) -> float:
        return max(0.0, self.expires_at - now)

    def to_json(self, now: float) -> dict:
        return {
            "token": self.token,
            "summary": self.summary,
            "action": self.job_spec.get("action"),
            "material": self.job_spec.get("material"),
            "utterance": self.utterance,
            "source": self.source,
            "evidence": dict(self.evidence),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "ttl_sec": round(self.expires_at - self.created_at, 3),
            "remaining_sec": round(self.remaining(now), 3),
            "is_simulated": True,
        }


@dataclass(frozen=True)
class ConfirmRejection:
    """확인을 받지 못했다. 작업은 만들어지지 않았다."""

    code: str
    reason: str


class ConfirmStore:
    """확인 대기함. 프로세스 안에서만 산다(재기동하면 비어 있다)."""

    def __init__(self, *, ttl_sec: float = DEFAULT_TTL_SEC,
                 clock: Callable[[], float] | None = None):
        self.ttl_sec = float(ttl_sec)
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}

    # ── 만들기 ──────────────────────────────────────────────────────────
    def create(self, *, job_spec: Mapping[str, Any], status: Mapping[str, Any],
               summary: str, evidence: Mapping[str, Any] | None = None,
               utterance: str = "", source: str = "") -> Pending:
        now = self._clock()
        pending = Pending(
            token=f"simconfirm_{uuid.uuid4().hex[:12]}",
            job_spec=dict(job_spec),
            fingerprint=fingerprint(status),
            created_at=now,
            expires_at=now + self.ttl_sec,
            summary=summary,
            evidence=dict(evidence or {}),
            utterance=utterance,
            source=source,
        )
        with self._lock:
            self._sweep(now)
            # 새 해석이 오면 이전 대기는 무효다 — 확인 카드는 한 번에 하나다.
            self._pending.clear()
            self._pending[pending.token] = pending
        return pending

    # ── 읽기 ────────────────────────────────────────────────────────────
    def current(self) -> Pending | None:
        now = self._clock()
        with self._lock:
            self._sweep(now)
            if not self._pending:
                return None
            return next(iter(self._pending.values()))

    def peek(self, token: str) -> Pending | None:
        with self._lock:
            self._sweep(self._clock())
            return self._pending.get(token)

    # ── 누르기 ──────────────────────────────────────────────────────────
    def cancel(self, token: str) -> bool:
        """사용자가 취소했다. 작업을 만들지 않고 대기만 지운다."""
        with self._lock:
            return self._pending.pop(token, None) is not None

    def take(self, token: str, status: Mapping[str, Any]) -> Pending | ConfirmRejection:
        """확인. 통과하면 대기를 **소비하고** job_spec을 돌려준다.

        여기서 작업을 만들지 않는다 — 호출한 라우트가 기존 시연 작업 실행기에
        그대로 넘긴다. 실패하면 대기도 지운다(같은 토큰을 다시 쓰지 못한다).
        """
        now = self._clock()
        with self._lock:
            self._sweep(now)
            pending = self._pending.pop(token, None)
        if pending is None:
            return ConfirmRejection(
                "not_found",
                "확인 대기가 없습니다 — 만료됐거나 이미 처리됐습니다. 다시 말해 주세요")
        if pending.remaining(now) <= 0.0:
            return ConfirmRejection(
                "expired",
                f"확인 시간이 지났습니다({round(self.ttl_sec)}초) — 다시 말해 주세요")
        if (status.get("running_job") or {}).get("job_id"):
            return ConfirmRejection(
                "busy", "다른 시연 작업이 실행 중입니다 — \"멈춰\"로 정지할 수 있습니다")
        if fingerprint(status) != pending.fingerprint:
            return ConfirmRejection(
                "state_changed",
                "확인을 기다리는 동안 자재 상태가 바뀌었습니다 — 다시 말해 주세요")
        return pending

    # ── 정리 ────────────────────────────────────────────────────────────
    def _sweep(self, now: float) -> None:
        dead = [token for token, row in self._pending.items()
                if row.remaining(now) <= 0.0]
        for token in dead:
            self._pending.pop(token, None)
        if len(self._pending) > MAX_PENDING:
            oldest = sorted(self._pending.values(), key=lambda r: r.created_at)
            for row in oldest[:len(self._pending) - MAX_PENDING]:
                self._pending.pop(row.token, None)
