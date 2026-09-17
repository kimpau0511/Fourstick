"""외부 입출력·설정 검증 경계 테스트 (Pydantic).

Pydantic이 형태를, core 계약이 의미를 검증하고 둘 다 ReasonCode로 올라오는지
확인한다. core 모듈은 Pydantic에 의존하지 않아야 한다.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.loader import (
    config_json_schemas,
    load_capability_profile,
    load_freshness_policy,
    load_safety_policy,
    load_stop_policy,
)
from core.boundary import BoundaryValidationError
from core.reason_codes import ReasonCode
from server.schemas import (
    ExecutionResultModel,
    PermitDecisionModel,
    TaskPlanModel,
    parse_task_plan,
    task_plan_json_schema,
)

PLAN_JSON = {
    "plan_id": "p",
    "robot_id": "r",
    "profile_id": "prof",
    "profile_version": "1.0",
    "created_at": 1_000.0,
    "ttl_sec": 60.0,
    "steps": [
        {"skill": "home"},
        {"skill": "move", "args": {"to": "loc_a"}},
        {"skill": "pick", "args": {"object": "obj_a", "from": "loc_a"}},
        {"skill": "home"},
    ],
    "terminal_hold": "obj_a",
}

PROFILE_JSON = {
    "profile_id": "fixture",
    "profile_version": "1.0",
    "dof": 2,
    "joint_limits": [
        {"name": "j1", "lower": -3.0, "upper": 3.0, "max_velocity": 1.0,
         "unit": "rad", "kind": "revolute"},
        {"name": "j2", "lower": -3.0, "upper": 3.0, "max_velocity": 1.0,
         "unit": "rad", "kind": "revolute"},
    ],
    "frames": {"base": "b", "tool": "t"},
    "tcp_offset": {"x": 0.0, "y": 0.0, "z": 0.1},
    "work_radius_m": 0.8,
    "payload_kg": 3.0,
    "supported_skills": ["home", "move", "pick", "place", "stop"],
    "gripper": {
        "joint_name": "g", "open_position": 0.02, "close_position": 0.55,
        "unit": "rad", "grasp_aperture_m": 0.0286, "max_effort": 50.0,
        "fully_open_margin": 0.05,
    },
}


class TestTaskPlanBoundary(unittest.TestCase):
    def test_valid_payload_converts_to_core(self):
        plan = parse_task_plan(PLAN_JSON).to_core()
        self.assertEqual(plan.plan_id, "p")
        self.assertEqual(plan.terminal_hold, "obj_a")

    def test_round_trip_preserves_hash(self):
        plan = parse_task_plan(PLAN_JSON).to_core()
        self.assertEqual(TaskPlanModel.from_core(plan).plan_hash, plan.plan_hash())

    def test_shape_errors_are_reason_coded(self):
        for payload, label in [
            ({**PLAN_JSON, "unknown_field": 1}, "미지의 필드"),
            ({**PLAN_JSON, "ttl_sec": 0}, "ttl 범위"),
            ({**PLAN_JSON, "steps": []}, "빈 steps"),
            ({**PLAN_JSON, "steps": [{"skill": "move", "args": {"to": [0.4, 0.3]}}]}, "좌표 삽입"),
            ({k: v for k, v in PLAN_JSON.items() if k != "plan_id"}, "필수 키 누락"),
        ]:
            with self.subTest(label=label):
                with self.assertRaises(BoundaryValidationError) as ctx:
                    parse_task_plan(payload)
                self.assertIs(ctx.exception.reason, ReasonCode.PLAN_SCHEMA_INVALID)
                self.assertTrue(ctx.exception.issues)

    def test_meaning_errors_keep_their_own_reason(self):
        for payload, reason in [
            ({**PLAN_JSON, "steps": [{"skill": "fly"}]}, ReasonCode.PLAN_UNSUPPORTED_SKILL),
            ({**PLAN_JSON, "steps": [{"skill": "pick", "args": {"object": "o"}}]},
             ReasonCode.PLAN_ARG_MISSING),
            ({**PLAN_JSON, "schema_version": "1.0"}, ReasonCode.PLAN_VERSION_UNSUPPORTED),
        ]:
            with self.subTest(reason=reason):
                with self.assertRaises(BoundaryValidationError) as ctx:
                    parse_task_plan(payload).to_core()
                self.assertIs(ctx.exception.reason, reason)

    def test_json_schema_is_generated_from_the_type(self):
        schema = task_plan_json_schema()
        self.assertIn("steps", schema["properties"])
        self.assertIn("terminal_hold", schema["properties"])


class TestResponseModels(unittest.TestCase):
    def test_execution_result_keeps_five_axes(self):
        from core.execution_result import success

        m = ExecutionResultModel.from_core(success({"lift_m": 0.01}))
        for f in ("request_accepted", "motion_completed", "target_reached",
                  "task_succeeded", "verified"):
            self.assertIn(f, m.model_dump())

    def test_permit_decision_carries_all_reasons(self):
        m = PermitDecisionModel(
            granted=False,
            reasons=[{"reason": "exec.permit_denied", "detail": "d1"},
                     {"reason": "robot.state_stale", "detail": "d2"}],
        )
        self.assertEqual(len(m.reasons), 2)


class TestConfigBoundary(unittest.TestCase):
    def test_valid_profile_loads(self):
        p = load_capability_profile(PROFILE_JSON)
        self.assertEqual(p.dof, 2)
        self.assertIsNotNone(p.gripper)

    def test_config_failures_are_reason_coded(self):
        for payload, reason, label in [
            ({k: v for k, v in PROFILE_JSON.items() if k != "gripper"},
             ReasonCode.CONFIG_MISSING, "그리퍼 누락"),
            ({**PROFILE_JSON, "dof": 3}, ReasonCode.CONFIG_INVALID, "dof 불일치"),
            ({**PROFILE_JSON, "frames": {"base": "b", "tool": "t", "galaxy": "g"}},
             ReasonCode.PLAN_UNKNOWN_FRAME, "미지의 프레임"),
            ({**PROFILE_JSON, "work_radius_m": 0}, ReasonCode.CONFIG_INVALID, "범위 위반"),
            ({**PROFILE_JSON, "surprise": 1}, ReasonCode.CONFIG_INVALID, "미지의 키"),
        ]:
            with self.subTest(label=label):
                with self.assertRaises(BoundaryValidationError) as ctx:
                    load_capability_profile(payload)
                self.assertIs(ctx.exception.reason, reason)

    def test_non_si_unit_is_rejected_at_the_boundary(self):
        bad = {**PROFILE_JSON, "joint_limits": [
            {**PROFILE_JSON["joint_limits"][0], "unit": "deg"},
            PROFILE_JSON["joint_limits"][1],
        ]}
        with self.assertRaises(BoundaryValidationError) as ctx:
            load_capability_profile(bad)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_policies_require_provenance(self):
        with self.assertRaises(BoundaryValidationError) as ctx:
            load_safety_policy({"policy_version": "1", "max_steps": 10, "provenance": {}})
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)

    def test_all_policies_load(self):
        self.assertEqual(
            load_safety_policy(
                {"policy_version": "1", "max_steps": 10, "provenance": {"max_steps": "why"}}
            ).max_steps,
            10,
        )
        self.assertEqual(
            load_freshness_policy({
                "policy_version": "1", "robot_state_max_age_sec": 1.0,
                "environment_max_age_sec": 3.0,
                "provenance": {"robot_state_max_age_sec": "w", "environment_max_age_sec": "w"},
            }).environment_max_age_sec,
            3.0,
        )
        self.assertEqual(
            load_stop_policy({
                "policy_version": "1", "position_tolerance": {"ch": 0.02}, "hold_sec": 0.5,
                "cancel_ack_timeout_sec": 5.0, "max_sample_gap_sec": 0.3,
                "provenance": {"hold_sec": "w", "cancel_ack_timeout_sec": "w",
                               "max_sample_gap_sec": "w"},
            }).tolerance_for("ch"),
            0.02,
        )

    def test_config_json_schemas_are_generated(self):
        self.assertEqual(
            set(config_json_schemas()),
            {"capability_profile", "safety_policy", "freshness_policy", "stop_policy",
             "stt_policy", "stt_model_config", "stt_profile_catalog",
         "resource_catalog", "skill_catalog", "planning_policy",
         "llm_provider"},
        )


class TestCoreStaysIndependent(unittest.TestCase):
    """core 계약과 검증기는 Pydantic을 import하지 않는다 — 경계에만 둔다.

    `core/boundary.py`는 pydantic의 ValidationError를 덕타이핑으로 받으므로
    주석·함수명에 이름은 나오지만 import는 없다. 검사 기준은 import 여부다.
    """

    IMPORT_RE = re.compile(r"^\s*(?:import\s+pydantic|from\s+pydantic)", re.MULTILINE)
    DEPENDENCY_FREE_DIRS = ("core", "validation", "robots/base")

    def test_dependency_free_layers_do_not_import_pydantic(self):
        root = Path(__file__).resolve().parents[2]
        checked = 0
        for d in self.DEPENDENCY_FREE_DIRS:
            for path in sorted((root / d).glob("*.py")):
                checked += 1
                with self.subTest(file=f"{d}/{path.name}"):
                    self.assertIsNone(
                        self.IMPORT_RE.search(path.read_text(encoding="utf-8")),
                        f"{d}/{path.name}이 pydantic을 import한다",
                    )
        self.assertGreater(checked, 0)

    def test_boundary_layers_do_import_pydantic(self):
        """반대로 경계 모듈은 실제로 Pydantic을 쓰고 있어야 한다."""
        root = Path(__file__).resolve().parents[2]
        for rel in ("server/schemas.py", "config/loader.py"):
            with self.subTest(file=rel):
                self.assertIsNotNone(
                    self.IMPORT_RE.search((root / rel).read_text(encoding="utf-8"))
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
