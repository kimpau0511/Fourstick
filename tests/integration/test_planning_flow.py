"""발화 → 계획 조율 검증 (md/개발플랜.md 3-03).

Provider는 test double이다. 확인 대상은 계획 품질이 아니라 **순서와 거부 조건**
이다.
- 정지는 모델을 거치지 않는다.
- 모델 실패가 성공으로 바뀌지 않는다.
- 모델이 만들어 낸 리소스가 계획으로 나가지 않는다.
- Mock 결과가 실제 성공과 구분된다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_capability_profile,
    load_resource_catalog,
    load_skill_catalog,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.policy import SafetyPolicy
from core.reason_codes import ReasonCode
from planning.mock_provider import MockPlanProvider
from planning.pipeline import plan_from_utterance
from validation.safety_validator import (
    RuleStatus,
    SafetyDecision,
    aggregate,
    evaluate,
)
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION
from planning.plan_provider import (
    DraftStep,
    PlanDraft,
    PlanningContext,
    PlanningMode,
    PlanProviderError,
)

CATALOG_FILE = ROOT / "examples" / "config" / "valid_resource_catalog.json"
SKILLS_FILE = ROOT / "examples" / "config" / "valid_skill_catalog.json"
PROFILE_FILE = ROOT / "examples" / "config" / "valid_capability_profile.json"
STOP_KEYWORDS = ("정지", "멈춰")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class ScriptedProvider:
    """정해진 초안이나 예외를 돌려준다. 받은 context를 보관한다."""

    def __init__(self, outcome):
        self._outcome = outcome
        self.contexts: list[PlanningContext] = []

    @property
    def provider_id(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted-model"

    def generate(self, context: PlanningContext) -> PlanDraft:
        self.contexts.append(context)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def draft(steps, terminal_hold=None, is_mock=False) -> PlanDraft:
    return PlanDraft(
        steps=tuple(steps), mode=PlanningMode.FUNCTION_CALLING,
        provider_id="scripted", model_name="scripted-model",
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        latency_sec=0.12, terminal_hold=terminal_hold, is_mock=is_mock,
    )


def termination(profile, safety_policy=None):
    """종료 조건을 정책·Profile에서 계산해 Mock에 주입한다.

    Mock도 실제 공급자와 같은 경로를 쓴다 — 종료 스킬을 코드에 박지 않는다.
    """
    from core.termination import termination_requirement

    return termination_requirement(
        profile=profile,
        safety_policy=safety_policy or SafetyPolicy(
            "fixture", 12,
            {"max_steps": "fixture", "required_final_skill": "fixture"},
            required_final_skill="home",
        ),
    )


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.catalog = load_resource_catalog(load(CATALOG_FILE))
        self.skill_catalog = load_skill_catalog(load(SKILLS_FILE))
        self.profile = load_capability_profile(load(PROFILE_FILE))
        self.ids = iter(f"plan_{i}" for i in range(1, 50))

    def run_flow(self, utterance, provider, **over):
        kw = dict(
            catalog=self.catalog, skill_catalog=self.skill_catalog,
            profile=self.profile, provider=provider,
            robot_id="robot_fake", plan_id_factory=lambda: next(self.ids),
            created_at=1_000.0, ttl_sec=60.0, stop_keywords=STOP_KEYWORDS,
            schema_version=TASK_PLAN_SCHEMA_VERSION,
        )
        kw.update(over)
        return plan_from_utterance(utterance, **kw)


class TestStopBypassesTheModel(FlowCase):
    def test_stop_utterance_never_reaches_the_provider(self):
        provider = ScriptedProvider(PlanProviderError(
            ReasonCode.PLAN_LLM_UNAVAILABLE, "불러선 안 된다"))
        out = self.run_flow("지금 멈춰", provider)
        self.assertTrue(out.ok)
        self.assertTrue(out.bypassed_model)
        self.assertEqual(provider.contexts, [], "정지에 모델을 호출했다")
        self.assertEqual([s.skill for s in out.plan.steps], ["stop"])
        self.assertIsNone(out.plan.terminal_hold)

    def test_stop_plan_records_the_utterance(self):
        out = self.run_flow("정지", ScriptedProvider(draft([DraftStep("home")])))
        self.assertEqual(out.plan.utterance, "정지")

    def test_stop_without_profile_support_is_refused_not_downgraded(self):
        profile = load_capability_profile(
            {**load(PROFILE_FILE), "supported_skills": ["home", "move"]}
        )
        out = self.run_flow("정지", ScriptedProvider(draft([DraftStep("home")])),
                            profile=profile)
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.ROBOT_SKILL_UNSUPPORTED)
        self.assertTrue(out.bypassed_model)


class TestProviderFailureStaysAFailure(FlowCase):
    def test_model_unavailable_is_reported_with_its_reason(self):
        out = self.run_flow(
            "A자재를 컨베이어로",
            ScriptedProvider(PlanProviderError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, "vLLM 응답 없음")),
        )
        self.assertFalse(out.ok)
        self.assertIsNone(out.plan)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_LLM_UNAVAILABLE)

    def test_slot_incomplete_does_not_become_an_empty_plan(self):
        out = self.run_flow(
            "그거 좀 해줘",
            ScriptedProvider(PlanProviderError(
                ReasonCode.PLAN_SLOT_INCOMPLETE, "슬롯 부족")),
        )
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_SLOT_INCOMPLETE)

    def test_empty_utterance_never_reaches_the_provider(self):
        provider = ScriptedProvider(draft([DraftStep("home")]))
        out = self.run_flow("   ", provider)
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_SLOT_INCOMPLETE)
        self.assertEqual(provider.contexts, [])


class TestInventedResourcesAreBlocked(FlowCase):
    def test_unknown_location_in_a_step_is_refused(self):
        out = self.run_flow(
            "A자재를 3번 팔레트로",
            ScriptedProvider(draft([
                DraftStep("pick", {"object": "obj_a", "from": "loc_pallet_1"}),
                DraftStep("place", {"object": "obj_a", "to": "loc_pallet_9"}),
            ])),
        )
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)
        self.assertIn("loc_pallet_9", out.failure.detail)
        # 초안은 남겨 어디서 끊겼는지 보이게 한다.
        self.assertIsNotNone(out.draft)

    def test_unknown_terminal_hold_is_refused(self):
        out = self.run_flow(
            "C자재를 들고 있어",
            ScriptedProvider(draft(
                [DraftStep("pick", {"object": "obj_a", "from": "loc_pallet_1"})],
                terminal_hold="obj_c",
            )),
        )
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)

    def test_unsupported_skill_from_the_model_is_refused(self):
        profile = load_capability_profile(
            {**load(PROFILE_FILE), "supported_skills": ["home", "move", "stop"]}
        )
        out = self.run_flow(
            "A자재를 집어",
            ScriptedProvider(draft(
                [DraftStep("pick", {"object": "obj_a", "from": "loc_pallet_1"})])),
            profile=profile,
        )
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.ROBOT_SKILL_UNSUPPORTED)

    def test_contract_violation_from_the_model_becomes_a_reason_code(self):
        out = self.run_flow(
            "A자재를 집어",
            ScriptedProvider(draft([DraftStep("pick", {"object": "obj_a"})])),
        )
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_ARG_MISSING)


class TestContextGivenToTheModel(FlowCase):
    def test_provider_receives_slots_and_allowed_lists_from_config(self):
        provider = ScriptedProvider(draft([DraftStep("move", {"to": "loc_conveyor"})]))
        self.run_flow("컨베이어로 가", provider)
        context = provider.contexts[0]
        self.assertEqual(context.slots.locations, ("loc_conveyor",))
        self.assertEqual(
            context.allowed_locations, ("loc_pallet_1", "loc_pallet_2", "loc_conveyor")
        )
        self.assertEqual(context.allowed_objects, ("obj_a", "obj_b"))
        self.assertEqual(context.allowed_skills, self.profile.supported_skills)
        self.assertEqual(context.catalog_version, self.catalog.catalog_version)
        self.assertEqual(context.profile_version, self.profile.profile_version)

    def test_references_are_passed_through_for_rag_later(self):
        provider = ScriptedProvider(draft([DraftStep("home")]))
        self.run_flow("홈으로", provider, references=("pick 문서",))
        self.assertEqual(provider.contexts[0].references, ("pick 문서",))


class TestMockIsNotRealSuccess(FlowCase):
    def test_mock_result_is_flagged_on_the_outcome(self):
        out = self.run_flow(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
            MockPlanProvider(hold_keywords=("들고 있어",),
                             termination=termination(self.profile)),
        )
        self.assertTrue(out.ok)
        self.assertTrue(out.used_mock, "Mock 결과가 실제 성공과 구분되지 않는다")
        self.assertTrue(out.draft.is_mock)
        self.assertEqual(out.draft.model_name, "mock-rule-based")

    def test_scripted_real_provider_is_not_flagged_as_mock(self):
        out = self.run_flow("홈으로", ScriptedProvider(draft([DraftStep("home")])))
        self.assertTrue(out.ok)
        self.assertFalse(out.used_mock)

    def test_mock_transfer_plan_follows_the_skill_contract(self):
        out = self.run_flow(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
            MockPlanProvider(hold_keywords=("들고 있어",),
                             termination=termination(self.profile)),
        )
        self.assertEqual(
            [s.skill for s in out.plan.steps],
            ["move", "pick", "move", "place", "home"],
        )
        self.assertIsNone(out.plan.terminal_hold)

    def test_mock_hold_plan_declares_its_terminal_hold(self):
        out = self.run_flow(
            "1번 팔레트에서 A자재를 집어서 들고 있어",
            MockPlanProvider(hold_keywords=("들고 있어",),
                             termination=termination(self.profile)),
        )
        self.assertEqual(out.plan.terminal_hold, "obj_a")

    def test_mock_refuses_instead_of_inventing_when_slots_are_missing(self):
        out = self.run_flow("그거 저기로",
                            MockPlanProvider(termination=termination(self.profile)))
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.PLAN_SLOT_INCOMPLETE)


class TestPlanIdentityIsInjected(FlowCase):
    def test_plan_id_comes_from_the_factory_not_the_model(self):
        out = self.run_flow("홈으로", ScriptedProvider(draft([DraftStep("home")])))
        self.assertEqual(out.plan.plan_id, "plan_1")

    def test_created_at_and_ttl_are_injected(self):
        out = self.run_flow("홈으로", ScriptedProvider(draft([DraftStep("home")])))
        self.assertEqual(out.plan.created_at, 1_000.0)
        self.assertEqual(out.plan.ttl_sec, 60.0)

    def test_same_content_twice_gives_the_same_plan_hash_with_different_ids(self):
        a = self.run_flow("홈으로", ScriptedProvider(draft([DraftStep("home")])))
        b = self.run_flow("홈으로", ScriptedProvider(draft([DraftStep("home")])))
        self.assertNotEqual(a.plan.plan_id, b.plan.plan_id)
        self.assertEqual(a.plan.plan_hash(), b.plan.plan_hash())


class TestSameCatalogFeedsSafetyValidation(FlowCase):
    """계획 생성과 안전 검증이 **같은 카탈로그**를 본다.

    이전에는 `validation`이 위치·물체 이름 집합을 담은 자기 타입을 따로 갖고
    있었다. 두 목록이 갈라지면 계획 생성은 통과하고 검증만 막히는(또는 그 반대)
    상태가 된다. 하나로 합쳤으므로 같은 객체를 양쪽에 넘긴다.
    """

    def validate(self, plan):
        policy = SafetyPolicy("fixture", 12,
                       {"max_steps": "fixture",
                        "required_final_skill": "fixture"},
                       required_final_skill="home")
        results = evaluate(plan, policy, self.catalog)
        return aggregate(results), results

    def test_planned_transfer_passes_safety_validation(self):
        out = self.run_flow(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
            MockPlanProvider(hold_keywords=("들고 있어",),
                             termination=termination(self.profile)),
        )
        self.assertTrue(out.ok)
        decision, results = self.validate(out.plan)
        self.assertIs(decision, SafetyDecision.ALLOW,
                      [r.message for r in results if r.status is not RuleStatus.PASS])

    def test_planned_hold_plan_passes_with_its_declared_terminal_hold(self):
        out = self.run_flow(
            "1번 팔레트에서 A자재를 집어서 들고 있어",
            MockPlanProvider(hold_keywords=("들고 있어",),
                             termination=termination(self.profile)),
        )
        self.assertEqual(out.plan.terminal_hold, "obj_a")
        decision, results = self.validate(out.plan)
        self.assertIs(decision, SafetyDecision.ALLOW,
                      [r.message for r in results if r.status is not RuleStatus.PASS])

    def test_validation_arg_check_uses_the_catalog_ids(self):
        self.assertEqual(self.catalog.locations,
                         frozenset({"loc_pallet_1", "loc_pallet_2", "loc_conveyor"}))
        self.assertEqual(self.catalog.objects, frozenset({"obj_a", "obj_b"}))


class TestModelCallIsInOnePlace(unittest.TestCase):
    def test_only_the_pipeline_calls_generate(self):
        planning = ROOT / "planning"
        callers = [
            p.name for p in planning.glob("*.py")
            if ".generate(" in p.read_text(encoding="utf-8")
        ]
        self.assertEqual(callers, ["pipeline.py"])

    def test_pipeline_holds_no_prompt_or_endpoint(self):
        text = (ROOT / "planning" / "pipeline.py").read_text(encoding="utf-8")
        for banned in ("http", "prompt", "system:", "temperature", "max_tokens"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, text.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
