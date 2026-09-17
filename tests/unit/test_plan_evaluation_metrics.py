"""계획 생성 평가 지표 검증 (md/개발플랜.md 5-02).

지표 정의가 사실을 말하는지 본다. 특히 두 위험을 섞지 않는지 확인한다.
- 안전 검증에서 막힌 계획이 허가에서도 막혔는가 (계약)
- 카탈로그·안전 규칙을 지키지만 **의도와 다른** 계획이 허가까지 갔는가 (위험)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from planning.evaluation import (
    CaseOutcome,
    EvalCase,
    EvalSummary,
    ExpectedStep,
    load_cases,
)

FIXTURE = ROOT / "fixtures" / "planning" / "eval_ko_commands.jsonl"


def plan_case(cid="c1", steps=(("move", {"to": "loc_a"}), ("home", {})), hold=None):
    return EvalCase(
        case_id=cid, utterance="발화", category="정상-이동", kind="plan",
        steps=tuple(ExpectedStep(skill=s, args=a) for s, a in steps),
        terminal_hold=hold,
    )


def reject_case(cid="r1", category="미등록-리소스", reasons=("plan.unknown_resource",)):
    return EvalCase(
        case_id=cid, utterance="발화", category=category, kind="reject",
        allowed_reasons=reasons,
    )


def outcome(**over):
    kw = dict(
        case_id="c1", category="정상-이동", kind="plan", planned=True, json_ok=True,
        semantics_ok=True, latency_sec=1.0,
    )
    kw.update(over)
    return CaseOutcome(**kw)


def steps(*pairs):
    return tuple(ExpectedStep(skill=s, args=a) for s, a in pairs)


class TestFixtureCoversEveryRequestedCategory(unittest.TestCase):
    def test_all_eleven_input_kinds_are_present(self):
        cases = load_cases(FIXTURE)
        categories = {c.category for c in cases}
        for required in ("정상-이송", "다단계", "미등록-리소스", "미지원-스킬",
                         "목적지-누락", "모호", "정지", "취소", "주입-지시무시",
                         "주입-좌표", "주입-임의스킬", "표현변형"):
            with self.subTest(category=required):
                self.assertIn(required, categories)

    def test_every_case_declares_an_expectation(self):
        for c in load_cases(FIXTURE):
            with self.subTest(case=c.case_id):
                self.assertIn(c.kind, ("plan", "reject", "stop_bypass"))
                if c.kind == "plan":
                    self.assertTrue(c.steps, "계획 기대에 스텝이 없다")
                if c.kind == "reject":
                    self.assertTrue(c.allowed_reasons, "거부 기대에 이유 코드가 없다")

    def test_repeat_cases_exist_for_stability(self):
        self.assertTrue([c for c in load_cases(FIXTURE) if c.repeat])


class TestMatchMetricsAreNotStringEquality(unittest.TestCase):
    def test_same_steps_in_different_order_do_not_match(self):
        case = plan_case(steps=(("move", {"to": "loc_a"}), ("pick", {"object": "o", "from": "loc_a"}), ("home", {})))
        wrong_order = outcome(
            actual_steps=steps(("pick", {"object": "o", "from": "loc_a"}),
                               ("move", {"to": "loc_a"}), ("home", {}))
        )
        self.assertFalse(wrong_order.order_match(case))
        # 스킬 집합은 같지만 순서가 다르다 — 스킬 일치율도 순서를 본다.
        self.assertFalse(wrong_order.skill_match(case))

    def test_right_skills_wrong_args_separate_the_two_rates(self):
        case = plan_case(steps=(("move", {"to": "loc_a"}), ("home", {})))
        wrong_arg = outcome(
            actual_steps=steps(("move", {"to": "loc_b"}), ("home", {}))
        )
        self.assertTrue(wrong_arg.skill_match(case))
        self.assertFalse(wrong_arg.arg_match(case))
        self.assertFalse(wrong_arg.order_match(case))

    def test_terminal_hold_difference_fails_order_match(self):
        case = plan_case(hold="obj_a")
        got = outcome(
            actual_steps=steps(("move", {"to": "loc_a"}), ("home", {})),
            actual_terminal_hold=None,
        )
        self.assertTrue(got.arg_match(case))
        self.assertFalse(got.order_match(case))

    def test_rejected_case_has_no_match_values(self):
        case = plan_case()
        got = outcome(planned=False, semantics_ok=False)
        self.assertIsNone(got.skill_match(case))
        self.assertIsNone(got.order_match(case))


class TestBlockingMetrics(unittest.TestCase):
    def test_blocked_with_an_unexpected_reason_does_not_count(self):
        case = reject_case(reasons=("plan.unknown_resource",))
        from core.reason_codes import ReasonCode

        wrong_reason = outcome(
            planned=False, semantics_ok=False,
            reason_code=ReasonCode.PLAN_LLM_TIMEOUT,
        )
        self.assertFalse(wrong_reason.blocked_as_expected(case),
                         "엉뚱한 이유로 막힌 것은 우연히 막힌 것이다")

    def test_blocked_with_an_expected_reason_counts(self):
        from core.reason_codes import ReasonCode

        case = reject_case(reasons=("plan.unknown_resource",))
        got = outcome(planned=False, semantics_ok=False,
                      reason_code=ReasonCode.PLAN_UNKNOWN_RESOURCE)
        self.assertTrue(got.blocked_as_expected(case))


class TestTwoRisksAreNotMixed(unittest.TestCase):
    """안전 차단과 의도 불일치를 한 지표로 묶지 않는다."""

    def summary(self, rows):
        s = EvalSummary(provider_id="p", model_id="m", is_mock=False)
        for case, out in rows:
            s.add(case, out)
        return s

    def test_safety_blocked_plan_must_be_denied_a_permit(self):
        s = self.summary([
            (plan_case(), outcome(safety_allowed=False, permit_granted=False)),
            (plan_case("c2"), outcome(safety_allowed=False, permit_granted=False)),
        ])
        self.assertEqual(s.unsafe_plan_permit_block_rate(), 1.0)

    def test_permit_granted_on_a_safety_blocked_plan_shows_up(self):
        s = self.summary([
            (plan_case(), outcome(safety_allowed=False, permit_granted=True)),
            (plan_case("c2"), outcome(safety_allowed=False, permit_granted=False)),
        ])
        self.assertEqual(s.unsafe_plan_permit_block_rate(), 0.5)

    def test_valid_but_wrong_plan_is_counted_as_misintent_not_as_safety_success(self):
        """카탈로그·안전 규칙을 지키지만 의도와 다른 계획.

        안전 계층은 이것을 막을 수 없다. 두 지표를 묶으면 이 위험이 안전
        성공률에 가려진다.
        """
        s = self.summary([
            (reject_case(), outcome(
                category="미등록-리소스", kind="reject", planned=True,
                safety_allowed=True, permit_granted=True,
                actual_steps=steps(("move", {"to": "loc_a"}), ("home", {})),
            )),
        ])
        self.assertEqual(s.misintent_executable_rate(), 1.0)
        # 안전에서 막힌 계획이 없으므로 그 지표는 "없음"이다 — 0.0이 아니다.
        self.assertIsNone(s.unsafe_plan_permit_block_rate())
        self.assertEqual(s.misintent_executable_cases(), ["r1"])

    def test_missing_trailing_home_is_reported_separately(self):
        case = plan_case(steps=(("move", {"to": "loc_a"}), ("home", {})))
        s = self.summary([
            (case, outcome(actual_steps=steps(("move", {"to": "loc_a"})))),
        ])
        self.assertEqual(s.missing_trailing_home_rate(), 1.0)
        self.assertEqual(s.step_order_rate(), 0.0)


class TestComparableDenominators(unittest.TestCase):
    """분모가 바뀌어 비교가 뒤집히지 않게 한다."""

    def summary(self, rows):
        s = EvalSummary(provider_id="p", model_id="m", is_mock=False)
        for case, out in rows:
            s.add(case, out)
        return s

    def test_fewer_plans_can_raise_the_narrow_rate_while_risk_drops(self):
        """계획을 덜 만들면 '계획난 것 중' 비율은 오르고 절대 위험은 내린다."""
        from core.reason_codes import ReasonCode

        def refused():
            return outcome(
                kind="reject", planned=False, semantics_ok=False,
                reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
            )

        def granted():
            return outcome(kind="reject", planned=True, safety_allowed=True,
                           permit_granted=True)

        # 기준선: 4건 중 2건이 계획 + 허가
        base = self.summary([
            (reject_case("r1"), granted()), (reject_case("r2"), granted()),
            (reject_case("r3"), outcome(kind="reject", planned=True,
                                        safety_allowed=False, permit_granted=False)),
            (reject_case("r4"), outcome(kind="reject", planned=True,
                                        safety_allowed=False, permit_granted=False)),
        ])
        # 개선안: 1건만 계획 + 허가, 나머지는 확인 요청
        better = self.summary([
            (reject_case("r1"), granted()), (reject_case("r2"), refused()),
            (reject_case("r3"), refused()), (reject_case("r4"), refused()),
        ])
        self.assertEqual(base.misintent_executable_rate(), 0.5)
        self.assertEqual(better.misintent_executable_rate(), 1.0)   # 좁은 분모
        # 전체 분모에서는 위험이 내려간 것이 보인다.
        self.assertEqual(base.misintent_executable_of_all_rejects(), 0.5)
        self.assertEqual(better.misintent_executable_of_all_rejects(), 0.25)

    def test_clarification_is_not_counted_as_a_semantics_failure(self):
        from core.reason_codes import ReasonCode

        s = self.summary([
            (plan_case(), outcome()),
            (reject_case("r1"), outcome(
                kind="reject", planned=False, semantics_ok=False,
                reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED)),
        ])
        # 확인 요청은 분모에서 빠져 계획 1건만 남는다.
        self.assertEqual(s.core_semantics_rate(), 1.0)
        self.assertEqual(s.clarification_rate(), 1.0)

    def test_real_semantics_failure_still_counts(self):
        from core.reason_codes import ReasonCode

        s = self.summary([
            (plan_case(), outcome()),
            (plan_case("c2"), outcome(
                planned=False, semantics_ok=False,
                reason_code=ReasonCode.PLAN_UNKNOWN_RESOURCE)),
        ])
        self.assertEqual(s.core_semantics_rate(), 0.5)


class TestNoArbitraryPassCriteria(unittest.TestCase):
    def test_summary_has_no_pass_or_threshold_field(self):
        s = EvalSummary(provider_id="p", model_id="m", is_mock=False)
        s.add(plan_case(), outcome())
        flat = str(s.to_dict())
        for banned in ("threshold", "합격", "기준치", "passed_criteria"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, flat)

    def test_mock_flag_is_carried_in_the_summary(self):
        s = EvalSummary(provider_id="mock", model_id="mock-rule-based", is_mock=True)
        self.assertTrue(s.to_dict()["is_mock"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
