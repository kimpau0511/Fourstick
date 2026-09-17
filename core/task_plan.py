"""Task Plan v2 (md/개발플랜.md 1-01).

스킬, 인자, schema version, robot/profile ID, plan hash, TTL을 담는다.

설계 근거:
- plan_hash: 생성된 계획과 실행되는 계획이 같은지 확인한다. 검증을 통과한 뒤
  내용이 바뀌어 실행되는 경로를 차단한다.
- ttl_sec: 계획은 만들어진 시점의 환경을 전제한다. 시간이 지나면 낡은 좌표로
  실행되지 않도록 만료시킨다.
- profile_id/profile_version: 어떤 Capability로 검증된 계획인지 고정한다.
  다른 Profile로 실행하려 하면 거부한다.
- 좌표를 계획에 직접 넣지 않는다. 인자는 Catalog가 해석할 "이름"만 담는다
  (계획.md 27장: 작업 좌표를 제품 코드/데이터에 하드코딩하지 않는다).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from core.constants import (
    ATOMIC_SKILLS,
    SKILL_ALLOWED_ARGS,
    SKILL_REQUIRED_ARGS,
    SUPPORTED_SCHEMA_VERSIONS,
    TASK_PLAN_SCHEMA_VERSION,
)
from core.reason_codes import ReasonCode


class PlanError(Exception):
    """계획이 계약을 만족하지 못할 때. 이유 코드를 함께 담는다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class TaskStep:
    skill: str
    args: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.skill not in ATOMIC_SKILLS:
            raise PlanError(
                ReasonCode.PLAN_UNSUPPORTED_SKILL,
                f"계약에 없는 스킬: {self.skill!r} (허용: {list(ATOMIC_SKILLS)})",
            )
        missing = [a for a in SKILL_REQUIRED_ARGS[self.skill] if a not in self.args]
        if missing:
            raise PlanError(
                ReasonCode.PLAN_ARG_MISSING, f"{self.skill}에 필수 인자 누락: {missing}"
            )
        unknown = sorted(set(self.args) - set(SKILL_ALLOWED_ARGS[self.skill]))
        if unknown:
            raise PlanError(
                ReasonCode.PLAN_ARG_UNKNOWN, f"{self.skill}에 허용되지 않은 인자: {unknown}"
            )
        for k, v in self.args.items():
            if not isinstance(v, str) or not v:
                raise PlanError(
                    ReasonCode.PLAN_SCHEMA_INVALID,
                    f"{self.skill}.{k}는 비어 있지 않은 문자열이어야 한다(좌표 금지)",
                )

    def to_dict(self) -> dict[str, Any]:
        return {"skill": self.skill, "args": dict(self.args)}


@dataclass(frozen=True)
class TaskPlan:
    plan_id: str
    robot_id: str
    profile_id: str
    profile_version: str
    steps: tuple[TaskStep, ...]
    #: 계획 생성 시각(epoch 초, UTC). 호출자가 주입한다 — 코드가 시계를 직접 읽지 않는다.
    created_at: float
    ttl_sec: float
    schema_version: str = TASK_PLAN_SCHEMA_VERSION
    utterance: str = ""
    #: 계획이 끝났을 때 로봇이 쥐고 있어야 하는 물체 이름. 아무것도 쥐지 않고
    #: 끝나는 것이 목표면 None이다.
    #:
    #: 이 필드가 필요한 이유: "쥔 채로 끝나는 계획"이 언제나 잘못은 아니다.
    #: 작업자가 "집어서 들고 있어"라고 지시하면 쥔 상태가 정상 종료다. 반대로
    #: 이송 지시였는데 쥔 채로 끝나면 잘못이다. 둘을 구별할 수 있어야 하므로
    #: 계획이 자기 목표 종료 상태를 명시하고, 검증기는 스텝을 따라간 결과가
    #: 그 목표와 일치하는지만 본다(validation/safety_validator.py E-HOLD-003).
    terminal_hold: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise PlanError(
                ReasonCode.PLAN_VERSION_UNSUPPORTED,
                f"지원하지 않는 schema_version: {self.schema_version!r} "
                f"(지원: {list(SUPPORTED_SCHEMA_VERSIONS)})",
            )
        for name in ("plan_id", "robot_id", "profile_id", "profile_version"):
            if not getattr(self, name):
                raise PlanError(ReasonCode.PLAN_SCHEMA_INVALID, f"{name}이 비어 있다")
        if not self.steps:
            raise PlanError(ReasonCode.PLAN_SCHEMA_INVALID, "steps가 비어 있다")
        if self.ttl_sec <= 0:
            raise PlanError(ReasonCode.PLAN_SCHEMA_INVALID, "ttl_sec가 0 이하")
        if self.terminal_hold is not None and not self.terminal_hold:
            raise PlanError(
                ReasonCode.PLAN_SCHEMA_INVALID,
                "terminal_hold는 None이거나 비어 있지 않은 물체 이름이어야 한다",
            )

    # ── 해시 ────────────────────────────────────────────────────────────
    def canonical_payload(self) -> dict[str, Any]:
        """해시 대상. 실행 의미에 영향을 주는 필드만 넣는다.
        utterance와 created_at은 제외한다 — 같은 계획을 다른 발화로 만들 수 있다."""
        return {
            "schema_version": self.schema_version,
            "robot_id": self.robot_id,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "steps": [s.to_dict() for s in self.steps],
            # 목표 종료 상태도 실행 의미의 일부다 — 해시에 포함한다.
            "terminal_hold": self.terminal_hold,
        }

    def plan_hash(self) -> str:
        blob = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def verify_hash(self, expected: str) -> None:
        if self.plan_hash() != expected:
            raise PlanError(
                ReasonCode.PLAN_HASH_MISMATCH,
                "계획 내용이 검증 시점과 다르다",
            )

    # ── TTL ─────────────────────────────────────────────────────────────
    def expires_at(self) -> float:
        return self.created_at + self.ttl_sec

    def is_expired(self, now: float) -> bool:
        """now는 호출자가 주입한다(테스트가 실제 시간을 기다리지 않게 한다)."""
        return now >= self.expires_at()

    def require_fresh(self, now: float) -> None:
        if self.is_expired(now):
            raise PlanError(
                ReasonCode.PLAN_EXPIRED,
                f"계획이 만료됐다(만료 {self.expires_at():.3f}, 현재 {now:.3f})",
            )

    # ── Profile 정합성 ──────────────────────────────────────────────────
    def require_profile(self, profile_id: str, profile_version: str) -> None:
        if (profile_id, profile_version) != (self.profile_id, self.profile_version):
            raise PlanError(
                ReasonCode.ROBOT_PROFILE_MISMATCH,
                f"계획은 {self.profile_id}@{self.profile_version} 기준인데 "
                f"{profile_id}@{profile_version}로 실행하려 한다",
            )

    def require_supported(self, supported_skills: tuple[str, ...]) -> None:
        """Profile이 지원하지 않는 스킬이 있으면 거부한다."""
        unsupported = sorted({s.skill for s in self.steps} - set(supported_skills))
        if unsupported:
            raise PlanError(
                ReasonCode.ROBOT_SKILL_UNSUPPORTED,
                f"이 로봇이 지원하지 않는 스킬: {unsupported}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.canonical_payload(),
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "ttl_sec": self.ttl_sec,
            "utterance": self.utterance,
            "plan_hash": self.plan_hash(),
        }
