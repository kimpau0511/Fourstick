"""기하 검사 계약 (md/개발플랜.md 6-05).

특정 충돌 검사 엔진이나 로봇 SDK에 종속되지 않는다. 여기에는 **입력·판정·이유**
만 있고, 실제 검사는 구현체(`GeometryValidator`)가 한다. 이 모듈은 MoveIt·FCL·
Bullet·제조사 SDK 같은 이름을 모른다.

설계 기준:

- **검사하지 않은 상태를 안전하다고 보지 않는다.** 구현체가 없거나 환경 정보가
  부족하면 ASK다. ALLOW는 구현체가 충분한 입력으로 검증한 경우에만 나온다.
- 입력에는 계획, 로봇 Profile 버전, 좌표계, 환경 snapshot 식별자, 검사 시각이
  들어간다. 환경 데이터 본문이나 기하 모델 전체는 들어가지 않는다 — 버전과
  hash로만 추적한다(`md/저장소_설계.md`).
- 원인은 서로 다른 ReasonCode로 구분한다: 충돌, 작업공간 이탈, 좌표계 불명,
  환경 정보 부족, snapshot 만료, 구현체 부재·제한시간·오류.
- 좌표계 이름 문자열을 이 모듈이 정하지 않는다. Capability Profile의
  `frames`가 유일한 출처다(계획.md 27장).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan


class GeometryError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class GeometryDecision(str, Enum):
    """기하 검사 판정. 안전 검증의 4단계 판정과 같은 원칙이다 —
    정보 부족(ASK)을 통과(ALLOW)로 승격하지 않는다."""

    ALLOW = "allow"
    BLOCK = "block"
    ASK = "ask"

    def __str__(self) -> str:
        return self.value


#: 판정별로 허용되는 이유 코드. 구현체가 뒤섞어 돌려주는 것을 막는다.
BLOCK_REASONS: frozenset[ReasonCode] = frozenset({
    ReasonCode.GEOMETRY_COLLISION,
    ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
})
ASK_REASONS: frozenset[ReasonCode] = frozenset({
    ReasonCode.GEOMETRY_FRAME_UNKNOWN,
    ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
    ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
    ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
    ReasonCode.GEOMETRY_VALIDATOR_TIMEOUT,
    ReasonCode.GEOMETRY_VALIDATOR_ERROR,
    ReasonCode.GEOMETRY_VERDICT_INVALID,
})


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """환경 관측 한 장의 **식별자**. 데이터 본문이 아니다.

    본문(점군·메시·장애물 목록)은 이 계약을 넘지 않는다. 실행 기록에 환경
    데이터를 복사하지 않기 위해, 추적은 `snapshot_id` + `snapshot_version` +
    `content_hash`로만 한다.
    """

    snapshot_id: str
    snapshot_version: str
    #: 환경 데이터 본문의 지문. 같은 id라도 내용이 바뀌면 값이 달라진다.
    content_hash: str
    #: 이 snapshot의 좌표계 이름(Profile의 frames에서 온 값).
    frame_id: str
    #: 관측 UTC epoch 초.
    captured_at: float
    #: 유효 기간. 0 이하면 만료 판정을 할 수 없다(계약 위반).
    ttl_sec: float
    #: 어디서 온 관측인가(어댑터 id, 센서, 개발용 등). 감사용 문자열이다.
    source: str = ""

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "snapshot_version", "content_hash", "frame_id"):
            if not getattr(self, name):
                raise GeometryError(ReasonCode.CONFIG_MISSING, f"{name}이 비어 있다")
        if self.captured_at <= 0:
            raise GeometryError(
                ReasonCode.CONFIG_INVALID, "captured_at이 0 이하 — 관측 시각이 필요하다"
            )
        if self.ttl_sec <= 0:
            raise GeometryError(
                ReasonCode.CONFIG_INVALID, "ttl_sec이 0 이하 — 만료를 판정할 수 없다"
            )

    def expires_at(self) -> float:
        return self.captured_at + self.ttl_sec

    def is_expired(self, now: float) -> bool:
        """now는 호출자가 주입한다(테스트가 실제 시간을 기다리지 않게 한다)."""
        return now > self.expires_at()

    def age_sec(self, now: float) -> float:
        return now - self.captured_at

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "content_hash": self.content_hash,
            "frame_id": self.frame_id,
            "captured_at": self.captured_at,
            "ttl_sec": self.ttl_sec,
            "source": self.source,
        }


def snapshot_hash(parts: Sequence[str]) -> str:
    """환경 지문 계산 보조. 구성 요소를 순서대로 이어 해시한다.

    무엇을 넣을지는 환경 제공자가 정한다 — 이 함수는 규칙을 만들지 않는다.
    """
    blob = "\x00".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AttachedObject:
    """로봇에 붙어 함께 움직이는 물체. **적재 상태 검사에 쓴다.**

    물체를 든 뒤의 자세를 물체 없이 검사하면, 들고 있는 물체가 무엇과 닿는지
    보지 못한다. 그래서 파지 이후 단계는 이 선언을 함께 넘긴다.

    형상은 **선언된 치수**에서만 온다(`size_m`). 여기서 치수를 만들지 않는다.
    `link`는 물체가 붙는 링크, `offset_m`은 그 링크 좌표계에서의 중심이다.
    `touch_links`는 **닿아도 되는 링크**(파지 패드)이며, 선언된 것만 넣는다 —
    닿아도 되는 범위를 넓혀 검사를 통과시키지 않는다.
    """

    object_id: str
    link: str
    size_m: tuple[float, float, float]
    offset_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    touch_links: tuple[str, ...] = ()
    source: str = ""

    def __post_init__(self) -> None:
        if not self.object_id or not self.link:
            raise ValueError("붙는 물체는 id와 링크가 있어야 한다")
        if len(self.size_m) != 3 or any(v <= 0 for v in self.size_m):
            raise ValueError(f"치수가 양수 3개가 아니다: {self.size_m}")
        if len(self.offset_m) != 3:
            raise ValueError(f"중심 오프셋이 3개가 아니다: {self.offset_m}")

    def to_dict(self) -> dict:
        return {
            "object_id": self.object_id,
            "link": self.link,
            "size_m": list(self.size_m),
            "offset_m": list(self.offset_m),
            "touch_links": list(self.touch_links),
            "source": self.source,
        }


@dataclass(frozen=True)
class GeometryRequest:
    """기하 검사 입력. 계획·Profile 버전·좌표계·환경 snapshot·검사 시각."""

    plan: TaskPlan
    plan_hash: str
    robot_id: str
    profile_id: str
    profile_version: str
    #: 계획이 전제하는 좌표계. Profile의 frames[BASE] 등에서 온다.
    frame_id: str
    #: 검사 시각(UTC epoch 초). 호출자가 주입한다.
    checked_at: float
    #: 환경 관측. 없으면 None — 구현체가 추정하지 않는다.
    snapshot: EnvironmentSnapshot | None = None
    #: 검사 제한시간. 초과는 ASK다.
    timeout_sec: float = 5.0
    #: 해석된 수치 모션(있으면). 기호 계획만 있을 때는 비어 있다.
    resolved_motion: Mapping[int, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("plan_hash", "robot_id", "profile_id", "profile_version"):
            if not getattr(self, name):
                raise GeometryError(ReasonCode.CONFIG_MISSING, f"{name}이 비어 있다")
        if self.checked_at <= 0:
            raise GeometryError(ReasonCode.CONFIG_INVALID, "checked_at이 0 이하")
        if self.timeout_sec <= 0:
            raise GeometryError(ReasonCode.CONFIG_INVALID, "timeout_sec이 0 이하")
        if self.plan.plan_hash() != self.plan_hash:
            raise GeometryError(
                ReasonCode.PLAN_HASH_MISMATCH,
                "요청의 plan_hash가 계획 내용과 다르다",
            )


@dataclass(frozen=True)
class GeometryReason:
    reason_code: ReasonCode
    detail: str = ""

    def to_dict(self) -> dict:
        return {"reason_code": self.reason_code.value, "detail": self.detail}


@dataclass(frozen=True)
class GeometryVerdict:
    """검사 결과. **ALLOW에는 근거가 필요하다.**

    `input_complete=False`(입력이 부족했다)인데 ALLOW를 돌려주는 판정은 계약
    위반이다 — 생성 시점에 거부한다. 구현체가 그런 값을 만들어 보내면
    `check_geometry`가 `geometry.verdict_invalid`로 바꿔 ASK로 돌린다.
    """

    decision: GeometryDecision
    validator_id: str
    validator_version: str
    #: 검사에 필요한 입력이 모두 있었는가. ALLOW의 전제다.
    input_complete: bool
    started_at: float
    finished_at: float
    reasons: tuple[GeometryReason, ...] = ()
    #: 검사한 환경·좌표계 식별자. 판정이 무엇을 보고 나왔는지 남긴다.
    snapshot_id: str | None = None
    snapshot_version: str | None = None
    snapshot_hash: str | None = None
    frame_id: str | None = None
    #: 구현체가 남기는 근거 요약(검사한 스텝 수, 최소 여유 등). 본문 데이터 금지.
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.validator_id or not self.validator_version:
            raise GeometryError(
                ReasonCode.CONFIG_MISSING, "validator_id/validator_version이 필요하다"
            )
        if self.finished_at < self.started_at:
            raise GeometryError(
                ReasonCode.CONFIG_INVALID, "finished_at이 started_at보다 이르다"
            )
        if self.decision is GeometryDecision.ALLOW:
            if not self.input_complete:
                raise GeometryError(
                    ReasonCode.GEOMETRY_VERDICT_INVALID,
                    "입력이 불완전한데 ALLOW다 — 검사하지 않은 상태를 통과로 쓸 수 없다",
                )
            if self.reasons:
                raise GeometryError(
                    ReasonCode.GEOMETRY_VERDICT_INVALID,
                    f"ALLOW에 이유 코드가 붙어 있다: {[r.reason_code.value for r in self.reasons]}",
                )
            if not (self.snapshot_id and self.frame_id):
                raise GeometryError(
                    ReasonCode.GEOMETRY_VERDICT_INVALID,
                    "ALLOW는 어떤 환경·좌표계에서 검증했는지 남겨야 한다",
                )
        else:
            if not self.reasons:
                raise GeometryError(
                    ReasonCode.CONFIG_INVALID,
                    f"{self.decision}인데 이유 코드가 없다",
                )
            allowed = (
                BLOCK_REASONS if self.decision is GeometryDecision.BLOCK else ASK_REASONS
            )
            wrong = [
                r.reason_code.value for r in self.reasons if r.reason_code not in allowed
            ]
            if wrong:
                raise GeometryError(
                    ReasonCode.GEOMETRY_VERDICT_INVALID,
                    f"{self.decision}에 맞지 않는 이유 코드: {wrong}",
                )

    @property
    def duration_sec(self) -> float:
        return self.finished_at - self.started_at

    def reason_codes(self) -> tuple[ReasonCode, ...]:
        return tuple(r.reason_code for r in self.reasons)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "validator_id": self.validator_id,
            "validator_version": self.validator_version,
            "input_complete": self.input_complete,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_sec": self.duration_sec,
            "reasons": [r.to_dict() for r in self.reasons],
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "snapshot_hash": self.snapshot_hash,
            "frame_id": self.frame_id,
            "evidence": dict(self.evidence),
        }


@runtime_checkable
class GeometryValidator(Protocol):
    """기하 검사 구현체 계약.

    구현체는 자기 엔진을 어떻게 쓰든 자유지만, 이 서명과 판정 계약만 지킨다.
    입력이 부족하면 ALLOW를 만들지 않고 ASK를 돌려준다.
    """

    @property
    def validator_id(self) -> str: ...

    @property
    def validator_version(self) -> str: ...

    def check(self, request: GeometryRequest) -> GeometryVerdict: ...


def ask(
    reason: ReasonCode, detail: str, *, validator_id: str, validator_version: str,
    started_at: float, finished_at: float, request: GeometryRequest | None = None,
) -> GeometryVerdict:
    """ASK 판정을 만든다. 입력 부족·구현체 부재를 통과로 바꾸지 않기 위한 통로."""
    snapshot = None if request is None else request.snapshot
    return GeometryVerdict(
        decision=GeometryDecision.ASK,
        validator_id=validator_id, validator_version=validator_version,
        input_complete=False, started_at=started_at, finished_at=finished_at,
        reasons=(GeometryReason(reason, detail),),
        snapshot_id=None if snapshot is None else snapshot.snapshot_id,
        snapshot_version=None if snapshot is None else snapshot.snapshot_version,
        snapshot_hash=None if snapshot is None else snapshot.content_hash,
        frame_id=None if request is None else request.frame_id,
    )
