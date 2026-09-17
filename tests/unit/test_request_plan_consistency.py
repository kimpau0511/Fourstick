"""요청↔계획 리소스 일치 검증 (7단계 실행 전 게이트).

5-03 실측에서 남은 위험을 이 규칙이 막는지 본다. 모델이 없는 리소스를 유효한
다른 리소스로 치환한 계획은 카탈로그·안전 검증을 모두 통과한다 — 걸러낼 근거는
사용자가 실제로 말한 것뿐이다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan, TaskStep
from planning.slot_extractor import extract_slots
from validation.request_plan_consistency import (
    ConsistencyStatus,
    RULE_CODE,
    as_rule_result,
    check_request_plan_consistency,
    plan_resource_ids,
)

CONFIG = ROOT / "examples" / "config"


def catalog():
    return load_resource_catalog(
        json.loads((CONFIG / "valid_resource_catalog.json").read_text(encoding="utf-8"))
    )


def plan(steps, terminal_hold=None) -> TaskPlan:
    return TaskPlan(
        plan_id="plan_1", robot_id="r", profile_id="p", profile_version="1.0",
        steps=tuple(TaskStep(skill, args) for skill, args in steps),
        created_at=1.0, ttl_sec=60.0, terminal_hold=terminal_hold,
    )


TRANSFER = [
    ("move", {"to": "loc_pallet_1"}),
    ("pick", {"object": "obj_a", "from": "loc_pallet_1"}),
    ("move", {"to": "loc_conveyor"}),
    ("place", {"object": "obj_a", "to": "loc_conveyor"}),
    ("home", {}),
]


class Case(unittest.TestCase):
    def setUp(self):
        self.catalog = catalog()

    def check(self, utterance, steps, terminal_hold=None):
        return check_request_plan_consistency(
            plan=plan(steps, terminal_hold),
            slots=extract_slots(utterance, self.catalog),
            catalog=self.catalog,
        )


class TestSubstitutionIsBlocked(Case):
    def test_unregistered_location_substituted_with_a_valid_one(self):
        """5-03 실측 사례: "3번 팔레트"를 1번으로 바꾼 계획."""
        report = self.check("3번 팔레트에서 A자재를 집어줘", TRANSFER)
        self.assertIs(report.status, ConsistencyStatus.MISMATCH)
        self.assertIs(report.reason, ReasonCode.PLAN_RESOURCE_MISMATCH)
        self.assertIn("loc_pallet_1", report.only_in_plan)
        self.assertFalse(report.allowed)

    def test_warehouse_substituted_with_the_conveyor(self):
        report = self.check(
            "창고에서 A자재를 가져와",
            [("move", {"to": "loc_conveyor"}),
             ("pick", {"object": "obj_a", "from": "loc_conveyor"}),
             ("home", {})],
        )
        self.assertIs(report.status, ConsistencyStatus.MISMATCH)
        self.assertIn("loc_conveyor", report.only_in_plan)

    def test_object_the_user_never_mentioned_is_blocked(self):
        report = self.check(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
            [("move", {"to": "loc_pallet_1"}),
             ("pick", {"object": "obj_b", "from": "loc_pallet_1"}),
             ("move", {"to": "loc_conveyor"}),
             ("place", {"object": "obj_b", "to": "loc_conveyor"}),
             ("home", {})],
        )
        self.assertIs(report.status, ConsistencyStatus.MISMATCH)
        self.assertEqual(report.only_in_plan, ("obj_b",))

    def test_terminal_hold_outside_the_request_is_blocked(self):
        report = self.check(
            "컨베이어로 이동해줘",
            [("move", {"to": "loc_conveyor"}), ("home", {})],
            terminal_hold="obj_a",
        )
        self.assertIs(report.status, ConsistencyStatus.MISMATCH)
        self.assertIn("obj_a", report.only_in_plan)


class TestConsistentPlansPass(Case):
    def test_transfer_that_matches_the_request(self):
        report = self.check(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘", TRANSFER
        )
        self.assertIs(report.status, ConsistencyStatus.CONSISTENT)
        self.assertTrue(report.allowed)
        self.assertIsNone(report.reason)
        self.assertEqual(report.only_in_plan, ())

    def test_alias_resolution_is_shown(self):
        """UI가 "말한 표현 → 카탈로그 ID"를 보여줄 수 있어야 한다."""
        report = self.check(
            "일번 팔레트에서 에이자재를 집어서 컨베이어 벨트에 올려줘", TRANSFER
        )
        self.assertIs(report.status, ConsistencyStatus.CONSISTENT)
        surfaces = {r.surface: r.resource_id for r in report.request_resources}
        self.assertEqual(surfaces.get("일번팔레트"), "loc_pallet_1")
        self.assertEqual(surfaces.get("에이자재"), "obj_a")
        self.assertEqual(surfaces.get("컨베이어벨트"), "loc_conveyor")

    def test_plan_using_fewer_resources_than_requested_is_reported_not_blocked(self):
        report = self.check(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
            [("move", {"to": "loc_pallet_1"}),
             ("pick", {"object": "obj_a", "from": "loc_pallet_1"}),
             ("home", {})],
            terminal_hold="obj_a",
        )
        self.assertIs(report.status, ConsistencyStatus.CONSISTENT)
        self.assertEqual(report.only_in_request, ("loc_conveyor",))

    def test_resourceless_plan_is_not_applicable(self):
        report = self.check("홈으로 보내줘", [("home", {})])
        self.assertIs(report.status, ConsistencyStatus.NOT_APPLICABLE)
        self.assertTrue(report.allowed)

    def test_stop_plan_is_not_applicable(self):
        report = self.check("지금 멈춰", [("stop", {})])
        self.assertIs(report.status, ConsistencyStatus.NOT_APPLICABLE)


class TestUnverifiable(Case):
    def test_plan_with_resources_but_request_had_none(self):
        """확인할 근거가 없으면 통과로 보지 않는다."""
        report = self.check("그거 저기로 옮겨줘", TRANSFER)
        self.assertIs(report.status, ConsistencyStatus.UNVERIFIABLE)
        self.assertIs(report.reason, ReasonCode.PLAN_CLARIFICATION_REQUIRED)
        self.assertFalse(report.allowed)

    def test_missing_slots_are_not_treated_as_consistent(self):
        report = check_request_plan_consistency(
            plan=plan(TRANSFER), slots=None, catalog=self.catalog
        )
        self.assertIs(report.status, ConsistencyStatus.UNVERIFIABLE)


class TestReportShape(Case):
    def test_plan_resources_keep_step_order(self):
        self.assertEqual(
            plan_resource_ids(plan(TRANSFER)),
            ("loc_pallet_1", "obj_a", "loc_conveyor"),
        )

    def test_rule_result_matches_the_safety_rule_shape(self):
        report = self.check("3번 팔레트에서 A자재를 집어줘", TRANSFER)
        rule = as_rule_result(report)
        self.assertEqual(rule["code"], RULE_CODE)
        self.assertEqual(rule["status"], "block")
        self.assertEqual(rule["reason"], ReasonCode.PLAN_RESOURCE_MISMATCH.value)

    def test_dict_exposes_the_difference_for_the_ui(self):
        payload = self.check("3번 팔레트에서 A자재를 집어줘", TRANSFER).to_dict()
        self.assertFalse(payload["allowed"])
        self.assertIn("loc_pallet_1", payload["only_in_plan"])
        self.assertTrue(payload["request_resources"])
        self.assertTrue(payload["plan_resources"])

    def test_not_applicable_and_unverifiable_are_distinct(self):
        self.assertNotEqual(
            self.check("홈으로", [("home", {})]).status,
            self.check("그거 저기로", TRANSFER).status,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
