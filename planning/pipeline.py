"""발화 → Task Plan 조율 (md/개발플랜.md 3-03).

여기에는 추출 규칙도, 모델 호출 코드도 없다. 순서와 거부 조건만 있다.

  1. 슬롯 추출 (모델 없음)
  2. 정지 키워드면 **모델을 건너뛰고** stop 계획으로 직행한다
  3. Provider에게 초안을 요청한다 (모델 호출은 여기 한 곳)
  4. 초안이 참조한 리소스를 ResourceCatalog와 대조한다 (존재 + 종류)
  5. 스킬이 SkillCatalog와 Profile에 있는지 본다
  6. 스킬·인자 계약으로 `TaskPlan`을 만든다

모델이 "확인이 필요하다"고 답하면 계획을 만들지 않고 질문을 그대로 전달한다
(`PlanningOutcome.clarification`). 모호한 요청에 그럴듯한 계획을 만드는 것보다
되묻는 것이 맞다 — 5-02 실측에서 카탈로그·안전 규칙을 모두 지키지만 사용자가
요청한 것이 아닌 계획 5건이 실행 허가까지 통과했다.

2번이 3번보다 먼저인 이유: 정지는 모델 응답을 기다릴 일이 아니다. STT 경로의
4-08과 같은 규칙을 텍스트 경로에도 둔다.

4번이 필요한 이유: 모델은 없는 위치를 만들어 낼 수 있다. 입력에 허용 목록을
줬다고 해서 출력이 그 안에 있다고 가정하지 않는다.

**계획 생성 성공은 실행 승인이 아니다.** 안전 검증(`validation/`)과 실행 허가는
이 뒤에 따로 온다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from core.capability_profile import CapabilityProfile
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind
from core.skill_catalog import SkillCatalog
from core.task_plan import PlanError, TaskPlan, TaskStep
from planning.plan_provider import (
    DraftStep,
    PlanDraft,
    PlanningContext,
    PlanningMode,
    PlanProvider,
    PlanProviderError,
    resource_args,
)
from planning.slot_extractor import (
    SlotExtraction,
    check_arg_kinds,
    check_plan_resources,
    extract_slots,
)


@dataclass(frozen=True)
class PlanningFailure:
    reason: ReasonCode
    detail: str


@dataclass(frozen=True)
class PlanningOutcome:
    """조율 결과. 성공·실패를 한 형태로 담는다.

    실패에도 `slots`와 `draft`를 남긴다 — 어디서 끊겼는지 알 수 있어야 한다.
    """

    #: 추출 결과. 모델 호출 자체가 제한시간에 걸린 경우처럼 추출까지 가지
    #: 못한 실패에서는 None이다.
    slots: SlotExtraction | None
    plan: TaskPlan | None = None
    draft: PlanDraft | None = None
    failure: PlanningFailure | None = None
    #: 모델을 호출하지 않고 끝났는가(정지 직행).
    bypassed_model: bool = False
    #: 모델이 요청한 확인 질문. 모호·정보 부족일 때 채워진다.
    clarification: str | None = None
    #: 초안이 없는 결말(확인 요청 등)의 호출 관측값. 기록에 쓴다.
    call: object | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None

    @property
    def used_mock(self) -> bool:
        """Mock 결과인가. 성공 여부와 별도로 전달한다(5-08)."""
        return bool(self.draft and self.draft.is_mock)


def plan_from_utterance(
    utterance: str,
    *,
    catalog: ResourceCatalog,
    skill_catalog: SkillCatalog,
    profile: CapabilityProfile,
    provider: PlanProvider,
    robot_id: str,
    plan_id_factory: Callable[[], str],
    created_at: float,
    ttl_sec: float,
    stop_keywords: tuple[str, ...],
    schema_version: str,
    references: tuple[str, ...] = (),
) -> PlanningOutcome:
    """발화 하나를 계획으로 만든다. 시각·TTL·plan_id는 주입받는다."""
    slots = extract_slots(utterance, catalog, stop_keywords=stop_keywords)

    if not utterance.strip():
        return PlanningOutcome(
            slots=slots,
            failure=PlanningFailure(ReasonCode.PLAN_SLOT_INCOMPLETE, "빈 발화다"),
        )

    # 2. 정지는 모델을 기다리지 않는다.
    #
    # 여기서 만드는 stop 계획은 **요청의 표현**이고 실행 경로가 아니다. 안전
    # 검증은 계획이 home으로 끝나기를 요구하므로 stop 단독 계획은 통과하지
    # 못한다(그게 맞다). 정지는 `core/stop_contract.py`의 STOP 계약으로
    # 처리한다 — 계획 검증·허가를 기다리면 정지가 늦어진다.
    if slots.stop_keyword_hit:
        if not profile.supports("stop"):
            return PlanningOutcome(
                slots=slots, bypassed_model=True,
                failure=PlanningFailure(
                    ReasonCode.ROBOT_SKILL_UNSUPPORTED,
                    "정지 요청인데 profile이 stop을 지원하지 않는다",
                ),
            )
        plan = TaskPlan(
            plan_id=plan_id_factory(), robot_id=robot_id,
            profile_id=profile.profile_id, profile_version=profile.profile_version,
            steps=(TaskStep("stop"),), created_at=created_at, ttl_sec=ttl_sec,
            schema_version=schema_version, utterance=utterance, terminal_hold=None,
        )
        return PlanningOutcome(slots=slots, plan=plan, bypassed_model=True)

    # 3. 모델 호출은 이 한 곳이다.
    try:
        context = PlanningContext(
            utterance=utterance, slots=slots,
            allowed_skills=profile.supported_skills,
            allowed_locations=catalog.ids_of_kind(ResourceKind.LOCATION),
            allowed_objects=catalog.ids_of_kind(ResourceKind.OBJECT),
            robot_id=robot_id, profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            catalog_version=catalog.catalog_version, schema_version=schema_version,
            references=references,
        )
        draft = provider.generate(context)
    except PlanProviderError as exc:
        # 확인 요청은 실패와 같은 형태로 담되 질문을 따로 남긴다. 계획을
        # 만들지 않았다는 점에서 통과가 아니고, 사용자에게 되물을 수 있다는
        # 점에서 오류와 다르다.
        return PlanningOutcome(
            slots=slots,
            failure=PlanningFailure(exc.reason, str(exc)[:200]),
            clarification=getattr(exc, "question", None),
            call=getattr(exc, "call", None),
        )

    # 4. 초안이 참조한 리소스를 카탈로그와 대조한다.
    issues = (
        check_plan_resources(resource_args(draft.steps), catalog)
        + check_arg_kinds(draft.steps, catalog, skill_catalog)
    )
    if issues:
        return PlanningOutcome(
            slots=slots, draft=draft,
            failure=PlanningFailure(
                issues[0].reason,
                "; ".join(i.detail for i in issues),
            ),
        )
    if draft.terminal_hold is not None and not catalog.has(draft.terminal_hold):
        return PlanningOutcome(
            slots=slots, draft=draft,
            failure=PlanningFailure(
                ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"목표 종료 상태가 카탈로그에 없는 물체다: {draft.terminal_hold!r}",
            ),
        )

    unsupported = [
        s.skill for s in draft.steps
        if not profile.supports(s.skill) or not skill_catalog.has(s.skill)
    ]
    if unsupported:
        # **"못 한다"와 "근거가 모자라 열지 않았다"를 구분한다.**
        # Profile이 관문에 막힌 스킬을 선언하면(extras.gated_skills) 그 스킬은
        # capability.profile_incomplete다 — 로봇이 그 동작을 못 하는 것이
        # 아니라 열어 줄 근거가 없다는 뜻이고, 사용자에게 다른 이야기다.
        gated = dict(getattr(profile, "extras", {}).get("gated_skills") or {})
        gated_skills = set(gated.get("skills") or ())
        blocked = sorted(set(unsupported) & gated_skills)
        if blocked and set(unsupported) <= gated_skills:
            detail = gated.get("message") or ""
            unmet = gated.get("unmet") or ()
            return PlanningOutcome(
                slots=slots, draft=draft,
                failure=PlanningFailure(
                    ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
                    f"{', '.join(blocked)}: {detail}"
                    + (f" (미충족: {', '.join(unmet)})" if unmet else ""),
                ),
            )
        return PlanningOutcome(
            slots=slots, draft=draft,
            failure=PlanningFailure(
                ReasonCode.ROBOT_SKILL_UNSUPPORTED,
                f"profile이 지원하지 않는 스킬: {sorted(set(unsupported))}",
            ),
        )

    # 5. 계약으로 계획을 만든다. 스킬·인자 위반은 여기서 이유 코드가 된다.
    try:
        plan = TaskPlan(
            plan_id=plan_id_factory(), robot_id=robot_id,
            profile_id=profile.profile_id, profile_version=profile.profile_version,
            steps=tuple(TaskStep(s.skill, dict(s.args)) for s in draft.steps),
            created_at=created_at, ttl_sec=ttl_sec, schema_version=schema_version,
            utterance=utterance, terminal_hold=draft.terminal_hold,
        )
    except PlanError as exc:
        return PlanningOutcome(
            slots=slots, draft=draft,
            failure=PlanningFailure(exc.reason, str(exc)[:200]),
        )
    return PlanningOutcome(slots=slots, plan=plan, draft=draft)
