"""STOP 체크포인트 resume 실행기(`run_resume`) 단위 검증 — mock으로만.

- 성공: 복구 접근 → place_descend → release → retreat → home, 첫 명령은 새
  복구 접근(중단 궤적 재생 없음), `simulation_transfer_resumed_completed`,
  체크포인트 제거, 자재 held_on_target, 래치 해제
- 실행 직전 재검증 실패(scene·pose·attachment·래치·goal·stale 관측·id):
  명령 0건, 체크포인트·래치 그대로, 상태 파일 기록 없음
- 실행 중 실패: 옛 체크포인트 제거, reset_required, 옛 id로 재실행 BLOCK
- 확인된 STOP: 새 현재 상태 체크포인트로 교체 / 미확인 STOP: 사유만

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.simulation_demo_state import (  # noqa: E402
    FAULT_UNRESTORED,
    HELD_ON_TARGET,
    OBJECT_HELD,
    POLICY_DEMO_HOLD,
    STOPPED_UNRESTORED,
    SimulationDemoState,
)
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    GRIPPER,
    HOME_A,
    build,
    stopped_result,
)
from tests.unit.test_simulation_demo_resume_preflight import (  # noqa: E402
    forward_stages,
)
from tests.unit.test_simulation_demo_return import conveyor_zone  # noqa: E402

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_resume_run", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

SCENE = "h" * 64
ON_CONVEYOR = (0.2493, -0.4998, 0.75)


class FakeClient:
    def __init__(self, hashes=None, contacts=()):
        self.hashes = list(hashes or [])
        self.contacts = [list(pair) for pair in contacts]

    def snapshot(self):
        value = self.hashes.pop(0) if self.hashes else SCENE
        return types.SimpleNamespace(content_hash=value)

    def check_state(self, joints, attached=None):
        return types.SimpleNamespace(contacts=self.contacts, out_of_bounds=())


class FakeTransport:
    def __init__(self, positions, *, stale=False, fail_on=None, cancel_ok=True):
        self.positions = dict(positions)
        self.stale = stale
        self.fail_on = fail_on
        self.cancel_ok = cancel_ok
        self.sent = 0
        self.arm_targets: list[dict] = []

    def joint_observation(self, timeout_sec, *, after=None):
        return types.SimpleNamespace(
            positions=dict(self.positions),
            velocities={name: 0.0 for name in self.positions},
            observed_at=0.0 if self.stale else time.time(), valid=True)

    def live_goals(self):
        return 0

    def _result(self):
        self.sent += 1
        failed = self.fail_on is not None and self.sent == self.fail_on
        return types.SimpleNamespace(accepted=True, error_code=-4 if failed else 0,
                                     detail="")

    def send_arm(self, target, seconds, timeout, **kwargs):
        self.arm_targets.append(dict(target))
        result = self._result()
        if not result.error_code:
            self.positions.update(target)
        return result

    def send_arm_async(self, target, seconds, timeout):
        return self._result()

    def send_gripper(self, value, seconds, timeout, **kwargs):
        result = self._result()
        self.positions[GRIPPER] = value
        return result

    def cancel_all(self, timeout):
        return types.SimpleNamespace(accepted=self.cancel_ok, goals_canceling=1)


class FakeFixture:
    def __init__(self, pose):
        self.pose = tuple(pose)
        self._held: set[str] = set()
        self.calls: list[str] = []

    def pose_of(self, model, *, timeout_sec=3.0, fresh=False):
        return self.pose

    def attach(self, model, *, pose_m):
        self.calls.append("attach")
        self._held.add(model)
        return types.SimpleNamespace(verified=True)

    def detach(self, model, *, pose_m):
        self.calls.append("detach")
        self._held.discard(model)
        self.pose = ON_CONVEYOR
        return types.SimpleNamespace(verified=True)

    def settle(self, model):
        return self.pose

    def held(self):
        return tuple(sorted(self._held))


class FakeFollower:
    def __init__(self):
        self.samples = [{"with_tool": True, "gap_m": 0.0}]

    def start(self):
        pass

    def sample(self):
        return {"with_tool": True, "gap_m": 0.0}

    def halt(self):
        pass


class ResumeRunBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.stages, self.bindings = forward_stages()
        self.state = SimulationDemoState(self.tmp / "state.json")
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=(0.4, 0, 1.05),
                              restored=None)
        self.cp = self.state.record_checkpoint(build(scene_hash_at_stop=SCENE,
                                                     scene_hash_now=SCENE)[0])
        self.latch = demo.StopLatchFile(self.tmp / "latch.json")
        self.latch.latch({"stop_execution_id": self.cp["stop_execution_id"],
                          "scene_hash": SCENE, "stage": "place_approach"})

    def context(self, *, client=None, transport=None, fixture=None,
                attachment=True, goals=0, gap=0.0001, expected=None):
        fixture = fixture or FakeFixture(self.cp["object_pose_m"])
        # 기본: 도구 위치(관절 FK + offset) = 지금 자재 위치 → 즉시 수렴.
        expected = expected or (lambda positions: getattr(fixture, "pose", None))
        return demo.ResumeContext(
            expected_object_pose=expected,
            model="material_a",
            checkpoint=self.state.checkpoints().get("material_a") or {},
            bindings=self.bindings, stages=self.stages,
            client=client or FakeClient(),
            transport=transport or FakeTransport(self.cp["joint_state"]["positions"]),
            fixture=fixture,
            latch=self.latch, demo_state=self.state,
            observe_attachment=lambda: {"static": attachment},
            observe_goals=lambda: {"active": goals},
            tool_object_gap=lambda observation, pose: gap,
            conveyor_zone=conveyor_zone(), origin_home_m=HOME_A,
            make_follower=FakeFollower)


class SuccessTest(ResumeRunBase):
    def test_success_runs_new_plan_and_switches_state(self):
        ctx = self.context()
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], demo.RESUMED_COMPLETED, report["reasons"])
        self.assertEqual(ctx.commands, ["send_arm", "send_arm", "send_gripper",
                                        "send_arm", "send_arm"])
        self.assertEqual([r["stage"] for r in report["stage_records"]],
                         ["place_approach", "place_descend", "gripper_open_release",
                          "retreat", "home_end"])
        # 첫 명령은 새 복구 접근의 목표(conveyor_approach)다 — 중단 궤적 재생 없음.
        self.assertEqual(report["plan"]["stages"][0]["stage"], "recovery_approach")
        self.assertEqual(report["plan"]["stages"][0]["to_pose"], "conveyor_approach")
        self.assertIs(report["replays_interrupted_trajectory"], False)
        self.assertEqual(ctx.transport.arm_targets[0],
                         self.bindings.poses["conveyor_approach"])
        self.assertEqual(ctx.fixture.calls, ["attach", "detach"])
        self.assertIsNone(self.latch.latched())
        status = self.state.status()
        self.assertFalse(status["checkpoint_available"])
        self.assertIsNone(status["resume_preflight"])
        row = status["objects"]["material_a"]
        self.assertEqual(row["state"], HELD_ON_TARGET)
        self.assertEqual(row["resumed_from_checkpoint"], self.cp["checkpoint_id"])
        self.assertEqual(row["target_id"], "loc_conveyor")
        self.assertTrue(status["state_hold_active"])
        self.assertEqual(status["last_run"]["status"], demo.RESUMED_COMPLETED)
        for key, value in (("is_simulated", True), ("real_hardware_ready", False),
                           ("real_hardware_verified", False)):
            self.assertIs(report[key], value)
            self.assertIs(status[key], value)


class PreVerificationBlockTest(ResumeRunBase):
    def assert_blocked(self, ctx, checkpoint_id=None, text=None):
        before_state = (self.tmp / "state.json").read_bytes()
        before_latch = (self.tmp / "latch.json").read_bytes()
        report = demo.run_resume(ctx, checkpoint_id=checkpoint_id
                                 or self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_blocked")
        self.assertEqual(report["robot_commands_sent"], 0)
        self.assertEqual(ctx.commands, [])
        self.assertEqual(ctx.fixture.calls, [])
        self.assertEqual((self.tmp / "state.json").read_bytes(), before_state)
        self.assertEqual((self.tmp / "latch.json").read_bytes(), before_latch)
        self.assertTrue(self.state.status()["checkpoint_available"])
        if text:
            self.assertTrue(any(text in r for r in report["reasons"]), report["reasons"])
        return report

    def test_scene_change_blocks(self):
        self.assert_blocked(self.context(client=FakeClient(hashes=["x" * 64])), text="scene")

    def test_object_pose_mismatch_blocks(self):
        pose = list(self.cp["object_pose_m"])
        pose[0] += 0.02
        self.assert_blocked(self.context(fixture=FakeFixture(pose)), text="자재 pose")

    def test_missing_attachment_blocks(self):
        self.assert_blocked(self.context(attachment=False), text="attachment")

    def test_latch_ownership_blocks(self):
        self.latch.latch({"stop_execution_id": "simstop_other", "scene_hash": SCENE})
        self.assert_blocked(self.context(), text="래치")

    def test_missing_latch_blocks(self):
        self.latch.path.unlink()
        before_state = (self.tmp / "state.json").read_bytes()
        ctx = self.context()
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_blocked")
        self.assertEqual(ctx.commands, [])
        self.assertEqual((self.tmp / "state.json").read_bytes(), before_state)

    def test_active_goal_blocks(self):
        self.assert_blocked(self.context(goals=1), text="goal")

    def test_stale_observation_blocks(self):
        transport = FakeTransport(self.cp["joint_state"]["positions"], stale=True)
        self.assert_blocked(self.context(transport=transport), text="새 관절 관측")

    def test_geometry_collision_blocks(self):
        client = FakeClient(contacts=[("robotiq_85_base_link", "conveyor__belt")])
        self.assert_blocked(self.context(client=client), text="geometry.collision")

    def test_wrong_checkpoint_id_blocks(self):
        self.assert_blocked(self.context(), checkpoint_id="simckpt_old")

    def test_saved_preflight_is_not_trusted(self):
        self.state.record_resume_preflight("material_a", {
            "allowed": True, "checkpoint_id": self.cp["checkpoint_id"],
            "resume_plan_id": "simresume_x", "scene_hash": SCENE})
        self.assert_blocked(self.context(attachment=False), text="attachment")


class ExecutionFailureTest(ResumeRunBase):
    def test_failure_drops_old_checkpoint_and_requires_reset(self):
        transport = FakeTransport(self.cp["joint_state"]["positions"], fail_on=2)
        ctx = self.context(transport=transport)
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_failed")
        self.assertTrue(any("exec.goal_rejected" in r for r in report["reasons"]))
        status = self.state.status()
        self.assertFalse(status["checkpoint_available"])
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertEqual(status["objects"]["material_a"]["state"], FAULT_UNRESTORED)
        self.assertTrue(status["objects"]["material_a"]["reasons"])
        # 옛 체크포인트로는 다시 들어올 수 없다.
        again = self.context()
        self.assertEqual(demo.run_resume(again, checkpoint_id=self.cp["checkpoint_id"])
                         ["status"], "resume_blocked")
        self.assertEqual(again.commands, [])


class StopDuringResumeTest(ResumeRunBase):
    def test_confirmed_stop_replaces_checkpoint_with_current_state(self):
        ctx = self.context()
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"],
                                 stop_at="place_descend")
        self.assertEqual(report["status"], "resume_stopped")
        self.assertIn("cancel_all", ctx.commands)
        new_id = report["new_checkpoint_id"]
        self.assertTrue(new_id and new_id != self.cp["checkpoint_id"])
        status = self.state.status()
        self.assertEqual(status["checkpoint_id"], new_id)
        self.assertEqual(status["object_state"], OBJECT_HELD)
        self.assertIs(status["resume_available"], False)
        self.assertEqual(status["checkpoint"]["stopped_stage"], "place_descend")
        self.assertEqual(status["objects"]["material_a"]["state"], STOPPED_UNRESTORED)
        latch = self.latch.latched()
        self.assertEqual(latch["stop_execution_id"],
                         status["checkpoint"]["stop_execution_id"])
        self.assertNotEqual(latch["stop_execution_id"], self.cp["stop_execution_id"])
        old = self.context()
        self.assertEqual(demo.run_resume(old, checkpoint_id=self.cp["checkpoint_id"])
                         ["status"], "resume_blocked")
        self.assertEqual(old.commands, [])

    def test_unconfirmed_stop_leaves_only_reasons(self):
        transport = FakeTransport(self.cp["joint_state"]["positions"], cancel_ok=False)
        ctx = self.context(transport=transport)
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"],
                                 stop_at="place_descend")
        self.assertEqual(report["status"], "resume_stopped")
        self.assertIsNone(report["new_checkpoint_id"])
        status = self.state.status()
        self.assertFalse(status["checkpoint_available"])
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertEqual(status["objects"]["material_a"]["state"], STOPPED_UNRESTORED)
        self.assertTrue(any("unconfirmed" in r
                            for r in status["objects"]["material_a"]["reasons"]))



class RevalidationEvidenceTest(ResumeRunBase):
    """실행 직전 재검증 증거: 성공·차단·실패·STOP 보고서 모두, 누락이면 BLOCK."""

    def assert_complete(self, evidence):
        self.assertIsNotNone(evidence)
        for key in demo.REVALIDATION_REQUIRED:
            with self.subTest(key=key):
                self.assertIsNotNone(demo._lookup(evidence, key), key)
        self.assertEqual(evidence["missing"], [])

    def test_success_report_carries_full_evidence(self):
        ctx = self.context()
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], demo.RESUMED_COMPLETED)
        evidence = report["revalidation"]
        self.assert_complete(evidence)
        self.assertEqual(evidence["checkpoint_id"], self.cp["checkpoint_id"])
        self.assertEqual(evidence["stop_execution_id"], self.cp["stop_execution_id"])
        self.assertIs(evidence["scene"]["match"], True)
        self.assertEqual(evidence["material_pose"]["error_m"], 0.0)
        self.assertEqual(evidence["tool_material_error_m"], 0.0001)
        self.assertIs(evidence["attachment"]["static"], True)
        self.assertIs(evidence["attachment"]["held_in_checkpoint"], True)
        self.assertEqual(evidence["active_goals"], {"controller": 0, "own": 0,
                                                    "total": 0})
        self.assertIs(evidence["joints"]["fresh"], True)
        self.assertEqual(evidence["joints"]["max_error_rad"], 0.0)
        self.assertEqual(evidence["joints"]["gripper_error_rad"], 0.0)
        self.assertEqual(evidence["recovery_approach"]["samples"],
                         report["plan"]["stages"][0]["path_samples"])
        self.assertIs(evidence["recovery_approach"]["geometry_passed"], True)
        self.assertIs(evidence["recovery_approach"]["remaining_passed"], True)
        self.assertIs(evidence["recovery_approach"]["scene_stable"], True)
        self.assertTrue(evidence["revalidated_at"])

    def test_failure_and_stop_reports_carry_evidence(self):
        transport = FakeTransport(self.cp["joint_state"]["positions"], fail_on=2)
        failed = demo.run_resume(self.context(transport=transport),
                                 checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(failed["status"], "resume_failed")
        self.assert_complete(failed["revalidation"])

    def test_stop_report_carries_evidence(self):
        stopped = demo.run_resume(self.context(),
                                  checkpoint_id=self.cp["checkpoint_id"],
                                  stop_at="place_descend")
        self.assertEqual(stopped["status"], "resume_stopped")
        self.assert_complete(stopped["revalidation"])

    def assert_blocked_without_commands(self, ctx, missing_key):
        before_state = (self.tmp / "state.json").read_bytes()
        before_latch = (self.tmp / "latch.json").read_bytes()
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_blocked")
        self.assertEqual(ctx.commands, [])
        self.assertEqual(report["robot_commands_sent"], 0)
        self.assertEqual((self.tmp / "state.json").read_bytes(), before_state)
        self.assertEqual((self.tmp / "latch.json").read_bytes(), before_latch)
        self.assertIn(missing_key, report["revalidation"]["missing"])
        return report

    def test_each_missing_observation_blocks(self):
        cases = {
            "attachment.static": dict(attachment=None),
            "active_goals.controller": dict(goals=None),
            "tool_material_error_m": dict(gap=None),
            "material_pose.observed_m": dict(fixture=types.SimpleNamespace(
                pose_of=lambda *a, **k: None, calls=[])),
            "scene.observed": dict(client=FakeClient(hashes=[None])),
        }
        for key, overrides in cases.items():
            with self.subTest(missing=key):
                self.assert_blocked_without_commands(self.context(**overrides), key)

    def test_invalid_joint_observation_blocks(self):
        class Invalid(FakeTransport):
            def joint_observation(self, timeout_sec, *, after=None):
                return types.SimpleNamespace(positions={}, velocities={},
                                             observed_at=None, valid=False)

        self.assert_blocked_without_commands(
            self.context(transport=Invalid({})), "joints.observed_at")

    def test_evidence_gap_alone_blocks_even_if_checks_pass(self):
        """다른 조건이 모두 맞아도 증거가 비면 성공으로 가지 않는다."""
        cp = dict(self.state.checkpoints()["material_a"])
        cp.pop("gripper")
        self.state.record_checkpoint(cp)
        report = self.assert_blocked_without_commands(self.context(),
                                                      "joints.gripper_error_rad")
        self.assertTrue(any("재검증 증거" in r for r in report["reasons"]))

class StateContractTest(unittest.TestCase):
    def test_record_resume_rejects_stale_or_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = SimulationDemoState(Path(tmp) / "s.json")
            state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                             result=stopped_result(), final_pose_m=(0.4, 0, 1),
                             restored=None)
            cp = state.record_checkpoint(build()[0])
            with self.assertRaises(ValueError):
                state.record_resume("material_a", checkpoint_id="simckpt_old",
                                    outcome="completed", final_pose_m=ON_CONVEYOR)
            with self.assertRaises(ValueError):
                state.record_resume("material_a", checkpoint_id=cp["checkpoint_id"],
                                    outcome="completed", final_pose_m=ON_CONVEYOR,
                                    new_checkpoint=cp)
            with self.assertRaises(ValueError):
                state.record_resume("material_a", checkpoint_id=cp["checkpoint_id"],
                                    outcome="stopped", final_pose_m=None,
                                    new_checkpoint=cp)
            self.assertEqual(json.loads((Path(tmp) / "s.json").read_text())
                             ["checkpoints"]["material_a"]["checkpoint_id"],
                             cp["checkpoint_id"])

    def test_general_pick_place_stays_blocked(self):
        from tests.unit.test_fr3_gazebo_adapter import build as build_adapter

        adapter, _, _, _ = build_adapter()
        adapter.connect(1.0)
        for result in (adapter.pick("mat_a", "loc_pallet_1", 1.0),
                       adapter.place("mat_a", "loc_conveyor", 1.0)):
            self.assertFalse(result.request_accepted)
            self.assertEqual(result.reason.value, "capability.profile_incomplete")

    def test_parser_resume_checkpoint_is_explicit(self):
        parser = demo.build_parser()
        self.assertIsNone(parser.parse_args(["-", "material_a"]).resume_checkpoint)
        self.assertEqual(parser.parse_args(
            ["-", "material_a", "--resume-checkpoint", "simckpt_x"]).resume_checkpoint,
            "simckpt_x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
