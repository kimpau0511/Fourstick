"""시뮬레이션 시연 자재의 원래 슬롯 복귀(`--return-held-to-origin`) 단위 검증.

- 원래 슬롯은 셀 설정의 프레임 부모 관계에서만 나온다
- 복귀 전 네 조건(기록 · 컨베이어 배치 · 슬롯 비점유 · geometry/scene)
- 관측으로 슬롯 복귀를 확인했을 때만 기록을 지운다
- STOP·실패·관측 불가는 기록을 남기고 reset_required=true 유지
- 컨베이어 위 파지 자세는 측정 결과(`conveyor_grasp_poses`)에서만 오고,
  없으면 `geometry.grasp_pose_unavailable`로 막는다

**ROS·Gazebo 없이** 돈다. planning scene은 기록용 대역이다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode  # noqa: E402
from robots.fr3_gazebo.adapter import load_workcell_resources  # noqa: E402
from validation.pick_place_plan import (  # noqa: E402
    HOLDING_STAGES,
    STAGE_SEQUENCE,
    check_stages,
)
from validation.simulation_demo_state import (  # noqa: E402
    CONVEYOR_GRASP_KEY,
    PALLET_PLACE_KEY,
    ORIGIN_TOLERANCE_M,
    POLICY_DEMO_HOLD,
    RETURN_FAILED,
    RETURN_STOPPED,
    SimulationDemoState,
    build_return_stages,
    conveyor_grasp_center,
    conveyor_grasp_pose,
    origin_slot,
    pallet_place_pose,
    return_bindings,
    return_preflight,
    slot_occupant,
)
from validation.simulation_e2e import (  # noqa: E402
    CRITERIA,
    Criterion,
    SimulationTransferResult,
    in_placement_zone,
    placement_zone,
)

_spec = importlib.util.spec_from_file_location(
    "demo_workcell_pick_place_for_return",
    ROOT / "scripts/demo_workcell_pick_place.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

WORKCELL = json.loads(demo.WORKCELL.read_text(encoding="utf-8"))
GRASP = json.loads(demo.GRASP.read_text(encoding="utf-8"))
#: 컨베이어에 안착한 material_a의 실측 pose(2026-09-18 hold 시연).
ON_CONVEYOR = (0.249262, -0.499764, 0.749999)
#: `derive_grasp_poses.py --conveyor`가 측정·등록한 자세 이름.
CONVEYOR_GRASP = "material_a_conveyor_grasp"


def bindings():
    resources = load_workcell_resources(
        demo.WORKCELL, demo.POSES, state_max_age_sec=0.5,
        mounting_path=demo.MOUNTING, grasp_path=demo.GRASP)
    return demo.build_bindings(resources)


def grasp_without_conveyor_poses():
    config = json.loads(json.dumps(GRASP))
    config.pop(CONVEYOR_GRASP_KEY, None)
    return config


def conveyor_zone():
    conveyor = WORKCELL["models"]["conveyor"]
    belt = next(p for p in conveyor["parts"] if p["name"] == "belt")
    rails = [p for p in conveyor["parts"] if p["name"].startswith("frame_")]
    center = demo._resolve(WORKCELL["frames"], conveyor["frame"])
    rail_inner = min(abs(p["center_xyz_m"][1]) - p["size_m"][1] / 2 for p in rails)
    return placement_zone(
        target_center_m=center,
        surface_half_extent_m=(belt["size_m"][0] / 2,
                               min(belt["size_m"][1] / 2, rail_inner)),
        object_size_m=(0.05, 0.05, 0.1), surface_top_z_m=center[2],
        z_tolerance_m=demo.PLACEMENT_Z_TOLERANCE_M)


class CleanScene:
    """접촉 없는 planning scene 대역. hash는 고정이다."""

    def __init__(self, contacts=()):
        self.contacts = [list(pair) for pair in contacts]
        self.calls = 0

    def check_state(self, joints, attached=None):
        self.calls += 1
        return types.SimpleNamespace(contacts=self.contacts, out_of_bounds=())

    def snapshot(self):
        return types.SimpleNamespace(content_hash="h" * 64)


def held_transfer():
    return SimulationTransferResult(
        scenario="loc_pallet_1/mat_a->loc_conveyor", object_id="mat_a",
        support_id="loc_pallet_1", target_id="loc_conveyor",
        criteria=tuple(Criterion(key=k, label=v, met=True, detail="관측")
                       for k, v in CRITERIA)).to_dict()


class OriginSlotTest(unittest.TestCase):
    def test_slots_come_from_declared_frame_parents(self):
        expected = {"material_a": ("pallet_1", "loc_pallet_1", (0.5, 0.2)),
                    "material_b": ("pallet_2", "loc_pallet_2", (0.5, 0.0)),
                    "material_c": ("pallet_3", "loc_pallet_3", (0.5, -0.2))}
        for model, (pallet, rid, xy) in expected.items():
            with self.subTest(model=model):
                slot = origin_slot(WORKCELL, model)
                self.assertEqual((slot.support_model, slot.support_id),
                                 (pallet, rid))
                self.assertAlmostEqual(slot.home_pose_m[0], xy[0])
                self.assertAlmostEqual(slot.home_pose_m[1], xy[1])
                self.assertAlmostEqual(slot.home_pose_m[2], 0.84)
                # 선언된 받침(grasp 측정 파일)과 같은 팔레트다.
                self.assertEqual(GRASP["object_support"][slot.object_id], rid)

    def test_non_material_is_rejected(self):
        for model in ("conveyor", "pallet_1", "nope"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                origin_slot(WORKCELL, model)


class ConveyorGraspPoseTest(unittest.TestCase):
    """측정 결과 파일에 등록된 컨베이어 파지 자세."""

    def test_each_material_has_a_measured_conveyor_grasp(self):
        entries = GRASP[CONVEYOR_GRASP_KEY]
        for rid, model in (("mat_a", "material_a"), ("mat_b", "material_b"),
                           ("mat_c", "material_c")):
            with self.subTest(model=model):
                name = conveyor_grasp_pose(GRASP, object_id=rid,
                                           target_id="loc_conveyor")
                self.assertEqual(name, f"{model}_conveyor_grasp")
                pose = entries[name]
                self.assertEqual(pose["status"], "verified")
                self.assertEqual(pose["support_resource_id"], "loc_conveyor")
                self.assertEqual(pose["source"],
                                 "scripts/derive_grasp_poses.py --conveyor")
                self.assertEqual(pose["sweep_step_m"], 0.005)
                # 선택값은 측정한 유효 구간의 최저점이다.
                self.assertEqual(pose["target_offset_m"][2],
                                 pose["usable_dz_range_m"][0])
                self.assertTrue(all(c["clean"] for c in pose["path_checks"].values()))
                self.assertTrue(pose["expected_contacts"])
                for a, b in pose["expected_contacts"]:
                    self.assertIn(a, GRASP["declared"]["pad_links"])
                    self.assertTrue(b.startswith(model))
                self.assertLessEqual(pose["position_error_m"], 0.002)
                # 측정 위치는 선언된 벨트 상면 중심 + 자재 높이 절반이다.
                top = demo._resolve(WORKCELL["frames"], "conveyor_frame")
                height = WORKCELL["models"][model]["size_m"][2]
                self.assertEqual(pose["object_world_center_m"],
                                 [round(top[0], 6), round(top[1], 6),
                                  round(top[2] + height / 2, 6)])

    def test_pallet_grasp_mapping_is_unchanged(self):
        """컨베이어 자세가 로더의 자재별 팔레트 파지 매핑을 덮어쓰지 않는다."""
        mapping = bindings().grasp_pose
        self.assertEqual(mapping, {"mat_a": "material_a_grasp",
                                   "mat_b": "material_b_grasp",
                                   "mat_c": "material_c_grasp"})
        for name in GRASP[CONVEYOR_GRASP_KEY]:
            self.assertNotIn(name, GRASP["poses"])

    def test_missing_conveyor_grasp_blocks_with_its_own_code(self):
        """놓기 자세로 대신하지 않는다 — 없으면 새 이유 코드로 막는다."""
        config = grasp_without_conveyor_poses()
        self.assertIsNone(conveyor_grasp_pose(config, object_id="mat_a",
                                              target_id="loc_conveyor"))
        base = return_bindings(bindings(), object_id="mat_a",
                               target_id="loc_conveyor", grasp_config=config)
        stages, findings = build_return_stages(
            base, object_id="mat_a", origin_id="loc_pallet_1",
            target_id="loc_conveyor", conveyor_grasp=None)
        self.assertEqual(stages, ())
        self.assertEqual([code for code, _ in findings],
                         [ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE])
        self.assertEqual(str(ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE),
                         "geometry.grasp_pose_unavailable")
        self.assertNotIn(ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                         [code for code, _ in findings])


class PalletPlacePoseTest(unittest.TestCase):
    """A/C 원래 슬롯 놓기 자세(`derive_grasp_poses.py --pallet-place`)."""

    def test_a_and_c_have_measured_place_poses_b_does_not(self):
        entries = GRASP[PALLET_PLACE_KEY]
        for rid, model, pallet in (("mat_a", "material_a", "loc_pallet_1"),
                                   ("mat_c", "material_c", "loc_pallet_3")):
            with self.subTest(model=model):
                name = pallet_place_pose(GRASP, object_id=rid, support_id=pallet)
                self.assertEqual(name, f"{model}_pallet_place")
                pose = entries[name]
                self.assertEqual(pose["status"], "verified")
                self.assertEqual(pose["kind"], "place")
                self.assertEqual(pose["source"],
                                 "scripts/derive_grasp_poses.py --pallet-place")
                self.assertEqual(pose["sweep_step_m"], 0.005)
                self.assertEqual(pose["target_offset_m"][2],
                                 pose["usable_dz_range_m"][0])
                self.assertTrue(all(c["clean"] for c in pose["path_checks"].values()))
                release = pose["release"]
                self.assertTrue(release["settles_in_slot"])
                self.assertGreater(release["drop_to_tray_m"], 0.0)
                self.assertLessEqual(release["xy_gap_m"], ORIGIN_TOLERANCE_M)
                # 든 자재 선언은 복귀가 실제로 쓰는 컨베이어 파지에서 왔다.
                self.assertEqual(pose["held_object_source"],
                                 f"conveyor_grasp_poses.{model}_conveyor_grasp")
                # 파지 자세보다 높다(트레이 접촉 회피) — 파지 자세를 복사하지 않았다.
                grasp_dz = GRASP["poses"][f"{model}_grasp"]["target_offset_m"][2]
                self.assertGreater(pose["target_offset_m"][2], grasp_dz)
                self.assertNotEqual(pose["joint_rad"],
                                    GRASP["poses"][f"{model}_grasp"]["joint_rad"])
        self.assertIsNone(pallet_place_pose(GRASP, object_id="mat_b",
                                            support_id="loc_pallet_2"))

    def test_a_c_use_pallet_place_b_keeps_grasp_pose(self):
        for rid, model, pallet, expected in (
                ("mat_a", "material_a", "loc_pallet_1", "material_a_pallet_place"),
                ("mat_b", "material_b", "loc_pallet_2", "material_b_grasp"),
                ("mat_c", "material_c", "loc_pallet_3", "material_c_pallet_place")):
            with self.subTest(model=model):
                base = return_bindings(bindings(), object_id=rid,
                                       target_id="loc_conveyor", grasp_config=GRASP,
                                       origin_id=pallet)
                stages, findings = build_return_stages(
                    base, object_id=rid, origin_id=pallet, target_id="loc_conveyor",
                    conveyor_grasp=conveyor_grasp_pose(
                        GRASP, object_id=rid, target_id="loc_conveyor"),
                    origin_place=pallet_place_pose(GRASP, object_id=rid,
                                                   support_id=pallet))
                self.assertEqual(findings, [])
                poses = {s.stage: s.pose_name for s in stages}
                self.assertEqual(poses["place_descend"], expected)
                self.assertEqual(poses["gripper_open_release"], expected)
                self.assertIn(expected, base.poses)

    def test_place_pose_does_not_leak_into_loader_mapping(self):
        for name in GRASP[PALLET_PLACE_KEY]:
            self.assertNotIn(name, GRASP["poses"])
        self.assertNotIn("material_a_pallet_place", bindings().poses)


class ReturnStagesTest(unittest.TestCase):
    def test_reverse_stages_use_only_declared_poses(self):
        name = conveyor_grasp_pose(GRASP, object_id="mat_a",
                                   target_id="loc_conveyor")
        self.assertEqual(name, CONVEYOR_GRASP)
        base = return_bindings(bindings(), object_id="mat_a",
                               target_id="loc_conveyor", grasp_config=GRASP)
        self.assertEqual(base.poses[name],
                         GRASP[CONVEYOR_GRASP_KEY][name]["joint_rad"])
        self.assertEqual(base.attached["mat_a"],
                         GRASP[CONVEYOR_GRASP_KEY][name]["attached_object"])
        stages, findings = build_return_stages(
            base, object_id="mat_a", origin_id="loc_pallet_1",
            target_id="loc_conveyor", conveyor_grasp=name)
        self.assertEqual(findings, [])
        self.assertEqual(tuple(s.stage for s in stages), STAGE_SEQUENCE)
        poses = {s.stage: s.pose_name for s in stages}
        self.assertEqual(poses["pick_approach"], "conveyor_approach")
        self.assertEqual(poses["grasp_approach"], CONVEYOR_GRASP)
        self.assertEqual(poses["place_approach"], "pallet_1_approach")
        # 원래 슬롯 해제는 그 자재의 **측정된 팔레트 파지 자세**다.
        self.assertEqual(poses["place_descend"], "material_a_grasp")
        self.assertEqual(poses["gripper_open_release"], "material_a_grasp")
        for stage in stages:
            if stage.stage in HOLDING_STAGES:
                self.assertEqual(stage.holds_object, "mat_a")
        self.assertIn("pallet_1", stages[8].scene_models)

    def test_return_bindings_move_support_to_conveyor_only_for_copy(self):
        base = bindings()
        copy = return_bindings(base, object_id="mat_a", target_id="loc_conveyor",
                               grasp_config=GRASP)
        self.assertEqual(copy.object_support["mat_a"], "loc_conveyor")
        self.assertEqual(base.object_support["mat_a"], "loc_pallet_1")
        self.assertEqual(copy.pad_links, base.pad_links)
        self.assertNotIn(CONVEYOR_GRASP, base.poses)

    def test_parser_has_explicit_return_flag_off_by_default(self):
        parser = demo.build_parser()
        self.assertFalse(parser.parse_args(["-", "material_a"]).return_held_to_origin)
        self.assertTrue(parser.parse_args(
            ["-", "material_a", "--return-held-to-origin"]).return_held_to_origin)


class ReturnFlowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = SimulationDemoState(Path(tmp.name) / "s.json")
        self.slot = origin_slot(WORKCELL, "material_a")

    def hold_a(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=held_transfer(), final_pose_m=ON_CONVEYOR,
                              restored=None)

    def preflight(self, *, scene=None, occupant=None, observed=ON_CONVEYOR,
                  unobserved=()):
        base = return_bindings(bindings(), object_id="mat_a",
                               target_id="loc_conveyor", grasp_config=GRASP)
        stages, stage_findings = build_return_stages(
            base, object_id="mat_a", origin_id="loc_pallet_1",
            target_id="loc_conveyor", conveyor_grasp=CONVEYOR_GRASP)
        scene = scene or CleanScene()
        _, found = check_stages(stages, bindings=base, client=scene)
        return return_preflight(
            record=self.state.held_record("material_a"), model="material_a",
            observed_pose=observed,
            on_conveyor=(in_placement_zone(observed, conveyor_zone())
                         if observed else None),
            occupant=occupant, unobserved_others=unobserved,
            grasp_object_center=conveyor_grasp_center(GRASP, CONVEYOR_GRASP),
            stage_findings=stage_findings,
            check_findings=[(f.reason_code, f.detail) for f in found],
            scene_stable=True)

    # ── 성공 ─────────────────────────────────────────────────────────────
    def test_held_material_a_returns_to_pallet_1_slot(self):
        self.hold_a()
        self.assertTrue(in_placement_zone(ON_CONVEYOR, conveyor_zone())[0])
        self.assertEqual(self.preflight(), [])
        observed_home = (self.slot.home_pose_m[0] + 0.003,
                         self.slot.home_pose_m[1] - 0.002,
                         self.slot.home_pose_m[2])
        attempt = self.state.record_return(
            "material_a", completed=True, stop_requested=False,
            final_pose_m=observed_home, origin_home_m=self.slot.home_pose_m)
        self.assertTrue(attempt["returned_to_origin"])
        status = self.state.status()
        self.assertNotIn("material_a", status["objects"])
        self.assertFalse(status["simulation_demo_reset_required"])
        self.assertFalse(status["state_hold_active"])
        self.assertEqual(status["last_reset"]["reason"], "return_to_origin")
        self.assertIs(status["is_simulated"], True)
        self.assertIs(status["real_hardware_ready"], False)

    def test_success_removes_only_that_record(self):
        self.hold_a()
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_b",
                              result=held_transfer(), final_pose_m=ON_CONVEYOR,
                              restored=None)
        self.state.record_return(
            "material_a", completed=True, stop_requested=False,
            final_pose_m=self.slot.home_pose_m,
            origin_home_m=self.slot.home_pose_m)
        status = self.state.status()
        self.assertEqual(list(status["objects"]), ["material_b"])
        self.assertTrue(status["simulation_demo_reset_required"])

    # ── 차단 ─────────────────────────────────────────────────────────────
    def test_material_without_record_is_blocked(self):
        findings = self.preflight()
        self.assertEqual([c for c, _ in findings],
                         [ReasonCode.PLAN_RESOURCE_MISMATCH])

    def test_record_that_is_not_held_is_blocked(self):
        self.hold_a()
        self.state.record_return("material_a", completed=False,
                                 stop_requested=False, final_pose_m=None,
                                 origin_home_m=self.slot.home_pose_m)
        self.assertIsNone(self.state.held_record("material_a"))
        self.assertEqual([c for c, _ in self.preflight()],
                         [ReasonCode.PLAN_RESOURCE_MISMATCH])

    def test_occupied_origin_slot_is_blocked(self):
        self.hold_a()
        others = {"material_b": (self.slot.home_pose_m, (0.05, 0.05, 0.1)),
                  "material_c": ((0.5, -0.2, 0.84), (0.05, 0.05, 0.1))}
        occupant = slot_occupant(self.slot, others)
        self.assertEqual(occupant, "material_b")
        self.assertIn(ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                      [c for c, _ in self.preflight(occupant=occupant)])

    def test_neighbouring_pallets_do_not_occupy_the_slot(self):
        others = {"material_b": ((0.5, 0.0, 0.84), (0.05, 0.05, 0.1)),
                  "material_c": ((0.5, -0.2, 0.84), (0.05, 0.05, 0.1)),
                  "material_a": (ON_CONVEYOR, (0.05, 0.05, 0.1))}
        self.assertIsNone(slot_occupant(self.slot, others))

    def test_unobserved_neighbour_blocks(self):
        self.hold_a()
        self.assertIn(ReasonCode.EXEC_UNVERIFIABLE,
                      [c for c, _ in self.preflight(unobserved=["material_b"])])

    def test_object_not_on_conveyor_is_blocked(self):
        self.hold_a()
        codes = [c for c, _ in self.preflight(observed=(0.5, 0.2, 0.84))]
        self.assertIn(ReasonCode.EXEC_SIM_OBJECT_ABSENT, codes)
        codes = [c for c, _ in self.preflight(observed=None)]
        self.assertIn(ReasonCode.EXEC_UNVERIFIABLE, codes)

    def test_object_away_from_measured_grasp_position_is_blocked(self):
        """배치 구역 안이라도 측정한 파지 위치에서 벗어나면 그 자세로 집지 않는다."""
        self.hold_a()
        center = conveyor_grasp_center(GRASP, CONVEYOR_GRASP)
        shifted = (center[0] + 0.1, center[1], center[2])
        self.assertTrue(in_placement_zone(shifted, conveyor_zone())[0])
        self.assertEqual([c for c, _ in self.preflight(observed=shifted)],
                         [ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE])

    def test_geometry_collision_blocks(self):
        self.hold_a()
        scene = CleanScene(contacts=[("robotiq_85_base_link", "conveyor__belt")])
        self.assertIn(ReasonCode.GEOMETRY_COLLISION,
                      [c for c, _ in self.preflight(scene=scene)])

    def test_unstable_or_unchecked_scene_blocks(self):
        self.hold_a()
        findings = return_preflight(
            record=self.state.held_record("material_a"), model="material_a",
            observed_pose=ON_CONVEYOR,
            on_conveyor=in_placement_zone(ON_CONVEYOR, conveyor_zone()),
            occupant=None, scene_stable=False)
        self.assertEqual([c for c, _ in findings],
                         [ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED])

    # ── STOP·실패·관측 불가: 기록 유지 ──────────────────────────────────
    def assert_kept(self, state_name):
        status = self.state.status()
        self.assertIn("material_a", status["objects"])
        self.assertEqual(status["objects"]["material_a"]["state"], state_name)
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertFalse(status["state_hold_active"])
        self.assertIsNone(status["display_label"])
        self.assertEqual(status["objects"]["material_a"]["held_pose_m"],
                         [round(v, 6) for v in ON_CONVEYOR])

    def test_stop_keeps_the_record(self):
        self.hold_a()
        attempt = self.state.record_return(
            "material_a", completed=False, stop_requested=True,
            final_pose_m=(0.4, 0.0, 1.0), origin_home_m=self.slot.home_pose_m,
            reason_codes=["exec.stopped"])
        self.assertFalse(attempt["returned_to_origin"])
        self.assert_kept(RETURN_STOPPED)

    def test_stop_even_at_the_slot_is_not_a_return(self):
        self.hold_a()
        self.state.record_return(
            "material_a", completed=True, stop_requested=True,
            final_pose_m=self.slot.home_pose_m,
            origin_home_m=self.slot.home_pose_m)
        self.assert_kept(RETURN_STOPPED)

    def test_failure_keeps_the_record(self):
        self.hold_a()
        self.state.record_return(
            "material_a", completed=False, stop_requested=False,
            final_pose_m=self.slot.home_pose_m,
            origin_home_m=self.slot.home_pose_m,
            reason_codes=["exec.goal_not_reached"])
        self.assert_kept(RETURN_FAILED)

    def test_unobserved_final_pose_keeps_the_record(self):
        self.hold_a()
        attempt = self.state.record_return(
            "material_a", completed=True, stop_requested=False,
            final_pose_m=None, origin_home_m=self.slot.home_pose_m)
        self.assertIsNone(attempt["gap_m"])
        self.assert_kept(RETURN_FAILED)

    def test_landing_outside_the_slot_keeps_the_record(self):
        self.hold_a()
        off = (self.slot.home_pose_m[0] + ORIGIN_TOLERANCE_M + 0.005,
               self.slot.home_pose_m[1], self.slot.home_pose_m[2])
        attempt = self.state.record_return(
            "material_a", completed=True, stop_requested=False,
            final_pose_m=off, origin_home_m=self.slot.home_pose_m)
        self.assertFalse(attempt["returned_to_origin"])
        self.assert_kept(RETURN_FAILED)

    def test_record_return_requires_a_record(self):
        with self.assertRaises(ValueError):
            self.state.record_return("material_a", completed=True,
                                     stop_requested=False,
                                     final_pose_m=self.slot.home_pose_m,
                                     origin_home_m=self.slot.home_pose_m)


class HeldStateStaysSimulationOnlyTest(unittest.TestCase):
    def test_pick_place_still_blocked(self):
        from tests.unit.test_fr3_gazebo_adapter import build

        adapter, _, _, _ = build()
        adapter.connect(1.0)
        for result in (adapter.pick("mat_a", "loc_conveyor", 1.0),
                       adapter.place("mat_a", "loc_pallet_1", 1.0)):
            self.assertFalse(result.request_accepted)
            self.assertEqual(result.reason.value, "capability.profile_incomplete")

    def test_return_path_does_not_touch_hardware_modules(self):
        import ast

        tree = ast.parse((ROOT / "scripts/demo_workcell_pick_place.py")
                         .read_text(encoding="utf-8"))
        imported = {node.module for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module}
        self.assertFalse([m for m in imported if m.startswith("robots.hardware")])
        text = (ROOT / "validation/simulation_demo_state.py").read_text(
            encoding="utf-8")
        self.assertNotIn("robots.hardware", text)
        self.assertNotIn("real_hardware_ready\": True", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
