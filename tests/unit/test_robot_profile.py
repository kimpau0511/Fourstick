"""조합형 로봇 Profile (md/개발플랜.md 8-02).

확인하는 것:

- Profile JSON ↔ Pydantic ↔ 계약 왕복
- 단위 누락·불일치 차단
- 관절 이름·순서 불일치 차단
- 제한값 누락 시 ASK(정보 부족) — 통과로 승격하지 않는다
- 그리퍼 닫힘 방향과 aperture 범위
- MountingProfile 누락 시 pick·place 비활성화
- 구성 버전이 바뀌면 지문(version_key)이 바뀐다
- 팔·장착만 교체하고 그리퍼 Profile을 재사용할 수 있다

**실제 로봇 수치를 만들지 않는다.** 여기 값은 시험용 합성값이고, 실제 구성
파일(`config/profiles/*.json`)은 별도 테스트에서 "미확보 상태 그대로"를 본다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_arm_profile,
    load_composite_profile,
    load_gripper_profile,
    load_mounting_profile,
)
from core.boundary import BoundaryValidationError
from core.capability_profile import CapabilityProfile
from core.frames import FrameKind
from core.provenance import (
    Measured,
    Provenance,
    ProvenanceError,
    VerificationStatus,
    measured,
    unavailable,
)
from core.robot_profile import (
    ArmCapabilityProfile,
    ArmJointSpec,
    CompositeRobotProfile,
    Environment,
    GripperCapabilityProfile,
    JointKind,
    MountingProfile,
    RobotProfileError,
    swap_arm,
)
from core.reason_codes import ReasonCode
from validation.capability_precheck import capability_decision, evaluate_capability
from validation.safety_validator import RuleStatus

NOW = 1_700_000_000.0
SRC = dict(source_kind="urdf", source="synthetic_arm.urdf",
           source_version="test-1.0", source_commit="deadbeef", checked_at=NOW)


def value(v: float, unit: str, **over) -> Measured:
    kw = dict(SRC)
    kw.update(over)
    return measured(v, unit, **kw)


def joint(name: str, **over) -> ArmJointSpec:
    kw = dict(
        kind=JointKind.REVOLUTE,
        lower=value(-1.0, "rad"), upper=value(1.0, "rad"),
        max_velocity=value(2.0, "rad/s"), max_acceleration=value(4.0, "rad/s^2"),
    )
    kw.update(over)
    return ArmJointSpec(name=name, **kw)


def arm(**over) -> ArmCapabilityProfile:
    kw = dict(
        arm_profile_id="synth_arm", arm_profile_version="test-1.0",
        display_name="Synthetic Arm",
        joints=tuple(joint(f"synth_j{i}") for i in range(1, 4)),
        payload=value(3.0, "kg"), reach=value(0.8, "m"),
        frames={FrameKind.BASE: "synth_base", FrameKind.TOOL: "synth_flange"},
        controller_requirement=">=1.0.0",
        declared_skills=("home", "move", "pick", "place", "stop"),
        state_max_age=value(0.5, "s"), connect_timeout=value(5.0, "s"),
        environment=Environment.SIMULATION, verified=False,
    )
    kw.update(over)
    return ArmCapabilityProfile(**kw)


def gripper(**over) -> GripperCapabilityProfile:
    kw = dict(
        gripper_profile_id="synth_gripper", gripper_profile_version="test-1.0",
        display_name="Synthetic 2F Gripper", two_finger_parallel=True,
        command_joint="synth_drive_joint",
        mimic_joints={"synth_follow_joint": -1.0},
        open_position=value(0.0, "rad"), closed_position=value(0.79, "rad"),
        joint_lower=value(0.0, "rad"), joint_upper=value(0.8, "rad"),
        max_velocity=value(0.5, "rad/s"), max_effort=value(50.0, "N*m"),
        pad_aperture_open=value(0.085, "m"), pad_aperture_closed=value(0.002, "m"),
        fully_open_margin=value(0.02, "rad"),
        command_interfaces=("position",), state_interfaces=("position", "velocity"),
        object_detection_real=True, object_detection_simulation=False,
        base_frame="synth_gripper_base",
    )
    kw.update(over)
    return GripperCapabilityProfile(**kw)


def mounting(**over) -> MountingProfile:
    kw = dict(
        mounting_profile_id="synth_mounting", mounting_profile_version="test-1.0",
        arm_flange_frame="synth_flange", gripper_base_frame="synth_gripper_base",
        xyz=(value(0.0, "m"), value(0.0, "m"), value(0.01, "m")),
        rpy=(value(0.0, "rad"), value(0.0, "rad"), value(0.0, "rad")),
        adapter_plate_id="synth-plate-001",
        tcp_frame="synth_tcp",
        tcp_xyz=(value(0.0, "m"), value(0.0, "m"), value(0.15, "m")),
    )
    kw.update(over)
    return MountingProfile(**kw)


def composite(**over) -> CompositeRobotProfile:
    kw = dict(
        composite_profile_id="synth_composite",
        composite_profile_version="test-1.0",
        arm=arm(), gripper=gripper(), mounting=mounting(),
        asset_manifest_version="assets-test-1",
        environment=Environment.SIMULATION, verified=False,
    )
    kw.update(over)
    return CompositeRobotProfile(**kw)


class TestProvenanceContract(unittest.TestCase):
    def test_value_requires_unit_and_source(self):
        with self.assertRaises(ProvenanceError):
            Measured(value=1.0, unit="", provenance=Provenance(
                source_kind="urdf", source="x", status=VerificationStatus.DECLARED,
                checked_at=NOW,
            ))

    def test_value_without_provenance_is_refused(self):
        with self.assertRaises(ProvenanceError):
            Measured(value=1.0, unit="m")

    def test_missing_value_must_be_unavailable(self):
        with self.assertRaises(ProvenanceError):
            Measured(value=None, unit="m", provenance=Provenance(
                source_kind="urdf", source="x", status=VerificationStatus.VERIFIED,
                checked_at=NOW,
            ))

    def test_status_requires_checked_at(self):
        with self.assertRaises(ProvenanceError):
            Provenance(source_kind="urdf", source="x",
                       status=VerificationStatus.DECLARED, checked_at=0.0)

    def test_unavailable_helper_keeps_the_unit(self):
        item = unavailable("rad", "근거 없음")
        self.assertIsNone(item.value)
        self.assertEqual(item.unit, "rad")
        self.assertIs(item.status, VerificationStatus.UNAVAILABLE)
        with self.assertRaises(ProvenanceError):
            item.require("테스트")


class TestUnitsAreChecked(unittest.TestCase):
    def test_wrong_position_unit_is_refused(self):
        with self.assertRaises(RobotProfileError) as ctx:
            joint("synth_j1", lower=value(-1.0, "deg"))
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_velocity_unit_must_be_per_second(self):
        with self.assertRaises(RobotProfileError) as ctx:
            joint("synth_j1", max_velocity=value(1.0, "rad"))
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_acceleration_unit_must_be_per_second_squared(self):
        with self.assertRaises(RobotProfileError):
            joint("synth_j1", max_acceleration=value(1.0, "rad/s"))

    def test_prismatic_joint_uses_length_units(self):
        spec = joint(
            "synth_rail", kind=JointKind.PRISMATIC,
            lower=value(0.0, "m"), upper=value(0.4, "m"),
            max_velocity=value(0.2, "m/s"), max_acceleration=value(0.5, "m/s^2"),
        )
        self.assertEqual(spec.to_joint_limit().unit, "m")

    def test_missing_unit_on_unavailable_value_is_still_declared(self):
        """값이 없어도 단위는 남긴다 — 나중에 채울 때 단위를 추정하지 않기 위해."""
        spec = joint("synth_j1", lower=unavailable("rad"))
        self.assertFalse(spec.complete)
        self.assertEqual(spec.lower.unit, "rad")


class TestJointNamesAndOrder(unittest.TestCase):
    def test_order_is_preserved(self):
        profile = arm()
        self.assertEqual(profile.joint_names, ("synth_j1", "synth_j2", "synth_j3"))

    def test_duplicate_names_are_refused(self):
        with self.assertRaises(RobotProfileError) as ctx:
            arm(joints=(joint("synth_j1"), joint("synth_j1")))
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)

    def test_reordered_joints_change_the_capability_profile(self):
        first = composite().capability_profile()
        shuffled = tuple(reversed(arm().joints))
        second = composite(arm=arm(joints=shuffled)).capability_profile()
        self.assertNotEqual(
            [j.name for j in first.joint_limits],
            [j.name for j in second.joint_limits],
        )

    def test_dof_follows_joint_count(self):
        self.assertEqual(arm().dof, 3)
        self.assertEqual(composite().capability_profile().dof, 3)

    def test_inverted_limits_are_refused(self):
        with self.assertRaises(RobotProfileError):
            joint("synth_j1", lower=value(1.0, "rad"), upper=value(-1.0, "rad"))


class TestMissingLimitsBlockExecution(unittest.TestCase):
    def test_missing_limit_makes_the_profile_incomplete(self):
        profile = arm(joints=(
            joint("synth_j1", max_velocity=unavailable("rad/s")),
            joint("synth_j2"),
        ))
        self.assertFalse(profile.complete)
        self.assertIn("synth_j1.max_velocity", profile.missing)

    def test_profile_without_motion_values_cannot_be_built(self):
        broken = composite(arm=arm(reach=unavailable("m")))
        with self.assertRaises(RobotProfileError) as ctx:
            broken.capability_profile()
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)

    def test_missing_payload_keeps_motion_but_drops_pick_and_place(self):
        """페이로드는 **드는 스킬**의 전제다. 이동은 페이로드 없이도 한다."""
        no_payload = composite(arm=arm(payload=unavailable("kg")))
        self.assertTrue(no_payload.motion_ready)
        self.assertFalse(no_payload.complete)
        self.assertEqual(set(no_payload.excluded_skills), {"pick", "place"})
        profile = no_payload.capability_profile()
        self.assertIsNone(profile.payload_kg)
        self.assertFalse(profile.can_lift)
        self.assertEqual(set(profile.supported_skills), {"home", "move", "stop"})

    def test_capability_profile_refuses_lifting_without_payload(self):
        from core.capability_profile import CapabilityProfile, ProfileError

        with self.assertRaises(ProfileError):
            CapabilityProfile(
                profile_id="x", profile_version="1", dof=1,
                joint_limits=(joint("synth_j1").to_joint_limit(),),
                frames={FrameKind.BASE: "b", FrameKind.TOOL: "t"},
                tcp_offset=__import__(
                    "core.frames", fromlist=["Vector3"]
                ).Vector3(0.0, 0.0, 0.0),
                work_radius_m=0.5, payload_kg=None,
                supported_skills=("pick", "place"),
                gripper=gripper().to_gripper_spec(),
            )

    def test_missing_acceleration_is_ask_in_the_precheck(self):
        """제한값이 없으면 사전 검사가 ASK(정보 부족)로 돌린다."""
        from core.motion import MotionRequest, Quantity
        from core.resource_catalog import ResourceCatalog, ResourceEntry, ResourceKind
        from core.skill_catalog import SkillCatalog, SkillEntry
        from core.task_plan import TaskPlan, TaskStep

        profile = composite(
            arm=arm(joints=(
                joint("synth_j1", max_acceleration=unavailable("rad/s^2")),
                joint("synth_j2"), joint("synth_j3"),
            ))
        ).capability_profile()
        skills = SkillCatalog(catalog_version="t", entries=(
            SkillEntry("home", "원점", {}),
            SkillEntry("move", "이동", {"to": ResourceKind.LOCATION}),
        ))
        resources = ResourceCatalog(catalog_version="t", entries=(
            ResourceEntry("loc_a", ResourceKind.LOCATION, "A", ("A", "loc_a")),
        ))
        plan = TaskPlan(
            "p", profile.profile_id, profile.profile_id, profile.profile_version,
            (TaskStep("move", {"to": "loc_a"}), TaskStep("home")),
            created_at=1.0, ttl_sec=60.0,
        )
        motion = {1: MotionRequest(
            joint_accelerations={"synth_j1": 1.0},
            units={Quantity.JOINT_ACCELERATION: "rad/s^2"}, source="시험",
        )}
        results = evaluate_capability(plan, profile, skills, resources, motion)
        self.assertIs(capability_decision(results), RuleStatus.INSUFFICIENT_DATA)

    def test_verified_with_missing_motion_items_is_refused(self):
        with self.assertRaises(RobotProfileError):
            arm(reach=unavailable("m"), verified=True)


class TestGripperContract(unittest.TestCase):
    def test_closing_direction_comes_from_values_not_guesses(self):
        self.assertTrue(gripper().closes_by_increasing)
        reversed_gripper = gripper(
            open_position=value(0.8, "rad"), closed_position=value(0.0, "rad")
        )
        self.assertFalse(reversed_gripper.closes_by_increasing)

    def test_unknown_direction_is_none(self):
        unknown = gripper(closed_position=unavailable("rad"))
        self.assertIsNone(unknown.closes_by_increasing)

    def test_same_open_and_closed_position_is_refused(self):
        with self.assertRaises(RobotProfileError):
            gripper(open_position=value(0.5, "rad"), closed_position=value(0.5, "rad"))

    def test_command_joint_cannot_be_a_mimic_joint(self):
        with self.assertRaises(RobotProfileError):
            gripper(mimic_joints={"synth_drive_joint": -1.0})

    def test_zero_mimic_multiplier_is_refused(self):
        with self.assertRaises(RobotProfileError):
            gripper(mimic_joints={"synth_follow_joint": 0.0})

    def test_non_parallel_gripper_is_refused(self):
        with self.assertRaises(RobotProfileError):
            gripper(two_finger_parallel=False)

    def test_aperture_range_is_required_for_the_gripper_spec(self):
        without = gripper(pad_aperture_closed=unavailable("m"))
        self.assertIn("pad_aperture_closed", without.missing)
        with self.assertRaises(ProvenanceError):
            without.to_gripper_spec()

    def test_aperture_values_reach_the_gripper_spec(self):
        spec = gripper().to_gripper_spec()
        self.assertEqual(spec.grasp_aperture_m, 0.002)
        self.assertTrue(spec.closes_by_increasing)

    def test_simulation_cannot_claim_object_detection_the_hardware_lacks(self):
        with self.assertRaises(RobotProfileError):
            gripper(object_detection_real=False, object_detection_simulation=True)

    def test_effort_is_not_treated_as_grasp_force(self):
        """관절 토크 한계를 파지력으로 쓰지 않는다 — 별 이름으로 남는다."""
        payload = gripper().to_dict()
        self.assertEqual(payload["max_effort"]["unit"], "N*m")
        self.assertNotIn("grasp_force", payload)


class TestMountingGatesPickAndPlace(unittest.TestCase):
    def test_complete_mounting_allows_pick_and_place(self):
        self.assertEqual(
            set(composite().supported_skills),
            {"home", "move", "pick", "place", "stop"},
        )

    def test_missing_mounting_profile_disables_pick_and_place(self):
        without = composite(mounting=None)
        self.assertNotIn("pick", without.supported_skills)
        self.assertNotIn("place", without.supported_skills)
        self.assertEqual(set(without.excluded_skills), {"pick", "place"})

    def test_incomplete_mounting_disables_pick_and_place(self):
        partial = composite(mounting=mounting(tcp_xyz=None, tcp_frame=""))
        self.assertNotIn("pick", partial.supported_skills)
        self.assertIn("mounting.tcp_xyz", partial.blocking_items)

    def test_identity_transform_is_not_substituted(self):
        """장착 변환이 없으면 0으로 채우지 않는다."""
        empty = mounting(
            xyz=(unavailable("m"), unavailable("m"), unavailable("m")),
            tcp_xyz=None,
        )
        self.assertFalse(empty.complete)
        with self.assertRaises(RobotProfileError):
            empty.tcp_offset()

    def test_missing_gripper_profile_disables_pick_and_place(self):
        without = composite(gripper=None)
        self.assertEqual(set(without.excluded_skills), {"pick", "place"})
        self.assertIn("gripper.missing_profile", without.blocking_items)


class TestCompositeVersioning(unittest.TestCase):
    def test_version_key_changes_with_any_part(self):
        base = composite().version_key()
        self.assertNotEqual(
            base, composite(arm=arm(arm_profile_version="test-2.0")).version_key()
        )
        self.assertNotEqual(
            base,
            composite(
                gripper=gripper(gripper_profile_version="test-2.0")
            ).version_key(),
        )
        self.assertNotEqual(
            base,
            composite(
                mounting=mounting(mounting_profile_version="test-2.0")
            ).version_key(),
        )
        self.assertNotEqual(
            base, composite(asset_manifest_version="assets-test-2").version_key()
        )

    def test_capability_profile_carries_the_composite_version(self):
        profile = composite().capability_profile()
        self.assertIsInstance(profile, CapabilityProfile)
        self.assertEqual(profile.profile_id, "synth_composite")
        self.assertEqual(profile.profile_version, "test-1.0")

    def test_provenance_summary_names_the_sources(self):
        profile = composite().capability_profile()
        self.assertIn("arm.payload", profile.provenance)
        self.assertIn("synthetic_arm.urdf", profile.provenance["arm.payload"])

    def test_verified_composite_requires_no_blocking_items(self):
        with self.assertRaises(RobotProfileError):
            composite(mounting=None, verified=True)

    def test_swapping_the_arm_reuses_the_same_gripper_profile(self):
        original = composite()
        other_arm = arm(arm_profile_id="other_arm", display_name="Other Arm")
        other_mounting = mounting(
            mounting_profile_id="other_mounting",
            arm_flange_frame="other_flange",
        )
        swapped = swap_arm(
            original, other_arm, other_mounting,
            composite_profile_id="other_composite",
            composite_profile_version="test-1.0",
        )
        # 같은 그리퍼 Profile 객체를 재사용한다.
        self.assertIs(swapped.gripper, original.gripper)
        self.assertEqual(
            swapped.gripper.gripper_profile_id, original.gripper.gripper_profile_id
        )
        self.assertFalse(swapped.verified)
        self.assertEqual(
            set(swapped.supported_skills), set(original.supported_skills)
        )


class TestJsonRoundTrip(unittest.TestCase):
    """JSON → Pydantic → 계약 → dict 왕복."""

    def round_trip(self, profile, loader):
        payload = json.loads(json.dumps(self.to_json(profile), ensure_ascii=False))
        restored = loader(payload)
        return restored

    @staticmethod
    def to_json(profile) -> dict:
        """계약 객체를 설정 JSON 모양으로 되돌린다(로더 입력 형태)."""
        def measured_json(item: Measured) -> dict:
            return item.to_dict()

        if isinstance(profile, ArmCapabilityProfile):
            return {
                "arm_profile_id": profile.arm_profile_id,
                "arm_profile_version": profile.arm_profile_version,
                "display_name": profile.display_name,
                "joints": [
                    {
                        "name": j.name, "kind": j.kind.value,
                        "lower": measured_json(j.lower),
                        "upper": measured_json(j.upper),
                        "max_velocity": measured_json(j.max_velocity),
                        "max_acceleration": measured_json(j.max_acceleration),
                    }
                    for j in profile.joints
                ],
                "payload": measured_json(profile.payload),
                "reach": measured_json(profile.reach),
                "frames": {k.value: v for k, v in profile.frames.items()},
                "controller_requirement": profile.controller_requirement,
                "declared_skills": list(profile.declared_skills),
                "state_max_age": measured_json(profile.state_max_age),
                "connect_timeout": measured_json(profile.connect_timeout),
                "environment": profile.environment.value,
                "verified": profile.verified,
                "unverified_items": list(profile.unverified_items),
                "notes": profile.notes,
            }
        if isinstance(profile, GripperCapabilityProfile):
            payload = {
                "gripper_profile_id": profile.gripper_profile_id,
                "gripper_profile_version": profile.gripper_profile_version,
                "display_name": profile.display_name,
                "two_finger_parallel": profile.two_finger_parallel,
                "command_joint": profile.command_joint,
                "mimic_joints": dict(profile.mimic_joints),
                "command_interfaces": list(profile.command_interfaces),
                "state_interfaces": list(profile.state_interfaces),
                "object_detection_real": profile.object_detection_real,
                "object_detection_simulation": profile.object_detection_simulation,
                "activation_interfaces": list(profile.activation_interfaces),
                "fault_interfaces": list(profile.fault_interfaces),
                "base_frame": profile.base_frame,
                "environment": profile.environment.value,
                "verified": profile.verified,
                "unverified_items": list(profile.unverified_items),
                "notes": profile.notes,
            }
            for name in ("open_position", "closed_position", "joint_lower",
                         "joint_upper", "max_velocity", "max_effort",
                         "pad_aperture_open", "pad_aperture_closed",
                         "fully_open_margin"):
                payload[name] = measured_json(getattr(profile, name))
            payload["command_timeout"] = (
                None if profile.command_timeout is None
                else measured_json(profile.command_timeout)
            )
            return payload
        if isinstance(profile, MountingProfile):
            return {
                "mounting_profile_id": profile.mounting_profile_id,
                "mounting_profile_version": profile.mounting_profile_version,
                "arm_flange_frame": profile.arm_flange_frame,
                "gripper_base_frame": profile.gripper_base_frame,
                "xyz": [measured_json(v) for v in profile.xyz],
                "rpy": [measured_json(v) for v in profile.rpy],
                "adapter_plate_id": profile.adapter_plate_id,
                "tcp_frame": profile.tcp_frame,
                "tcp_xyz": (
                    None if profile.tcp_xyz is None
                    else [measured_json(v) for v in profile.tcp_xyz]
                ),
                "environment": profile.environment.value,
                "verified": profile.verified,
                "unverified_items": list(profile.unverified_items),
                "notes": profile.notes,
            }
        raise AssertionError(f"낯선 Profile: {type(profile)}")

    def test_arm_round_trip_keeps_values_and_provenance(self):
        restored = self.round_trip(arm(), load_arm_profile)
        self.assertEqual(restored.joint_names, arm().joint_names)
        self.assertEqual(restored.payload.value, 3.0)
        self.assertEqual(restored.payload.provenance.source_commit, "deadbeef")
        self.assertIs(restored.payload.status, VerificationStatus.DECLARED)

    def test_gripper_round_trip(self):
        restored = self.round_trip(gripper(), load_gripper_profile)
        self.assertEqual(restored.command_joint, "synth_drive_joint")
        self.assertEqual(restored.mimic_joints, {"synth_follow_joint": -1.0})
        self.assertTrue(restored.closes_by_increasing)
        self.assertTrue(restored.object_detection_real)
        self.assertFalse(restored.object_detection_simulation)

    def test_mounting_round_trip(self):
        restored = self.round_trip(mounting(), load_mounting_profile)
        self.assertEqual(restored.tcp_frame, "synth_tcp")
        self.assertEqual(restored.tcp_offset().z, 0.15)

    def test_unavailable_values_survive_the_round_trip(self):
        restored = self.round_trip(
            arm(payload=unavailable("kg", "데이터시트 미확보")), load_arm_profile
        )
        self.assertIsNone(restored.payload.value)
        self.assertEqual(restored.payload.unit, "kg")
        self.assertIn("미확보", restored.payload.provenance.note)
        # 이동 값은 그대로 있으므로 이동은 가능하고, 드는 스킬만 막힌다.
        self.assertTrue(restored.complete)
        self.assertEqual(restored.lift_missing, ("payload",))

    def test_unknown_field_is_refused(self):
        payload = self.to_json(arm())
        payload["unexpected"] = 1
        with self.assertRaises(BoundaryValidationError):
            load_arm_profile(payload)

    def test_wrong_type_is_refused_not_coerced(self):
        payload = self.to_json(arm())
        payload["payload"]["value"] = "3.0"
        with self.assertRaises(BoundaryValidationError):
            load_arm_profile(payload)

    def test_unknown_joint_kind_is_refused(self):
        payload = self.to_json(arm())
        payload["joints"][0]["kind"] = "screw"
        with self.assertRaises(BoundaryValidationError):
            load_arm_profile(payload)

    def test_composite_needs_registered_parts(self):
        payload = {
            "composite_profile_id": "c", "composite_profile_version": "1",
            "arm_profile": "missing_arm", "gripper_profile": None,
            "mounting_profile": None, "asset_manifest_version": "a",
            "environment": "simulation", "verified": False, "notes": "",
        }
        with self.assertRaises(BoundaryValidationError):
            load_composite_profile(payload, arms={}, grippers={}, mountings={})


if __name__ == "__main__":
    unittest.main(verbosity=2)
