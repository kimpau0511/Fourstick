"""planning scene ↔ 실제 자재 위치 동기화 · 단계 사이 경로 표본 검사."""

from __future__ import annotations

import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.pick_place_plan import (  # noqa: E402
    PATH_STEP_RAD,
    PickPlaceStage,
    check_path,
    classify_contacts,
    path_samples,
)
from validation.scene_sync import (  # noqa: E402
    SYNC_RECORD_TOLERANCE_M,
    plan_scene_sync,
    verify_scene_sync,
)

IDENTITY = (0.0, 0.0, 0.0, 1.0)
HOME = {"material_a": (0.5, 0.2, 0.84), "material_b": (0.5, 0.0, 0.84),
        "material_c": (0.5, -0.2, 0.84)}
SLOT = {"slot_1": (0.25, -0.5, 0.75), "slot_2": (0.15, -0.5, 0.75)}
PALLET = {"loc_pallet_3": (0.5, -0.2, 0.84)}


def expected_at(model, record):
    if not record:
        return HOME[model], "원래 자리"
    if record.get("state") == "held_on_target":
        return SLOT.get(record.get("slot")), record.get("slot")
    if record.get("state") == "on_pallet":
        return PALLET.get(record.get("pallet")), record.get("pallet")
    return None, str(record.get("state"))


def scene_at_home():
    return {m: p + IDENTITY for m, p in HOME.items()}


def sync(records, observed, scene=None, **kw):
    return plan_scene_sync(materials=list(HOME), records=records, observed=observed,
                           scene_poses=scene or scene_at_home(), expected_at=expected_at, **kw)


class SyncPlanTest(unittest.TestCase):
    def test_moved_materials_are_moved_in_the_scene(self):
        # "파란 자재를 초록 팔레트로" 뒤: C는 1번 칸, B는 3번 팔레트.
        observed = {"material_a": HOME["material_a"] + IDENTITY,
                    "material_b": (0.5001, -0.2002, 0.84) + IDENTITY,
                    "material_c": (0.2493, -0.4993, 0.75) + IDENTITY}
        plan = sync({"material_b": {"state": "on_pallet", "pallet": "loc_pallet_3"},
                     "material_c": {"state": "held_on_target", "slot": "slot_1"}}, observed)
        self.assertTrue(plan.ok, plan.findings)
        self.assertEqual(sorted(plan.moves), ["material_b", "material_c"])
        # scene에는 **관측 pose**를 넣는다(선언 중심이 아니다).
        self.assertEqual(plan.moves["material_c"][:3], (0.2493, -0.4993, 0.75))
        self.assertEqual(set(plan.desired), set(HOME))

    def test_observed_orientation_is_used(self):
        yawed = (0.0, 0.0, 0.0998, 0.995)
        plan = sync({}, {m: p + (yawed if m == "material_a" else IDENTITY)
                         for m, p in HOME.items()})
        self.assertEqual(list(plan.moves), ["material_a"])
        self.assertEqual(plan.moves["material_a"][3:], yawed)

    def test_record_observation_mismatch_blocks(self):
        observed = {m: p + IDENTITY for m, p in HOME.items()}   # C는 실제로 원래 자리
        plan = sync({"material_c": {"state": "held_on_target", "slot": "slot_1"}}, observed)
        self.assertFalse(plan.ok)
        self.assertIn("material_c", plan.findings[0])
        self.assertIn(str(SYNC_RECORD_TOLERANCE_M), plan.findings[0])

    def test_uncertain_record_or_missing_observation_blocks(self):
        observed = {m: p + IDENTITY for m, p in HOME.items()}
        plan = sync({"material_a": {"state": "stopped_unrestored"}}, observed)
        self.assertIn("확정하지 않는다", plan.findings[0])
        plan = sync({}, {**observed, "material_b": None})
        self.assertIn("관측하지 못했다", plan.findings[0])
        scene = scene_at_home()
        del scene["material_c"]
        self.assertIn("scene에 자재 상자가 없다", sync({}, observed, scene).findings[0])

    def test_excluded_held_material_is_left_alone(self):
        observed = {m: p + IDENTITY for m, p in HOME.items() if m != "material_a"}
        plan = sync({"material_a": {"state": "stopped_unrestored"}}, observed,
                    exclude=("material_a",))
        self.assertTrue(plan.ok, plan.findings)
        self.assertNotIn("material_a", plan.desired)

    def test_readback_must_match(self):
        desired = {"material_b": (0.5, -0.2, 0.84) + IDENTITY}
        self.assertEqual(verify_scene_sync(desired, {"material_b": desired["material_b"]}), [])
        self.assertTrue(verify_scene_sync(desired, scene_at_home()))
        self.assertTrue(verify_scene_sync(desired, {}))


