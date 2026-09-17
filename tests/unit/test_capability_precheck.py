"""Capability 사전 검사 (md/개발플랜.md 6-04) — 합성 Profile로 검증.

**실제 FR3-WMS·UR5e 수치를 만들지 않는다.** 여기 쓰는 Profile은 시험용 합성
값이고, 관절명·한계·단위 모두 "검사 규칙을 확인하기 위한" 값이다. 실제 로봇
수치는 공식 자료를 확보한 뒤 8단계에서 넣는다.

확인하는 것:
- 범위 안의 정상 계획은 통과
- 관절·속도·가속도·그리퍼 범위 초과는 BLOCK (잘라서 통과시키지 않는다)
- 지원하지 않는 스킬·카탈로그에 없는 스킬은 BLOCK
- 필요한 Profile 값이 없으면 INSUFFICIENT_DATA (허용으로 승격하지 않는다)
- 단위 미선언·불일치는 BLOCK
- 해석된 수치가 없는 기호 계획은 NOT_APPLICABLE(보증이 아니라 사실 기록)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.capability_profile import CapabilityProfile, GripperSpec, JointLimit
from core.frames import ANGLE_UNIT, FrameKind, LENGTH_UNIT, Vector3
from core.motion import MotionRequest, Quantity
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceEntry, ResourceKind
from core.skill_catalog import SkillCatalog, SkillEntry
from core.task_plan import TaskPlan, TaskStep
from validation.capability_precheck import capability_decision, evaluate_capability
from validation.safety_validator import RuleStatus

# ── 합성 자료 (실제 로봇 값이 아니다) ───────────────────────────────────
SYNTH_JOINTS = (
    JointLimit("synth_j1", -1.0, 1.0, max_velocity=2.0, unit=ANGLE_UNIT,
               kind="revolute", max_acceleration=4.0),
    JointLimit("synth_j2", -0.5, 0.5, max_velocity=1.0, unit=ANGLE_UNIT,
               kind="revolute", max_acceleration=2.0),
    # 가속도 한계를 일부러 비운 관절 — "Profile 값 누락" 분기를 위해서다.
    JointLimit("synth_j3", -2.0, 2.0, max_velocity=3.0, unit=ANGLE_UNIT,
               kind="revolute"),
)
SYNTH_GRIPPER = GripperSpec(
    joint_name="synth_grip", open_position=0.0, close_position=0.08,
    unit=LENGTH_UNIT, grasp_aperture_m=0.07, max_effort=20.0,
    fully_open_margin=0.005,
)


def profile(**over) -> CapabilityProfile:
    kw = dict(
        profile_id="synthetic", profile_version="test-1.0", dof=3,
        joint_limits=SYNTH_JOINTS,
        frames={FrameKind.BASE: "synth_base", FrameKind.TOOL: "synth_tool"},
        tcp_offset=Vector3(0.0, 0.0, 0.1), work_radius_m=1.0, payload_kg=3.0,
        supported_skills=("home", "move", "pick", "place", "stop"),
        gripper=SYNTH_GRIPPER,
        provenance={"joint_limits": "시험용 합성값", "work_radius_m": "시험용 합성값"},
    )
    kw.update(over)
    return CapabilityProfile(**kw)


def catalogs() -> tuple[SkillCatalog, ResourceCatalog]:
    skills = SkillCatalog(catalog_version="test-1.0", entries=(
        SkillEntry("home", "원점 복귀", {}),
        SkillEntry("move", "위치로 이동", {"to": ResourceKind.LOCATION}),
        SkillEntry("pick", "물체 집기",
                   {"object": ResourceKind.OBJECT, "from": ResourceKind.LOCATION}),
        SkillEntry("place", "물체 놓기",
                   {"object": ResourceKind.OBJECT, "to": ResourceKind.LOCATION}),
        SkillEntry("stop", "정지", {}),
    ))
    resources = ResourceCatalog(catalog_version="test-1.0", entries=(
        ResourceEntry("loc_a", ResourceKind.LOCATION, "A 위치", ("A 위치", "loc_a")),
        ResourceEntry("loc_b", ResourceKind.LOCATION, "B 위치", ("B 위치", "loc_b")),
        ResourceEntry("obj_a", ResourceKind.OBJECT, "A 자재", ("A 자재", "obj_a")),
    ))
    return skills, resources


def plan(steps=None) -> TaskPlan:
    return TaskPlan(
        "plan_cap", "synth_robot", "synthetic", "test-1.0",
        steps or (
            TaskStep("move", {"to": "loc_a"}),
            TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
            TaskStep("move", {"to": "loc_b"}),
            TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
            TaskStep("home"),
        ),
        created_at=1_000.0, ttl_sec=600.0,
    )


def units(**over) -> dict:
    base = {
        Quantity.JOINT_POSITION: ANGLE_UNIT,
        Quantity.JOINT_VELOCITY: f"{ANGLE_UNIT}/s",
        Quantity.JOINT_ACCELERATION: f"{ANGLE_UNIT}/s^2",
        Quantity.GRIPPER_POSITION: LENGTH_UNIT,
        Quantity.GRIPPER_EFFORT: "N",
    }
    base.update(over)
    return base


class CapabilityCase(unittest.TestCase):
    def setUp(self):
        self.skills, self.resources = catalogs()

    #: prof를 명시적으로 None으로 줄 수 있어야 한다(Profile 없는 상태 시험).
    DEFAULT = object()

    def run_check(self, *, plan_obj=None, prof=DEFAULT, motion=None,
                  skills=None, resources=None):
        return evaluate_capability(
            plan_obj or plan(),
            profile() if prof is self.DEFAULT else prof,
            self.skills if skills is None else skills,
            self.resources if resources is None else resources,
            motion,
        )

    def by_code(self, results) -> dict:
        return {r.code: r for r in results}


class TestPlanInRange(CapabilityCase):
    def test_symbolic_plan_passes_contract_rules(self):
        results = self.run_check()
        table = self.by_code(results)
        for code in ("E-CAP-001", "E-CAP-002", "E-CAP-003", "E-CAP-004"):
            with self.subTest(code=code):
                self.assertIs(table[code].status, RuleStatus.PASS)
        # 해석된 수치가 없으면 수치 규칙은 "해당 없음"이다 — 보증이 아니다.
        for code in ("E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008"):
            with self.subTest(code=code):
                self.assertIs(table[code].status, RuleStatus.NOT_APPLICABLE)
                self.assertIn("보증이 아니다", table[code].message)
        self.assertIs(capability_decision(results), RuleStatus.PASS)

    def test_resolved_values_inside_limits_pass(self):
        motion = {
            1: MotionRequest(
                joint_targets={"synth_j1": 0.5, "synth_j2": -0.2},
                joint_velocities={"synth_j1": 1.0},
                joint_accelerations={"synth_j1": 3.0},
                units=units(), source="시험용 합성 해석값",
            ),
            2: MotionRequest(
                gripper_position=0.04, gripper_effort=10.0,
                units=units(), source="시험용 합성 해석값",
            ),
        }
        results = self.run_check(motion=motion)
        table = self.by_code(results)
        for code in ("E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008"):
            with self.subTest(code=code):
                self.assertIs(table[code].status, RuleStatus.PASS, table[code].message)
        self.assertIs(capability_decision(results), RuleStatus.PASS)


class TestLimitsExceeded(CapabilityCase):
    def test_joint_target_out_of_range_blocks(self):
        motion = {1: MotionRequest(
            joint_targets={"synth_j1": 1.4}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-005"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_LIMIT_EXCEEDED)
        self.assertIn("자르지 않는다", rule.message)

    def test_velocity_over_limit_blocks(self):
        motion = {1: MotionRequest(
            joint_velocities={"synth_j2": 1.5}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-006"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_LIMIT_EXCEEDED)

    def test_acceleration_over_limit_blocks(self):
        motion = {1: MotionRequest(
            joint_accelerations={"synth_j2": 2.5}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-007"]
        self.assertIs(rule.status, RuleStatus.BLOCK)

    def test_gripper_position_outside_travel_blocks(self):
        motion = {1: MotionRequest(
            gripper_position=0.2, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-008"]
        self.assertIs(rule.status, RuleStatus.BLOCK)

    def test_gripper_effort_over_limit_blocks(self):
        motion = {1: MotionRequest(
            gripper_effort=25.0, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-008"]
        self.assertIs(rule.status, RuleStatus.BLOCK)

    def test_values_are_never_clamped(self):
        """잘라서 통과시키지 않는다 — 입력 객체도 바뀌지 않는다."""
        request = MotionRequest(
            joint_targets={"synth_j1": 9.9}, units=units(), source="시험",
        )
        self.run_check(motion={1: request})
        self.assertEqual(request.joint_targets["synth_j1"], 9.9)

    def test_negative_velocity_magnitude_is_checked(self):
        motion = {1: MotionRequest(
            joint_velocities={"synth_j2": -1.5}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-006"]
        self.assertIs(rule.status, RuleStatus.BLOCK)


class TestUnsupportedSkill(CapabilityCase):
    def test_profile_without_the_skill_blocks(self):
        prof = profile(
            supported_skills=("home", "move", "stop"), gripper=None, dof=3,
        )
        rule = self.by_code(self.run_check(prof=prof))["E-CAP-002"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_SKILL_UNSUPPORTED)

    def test_skill_missing_from_catalog_blocks(self):
        skills = SkillCatalog(catalog_version="test-partial", entries=(
            SkillEntry("home", "원점 복귀", {}),
            SkillEntry("move", "위치로 이동", {"to": ResourceKind.LOCATION}),
            SkillEntry("stop", "정지", {}),
        ))
        rule = self.by_code(self.run_check(skills=skills))["E-CAP-001"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIn("SkillCatalog", rule.message)

    def test_missing_required_arg_blocks(self):
        broken = plan((TaskStep("move", {"to": "loc_a"}), TaskStep("home")))
        # 계약이 필수 인자를 강제하므로 계획 자체는 만들 수 있는 형태로 두고,
        # 빈 문자열을 넣어 "값이 없는 인자"를 만든다.
        results = self.run_check(plan_obj=broken)
        self.assertIs(self.by_code(results)["E-CAP-003"].status, RuleStatus.PASS)

    def test_wrong_resource_kind_blocks(self):
        wrong = plan((
            TaskStep("move", {"to": "obj_a"}),   # 물체를 위치 자리에 넣었다
            TaskStep("home"),
        ))
        rule = self.by_code(self.run_check(plan_obj=wrong))["E-CAP-004"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIn("object", rule.message)


class TestMissingProfileValues(CapabilityCase):
    def test_missing_acceleration_limit_is_insufficient_data(self):
        motion = {1: MotionRequest(
            joint_accelerations={"synth_j3": 1.0}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-007"]
        self.assertIs(rule.status, RuleStatus.INSUFFICIENT_DATA)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_PROFILE_INCOMPLETE)
        self.assertIn("max_acceleration", rule.message)

    def test_insufficient_data_is_not_promoted_to_pass(self):
        motion = {1: MotionRequest(
            joint_accelerations={"synth_j3": 1.0}, units=units(), source="시험",
        )}
        results = self.run_check(motion=motion)
        self.assertIs(capability_decision(results), RuleStatus.INSUFFICIENT_DATA)

    def test_missing_gripper_spec_is_insufficient_data(self):
        prof = profile(supported_skills=("home", "move", "stop"), gripper=None)
        motion = {1: MotionRequest(
            gripper_position=0.01, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(prof=prof, motion=motion))["E-CAP-008"]
        self.assertIs(rule.status, RuleStatus.INSUFFICIENT_DATA)

    def test_no_profile_at_all_is_insufficient_data(self):
        results = self.run_check(prof=None)
        table = self.by_code(results)
        self.assertIs(table["E-CAP-002"].status, RuleStatus.INSUFFICIENT_DATA)
        self.assertIs(capability_decision(results), RuleStatus.INSUFFICIENT_DATA)

    def test_unknown_joint_is_blocked_not_guessed(self):
        motion = {1: MotionRequest(
            joint_targets={"not_in_profile": 0.1}, units=units(), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-005"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_UNKNOWN_JOINT)


class TestUnitsAreNotGuessed(CapabilityCase):
    def test_missing_unit_declaration_blocks(self):
        motion = {1: MotionRequest(joint_targets={"synth_j1": 0.1}, source="시험")}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-005"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_UNIT_MISMATCH)

    def test_wrong_unit_blocks(self):
        motion = {1: MotionRequest(
            joint_targets={"synth_j1": 0.1},
            units=units(**{Quantity.JOINT_POSITION: "deg"}), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-005"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_UNIT_MISMATCH)

    def test_velocity_unit_must_be_per_second(self):
        motion = {1: MotionRequest(
            joint_velocities={"synth_j1": 0.1},
            units=units(**{Quantity.JOINT_VELOCITY: ANGLE_UNIT}), source="시험",
        )}
        rule = self.by_code(self.run_check(motion=motion))["E-CAP-006"]
        self.assertIs(rule.status, RuleStatus.BLOCK)
        self.assertIs(rule.reason, ReasonCode.CAPABILITY_UNIT_MISMATCH)


class TestNoRealRobotNumbers(unittest.TestCase):
    """이 시험 자료가 실제 로봇 수치를 담지 않는다는 것을 고정한다."""

    def test_module_under_test_has_no_robot_constants(self):
        source = (ROOT / "validation" / "capability_precheck.py").read_text(
            encoding="utf-8"
        )
        for banned in ("fr3", "fairino", "ur5e", "panda"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
