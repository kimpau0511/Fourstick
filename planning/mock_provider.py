"""규칙 기반 Mock Provider (md/개발플랜.md 3-03 시험용 / 5-08 구분 대상).

**실제 모델이 아니다.** `is_mock=True`를 붙여 돌려주므로, 기록과 API가 이
결과를 실제 성공과 섞지 않는다.

여기 있는 규칙은 계획 생성 품질을 대신하지 않는다. 모델 없이 3-03의 책임 분리
(추출 → 호출 경계 → 카탈로그 대조 → 계약)를 끝까지 시험하기 위한 최소 구현이다.
슬롯이 부족하면 그럴듯한 계획을 만들지 않고 `PLAN_SLOT_INCOMPLETE`로 실패한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceKind
from core.termination import TerminationRequirement
from planning.output_parser import draft_from_output
from planning.plan_provider import (
    DraftStep,
    PlanningContext,
    PlanningMode,
    PlanProviderError,
)
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION


@dataclass
class MockPlanProvider:
    """위치·물체 개수만 보고 이송·파지 계획을 만든다.

    **계약 테스트 전용이다.** 실제 LLM 검증을 대신하지 않는다. 결과에는
    `is_mock=True`가 붙고, 기록·통계는 이 값을 지우지 않는다(5-08).
    """

    #: 이 표현이 발화에 있으면 "쥔 채 종료"를 목표로 삼는다. 설정으로 주입한다.
    hold_keywords: tuple[str, ...] = ()
    #: 종료 조건. 주입되면 마지막 스텝을 여기서 정한다.
    termination: TerminationRequirement | None = None
    latency_sec: float = 0.0
    mode: PlanningMode = PlanningMode.FUNCTION_CALLING

    @property
    def provider_id(self) -> str:
        return "mock"

    @property
    def prompt_template_version(self) -> str:
        return PROMPT_TEMPLATE_VERSION

    @property
    def output_schema_version(self) -> str:
        return OUTPUT_SCHEMA_VERSION

    @property
    def is_mock(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        return "mock-rule-based"

    def generate(self, context: PlanningContext) -> PlanDraft:
        slots = context.slots
        objects = slots.distinct(ResourceKind.OBJECT)
        locations = slots.distinct(ResourceKind.LOCATION)
        hold = any(k in context.utterance for k in self.hold_keywords)

        # pick·place 직전에는 그 위치로의 move가 있어야 한다(E-SEQ-003).
        # 안전 규칙을 아는 것이 아니라, 계획이 그 규칙을 통과하도록 만든다 —
        # 판정은 validation/safety_validator.py가 한다.
        if not objects and locations:
            steps = [DraftStep("move", {"to": locations[0]})]
            terminal_hold = None
        elif objects and len(locations) >= 2:
            steps = [
                DraftStep("move", {"to": locations[0]}),
                DraftStep("pick", {"object": objects[0], "from": locations[0]}),
                DraftStep("move", {"to": locations[1]}),
                DraftStep("place", {"object": objects[0], "to": locations[1]}),
            ]
            terminal_hold = None
        elif objects and len(locations) == 1 and hold:
            steps = [
                DraftStep("move", {"to": locations[0]}),
                DraftStep("pick", {"object": objects[0], "from": locations[0]}),
            ]
            terminal_hold = objects[0]
        else:
            raise PlanProviderError(
                ReasonCode.PLAN_SLOT_INCOMPLETE,
                f"규칙으로 계획을 만들 수 없다 (물체 {len(objects)}개 / "
                f"위치 {len(locations)}개 / 쥔 채 종료 {hold})",
            )
        # 종료 스킬은 주입된 종료 조건에서 온다. 규칙을 코드에 박지 않는다.
        if self.termination is not None and self.termination.final_skill:
            if self.termination.final_skill in context.allowed_skills:
                steps.append(DraftStep(self.termination.final_skill))
        # Mock도 같은 출력 경로를 지난다 — 파싱·구조 검증을 건너뛰지 않는다.
        return draft_from_output(
            {
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "result": "plan",
                "steps": [{"skill": s.skill, "args": dict(s.args)} for s in steps],
                "terminal_hold": terminal_hold,
            },
            provider_id=self.provider_id, model_id=self.model_name,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            mode=self.mode, latency_sec=self.latency_sec, is_mock=True,
            notes={"rule": "slot-count"},
        )
