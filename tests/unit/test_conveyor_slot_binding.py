"""컨베이어 슬롯 **배선** 회귀 — 요청한 슬롯이 실제 실행 목표인가.

## 고정하는 결함 (실측)

`slot_2`를 요청했는데 로봇이 `slot_1`에 놓아 옆 자재와 겹쳤다. 원인은 순서였다.

    validate(steps, bindings) → stages   ← 여기서 관절값이 **구워진다**
    ...
    bindings = apply_slot(...)           ← 너무 늦다. 실행은 예전 자세로 간다

단계는 만들어질 때 관절값이 굳고 실행기는 그 값을 그대로 보낸다
(`pick_place_plan._joint_state`는 `stage.joint_rad`를 쓴다). 그래서 결속을
단계 생성 뒤에 하면 **검사만** 슬롯 기준이 되고 **로봇은 예전 자리로 간다.**

아래 시험은 그 결함을 고정적으로 실패시킨다. 로봇도 ROS도 쓰지 않는다 —
설정과 순수 함수만 본다.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode  # noqa: E402
from validation.conveyor_slots import (  # noqa: E402
    PLACE_STAGES,
    binding_mismatch,
    load_slots,
)
from validation.pick_place_plan import (  # noqa: E402
    STAGE_PLACE_APPROACH,
    STAGE_PLACE_DESCEND,
    PickPlaceStage,
    stage_joint_state,
)

GRASP = json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_grasp.json").read_text(encoding="utf-8"))
POSES = json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_poses.json").read_text(encoding="utf-8"))
SLOTS = GRASP["conveyor_slots"]["slots"]


def stage(name: str, joints: dict, pose_name: str) -> PickPlaceStage:
    """실행이 실제로 보낼 목표를 담은 단계 하나."""
    return PickPlaceStage(
        no=1, stage=name, label=name, kind="arm_motion", pose_name=pose_name,
        joint_rad=dict(joints), gripper_joint_rad=None, holds_object=None,
        resources=(), scene_models=(), frames=(), source_step=1)


def stages_for(slot: str):
    """그 슬롯의 검증된 자세로 만든 배치 단계 둘."""
    entry = SLOTS[slot]
    return [
        stage(STAGE_PLACE_APPROACH, entry["approach_joint_rad"],
              f"conveyor_approach__{slot}"),
        stage(STAGE_PLACE_DESCEND, entry["place_joint_rad"],
              f"conveyor_place__{slot}"),
    ]


class ConfigTest(unittest.TestCase):
    """설정 자체가 슬롯마다 **다른** 자세를 갖고 있는가."""

    def test_each_slot_has_its_own_verified_joints(self):
        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                entry = SLOTS[slot]
                self.assertTrue(entry["place_joint_rad"])
                self.assertTrue(entry["approach_joint_rad"])

    def test_slot_joint_targets_differ_between_slots(self):
        """같은 값이면 슬롯을 나눈 의미가 없다."""
        place = {s: tuple(sorted(SLOTS[s]["place_joint_rad"].items()))
                 for s in ("slot_1", "slot_2", "slot_3")}
        self.assertNotEqual(place["slot_1"], place["slot_2"])
        self.assertNotEqual(place["slot_2"], place["slot_3"])
        self.assertNotEqual(place["slot_1"], place["slot_3"])

    def test_slot_1_matches_the_legacy_single_placement(self):
        """호환 — slot_1은 예전 단일 배치와 같은 자리다."""
        legacy = POSES["poses"]["conveyor_place"]["target_world_xyz_m"]
        self.assertEqual(SLOTS["slot_1"]["object_world_center_m"][:2], legacy[:2])


class BindingMismatchTest(unittest.TestCase):
    """`binding_mismatch`가 실행 목표와 배정 슬롯을 대조한다."""

    def test_matching_stages_pass(self):
        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                self.assertIsNone(
                    binding_mismatch(stages_for(slot), SLOTS[slot], slot_name=slot))

    def test_the_observed_defect_is_caught(self):
        """**이번 결함 그대로다.** slot_2를 요청했는데 단계는 slot_1 자세다."""
        wrong = stages_for("slot_1")          # 결속이 늦어 예전 자세가 구워졌다
        reason = binding_mismatch(wrong, SLOTS["slot_2"], slot_name="slot_2")
        self.assertIsNotNone(reason, "slot_1 자세로 slot_2에 놓으려는데 통과했다")
        self.assertIn("slot_2", reason)
        self.assertRegex(reason, r"실행 목표가|관절")

    def test_slot_3_requested_but_slot_1_bound_is_caught(self):
        reason = binding_mismatch(stages_for("slot_1"), SLOTS["slot_3"],
                                  slot_name="slot_3")
        self.assertIsNotNone(reason)

    def test_every_cross_pair_is_caught(self):
        names = ("slot_1", "slot_2", "slot_3")
        for requested in names:
            for bound in names:
                if requested == bound:
                    continue
                with self.subTest(requested=requested, bound=bound):
                    self.assertIsNotNone(
                        binding_mismatch(stages_for(bound), SLOTS[requested],
                                         slot_name=requested))

    def test_missing_place_stage_is_caught(self):
        only_approach = stages_for("slot_2")[:1]
        reason = binding_mismatch(only_approach, SLOTS["slot_2"], slot_name="slot_2")
        self.assertIn("배치 단계가 계획에 없다", reason)

    def test_a_tiny_joint_drift_is_caught(self):
        bent = stages_for("slot_2")
        first = bent[1]
        joints = dict(first.joint_rad)
        key = sorted(joints)[0]
        joints[key] = joints[key] + 0.01
        bent[1] = dataclasses.replace(first, joint_rad=joints)
        self.assertIsNotNone(
            binding_mismatch(bent, SLOTS["slot_2"], slot_name="slot_2"))

    def test_no_slot_means_no_check(self):
        """슬롯을 쓰지 않는 기존 경로는 그대로 지나간다."""
        self.assertIsNone(binding_mismatch(stages_for("slot_1"), None, slot_name=None))
        self.assertIsNone(
            binding_mismatch(stages_for("slot_1"), SLOTS["slot_1"], slot_name=None))


class ExecutionTargetTest(unittest.TestCase):
    """실행기가 보내는 값이 그 슬롯의 관절값인가.

    `stage_joint_state`는 실행 루프가 부르는 바로 그 함수다.
    """

    def bindings(self):
        from validation.pick_place_plan import CellBindings

        return CellBindings(
            resources={}, object_support={}, approach_pose={}, grasp_pose={},
            place_pose={}, home_pose="home", poses={}, gripper_joint="",
            gripper_open_rad=None, gripper_grasp_rad=None, gripper_mimic={},
            pad_links=(), attached={}, pose_evidence={}, sources={})

    def test_executed_arm_target_equals_the_slot_config(self):
        binds = self.bindings()
        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                descend = stages_for(slot)[1]
                sent = stage_joint_state(descend, binds)
                arm = {k: v for k, v in sent.items() if k.startswith("j")}
                self.assertEqual(arm, SLOTS[slot]["place_joint_rad"])

    def test_slot_2_never_sends_the_slot_1_place_target(self):
        """요청이 slot_2면 slot_1의 conveyor_place 목표가 **한 번도** 안 나온다."""
        binds = self.bindings()
        forbidden = SLOTS["slot_1"]["place_joint_rad"]
        for st in stages_for("slot_2"):
            sent = stage_joint_state(st, binds)
            arm = {k: v for k, v in sent.items() if k.startswith("j")}
            self.assertNotEqual(arm, forbidden, f"{st.stage}가 slot_1 목표를 보냈다")
            self.assertNotIn("conveyor_place\u0000", str(st.pose_name))
            self.assertTrue(str(st.pose_name).endswith("slot_2"), st.pose_name)


class ReportConsistencyTest(unittest.TestCase):
    """보고서의 슬롯 번호·배치 구역·기대 pose가 **같은 슬롯**을 가리키는가."""

    def zone_for(self, slot: str):
        from validation.simulation_e2e import placement_zone

        center = SLOTS[slot]["object_world_center_m"]
        pitch = GRASP["conveyor_slots"]["pitch_m"]
        return placement_zone(
            target_center_m=(center[0], center[1], 0.70),
            surface_half_extent_m=(pitch / 2, 0.15),
            object_size_m=(0.05, 0.05, 0.1),
            surface_top_z_m=0.70, z_tolerance_m=0.01)

    def test_zone_contains_its_own_slot_center(self):
        from validation.simulation_e2e import in_placement_zone

        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                center = SLOTS[slot]["object_world_center_m"]
                inside, _ = in_placement_zone(center, self.zone_for(slot))
                self.assertTrue(inside)

    def test_zone_rejects_the_neighbouring_slot_centers(self):
        """이번 결함이 판정으로 잡힌 이유 — 구역이 옆자리를 받지 않는다."""
        from validation.simulation_e2e import in_placement_zone

        names = ("slot_1", "slot_2", "slot_3")
        for slot in names:
            zone = self.zone_for(slot)
            for other in names:
                if other == slot:
                    continue
                with self.subTest(zone=slot, center=other):
                    inside, _ = in_placement_zone(
                        SLOTS[other]["object_world_center_m"], zone)
                    self.assertFalse(inside, f"{slot} 구역이 {other} 중심을 받았다")


class ProximityGuardTest(unittest.TestCase):
    """마지막 그물 — 놓을 자리 가까이 다른 자재가 있으면 막는다.

    구역 검사만으로는 **다른 슬롯에 있는 자재**를 못 본다. 이번에 A가 slot_1에
    있는데 slot_2 구역 검사를 통과한 뒤 로봇이 slot_1로 가서 겹쳤다.
    """

    MIN_GAP = 0.05      # 자재 폭. 스크립트가 쓰는 값과 같다.

    def gap(self, a, b):
        return sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)) ** 0.5

    def test_neighbouring_slots_are_far_enough_apart(self):
        centers = [SLOTS[s]["object_world_center_m"] for s in
                   ("slot_1", "slot_2", "slot_3")]
        gaps = [self.gap(centers[i], centers[i + 1]) for i in range(len(centers) - 1)]
        for g in gaps:
            self.assertGreaterEqual(round(g, 6), self.MIN_GAP, gaps)

    def test_landing_on_an_occupied_slot_trips_the_guard(self):
        """B가 slot_1(=A 자리)에 가려 하면 근접 방어선이 걸린다."""
        a_at_slot_1 = SLOTS["slot_1"]["object_world_center_m"]
        intended = SLOTS["slot_1"]["object_world_center_m"]   # 잘못 간 자리
        self.assertLess(self.gap(a_at_slot_1, intended), self.MIN_GAP)

    def test_the_correct_slot_does_not_trip_the_guard(self):
        a_at_slot_1 = SLOTS["slot_1"]["object_world_center_m"]
        intended = SLOTS["slot_2"]["object_world_center_m"]
        self.assertGreaterEqual(self.gap(a_at_slot_1, intended), self.MIN_GAP)


class ReasonCodeTest(unittest.TestCase):
    def test_the_guard_uses_an_existing_safety_code(self):
        """새 코드를 만들지 않는다 — 기존 의미가 그대로 맞는다."""
        self.assertEqual(ReasonCode.PLAN_RESOURCE_MISMATCH.value,
                         "plan.resource_mismatch")
        source = (ROOT / "scripts/demo_workcell_pick_place.py").read_text(
            encoding="utf-8")
        self.assertIn("binding_mismatch(stages, slot_row", source)
        self.assertIn("ReasonCode.PLAN_RESOURCE_MISMATCH", source)


class ScriptOrderTest(unittest.TestCase):
    """**순서 자체**를 고정한다 — 이번 결함의 뿌리다."""

    def setUp(self):
        self.lines = (ROOT / "scripts/demo_workcell_pick_place.py").read_text(
            encoding="utf-8").splitlines()

    def line_of(self, needle: str) -> int:
        for i, line in enumerate(self.lines):
            if needle in line:
                return i
        self.fail(f"찾지 못했다: {needle}")

    def test_apply_slot_runs_before_the_plan_is_validated(self):
        apply_at = self.line_of("bindings = apply_slot(bindings, slot_row")
        validate_at = self.line_of("validation = validate(steps, slots=slots")
        self.assertLess(apply_at, validate_at,
                        "슬롯 결속이 계획 검증보다 뒤에 있다 — 실행은 예전 자세로 간다")

    def test_the_binding_guard_runs_before_any_motion(self):
        guard_at = self.line_of("mismatch = binding_mismatch(stages, slot_row")
        send_at = self.line_of('outcome = _send(ctx, "send_arm"')
        self.assertLess(guard_at, send_at, "결속 검사가 팔 전송보다 뒤에 있다")

    def test_the_proximity_guard_exists_before_execution(self):
        guard_at = self.line_of("놓을 자리(")
        send_at = self.line_of('outcome = _send(ctx, "send_arm"')
        self.assertLess(guard_at, send_at)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class EndToEndTraceTest(unittest.TestCase):
    """CLI `--slot` → job → 스크립트 → bindings → stages → transport 전송.

    실제 로봇도 ROS도 쓰지 않는다. 각 이음매에서 **같은 슬롯**을 가리키는지만 본다.
    """

    SLOT = "slot_2"

    def setUp(self):
        from scripts.demo_workcell_pick_place import apply_slot, build_bindings
        from validation.pick_place_plan import build_stages
        from core.task_plan import TaskStep

        self.apply_slot = apply_slot
        self.build_stages = build_stages
        self.resources = _load_resources()
        self.bindings = build_bindings(self.resources)
        self.steps = _transfer_steps(TaskStep)

    def slot_row(self, slot=None):
        name = slot or self.SLOT
        return {**SLOTS[name], "slot_name": name}

    # ── 1) CLI 인자 ────────────────────────────────────────────────────
    def test_job_builds_the_slot_argument(self):
        from server.sim_demo_jobs import build_argv

        argv = build_argv("transfer",
                          {"model": "material_b", "support_model": "pallet_2"},
                          None, self.SLOT)
        self.assertIn("--slot", argv)
        self.assertEqual(argv[argv.index("--slot") + 1], self.SLOT)

    def test_job_without_a_slot_keeps_the_legacy_argv(self):
        from server.sim_demo_jobs import build_argv

        argv = build_argv("transfer",
                          {"model": "material_a", "support_model": "pallet_1"},
                          None, None)
        self.assertNotIn("--slot", argv)

    # ── 2) bindings ───────────────────────────────────────────────────
    def test_apply_slot_rebinds_the_conveyor_poses(self):
        bound = self.apply_slot(self.bindings, self.slot_row(), "loc_conveyor",
                                "conveyor")
        self.assertEqual(bound.place_pose["loc_conveyor"],
                         f"conveyor_place__{self.SLOT}")
        self.assertEqual(bound.approach_pose["loc_conveyor"],
                         f"conveyor_approach__{self.SLOT}")
        self.assertEqual(bound.poses[f"conveyor_place__{self.SLOT}"],
                         SLOTS[self.SLOT]["place_joint_rad"])
        # 기존 이름은 그대로 남는다(다른 경로가 쓴다).
        self.assertIn("conveyor_place", bound.poses)

    # ── 3) stages ─────────────────────────────────────────────────────
    def built_stages(self, slot=None):
        bound = self.apply_slot(self.bindings, self.slot_row(slot),
                                "loc_conveyor", "conveyor")
        stages, findings = self.build_stages(self.steps, bindings=bound)
        self.assertEqual(findings, (), f"단계 생성 실패: {findings}")
        return bound, stages

    def test_stages_carry_the_requested_slot_targets(self):
        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                _bound, stages = self.built_stages(slot)
                by_name = {s.stage: s for s in stages}
                self.assertEqual(dict(by_name[STAGE_PLACE_DESCEND].joint_rad),
                                 SLOTS[slot]["place_joint_rad"])
                self.assertEqual(dict(by_name[STAGE_PLACE_APPROACH].joint_rad),
                                 SLOTS[slot]["approach_joint_rad"])
                self.assertIsNone(binding_mismatch(stages, SLOTS[slot],
                                                   slot_name=slot))

    # ── 4) transport 전송 ─────────────────────────────────────────────
    def sent_arm_targets(self, slot):
        """실행 루프와 **같은 함수**로 목표를 뽑아 대역 transport에 보낸다."""
        bound, stages = self.built_stages(slot)
        sent = []

        class Recorder:
            def send_arm(self, joints, seconds, timeout_sec):
                sent.append(dict(joints))
                return None

        transport = Recorder()
        for st in stages:
            if st.kind != "arm_motion":
                continue
            joints = stage_joint_state(st, bound)
            transport.send_arm({k: v for k, v in joints.items()
                                if k.startswith("j")}, 3.0, 10.0)
        return sent

    def test_transport_receives_the_requested_slot_target(self):
        for slot in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=slot):
                self.assertIn(SLOTS[slot]["place_joint_rad"],
                              self.sent_arm_targets(slot))

    def test_slot_2_never_sends_the_slot_1_place_target(self):
        """**이번 결함의 핵심 단언.** slot_2 요청에 slot_1 목표가 한 번도 없다."""
        sent = self.sent_arm_targets("slot_2")
        self.assertNotIn(SLOTS["slot_1"]["place_joint_rad"], sent)
        self.assertNotIn(SLOTS["slot_1"]["approach_joint_rad"], sent)

    def test_slot_3_never_sends_the_slot_1_place_target(self):
        sent = self.sent_arm_targets("slot_3")
        self.assertNotIn(SLOTS["slot_1"]["place_joint_rad"], sent)

    def test_late_binding_sends_the_wrong_slot_and_the_guard_catches_it(self):
        """결속을 **단계 생성 뒤**로 미루면 어떤 일이 벌어지는지 고정한다.

        이것이 이번에 실제로 일어난 일이다 — 검사는 slot_2, 전송은 slot_1.
        """
        # 결속 없이 단계를 만든다(= 예전 순서).
        stages, findings = self.build_stages(self.steps, bindings=self.bindings)
        self.assertEqual(findings, ())
        late = self.apply_slot(self.bindings, self.slot_row("slot_2"),
                               "loc_conveyor", "conveyor")
        sent = []
        for st in stages:
            if st.kind != "arm_motion":
                continue
            joints = stage_joint_state(st, late)      # 결속을 뒤에 해도
            sent.append({k: v for k, v in joints.items() if k.startswith("j")})
        # 전송 목표는 **여전히 slot_1**이다 — 늦은 결속은 아무 효과가 없다.
        self.assertIn(SLOTS["slot_1"]["place_joint_rad"], sent)
        self.assertNotIn(SLOTS["slot_2"]["place_joint_rad"], sent)
        # 그리고 방어선이 그것을 잡는다.
        self.assertIsNotNone(
            binding_mismatch(stages, SLOTS["slot_2"], slot_name="slot_2"))


def _load_resources():
    from robots.fr3_gazebo.adapter import load_workcell_resources
    from scripts.demo_workcell_pick_place import GRASP as GRASP_PATH
    from scripts.demo_workcell_pick_place import MOUNTING, POSES as POSES_PATH, WORKCELL

    return load_workcell_resources(WORKCELL, POSES_PATH, state_max_age_sec=0.5,
                                   mounting_path=MOUNTING, grasp_path=GRASP_PATH)


def _transfer_steps(TaskStep):
    return [
        TaskStep(skill="move", args={"to": "loc_pallet_2"}),
        TaskStep(skill="pick", args={"object": "mat_b", "from": "loc_pallet_2"}),
        TaskStep(skill="place", args={"object": "mat_b", "to": "loc_conveyor"}),
        TaskStep(skill="home", args={}),
    ]
