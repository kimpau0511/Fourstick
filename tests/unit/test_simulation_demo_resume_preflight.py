"""STOP 체크포인트 resume 사전검증·재계획(실행 없음) 단위 검증.

- 모든 관측 조건이 맞으면 통과, 새 복구 접근 + 남은 단계로 재계획
- scene 변경 · 자재 pose 불일치 · attachment 없음 · stale 관측 · 래치 없음 ·
  active goal 남음 · 관절 이동 · 도구-자재 이격 → 각각 BLOCK
- BLOCK·통과 모두 체크포인트는 그대로, resume_available은 false
- 사전검증 경로는 로봇 명령·래치 해제·고정 장치 조작을 하지 않는다

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.task_plan import TaskStep  # noqa: E402
from robots.fr3_gazebo.adapter import load_workcell_resources  # noqa: E402
from validation.pick_place_plan import build_stages  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    OBJECT_HELD,
    POLICY_DEMO_HOLD,
    RESUME_PATH_STEP_RAD,
    SimulationDemoState,
    build_resume_stages,
    interpolate_joints,
    resume_preflight_findings,
)
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    GRIPPER,
    build,
    stopped_result,
)

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_resume", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

STARTED = 200.0


def checkpoint():
    return build()[0]


def ok_inputs(cp, **overrides):
    positions = dict(cp["joint_state"]["positions"])
    kwargs = dict(
        checkpoint=cp, model="material_a", scene_hash_now=cp["scene_hash"],
        object_pose_m=tuple(cp["object_pose_m"]), attachment_static=True,
        joint_observation={"positions": positions, "observed_at": STARTED + 0.5,
                           "valid": True},
        preflight_started_at=STARTED, gripper_joint=GRIPPER,
        latch={"stop_execution_id": cp["stop_execution_id"]}, live_goals=0,
        tool_object_gap_m=0.0001, follow_tolerance_m=demo.FOLLOW_TOLERANCE_M)
    kwargs.update(overrides)
    return kwargs


class PreflightFindingsTest(unittest.TestCase):
    def setUp(self):
        self.cp = checkpoint()

    def blocked(self, **overrides):
        reasons = resume_preflight_findings(**ok_inputs(self.cp, **overrides))
        self.assertTrue(reasons, overrides)
        return reasons

    def test_all_conditions_met_passes(self):
        self.assertEqual(resume_preflight_findings(**ok_inputs(self.cp)), [])

    def test_scene_hash_change_blocks(self):
        self.assertIn("scene", self.blocked(scene_hash_now="x" * 64)[0])
        self.blocked(scene_hash_now=None)

    def test_object_pose_mismatch_blocks(self):
        pose = list(self.cp["object_pose_m"])
        pose[2] -= 0.01
        self.assertIn("자재 pose", self.blocked(object_pose_m=pose)[0])
        self.blocked(object_pose_m=None)

    def test_missing_attachment_blocks(self):
        for value in (False, None):
            with self.subTest(static=value):
                self.assertIn("attachment", self.blocked(attachment_static=value)[0])

    def test_stale_or_missing_observation_blocks(self):
        stale = {"positions": dict(self.cp["joint_state"]["positions"]),
                 "observed_at": STARTED - 1.0, "valid": True}
        self.assertIn("새 관절 관측", self.blocked(joint_observation=stale)[0])
        self.blocked(joint_observation=None)
        no_gripper = dict(self.cp["joint_state"]["positions"])
        no_gripper.pop(GRIPPER)
        self.blocked(joint_observation={"positions": no_gripper,
                                        "observed_at": STARTED + 1, "valid": True})

    def test_moved_arm_blocks(self):
        moved = dict(self.cp["joint_state"]["positions"])
        moved["j1"] += 0.2
        self.assertIn("관절", self.blocked(joint_observation={
            "positions": moved, "observed_at": STARTED + 1, "valid": True})[0])

    def test_latch_and_goals_block(self):
        self.blocked(latch=None)
        self.blocked(latch={"stop_execution_id": "simstop_other"})
        self.blocked(live_goals=1)
        self.blocked(live_goals=None)

    def test_tool_object_gap_blocks(self):
        self.blocked(tool_object_gap_m=0.2)
        self.blocked(tool_object_gap_m=None)

    def test_checkpoint_contract_blocks(self):
        for field, value in (("stop_confirmed", False), ("object_state", "conveyor"),
                             ("stopped_stage", "lift")):
            with self.subTest(field=field):
                cp = {**self.cp, field: value}
                self.assertTrue(resume_preflight_findings(**ok_inputs(cp)))
        self.assertTrue(resume_preflight_findings(
            **{**ok_inputs(self.cp), "checkpoint": None}))


def forward_stages():
    resources = load_workcell_resources(
        demo.WORKCELL, demo.POSES, state_max_age_sec=0.5,
        mounting_path=demo.MOUNTING, grasp_path=demo.GRASP)
    bindings = demo.build_bindings(resources)
    steps = (TaskStep(skill="pick", args={"object": "mat_a", "from": "loc_pallet_1"}),
             TaskStep(skill="move", args={"to": "loc_conveyor"}),
             TaskStep(skill="place", args={"object": "mat_a", "to": "loc_conveyor"}),
             TaskStep(skill="home"))
    stages, findings = build_stages(steps, bindings=bindings)
    assert not findings
    return stages, bindings


class ResumeStagesTest(unittest.TestCase):
    def test_new_recovery_approach_then_only_remaining_stages(self):
        stages, bindings = forward_stages()
        cp = checkpoint()
        current = dict(cp["joint_state"]["positions"])
        approach, after, reasons = build_resume_stages(stages, cp, current)
        self.assertEqual(reasons, [])
        # 새 경로: 현재 관측 관절에서 시작해 정지 단계 목표 자세에서 끝난다.
        arm_now = {k: v for k, v in current.items() if k.startswith("j")}
        self.assertEqual(approach[0].joint_rad, arm_now)
        self.assertEqual(approach[-1].joint_rad, bindings.poses["conveyor_approach"])
        self.assertEqual(approach[-1].pose_name, "conveyor_approach")
        self.assertTrue(all(s.holds_object == "mat_a" for s in approach))
        for a, b in zip(approach, approach[1:]):
            step = max(abs(a.joint_rad[n] - b.joint_rad[n]) for n in a.joint_rad)
            self.assertLessEqual(step, RESUME_PATH_STEP_RAD + 1e-9)
        # 그 뒤는 체크포인트의 남은 단계만(정지 단계는 복구 접근으로 대체).
        self.assertEqual([s.stage for s in after],
                         ["place_descend", "gripper_open_release", "retreat",
                          "home_end"])

    def test_mismatched_remaining_stages_block(self):
        stages, _ = forward_stages()
        cp = {**checkpoint(), "remaining_stages": ["retreat", "home_end"]}
        _, _, reasons = build_resume_stages(stages, cp,
                                            cp["joint_state"]["positions"])
        self.assertTrue(reasons)

    def test_interpolation_includes_both_ends(self):
        samples = interpolate_joints({"j1": 0.0}, {"j1": 0.12}, step_rad=0.05)
        self.assertEqual(samples[0], {"j1": 0.0})
        self.assertAlmostEqual(samples[-1]["j1"], 0.12)
        self.assertEqual(len(samples), 4)


class PreflightRecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = SimulationDemoState(Path(tmp.name) / "s.json")
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=(0.4, 0, 1),
                              restored=None)
        self.cp = self.state.record_checkpoint(checkpoint())

    def test_default_has_no_preflight_and_resume_unavailable(self):
        status = self.state.status()
        self.assertIsNone(status["resume_preflight"])
        self.assertIs(status["resume_available"], False)

    def test_passed_preflight_is_recorded_but_resume_stays_unavailable(self):
        self.state.record_resume_preflight("material_a", {
            "allowed": True, "checkpoint_id": self.cp["checkpoint_id"],
            "resume_plan_id": "simresume_x", "validated_at": "t",
            "scene_hash": self.cp["scene_hash"], "reasons": []})
        status = self.state.status()
        self.assertIs(status["resume_preflight"]["allowed"], True)
        self.assertEqual(status["resume_preflight"]["resume_plan_id"], "simresume_x")
        self.assertIs(status["resume_preflight"]["resume_executed"], False)
        self.assertIs(status["resume_available"], False)
        self.assertEqual(status["checkpoint_id"], self.cp["checkpoint_id"])

    def test_blocked_preflight_keeps_checkpoint(self):
        self.state.record_resume_preflight("material_a", {
            "allowed": False, "checkpoint_id": self.cp["checkpoint_id"],
            "validated_at": "t", "scene_hash": "y" * 64,
            "reasons": ["scene이 체크포인트와 다르다"]})
        status = self.state.status()
        self.assertIs(status["checkpoint_available"], True)
        self.assertEqual(status["object_state"], OBJECT_HELD)
        self.assertIs(status["resume_preflight"]["allowed"], False)
        self.assertEqual(self.state.checkpoints()["material_a"], self.cp)

    def test_record_requires_current_checkpoint_and_plan_id(self):
        with self.assertRaises(ValueError):
            self.state.record_resume_preflight("material_a", {
                "allowed": False, "checkpoint_id": "simckpt_old"})
        with self.assertRaises(ValueError):
            self.state.record_resume_preflight("material_a", {
                "allowed": True, "checkpoint_id": self.cp["checkpoint_id"]})

    def test_restore_drops_checkpoint_and_preflight(self):
        self.state.record_resume_preflight("material_a", {
            "allowed": True, "checkpoint_id": self.cp["checkpoint_id"],
            "resume_plan_id": "simresume_x"})
        self.state.mark_restored("material_a", verified=True)
        status = self.state.status()
        self.assertIsNone(status["resume_preflight"])
        self.assertFalse(status["checkpoint_available"])


class PreflightSendsNoCommandsTest(unittest.TestCase):
    def test_preflight_path_has_no_command_or_latch_calls(self):
        tree = ast.parse(DEMO_PATH.read_text(encoding="utf-8"))
        forbidden = {"send_arm", "send_arm_async", "send_gripper", "cancel_all",
                     "release", "latch", "attach", "detach", "restore", "place",
                     "follow", "settle", "record_checkpoint", "record_run",
                     "record_return", "mark_restored"}
        for name in ("resume_preflight", "verify_resume", "observe_attachment",
                     "observe_active_goals"):
            function = next(n for n in tree.body
                            if isinstance(n, ast.FunctionDef) and n.name == name)
            with self.subTest(function=name):
                attrs = {c.func.attr for c in ast.walk(function)
                         if isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Attribute)}
                self.assertEqual(attrs & forbidden, set())
        body = ast.unparse(next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                                and n.name == "resume_preflight"))
        self.assertIn("shutdown_ros(", body)
        self.assertIn("'robot_commands_sent': 0", body)

    def test_parser_flag_is_explicit_and_off_by_default(self):
        parser = demo.build_parser()
        self.assertFalse(parser.parse_args(["-", "material_a"]).resume_preflight)
        self.assertTrue(parser.parse_args(
            ["-", "material_a", "--resume-preflight"]).resume_preflight)


if __name__ == "__main__":
    unittest.main(verbosity=2)
