"""시뮬레이션 시연 STOP 체크포인트(기록만, 재개 없음) 단위 검증.

- 확인된 STOP + 정지 뒤 새 관측 → 체크포인트 생성(필수 값 전부)
- unconfirmed STOP · 오래된 관측 · scene 변경/없음 · 자재 상태 unknown → 미생성
- E2E 초기화·수동 restore·완료된 새 실행·복귀 성공 → 그 자재 체크포인트 제거
- API 상태: checkpoint_available · checkpoint_id · object_state ·
  resume_available=false · reset_required
- 상태 조회는 어댑터(로봇 명령)·래치·기록 파일을 건드리지 않는다

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
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
from validation.pick_place_plan import STAGE_SEQUENCE  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    OBJECT_CONVEYOR,
    OBJECT_HELD,
    OBJECT_PALLET,
    OBJECT_UNKNOWN,
    POLICY_DEMO_HOLD,
    STOPPED_UNRESTORED,
    SimulationDemoState,
    build_stop_checkpoint,
    classify_object_state,
)
from validation.simulation_e2e import (  # noqa: E402
    CRITERIA,
    Criterion,
    SimulationTransferResult,
    placement_zone,
)

ZONE = placement_zone(target_center_m=(0.25, -0.5, 0.70),
                      surface_half_extent_m=(0.375, 0.125),
                      object_size_m=(0.05, 0.05, 0.1),
                      surface_top_z_m=0.70, z_tolerance_m=0.01)
HOME_A = (0.5, 0.2, 0.84)
ON_CONVEYOR = (0.2493, -0.4998, 0.75)
IN_AIR = (0.40, 0.00, 1.05)
GRIPPER = "robotiq_85_left_knuckle_joint"
STOP_AT = 100.0


def observation(*, at=STOP_AT + 0.6, valid=True, gripper=True):
    positions = {f"j{i}": 0.1 * i for i in range(1, 7)}
    if gripper:
        positions[GRIPPER] = 0.356646
    return {"positions": positions, "observed_at": at, "valid": valid}


def build(**overrides):
    kwargs = dict(
        run_id="simdemo_test", mode="forward", model="material_a",
        object_id="mat_a", source_id="loc_pallet_1",
        destination_id="loc_conveyor", origin_slot_id="loc_pallet_1",
        stage_names=list(STAGE_SEQUENCE), stopped_stage="place_approach",
        completed_stages=list(STAGE_SEQUENCE[:7]), stop_confirmed=True,
        stop_execution_id="simstop_abc", stop_requested_at=STOP_AT,
        joint_observation=observation(), gripper_joint=GRIPPER, held=True,
        object_pose_m=IN_AIR, conveyor_zone=ZONE, origin_home_m=HOME_A,
        scene_hash_at_stop="h" * 64, scene_hash_now="h" * 64)
    kwargs.update(overrides)
    if "convergence" not in overrides and kwargs["object_pose_m"] is not None:
        # 기본: 자재가 도구 위치에 수렴한 것으로 관측됐다(A 실측과 같은 경우).
        kwargs["convergence"] = converged(kwargs["object_pose_m"])
    return build_stop_checkpoint(**kwargs)


def converged(pose, *, ok=True, error=0.0):
    return {"converged": ok, "expected_pose_m": list(pose),
            "observed_pose_m": list(pose), "error_m": error, "samples": 2,
            "consecutive_within": 2 if ok else 0, "required_consecutive": 2,
            "tolerance_m": 0.05, "timeout_s": 5.0, "settle_elapsed_s": 0.2}


class BuildCheckpointTest(unittest.TestCase):
    def test_confirmed_stop_with_fresh_observation_creates_checkpoint(self):
        checkpoint, reasons = build()
        self.assertEqual(reasons, [])
        for key in ("checkpoint_id", "run_id", "object_id", "source_id",
                    "destination_id", "previous_stage", "stopped_stage",
                    "remaining_stages", "joint_state", "gripper", "object_state",
                    "object_pose_m", "scene_hash", "captured_at",
                    "stop_execution_id"):
            self.assertIsNotNone(checkpoint[key], key)
        self.assertEqual(checkpoint["previous_stage"], "lift")
        self.assertEqual(checkpoint["stopped_stage"], "place_approach")
        self.assertEqual(checkpoint["remaining_stages"],
                         list(STAGE_SEQUENCE[7:]))
        self.assertEqual(checkpoint["object_state"], OBJECT_HELD)
        self.assertEqual(checkpoint["gripper"],
                         {"joint": GRIPPER, "position_rad": 0.356646})
        self.assertEqual(checkpoint["stop_execution_id"], "simstop_abc")
        self.assertIs(checkpoint["is_simulated"], True)
        self.assertIs(checkpoint["stop_confirmed"], True)
        self.assertIs(checkpoint["resume_available"], False)
        self.assertIs(checkpoint["real_hardware_ready"], False)
        self.assertIs(checkpoint["real_hardware_verified"], False)

    def test_unconfirmed_stop_creates_nothing(self):
        for value in (False, None):
            with self.subTest(stop_confirmed=value):
                checkpoint, reasons = build(stop_confirmed=value)
                self.assertIsNone(checkpoint)
                self.assertTrue(any("unconfirmed" in r for r in reasons))

    def test_stale_or_missing_observation_creates_nothing(self):
        for obs in (observation(at=STOP_AT - 1.0), observation(valid=False),
                    observation(gripper=False), None):
            with self.subTest(obs=obs):
                self.assertIsNone(build(joint_observation=obs)[0])

    def test_scene_change_or_missing_hash_creates_nothing(self):
        for now in ("x" * 64, None, ""):
            with self.subTest(scene_now=now):
                checkpoint, reasons = build(scene_hash_now=now)
                self.assertIsNone(checkpoint)
                self.assertTrue(reasons)
        self.assertIsNone(build(scene_hash_at_stop=None)[0])

    def test_unknown_object_state_creates_nothing(self):
        checkpoint, reasons = build(held=False, object_pose_m=IN_AIR)
        self.assertIsNone(checkpoint)
        self.assertTrue(any("unknown" in r for r in reasons))

    def test_missing_object_pose_creates_nothing_even_when_held(self):
        self.assertIsNone(build(object_pose_m=None)[0])

    def test_object_state_classification(self):
        self.assertEqual(classify_object_state(
            held=True, pose_m=None, conveyor_zone=ZONE,
            origin_home_m=HOME_A), OBJECT_HELD)
        self.assertEqual(classify_object_state(
            held=False, pose_m=(0.501, 0.2, 0.84), conveyor_zone=ZONE,
            origin_home_m=HOME_A), OBJECT_PALLET)
        self.assertEqual(classify_object_state(
            held=False, pose_m=ON_CONVEYOR, conveyor_zone=ZONE,
            origin_home_m=HOME_A), OBJECT_CONVEYOR)
        self.assertEqual(classify_object_state(
            held=False, pose_m=IN_AIR, conveyor_zone=ZONE,
            origin_home_m=HOME_A), OBJECT_UNKNOWN)

    def test_return_mode_records_its_own_source_and_destination(self):
        checkpoint, _ = build(mode="return", source_id="loc_conveyor",
                              destination_id="loc_pallet_1", held=False,
                              object_pose_m=ON_CONVEYOR,
                              stopped_stage="pick_approach",
                              completed_stages=["home_start"])
        self.assertEqual((checkpoint["source_id"], checkpoint["destination_id"]),
                         ("loc_conveyor", "loc_pallet_1"))
        self.assertEqual(checkpoint["object_state"], OBJECT_CONVEYOR)
        self.assertEqual(checkpoint["remaining_stages"][0], "pick_approach")


def stopped_result():
    return SimulationTransferResult(
        scenario="loc_pallet_1/mat_a->loc_conveyor", object_id="mat_a",
        support_id="loc_pallet_1", target_id="loc_conveyor",
        criteria=tuple(Criterion(key=k, label=v, met=k != "no_fault_during_run",
                                 detail="관측",
                                 reason_code=(ReasonCode.EXEC_STOPPED
                                              if k == "no_fault_during_run"
                                              else None))
                       for k, v in CRITERIA),
        stop_requested=True, stop_confirmed=True).to_dict()


def completed_result():
    return SimulationTransferResult(
        scenario="loc_pallet_1/mat_a->loc_conveyor", object_id="mat_a",
        support_id="loc_pallet_1", target_id="loc_conveyor",
        criteria=tuple(Criterion(key=k, label=v, met=True, detail="관측")
                       for k, v in CRITERIA)).to_dict()


class StoredCheckpointTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "s.json"
        self.state = SimulationDemoState(self.path)

    def stop_a(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=IN_AIR,
                              restored=None)
        checkpoint, _ = build()
        return self.state.record_checkpoint(checkpoint)

    def test_status_exposes_checkpoint_read_only_fields(self):
        empty = self.state.status()
        self.assertIs(empty["checkpoint_available"], False)
        self.assertIsNone(empty["checkpoint_id"])
        self.assertIsNone(empty["object_state"])
        self.assertIs(empty["resume_available"], False)
        checkpoint = self.stop_a()
        status = self.state.status()
        self.assertIs(status["checkpoint_available"], True)
        self.assertEqual(status["checkpoint_id"], checkpoint["checkpoint_id"])
        self.assertEqual(status["object_state"], OBJECT_HELD)
        self.assertIs(status["resume_available"], False)
        self.assertIs(status["simulation_demo_reset_required"], True)
        self.assertEqual(status["objects"]["material_a"]["state"],
                         STOPPED_UNRESTORED)
        self.assertIs(status["is_simulated"], True)
        self.assertIs(status["real_hardware_ready"], False)

    def test_record_rejects_checkpoints_breaking_the_contract(self):
        checkpoint, _ = build()
        for key, value in (("resume_available", True), ("is_simulated", False),
                           ("stop_confirmed", False),
                           ("object_state", OBJECT_UNKNOWN)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.state.record_checkpoint({**checkpoint, key: value})
        self.assertFalse(self.path.exists())

    def test_manual_restore_removes_checkpoint(self):
        self.stop_a()
        self.state.mark_restored("material_a", verified=False)
        self.assertTrue(self.state.status()["checkpoint_available"])
        self.state.mark_restored("material_a", verified=True)
        status = self.state.status()
        self.assertFalse(status["checkpoint_available"])
        self.assertFalse(status["simulation_demo_reset_required"])

    def test_e2e_reset_removes_checkpoint(self):
        spec = importlib.util.spec_from_file_location(
            "e2e_for_checkpoint", ROOT / "scripts/verify_pick_place_sim_e2e.py")
        e2e = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(e2e)
        self.stop_a()

        class Fixture:
            def restore(self, model):
                return types.SimpleNamespace(verified=True, pose_m=(0, 0, 0))

        e2e.reset_cell(Fixture(), {"material_a": 1, "material_b": 1}, self.state,
                       phase="before")
        self.assertFalse(self.state.status()["checkpoint_available"])
        self.assertEqual(self.state.checkpoints(), {})

    def test_completed_new_run_removes_stale_checkpoint(self):
        self.stop_a()
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=completed_result(), final_pose_m=ON_CONVEYOR,
                              restored=None)
        self.assertFalse(self.state.status()["checkpoint_available"])

    def test_stop_record_alone_keeps_checkpoint(self):
        self.stop_a()
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=IN_AIR,
                              restored=None)
        self.assertTrue(self.state.status()["checkpoint_available"])

    def test_checkpoint_of_other_material_is_kept(self):
        self.stop_a()
        self.state.mark_restored("material_b", verified=True)
        self.assertTrue(self.state.status()["checkpoint_available"])


class ReadOnlyStatusTest(unittest.TestCase):
    """상태 조회가 로봇 명령·래치 해제·계획 생성을 일으키지 않는다."""

    def setUp(self):
        from server.config import ServerConfig
        from server.runtime import build_runtime

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        config = dataclasses.replace(
            ServerConfig.from_env(), db_path=self.tmp / "web.sqlite3",
            enable_stt=False, llm_config_name="__absent__.json")
        self.runtime = build_runtime(config)
        self.addCleanup(self.runtime.repository.close)
        self.state_path = self.tmp / "sim_demo_state.json"
        self.runtime.simulation_demo_state_path = self.state_path
        state = SimulationDemoState(self.state_path)
        state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                         result=stopped_result(), final_pose_m=IN_AIR,
                         restored=None)
        state.record_checkpoint(build()[0])

    def test_status_touches_nothing(self):
        before = self.state_path.read_bytes()
        latch = Path("/tmp/forstick2_workcell/sim_stop_latch.json")
        latch_before = latch.read_bytes() if latch.is_file() else None
        called = []
        self.runtime.adapter = lambda: called.append("adapter")
        for _ in range(3):
            status = self.runtime.simulation_demo_status()
        self.assertEqual(called, [])
        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(latch.read_bytes() if latch.is_file() else None,
                         latch_before)
        self.assertTrue(status["checkpoint_available"])
        self.assertIs(status["resume_available"], False)

    def test_robots_route_carries_checkpoint_fields(self):
        from server.routes import robot

        ctx = types.SimpleNamespace(runtime=self.runtime,
                                    repository=self.runtime.repository)
        status, _, body = asyncio.run(
            robot.handle(ctx, "GET", "/v1/robots", None, {}))
        self.assertEqual(status, 200)
        demo = json.loads(body)["simulation_demo"]
        self.assertIs(demo["checkpoint_available"], True)
        self.assertTrue(demo["checkpoint_id"].startswith("simckpt_"))
        self.assertEqual(demo["object_state"], OBJECT_HELD)
        self.assertIs(demo["resume_available"], False)
        self.assertIs(demo["simulation_demo_reset_required"], True)
        # 경로는 GET만 받는다 — 상태를 바꾸는 요청 경로가 없다.
        self.assertIsNone(asyncio.run(
            robot.handle(ctx, "POST", "/v1/robots", None, {})))

    def test_pick_place_stays_blocked(self):
        from tests.unit.test_fr3_gazebo_adapter import build as build_adapter

        adapter, _, _, _ = build_adapter()
        adapter.connect(1.0)
        for result in (adapter.pick("mat_a", "loc_pallet_1", 1.0),
                       adapter.place("mat_a", "loc_conveyor", 1.0)):
            self.assertFalse(result.request_accepted)
            self.assertEqual(result.reason.value, "capability.profile_incomplete")


class DemoScriptWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "scripts/demo_workcell_pick_place.py").read_text(
            encoding="utf-8")
        tree = ast.parse(cls.source)
        cls.functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def test_both_stop_branches_capture_and_record_after_verdict(self):
        for name, record in (("main", "demo_state.record_run("),
                             ("return_held_to_origin", "demo_state.record_return(")):
            with self.subTest(path=name):
                body = ast.unparse(self.functions[name])
                self.assertEqual(body.count("capture_stop_checkpoint("), 1)
                self.assertLess(body.rindex(record),
                                body.index("record_stop_checkpoint("))

    def test_forward_checkpoint_only_for_explicit_hold_policy(self):
        body = ast.unparse(self.functions["main"])
        guard = body.index("if args.cell_policy == POLICY_DEMO_HOLD:\n"
                           "                pending_checkpoint = capture_stop_checkpoint(")
        self.assertGreater(guard, 0)

    def test_resume_entry_is_only_the_two_explicit_flags(self):
        """resume는 명시적 플래그로만 들어온다. 로봇 명령은 `_send` 한 곳에서만."""
        tree = ast.parse(self.source)
        flags = {a.value for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"
                 for a in n.args[:1] if isinstance(a, ast.Constant)}
        self.assertEqual({f for f in flags if f.startswith("--resume")},
                         {"--resume-preflight", "--resume-checkpoint"})
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                   and n.name == "run_resume")
        direct = {c.func.attr for c in ast.walk(run) if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Attribute)
                  and c.func.attr in {"send_arm", "send_arm_async", "send_gripper",
                                      "cancel_all"}}
        self.assertEqual(direct, set())
        self.assertNotIn('"resume_available": True', self.source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
