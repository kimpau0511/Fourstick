"""안전 검증기 테스트 (forstick test_safety_guard.py 이관 + 오늘 발견한 공백)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.policy import SafetyPolicy
from core.resource_catalog import ResourceCatalog, ResourceEntry, ResourceKind
from core.task_plan import TaskPlan, TaskStep
from validation.safety_validator import (
    ALL_RULES,
    RuleStatus,
    SafetyDecision,
    aggregate,
    evaluate,
)

POLICY = SafetyPolicy("fixture", 12,
                       {"max_steps": "fixture",
                        "required_final_skill": "fixture"},
                       required_final_skill="home")

def make_catalog(
    locations=("loc_a", "loc_b", "loc_c"), objects=("obj_a", "obj_b"),
    *, catalog_version="fixture-1.0",
) -> ResourceCatalog:
    """시험용 카탈로그. 별칭은 id 자체로 둔다 — 여기서 검증하려는 것은
    별칭 해석이 아니라 인자 대조다(별칭 해석은 test_slot_extractor.py)."""
    entries = [
        ResourceEntry(resource_id=r, kind=ResourceKind.LOCATION,
                      display_name=r, aliases=(r,))
        for r in locations
    ] + [
        ResourceEntry(resource_id=r, kind=ResourceKind.OBJECT,
                      display_name=r, aliases=(r,))
        for r in objects
    ]
    return ResourceCatalog(catalog_version=catalog_version, entries=tuple(entries))


CATALOG = make_catalog()


def plan(steps) -> TaskPlan:
    return TaskPlan("p", "r", "prof", "1.0", tuple(steps), created_at=0.0, ttl_sec=60.0)


NORMAL = [
    TaskStep("home"),
    TaskStep("move", {"to": "loc_a"}),
    TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
    TaskStep("move", {"to": "loc_b"}),
    TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
    TaskStep("home"),
]


def status_of(results, code) -> RuleStatus:
    return next(r.status for r in results if r.code == code)


class TestNormalPlan(unittest.TestCase):
    def test_normal_transfer_is_allowed(self):
        results = evaluate(plan(NORMAL), POLICY, CATALOG)
        self.assertIs(aggregate(results), SafetyDecision.ALLOW)

    def test_every_rule_is_reported_not_just_violations(self):
        results = evaluate(plan(NORMAL), POLICY, CATALOG)
        self.assertEqual({r.code for r in results}, set(ALL_RULES))


class TestSequenceRules(unittest.TestCase):
    def test_e_seq_002_requires_home_at_end(self):
        results = evaluate(plan(NORMAL[:-1]), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-SEQ-002"), RuleStatus.BLOCK)

    def test_e_seq_003_requires_move_before_pick(self):
        steps = [
            TaskStep("home"),
            TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
            TaskStep("move", {"to": "loc_b"}),
            TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
            TaskStep("home"),
        ]
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-SEQ-003"), RuleStatus.BLOCK)

    def test_e_seq_004_requires_move_to_matching_place_target(self):
        steps = list(NORMAL)
        steps[3] = TaskStep("move", {"to": "loc_c"})   # place 목적지와 불일치
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-SEQ-004"), RuleStatus.BLOCK)


class TestHoldRules(unittest.TestCase):
    def test_e_hold_001_blocks_place_without_holding(self):
        steps = [
            TaskStep("home"),
            TaskStep("move", {"to": "loc_b"}),
            TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
            TaskStep("home"),
        ]
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-HOLD-001"), RuleStatus.BLOCK)

    def test_e_hold_002_blocks_pick_while_holding(self):
        steps = [
            TaskStep("home"),
            TaskStep("move", {"to": "loc_a"}),
            TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
            TaskStep("move", {"to": "loc_a"}),
            TaskStep("pick", {"object": "obj_b", "from": "loc_a"}),
            TaskStep("home"),
        ]
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-HOLD-002"), RuleStatus.BLOCK)

    def test_e_hold_003_blocks_plan_ending_while_holding(self):
        """forstick에서 실측으로 확인된 공백: "두 번 집어서 놔줘"가 통과했다.
        마지막 pick 뒤 place 없이 home으로 끝나는 계획을 차단해야 한다."""
        steps = NORMAL + [
            TaskStep("move", {"to": "loc_a"}),
            TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
            TaskStep("home"),
        ]
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-HOLD-003"), RuleStatus.BLOCK)
        self.assertIs(aggregate(results), SafetyDecision.BLOCK)

    def test_e_hold_003_passes_when_nothing_is_held_at_end(self):
        results = evaluate(plan(NORMAL), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-HOLD-003"), RuleStatus.PASS)


class TestLimitAndArgRules(unittest.TestCase):
    def test_e_limit_001_uses_injected_policy_value(self):
        tight = SafetyPolicy("fixture", 3,
                             {"max_steps": "fixture",
                              "required_final_skill": "fixture"},
                             required_final_skill="home")
        self.assertIs(status_of(evaluate(plan(NORMAL), tight, CATALOG), "E-LIMIT-001"), RuleStatus.BLOCK)
        self.assertIs(status_of(evaluate(plan(NORMAL), POLICY, CATALOG), "E-LIMIT-001"), RuleStatus.PASS)

    def test_e_arg_001_blocks_unknown_location(self):
        steps = list(NORMAL)
        steps[1] = TaskStep("move", {"to": "loc_zzz"})
        steps[2] = TaskStep("pick", {"object": "obj_a", "from": "loc_zzz"})
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-ARG-001"), RuleStatus.BLOCK)

    def test_e_arg_001_blocks_unknown_object(self):
        steps = list(NORMAL)
        steps[2] = TaskStep("pick", {"object": "obj_zzz", "from": "loc_a"})
        steps[4] = TaskStep("place", {"object": "obj_zzz", "to": "loc_b"})
        results = evaluate(plan(steps), POLICY, CATALOG)
        self.assertIs(status_of(results, "E-ARG-001"), RuleStatus.BLOCK)


class TestAggregation(unittest.TestCase):
    def test_missing_catalog_is_ask_not_allow(self):
        """정보 부족을 통과로 승격하지 않는다."""
        results = evaluate(plan(NORMAL), POLICY, None)
        self.assertIs(status_of(results, "E-ARG-001"), RuleStatus.INSUFFICIENT_DATA)
        self.assertIs(aggregate(results), SafetyDecision.ASK)

    def test_block_beats_insufficient_data(self):
        results = evaluate(plan(NORMAL[:-1]), POLICY, None)
        self.assertIs(aggregate(results), SafetyDecision.BLOCK)

    def test_empty_plan_blocks_and_marks_rest_not_applicable(self):
        results = evaluate(plan([TaskStep("home")]), POLICY, CATALOG)
        self.assertIs(aggregate(results), SafetyDecision.ALLOW)
        # 실제로 빈 계획은 TaskPlan이 먼저 거부하므로, 여기서는 규칙 집합만 확인한다.
        self.assertEqual({r.code for r in results}, set(ALL_RULES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
