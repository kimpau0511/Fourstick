"""실행 직전 재검증 (Execution Permit).

forstick `execution_permit.py`의 "실행 요청 시점마다 전부 다시 확인한다"
패턴을 가져와 forstick2 계약 위에 다시 썼다. 요구정의서의 이중 안전 구조
("LLM은 계획만 생성하고 실행 권한은 규칙 기반 검증기가 통제")에서 실행 쪽
절반이다.

이관한 원칙:
- 계획 생성 시점이 아니라 **실행 요청 시점에** 다시 판정한다.
- Safety 판정이 ALLOW가 아니면 거부한다. BLOCK뿐 아니라 **ASK(정보 부족)도
  실행 허가로 치지 않는다.**
- 상태는 지금 이 순간 관측값으로 본다. 과거 값·추정값을 신뢰하지 않는다.
- 관측이 오래됐으면 값이 정상처럼 보여도 신뢰하지 않는다.
- 여러 조건이 동시에 실패할 수 있으므로 사유를 전부 모아 돌려준다. 하나
  고치고 재시도했더니 다른 이유로 또 막히는 상황을 피한다.
- **확인되지 않은 항목을 사용자 자기 신고나 체크박스로 메워 통과시키지 않는다.**
  검사할 데이터가 없으면 검사를 생략하는 것이지, 없는 값을 채우지 않는다.

forstick과 달라진 점:
- 신선도 임계값이 코드 상수가 아니라 FreshnessPolicy에서 온다(계획.md 27장).
- 특정 로봇/시뮬레이터에 의존하지 않는다. 관측값은 PermitContext로 주입받는다.
- 사유가 문자열이 아니라 공통 ReasonCode를 함께 갖는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from core.policy import FreshnessPolicy
from core.reason_codes import ReasonCode
from core.task_plan import PlanError, TaskPlan
from validation.safety_validator import (
    RuleResult,
    SafetyDecision,
    aggregate,
    blocking_results,
)


@dataclass(frozen=True)
class PermitReason:
    reason: ReasonCode
    detail: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}"


@dataclass(frozen=True)
class PermitContext:
    """실행 요청 시점의 관측값. 호출자가 지금 조회해서 채운다.

    None은 "확인하지 못했다"는 뜻이고, 통과로 처리하지 않는다.
    """

    now: float
    #: 컨트롤러/로봇이 명령을 받을 준비가 됐는지. None이면 조회 실패.
    robot_ready: bool | None
    #: 로봇 상태 관측 시각. None이면 한 번도 받지 못함.
    robot_state_observed_at: float | None
    #: 로봇 상태 관측이 유효했는지(표본 자체의 valid).
    robot_state_valid: bool
    #: 환경(셀) 전수 확인 시각. None이면 확인 이력 없음.
    environment_observed_at: float | None
    #: 계획 등록 당시 환경 버전/세션과 현재 값. 다르면 계획이 낡았다.
    recorded_environment_version: int | None
    current_environment_version: int | None
    recorded_environment_session: str | None
    current_environment_session: str | None
    #: 계획 등록 시 기록한 plan hash. 실행 시점 계획과 다르면 거부.
    recorded_plan_hash: str | None
    #: 기대 Profile. 계획의 profile과 다르면 거부.
    profile_id: str | None = None
    profile_version: str | None = None
    supported_skills: tuple[str, ...] = ()
    #: 승인 당시 Policy와 지금 Policy. 다르면 거부한다(개발플랜 6-07).
    #: 둘 다 주지 않으면 이 검사를 하지 않는다 — 호출자가 Policy 묶기를
    #: 제공하지 않은 경우다. 서버 경로는 항상 준다(테스트로 고정).
    recorded_policy_id: str | None = None
    current_policy_id: str | None = None
    recorded_policy_version: str | None = None
    current_policy_version: str | None = None
    #: 승인 당시 환경(기하) snapshot과 지금 snapshot. 내용이 바뀌면 거부한다.
    recorded_snapshot_id: str | None = None
    current_snapshot_id: str | None = None
    recorded_snapshot_hash: str | None = None
    current_snapshot_hash: str | None = None


@dataclass(frozen=True)
class PermitDecision:
    granted: bool
    reasons: tuple[PermitReason, ...] = field(default_factory=tuple)

    def reason_codes(self) -> tuple[ReasonCode, ...]:
        return tuple(r.reason for r in self.reasons)


def check_execution_permit(
    plan: TaskPlan,
    safety_results: Sequence[RuleResult],
    context: PermitContext,
    freshness: FreshnessPolicy,
) -> PermitDecision:
    """모든 조건을 확인하고 (허가 여부, 사유 전체)를 돌려준다."""
    reasons: list[PermitReason] = []

    # 1) Safety 판정 — ALLOW만 통과. ASK도 거부한다.
    decision = aggregate(safety_results)
    if decision is not SafetyDecision.ALLOW:
        detail = "; ".join(
            f"{r.code}({r.status}): {r.message}" for r in blocking_results(safety_results)
        )
        reasons.append(
            PermitReason(
                ReasonCode.EXEC_PERMIT_DENIED,
                f"안전 판정={decision} — {detail}",
            )
        )

    # 2) 로봇 준비 상태 — 지금 조회한 값만 쓴다.
    if context.robot_ready is None:
        reasons.append(
            PermitReason(ReasonCode.ROBOT_STATE_UNAVAILABLE, "로봇 준비 상태를 확인하지 못함")
        )
    elif not context.robot_ready:
        reasons.append(PermitReason(ReasonCode.ROBOT_NOT_CONNECTED, "로봇이 준비 상태가 아님"))

    # 3) 관측 신선도 — 오래된 값은 정상처럼 보여도 신뢰하지 않는다.
    if not context.robot_state_valid:
        reasons.append(
            PermitReason(ReasonCode.ROBOT_STATE_UNAVAILABLE, "로봇 상태 관측이 무효(valid=False)")
        )
    if context.robot_state_observed_at is None:
        reasons.append(
            PermitReason(ReasonCode.ROBOT_STATE_UNAVAILABLE, "로봇 상태를 한 번도 받지 못함")
        )
    else:
        age = context.now - context.robot_state_observed_at
        if age > freshness.robot_state_max_age_sec:
            reasons.append(
                PermitReason(
                    ReasonCode.ROBOT_STATE_STALE,
                    f"로봇 상태가 {age:.3f}s 전 값 "
                    f"(상한 {freshness.robot_state_max_age_sec:.3f}s)",
                )
            )
    if context.environment_observed_at is None:
        reasons.append(
            PermitReason(ReasonCode.SAFETY_INSUFFICIENT_DATA, "환경 전수 확인 이력이 없음")
        )
    else:
        age = context.now - context.environment_observed_at
        if age > freshness.environment_max_age_sec:
            reasons.append(
                PermitReason(
                    ReasonCode.SAFETY_INSUFFICIENT_DATA,
                    f"환경 확인이 {age:.3f}s 전 값 "
                    f"(상한 {freshness.environment_max_age_sec:.3f}s)",
                )
            )

    # 4) 계획 유효성 — hash, TTL, Profile, 환경 버전/세션
    if context.recorded_plan_hash is None:
        reasons.append(PermitReason(ReasonCode.PLAN_HASH_MISMATCH, "등록된 plan hash가 없음"))
    else:
        try:
            plan.verify_hash(context.recorded_plan_hash)
        except PlanError as exc:
            reasons.append(PermitReason(exc.reason, str(exc)))

    try:
        plan.require_fresh(context.now)
    except PlanError as exc:
        reasons.append(PermitReason(exc.reason, str(exc)))

    if context.profile_id is not None and context.profile_version is not None:
        try:
            plan.require_profile(context.profile_id, context.profile_version)
        except PlanError as exc:
            reasons.append(PermitReason(exc.reason, str(exc)))
    if context.supported_skills:
        try:
            plan.require_supported(context.supported_skills)
        except PlanError as exc:
            reasons.append(PermitReason(exc.reason, str(exc)))

    for label, recorded, current, code in (
        (
            "환경 버전",
            context.recorded_environment_version,
            context.current_environment_version,
            ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        ),
        (
            "환경 세션",
            context.recorded_environment_session,
            context.current_environment_session,
            ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        ),
    ):
        if recorded is None or current is None:
            reasons.append(
                PermitReason(ReasonCode.SAFETY_INSUFFICIENT_DATA, f"{label}을 확인할 수 없음")
            )
        elif recorded != current:
            reasons.append(
                PermitReason(code, f"{label}이 계획 등록 시점({recorded})과 다름({current})")
            )

    # 5) 승인 당시 Policy·환경 snapshot과 지금 값 대조 (6-07).
    #    한쪽만 있으면 정보 부족이다 — 통과로 승격하지 않는다.
    for label, recorded, current, code in (
        (
            "Policy 식별자", context.recorded_policy_id, context.current_policy_id,
            ReasonCode.CONFIG_VERSION_MISMATCH,
        ),
        (
            "Policy 버전", context.recorded_policy_version,
            context.current_policy_version, ReasonCode.CONFIG_VERSION_MISMATCH,
        ),
        (
            "환경 snapshot", context.recorded_snapshot_id,
            context.current_snapshot_id, ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        ),
        (
            "환경 snapshot 지문", context.recorded_snapshot_hash,
            context.current_snapshot_hash, ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        ),
    ):
        if recorded is None and current is None:
            continue   # 이 호출자는 해당 묶기를 제공하지 않는다
        if recorded is None or current is None:
            reasons.append(
                PermitReason(
                    ReasonCode.SAFETY_INSUFFICIENT_DATA,
                    f"{label}을 확인할 수 없음 (승인 {recorded!r} / 현재 {current!r})",
                )
            )
        elif recorded != current:
            reasons.append(
                PermitReason(
                    code,
                    f"{label}이 승인 시점({recorded})과 다름({current})",
                )
            )

    return PermitDecision(granted=not reasons, reasons=tuple(reasons))