def stage(no, name, kind, joints, *, gripper_joint_rad=None, holds_object=None):
    return PickPlaceStage(no=no, stage=name, label=name, kind=kind, pose_name=name,
                          joint_rad=dict(joints), gripper_joint_rad=gripper_joint_rad,
                          holds_object=holds_object, resources=("mat_a",),
                          scene_models=(), frames=(), source_step=None)


class PathSampleTest(unittest.TestCase):
    def stages(self):
        a = {"j1": 0.0, "j2": 0.0}
        b = {"j1": 0.1, "j2": -0.05}
        return (stage(1, "home_start", "arm_motion", a),
                stage(2, "gripper_close", "gripper", b, gripper_joint_rad=0.5),
                stage(3, "lift", "arm_motion", b, holds_object="mat_a"))

    def test_interior_samples_follow_the_joint_space_line(self):
        samples = path_samples(self.stages(), step_rad=0.02)
        # 0.1 rad / 0.02 = 5구간 → 내부 4점. 그리퍼 단계는 팔을 움직이지 않는다.
        self.assertEqual(len(samples), 4)
        self.assertAlmostEqual(samples[0].joint_rad["j1"], 0.02)
        self.assertAlmostEqual(samples[0].joint_rad["j2"], -0.01)
        # 표본은 도착 단계의 규칙(든 물체 등)을 따른다.
        self.assertTrue(all(s.stage == "lift" and s.holds_object == "mat_a" for s in samples))
        self.assertTrue(all("경로" in s.label for s in samples))

    def test_first_segment_starts_at_the_observed_joints(self):
        samples = path_samples(self.stages(), start_joints={"j1": -0.1, "j2": 0.0,
                                                            "gripper": 0.3})
        # 관측 → home_start 0.1 rad(내부 4점) + home_start → lift 0.1 rad(내부 4점).
        self.assertEqual(PATH_STEP_RAD, 0.02)
        self.assertEqual(len(samples), 8)
        self.assertAlmostEqual(samples[0].joint_rad["j1"], -0.08)
        self.assertNotIn("gripper", samples[0].joint_rad)

    def test_path_collision_is_reported_with_path_key(self):
        class Client:
            def check_state(self, joints, attached=None):
                hit = 0.03 < joints["j1"] < 0.07
                return types.SimpleNamespace(
                    contacts=[("fr3_link7", "material_c")] if hit else [],
                    out_of_bounds=())

        bindings = types.SimpleNamespace(
            gripper_joint="g", gripper_mimic={}, attached={}, pad_links=(),
            object_support={}, scene_model=lambda rid: None)
        stages = tuple(replace(s, holds_object=None) for s in self.stages())
        checks, findings, count = check_path(stages, bindings=bindings, client=Client(),
                                             step_rad=0.01)
        self.assertEqual(count, 9)
        self.assertTrue(findings)
        self.assertTrue(all(f.key.startswith("path:") for f in findings))
        self.assertIn("material_c", findings[0].detail)
        self.assertIn("경로", findings[0].detail)


class ContactRuleTest(unittest.TestCase):
    def test_moved_material_copy_is_no_longer_an_exception(self):
        # 예전 실행 규칙 4(떠난 자리의 사본 허용)는 없다 — 접촉은 충돌이다.
        allowed, blocked = classify_contacts([("material_c", "material_b__held")],
                                             object_ids=("material_b__held", "material_b"))
        self.assertEqual(allowed, ())
        self.assertEqual(blocked, (("material_c", "material_b__held"),))
        with self.assertRaises(TypeError):
            classify_contacts([], absent_ids=("material_c",))


if __name__ == "__main__":
    unittest.main()
