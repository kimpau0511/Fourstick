"""실행 허가 테스트 (forstick test_execution_permit.py 이관).

원본은 Gazebo가 필요한 통합 테스트였다. forstick2에서는 관측값을 주입받는
구조이므로 시뮬레이터 없이 결정적으로 검증한다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.constants import ATOMIC_SKILLS
from core.policy import FreshnessPolicy, SafetyPolicy
from core.reason_codes import ReasonCode
from core.resource_catalog import (
    ResourceCatalog,
    ResourceEntry,
    ResourceKind,
)
from core.task_plan import TaskPlan, TaskStep
from validation.execution_permit import PermitContext, check_execution_permit
from validation.safety_validator import ResourceCatalog, evaluate

SAFETY = SafetyPolicy("fixture", 12,
                       {"max_steps": "fixture",
                        "required_final_skill": "fixture"},
                       required_final_skill="home")
FRESH = FreshnessPolicy(
    "fixture", 1.0, 3.0,
    {"robot_state_max_age_sec": "fixture", "environment_max_age_sec": "fixture"},
)

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


CATALOG = make_catalog(locations=("loc_a", "loc_b"), objects=("obj_a",))

PLAN = TaskPlan(
    "p", "r", "prof", "1.0",
    (
        TaskStep("home"),
        TaskStep("move", {"to": "loc_a"}),
        TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
        TaskStep("move", {"to": "loc_b"}),
        TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
        TaskStep("home"),
    ),
    created_at=1_000.0, ttl_sec=60.0,
)


def context(**over) -> PermitContext:
    kw = dict(
        now=1_001.0,
        robot_ready=True,
        robot_state_observed_at=1_000.9,
        robot_state_valid=True,
        environment_observed_at=1_000.5,
        recorded_environment_version=3,
        current_environment_version=3,
        recorded_environment_session="sess",
        current_environment_session="sess",
        recorded_plan_hash=PLAN.plan_hash(),
        profile_id="prof",
        profile_version="1.0",
        supported_skills=ATOMIC_SKILLS,
    )
    kw.update(over)
    return PermitContext(**kw)


def decide(ctx, catalog=CATALOG):
    return check_execution_permit(PLAN, evaluate(PLAN, SAFETY, catalog), ctx, FRESH)


class TestPermitGranted(unittest.TestCase):
    def test_all_conditions_met_grants_permit(self):
        d = decide(context())
        self.assertTrue(d.granted, d.reasons)
        self.assertEqual(d.reasons, ())


class TestPermitDenied(unittest.TestCase):
    def test_safety_ask_is_denied(self):
        """BLOCK뿐 아니라 ASK(정보 부족)도 실행 허가로 치지 않는다."""
        d = decide(context(), catalog=None)
        self.assertFalse(d.granted)
        self.assertIn(ReasonCode.EXEC_PERMIT_DENIED, d.reason_codes())

    def test_robot_not_ready_is_denied(self):
        self.assertIn(ReasonCode.ROBOT_NOT_CONNECTED, decide(context(robot_ready=False)).reason_codes())

    def test_robot_state_unknown_is_denied(self):
        self.assertIn(
            ReasonCode.ROBOT_STATE_UNAVAILABLE, decide(context(robot_ready=None)).reason_codes()
        )

    def test_invalid_state_sample_is_denied(self):
        self.assertIn(
            ReasonCode.ROBOT_STATE_UNAVAILABLE,
            decide(context(robot_state_valid=False)).reason_codes(),
        )

    def test_stale_robot_state_is_denied(self):
        self.assertIn(
            ReasonCode.ROBOT_STATE_STALE,
            decide(context(robot_state_observed_at=999.0)).reason_codes(),
        )

    def test_stale_environment_is_denied(self):
        self.assertIn(
            ReasonCode.SAFETY_INSUFFICIENT_DATA,
            decide(context(environment_observed_at=990.0)).reason_codes(),
        )

    def test_missing_environment_history_is_denied(self):
        self.assertIn(
            ReasonCode.SAFETY_INSUFFICIENT_DATA,
            decide(context(environment_observed_at=None)).reason_codes(),
        )

    def test_tampered_plan_is_denied(self):
        self.assertIn(
            ReasonCode.PLAN_HASH_MISMATCH, decide(context(recorded_plan_hash="0" * 64)).reason_codes()
        )

    def test_expired_plan_is_denied(self):
        self.assertIn(ReasonCode.PLAN_EXPIRED, decide(context(now=1_100.0)).reason_codes())

    def test_profile_mismatch_is_denied(self):
        self.assertIn(
            ReasonCode.ROBOT_PROFILE_MISMATCH, decide(context(profile_version="2.0")).reason_codes()
        )

    def test_unsupported_skill_is_denied(self):
        self.assertIn(
            ReasonCode.ROBOT_SKILL_UNSUPPORTED,
            decide(context(supported_skills=("home", "move"))).reason_codes(),
        )

    def test_environment_version_change_is_denied(self):
        self.assertIn(
            ReasonCode.EXEC_ENVIRONMENT_CHANGED,
            decide(context(current_environment_version=4)).reason_codes(),
        )

    def test_environment_session_change_is_denied(self):
        self.assertIn(
            ReasonCode.EXEC_ENVIRONMENT_CHANGED,
            decide(context(current_environment_session="other")).reason_codes(),
        )

    def test_unknown_environment_version_is_denied(self):
        self.assertIn(
            ReasonCode.SAFETY_INSUFFICIENT_DATA,
            decide(context(current_environment_version=None)).reason_codes(),
        )

    def test_all_failures_are_reported_together(self):
        """하나 고치고 재시도했더니 다른 이유로 또 막히는 상황을 피한다."""
        d = decide(
            context(
                robot_ready=False,
                robot_state_observed_at=900.0,
                environment_observed_at=None,
                recorded_plan_hash="0" * 64,
            )
        )
        self.assertFalse(d.granted)
        self.assertGreaterEqual(len(d.reasons), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
