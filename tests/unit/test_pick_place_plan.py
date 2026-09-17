"""pick/place 계획 검증 (md/개발플랜.md 8-10).

**합성 값으로 계약을 검증한다.** 실제 셀 수치를 만들지 않는다.

확인하는 것:

- 원자 계획(pick/place)을 12단계로 펼치고, 순서가 선언된 순서와 같다
- 원자 스킬을 늘리지 않는다(단계는 검증용 표현이며 계약이 아니다)
- 자원 누락·발화 불일치·잘못된 자재/받침 조합이 **서로 다른 이유 코드**다
- 검증된 자세가 없으면 자세를 만들지 않고 막는다
- 그리퍼 명령값 근거가 없으면 막는다
- 적재 단계만 물체를 붙여 검사한다(그 밖의 단계에는 붙이지 않는다)
- 접촉 허용은 선언된 것만이다(패드·같은 물체·파지 단계의 받침면)
- 관절 제한 위반과 충돌이 각각 다른 이유 코드다
- 검사 중 scene이 바뀌면 통과시키지 않는다
- **계획 검증을 모두 통과해도 `execution_allowed`는 False다**
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.constants import ATOMIC_SKILLS
from core.reason_codes import ReasonCode
from core.task_plan import TaskStep
from robots.moveit.scene import SceneSnapshot, StateValidity
from validation.pick_place_plan import (
    GRIPPER_STAGES,
    HOLDING_STAGES,
    STAGE_GRASP_APPROACH,
    STAGE_GRIPPER_CLOSE,
    STAGE_LIFT,
    STAGE_SEQUENCE,
    CellBindings,
    build_stages,
    classify_contacts,
    cross_check_resources,
    validate,
)

ARM = {"j1": 0.0, "j2": -1.0, "j3": 1.0, "j4": -1.0, "j5": -1.5, "j6": 0.0}


def bindings(**overrides) -> CellBindings:
    base = dict(
        resources={
            "loc_src": {"korean": "출발", "gazebo_model": "tray_1",
                        "frame": "tray_1_frame"},
            "loc_dst": {"korean": "도착", "gazebo_model": "belt",
                        "frame": "belt_frame"},
            "obj_x": {"korean": "X부품", "gazebo_model": "part_x",
                      "frame": "part_x_frame"},
            "obj_y": {"korean": "Y부품", "gazebo_model": "part_y",
                      "frame": "part_y_frame"},
        },
        object_support={"obj_x": "loc_src", "obj_y": "loc_dst"},
        approach_pose={"loc_src": "src_approach", "loc_dst": "dst_approach"},
        grasp_pose={"obj_x": "x_grasp"},
        place_pose={"loc_dst": "dst_place"},
        home_pose="home",
        poses={
            "home": dict(ARM),
            "src_approach": dict(ARM, j1=0.1),
            "x_grasp": dict(ARM, j1=0.2),
            "dst_approach": dict(ARM, j1=0.3),
            "dst_place": dict(ARM, j1=0.4),
        },
        gripper_joint="grip",
        gripper_open_rad=0.0,
        gripper_grasp_rad=0.35,
        gripper_mimic={"grip_mirror": (-1.0, 0.0)},
        pad_links=("pad_left", "pad_right"),
        attached={"obj_x": {
            "object_id": "part_x__held", "world_instance_id": "part_x",
            "link": "tcp", "size_m": [0.05, 0.05, 0.1],
            "offset_m": [0.0, 0.0, 0.03],
            "touch_links": ["pad_left", "pad_right"],
        }},
    )
    base.update(overrides)
    return CellBindings(**base)


class Slots:
    class Match:
        def __init__(self, resource_id, surface):
            self.resource_id = resource_id
            self.surface = surface

    def __init__(self, pairs):
        self.matches = [self.Match(rid, surface) for rid, surface in pairs]


FULL_SLOTS = Slots([("loc_src", "출발"), ("obj_x", "X부품"), ("loc_dst", "도착")])

PLAN = (
    TaskStep(skill="pick", args={"object": "obj_x", "from": "loc_src"}),
    TaskStep(skill="move", args={"to": "loc_dst"}),
    TaskStep(skill="place", args={"object": "obj_x", "to": "loc_dst"}),
    TaskStep(skill="home"),
)


def snapshot(content_hash: str = "hash-a") -> SceneSnapshot:
    return SceneSnapshot(
        snapshot_id="scene-1", snapshot_version="7", content_hash=content_hash,
        frame_id="base", captured_at=1000.0, ttl_sec=5.0, source="test",
        summary={"world_object_count": 5},
    )


class FakeSceneClient:
    """계약만 만족하는 대역. 호출 기록을 남긴다."""

    def __init__(self, *, contacts=(), out_of_bounds=(), hashes=("hash-a",)):
        self._contacts = contacts
        self._out_of_bounds = out_of_bounds
        self._hashes = list(hashes)
        self.calls: list[dict] = []
        self.snapshots = 0

    def snapshot(self) -> SceneSnapshot:
        index = min(self.snapshots, len(self._hashes) - 1)
        self.snapshots += 1
        return snapshot(self._hashes[index])

    def check_state(self, joints, *, attached=()):
        self.calls.append({"joints": dict(joints),
                           "attached": [item.object_id for item in attached]})
        contacts = self._contacts(joints, attached) if callable(self._contacts) \
            else self._contacts
        oob = tuple(self._out_of_bounds)
        return StateValidity(valid=not contacts and not oob,
                             contacts=tuple(contacts), out_of_bounds=oob)


class TestStageExpansion(unittest.TestCase):
    def test_twelve_stages_in_declared_order(self):
        stages, findings = build_stages(PLAN, bindings=bindings())
        self.assertEqual(findings, ())
        self.assertEqual(tuple(s.stage for s in stages), STAGE_SEQUENCE)
        self.assertEqual(len(stages), 12)
        self.assertEqual([s.no for s in stages], list(range(1, 13)))

    def test_stages_are_not_new_atomic_skills(self):
        """단계 이름이 계약의 원자 스킬에 섞여 들어가지 않는다."""
        for name in STAGE_SEQUENCE:
            self.assertNotIn(name, ATOMIC_SKILLS)

    def test_stage_poses_come_from_verified_table_only(self):
        cell = bindings()
        stages, _ = build_stages(PLAN, bindings=cell)
        for stage in stages:
            self.assertIn(stage.pose_name, cell.poses)
            self.assertEqual(stage.joint_rad, cell.poses[stage.pose_name])

    def test_gripper_stages_keep_the_arm_in_place(self):
        stages, _ = build_stages(PLAN, bindings=bindings())
        by_stage = {s.stage: s for s in stages}
        for name in GRIPPER_STAGES:
            self.assertEqual(by_stage[name].kind, "gripper")
        # 그리퍼 닫기는 pick 접근 자세 그대로다.
        self.assertEqual(by_stage[STAGE_GRIPPER_CLOSE].pose_name,
                         by_stage[STAGE_GRASP_APPROACH].pose_name)

    def test_holding_stages_declare_the_object(self):
        stages, _ = build_stages(PLAN, bindings=bindings())
        held = {s.stage for s in stages if s.holds_object}
        self.assertEqual(held, set(HOLDING_STAGES))

    def test_lift_reuses_the_verified_approach_pose(self):
        stages, _ = build_stages(PLAN, bindings=bindings())
        by_stage = {s.stage: s for s in stages}
        self.assertEqual(by_stage[STAGE_LIFT].pose_name, "src_approach")

    def test_missing_grasp_pose_blocks_without_inventing_one(self):
        stages, findings = build_stages(
            PLAN, bindings=bindings(grasp_pose={}))
        self.assertEqual(stages, ())
        codes = {f.reason_code for f in findings}
        self.assertIn(ReasonCode.GEOMETRY_WORKSPACE_VIOLATION, codes)

    def test_missing_gripper_command_blocks(self):
        stages, findings = build_stages(
            PLAN, bindings=bindings(gripper_grasp_rad=None))
        self.assertEqual(stages, ())
        self.assertEqual({f.reason_code for f in findings},
                         {ReasonCode.CONFIG_MISSING})

    def test_plan_without_place_cannot_be_expanded(self):
        stages, findings = build_stages(
            (TaskStep(skill="pick", args={"object": "obj_x", "from": "loc_src"}),),
            bindings=bindings())
        self.assertEqual(stages, ())
        self.assertEqual({f.reason_code for f in findings},
                         {ReasonCode.PLAN_SLOT_INCOMPLETE})

    def test_source_step_points_back_to_the_atomic_plan(self):
        stages, _ = build_stages(PLAN, bindings=bindings())
        by_stage = {s.stage: s for s in stages}
        self.assertEqual(by_stage[STAGE_GRASP_APPROACH].source_step, 1)
        self.assertEqual(by_stage["place_descend"].source_step, 3)


class TestResourceCrossCheck(unittest.TestCase):
    def test_matched_rows_carry_model_and_frame(self):
        rows, findings = cross_check_resources(
            PLAN, slots=FULL_SLOTS, bindings=bindings())
        self.assertEqual(findings, ())
        by_id = {(r.resource_id, r.role): r for r in rows}
        self.assertEqual(by_id[("obj_x", "object")].scene_model, "part_x")
        self.assertEqual(by_id[("loc_src", "source")].frame, "tray_1_frame")
        self.assertTrue(all(r.matched for r in rows))

    def test_unknown_resource_gets_its_own_code(self):
        plan = (
            TaskStep(skill="pick", args={"object": "obj_z", "from": "loc_src"}),
            TaskStep(skill="place", args={"object": "obj_z", "to": "loc_dst"}),
        )
        _, findings = cross_check_resources(
            plan, slots=Slots([("loc_src", "출발"), ("loc_dst", "도착")]),
            bindings=bindings())
        self.assertIn(ReasonCode.PLAN_UNKNOWN_RESOURCE,
                      {f.reason_code for f in findings})

    def test_resource_absent_from_utterance_gets_mismatch_code(self):
        _, findings = cross_check_resources(
            PLAN, slots=Slots([("obj_x", "X부품")]), bindings=bindings())
        codes = {f.reason_code for f in findings}
        self.assertEqual(codes, {ReasonCode.PLAN_RESOURCE_MISMATCH})

    def test_wrong_object_support_combination(self):
        """선언된 받침과 다른 곳에서 집으려 하면 조합 불일치다."""
        plan = (
            TaskStep(skill="pick", args={"object": "obj_y", "from": "loc_src"}),
            TaskStep(skill="place", args={"object": "obj_y", "to": "loc_dst"}),
        )
        _, findings = cross_check_resources(
            plan, slots=Slots([("obj_y", "Y부품"), ("loc_src", "출발"),
                               ("loc_dst", "도착")]),
            bindings=bindings())
        support = [f for f in findings if f.key.startswith("support_mismatch")]
        self.assertEqual(len(support), 1)
        self.assertIs(support[0].reason_code, ReasonCode.PLAN_RESOURCE_MISMATCH)
        self.assertIn("loc_dst", support[0].detail)

    def test_pick_and_place_must_name_the_same_object(self):
        plan = (
            TaskStep(skill="pick", args={"object": "obj_x", "from": "loc_src"}),
            TaskStep(skill="place", args={"object": "obj_y", "to": "loc_dst"}),
        )
        _, findings = build_stages(plan, bindings=bindings())
        self.assertIn(ReasonCode.PLAN_RESOURCE_MISMATCH,
                      {f.reason_code for f in findings})


class TestContactClassification(unittest.TestCase):
    def test_pad_touching_the_target_object_is_declared(self):
        allowed, blocked = classify_contacts(
            [("pad_left", "part_x__held")],
            object_ids=["part_x__held"], pad_links=["pad_left", "pad_right"])
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, ())

    def test_same_object_world_instance_overlap_is_declared(self):
        allowed, blocked = classify_contacts(
            [("part_x__held", "part_x")],
            object_ids=["part_x__held", "part_x"], pad_links=["pad_left"])
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, ())

    def test_support_contact_only_when_declared(self):
        contacts = [("part_x__held", "tray_1__top")]
        allowed, blocked = classify_contacts(
            contacts, object_ids=["part_x__held"], pad_links=["pad_left"])
        self.assertEqual(blocked, (("part_x__held", "tray_1__top"),))
        allowed, blocked = classify_contacts(
            contacts, object_ids=["part_x__held"], pad_links=["pad_left"],
            support_ids=["tray_1__top"])
        self.assertEqual(len(allowed), 1)
        self.assertEqual(blocked, ())

    def test_body_link_touching_the_object_is_a_collision(self):
        _, blocked = classify_contacts(
            [("gripper_body", "part_x")],
            object_ids=["part_x"], pad_links=["pad_left", "pad_right"])
        self.assertEqual(blocked, (("gripper_body", "part_x"),))

    def test_other_object_contact_is_a_collision(self):
        _, blocked = classify_contacts(
            [("part_x__held", "part_y")],
            object_ids=["part_x__held", "part_x"], pad_links=["pad_left"])
        self.assertEqual(blocked, (("part_x__held", "part_y"),))


class TestStageChecks(unittest.TestCase):
    def test_all_stages_pass_but_execution_is_still_not_allowed(self):
        client = FakeSceneClient()
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertTrue(result.plan_verified)
        # **여기가 핵심이다.** 계획 검증이 전부 통과해도 실행 허가가 아니다.
        self.assertFalse(result.execution_allowed)
        self.assertFalse(result.to_dict()["execution_allowed"])
        self.assertEqual(result.to_dict()["checks_passed"], 12)

    def test_object_is_attached_only_on_holding_stages(self):
        client = FakeSceneClient()
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertEqual(len(client.calls), 12)
        attached_stages = [
            stage.stage for stage, call in zip(result.stages, client.calls)
            if call["attached"]
        ]
        self.assertEqual(set(attached_stages), set(HOLDING_STAGES))
        for call in client.calls:
            if call["attached"]:
                self.assertEqual(call["attached"], ["part_x__held"])

    def test_gripper_mimic_joints_are_filled_from_the_declaration(self):
        client = FakeSceneClient()
        validate(PLAN, slots=FULL_SLOTS, bindings=bindings(), client=client)
        closing = [c for c in client.calls if c["joints"]["grip"] == 0.35]
        self.assertTrue(closing)
        for call in closing:
            self.assertAlmostEqual(call["joints"]["grip_mirror"], -0.35)

    def test_joint_limit_violation_is_a_workspace_violation(self):
        client = FakeSceneClient(out_of_bounds=("j6",))
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertFalse(result.plan_verified)
        self.assertIn(ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                      result.reason_codes)

    def test_undeclared_contact_is_a_collision(self):
        client = FakeSceneClient(contacts=(("gripper_body", "belt"),))
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertFalse(result.plan_verified)
        self.assertIn(ReasonCode.GEOMETRY_COLLISION, result.reason_codes)

    def test_scene_change_during_check_is_not_a_pass(self):
        client = FakeSceneClient(hashes=("hash-a", "hash-b"))
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertFalse(result.scene_stable)
        self.assertFalse(result.plan_verified)
        self.assertIn(ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED, result.reason_codes)

    def test_missing_scene_client_is_not_a_pass(self):
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=None)
        self.assertFalse(result.plan_verified)
        self.assertIn(ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
                      result.reason_codes)
        self.assertEqual(result.checks, ())

    def test_missing_attach_declaration_leaves_holding_stages_unchecked(self):
        client = FakeSceneClient()
        result = validate(PLAN, slots=FULL_SLOTS,
                          bindings=bindings(attached={}), client=client)
        self.assertFalse(result.plan_verified)
        self.assertIn(ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                      result.reason_codes)
        unchecked = [c.stage for c in result.checks if not c.checked]
        self.assertEqual(set(unchecked), set(HOLDING_STAGES))

    def test_snapshot_is_recorded_with_the_verdict(self):
        client = FakeSceneClient()
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertEqual(result.snapshot["snapshot_id"], "scene-1")
        self.assertEqual(result.snapshot["content_hash"], "hash-a")
        self.assertEqual(result.snapshot["captured_at"], 1000.0)

    def test_grasp_observation_is_carried_into_the_result(self):
        client = FakeSceneClient()
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client,
                          grasp_observation={"availability": "unavailable",
                                             "held": None})
        self.assertEqual(result.grasp_observation["availability"], "unavailable")
        self.assertIsNone(result.grasp_observation["held"])

    def test_declared_support_contact_passes_only_at_the_grasp_stages(self):
        def contacts(joints, attached):
            # 파지 자세(j1=0.2)에서만 받침면 접촉이 있다고 가정한다.
            if abs(joints["j1"] - 0.2) < 1e-9:
                return (("part_x__held", "tray_1__top"),) if attached else ()
            return ()

        client = FakeSceneClient(contacts=contacts)
        result = validate(PLAN, slots=FULL_SLOTS, bindings=bindings(),
                          client=client)
        self.assertTrue(result.plan_verified, result.to_dict()["findings"])
        close = next(c for c in result.checks if c.stage == STAGE_GRIPPER_CLOSE)
        self.assertEqual(len(close.expected_contacts), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
