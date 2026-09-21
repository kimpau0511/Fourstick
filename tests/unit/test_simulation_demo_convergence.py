"""STOP 뒤 자재 수렴 확인 후에만 체크포인트를 만드는지 단위 검증.

- 초기 불일치 뒤 수렴 → 체크포인트 생성(저장 pose = 수렴 관측값)
- 끝까지 불일치 → 미생성, `simulation_demo_checkpoint_unavailable` 기록,
  로봇 명령 경로 없음, 자재 기록(reset_required)은 그대로
- fresh 관측이 없는(stale) 표본은 수렴 표본으로 치지 않는다
- follower.halt()는 수렴 확인 **뒤에만** 불린다
- 새 허용 오차·대기값을 만들지 않는다(기존 값 재사용)

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.simulation_demo_state import (  # noqa: E402
    CHECKPOINT_UNAVAILABLE,
    OBJECT_CONVEYOR,
    POLICY_DEMO_HOLD,
    STOPPED_UNRESTORED,
    SimulationDemoState,
)
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    ON_CONVEYOR,
    build,
    converged,
    stopped_result,
)

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_convergence", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

#: 실측 B: 도구 기대 위치와 캡처 pose.
EXPECTED = (0.525860, 0.001649, 1.238641)
LAGGING = (0.542255, 0.008733, 1.157184)   # 83.4 mm 떨어져 있었다
NEAR = (0.525861, 0.001650, 1.238640)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class SequenceFixture:
    """정해 둔 순서로 pose를 돌려준다. None은 '새 메시지 없음'이다."""

    def __init__(self, poses, clock, *, step=0.2, held=("material_b",), log=None):
        self.poses = list(poses)
        self.clock = clock
        self.step = step
        self._held = set(held)
        self.log = log if log is not None else []
        self.fresh_flags: list[bool] = []

    def pose_of(self, model, *, timeout_sec=3.0, fresh=False):
        self.clock.t += self.step
        self.fresh_flags.append(fresh)
        self.log.append("pose_of")
        value = self.poses.pop(0) if self.poses else self.last
        self.last = value
        return value

    def held(self):
        return tuple(sorted(self._held))


class AwaitConvergenceTest(unittest.TestCase):
    def run_wait(self, poses, **kwargs):
        clock = Clock()
        fixture = SequenceFixture(poses, clock)
        result = demo.await_object_convergence(
            fixture=fixture, model="material_b", expected_pose_m=EXPECTED,
            clock=clock, **kwargs)
        return result, fixture

    def test_initial_mismatch_then_converges(self):
        result, fixture = self.run_wait([LAGGING, LAGGING, NEAR, NEAR])
        self.assertTrue(result["converged"])
        self.assertEqual(result["samples"], 4)
        self.assertEqual(result["consecutive_within"], 2)
        self.assertEqual(result["observed_pose_m"], list(NEAR))
        self.assertLessEqual(result["error_m"], demo.FOLLOW_TOLERANCE_M)
        self.assertEqual(result["expected_pose_m"], list(EXPECTED))
        self.assertTrue(all(fixture.fresh_flags))

    def test_never_converges_within_the_bounded_wait(self):
        result, _ = self.run_wait([LAGGING] * 100)
        self.assertFalse(result["converged"])
        self.assertAlmostEqual(result["error_m"], 0.083392, places=5)
        self.assertGreaterEqual(result["settle_elapsed_s"], demo.STOP_SETTLE_TIMEOUT_SEC)
        self.assertEqual(result["observed_pose_m"], list(LAGGING))

    def test_stale_samples_do_not_count(self):
        # 가까운 표본 사이에 '새 메시지 없음'이 끼면 연속으로 치지 않는다.
        result, _ = self.run_wait([NEAR, None] * 50)
        self.assertFalse(result["converged"])
        self.assertLessEqual(result["consecutive_within"], 1)

    def test_single_good_sample_is_not_enough(self):
        result, _ = self.run_wait([NEAR, LAGGING] * 50)
        self.assertFalse(result["converged"])

    def test_unknown_expected_pose_is_not_convergence(self):
        clock = Clock()
        fixture = SequenceFixture([NEAR, NEAR], clock)
        result = demo.await_object_convergence(
            fixture=fixture, model="material_b", expected_pose_m=None, clock=clock)
        self.assertFalse(result["converged"])
        self.assertEqual(result["samples"], 0)

    def test_reuses_existing_bounds(self):
        self.assertEqual(demo.STOP_SETTLE_TIMEOUT_SEC, 5.0)
        self.assertEqual(demo.FOLLOW_SAMPLE_TIMEOUT_SEC, 1.0)
        self.assertEqual(demo.FOLLOW_TOLERANCE_M, 0.05)
        source = DEMO_PATH.read_text(encoding="utf-8")
        self.assertIn("settled_observation(transport, timeout_sec=STOP_SETTLE_TIMEOUT_SEC)",
                      source)
        self.assertIn("timeout_sec=FOLLOW_SAMPLE_TIMEOUT_SEC", source)


def offset_from_expected(distance_m: float) -> tuple:
    """기대 pose에서 z로 distance_m 떨어진 pose."""
    return (EXPECTED[0], EXPECTED[1], EXPECTED[2] - distance_m)


class ToleranceAlignmentTest(unittest.TestCase):
    """수렴 허용치 = min(추종 허용치, resume 자재 pose 허용치)."""

    def run_wait(self, poses):
        clock = Clock()
        fixture = SequenceFixture(poses, clock)
        return demo.await_object_convergence(
            fixture=fixture, model="material_b", expected_pose_m=EXPECTED,
            clock=clock)

    def test_applied_tolerance_is_the_narrower_existing_value(self):
        from validation.simulation_demo_state import RESUME_POSE_TOLERANCE_M

        result = self.run_wait([EXPECTED, EXPECTED])
        self.assertEqual(result["applied_tolerance_m"],
                         min(demo.FOLLOW_TOLERANCE_M, RESUME_POSE_TOLERANCE_M))
        self.assertEqual(result["applied_tolerance_m"], 0.005)
        self.assertEqual(result["follow_tolerance_m"], demo.FOLLOW_TOLERANCE_M)
        self.assertEqual(result["resume_pose_tolerance_m"], RESUME_POSE_TOLERANCE_M)
        self.assertEqual(result["tolerance_m"], result["applied_tolerance_m"])

    def test_4mm_twice_converges(self):
        near = offset_from_expected(0.004)
        result = self.run_wait([near, near])
        self.assertTrue(result["converged"])
        self.assertAlmostEqual(result["error_m"], 0.004, places=6)

    def test_between_5mm_and_50mm_does_not_converge(self):
        for distance in (0.0051, 0.02, 0.049):
            with self.subTest(distance=distance):
                pose = offset_from_expected(distance)
                result = self.run_wait([pose] * 100)
                self.assertFalse(result["converged"])
                checkpoint, reasons = build(model="material_b", object_pose_m=pose,
                                            convergence=result)
                self.assertIsNone(checkpoint)
                self.assertTrue(any(CHECKPOINT_UNAVAILABLE in r for r in reasons))

    def test_b_83mm_case_does_not_converge(self):
        result = self.run_wait([LAGGING] * 100)
        self.assertFalse(result["converged"])
        self.assertAlmostEqual(result["error_m"], 0.083392, places=5)

    def test_converged_checkpoint_passes_resume_pose_check(self):
        """0.004 m에서 캡처해도, 자재가 도구 위치로 마저 수렴한 뒤 resume 재검증의
        pose 조건(체크포인트와 0.005 m 이내)을 통과한다."""
        from validation.simulation_demo_state import resume_preflight_findings

        near = offset_from_expected(0.004)
        record = self.run_wait([near, near])
        checkpoint, reasons = build(model="material_b", object_pose_m=near,
                                    convergence=record)
        self.assertEqual(reasons, [])
        positions = dict(checkpoint["joint_state"]["positions"])
        for current in (near, EXPECTED):
            with self.subTest(current=current):
                found = resume_preflight_findings(
                    checkpoint=checkpoint, model="material_b",
                    scene_hash_now=checkpoint["scene_hash"], object_pose_m=current,
                    attachment_static=True,
                    joint_observation={"positions": positions,
                                       "observed_at": 1e12, "valid": True},
                    preflight_started_at=0.0,
                    gripper_joint="robotiq_85_left_knuckle_joint",
                    latch={"stop_execution_id": checkpoint["stop_execution_id"]},
                    live_goals=0, tool_object_gap_m=0.0,
                    follow_tolerance_m=demo.FOLLOW_TOLERANCE_M)
                self.assertFalse([r for r in found if "자재 pose" in r], found)

class Follower:
    def __init__(self, log):
        self.log = log
        self.samples = []

    def halt(self):
        self.log.append("halt")


class HaltOrderTest(unittest.TestCase):
    def settle(self, *, confirmed=True, capture=True, held=("material_b",)):
        log: list[str] = []
        clock = Clock()
        fixture = SequenceFixture([LAGGING, NEAR, NEAR], clock, held=held, log=log)
        original = demo.await_object_convergence

        def bounded(**kwargs):
            return original(clock=clock, **kwargs)

        demo.await_object_convergence = bounded
        try:
            result = demo.settle_held_object(
                follower=Follower(log), fixture=fixture, model="material_b",
                confirmed=confirmed, capture=capture, expected_pose_m=EXPECTED)
        finally:
            demo.await_object_convergence = original
        return result, log

    def test_halt_only_after_convergence(self):
        result, log = self.settle()
        self.assertTrue(result["converged"])
        self.assertEqual(log, ["pose_of", "pose_of", "pose_of", "halt"])

    def test_no_wait_when_not_capturing_or_unconfirmed_or_not_held(self):
        for kwargs in (dict(capture=False), dict(confirmed=False), dict(held=())):
            with self.subTest(**kwargs):
                result, log = self.settle(**kwargs)
                self.assertIsNone(result)
                self.assertEqual(log, ["halt"])

    def test_wait_path_takes_no_transport(self):
        import inspect

        for fn in (demo.await_object_convergence, demo.settle_held_object):
            with self.subTest(fn=fn.__name__):
                self.assertNotIn("transport", inspect.signature(fn).parameters)


class BuildCheckpointConvergenceTest(unittest.TestCase):
    def test_held_without_convergence_is_unavailable(self):
        for record in (None, converged(LAGGING, ok=False, error=0.083392)):
            with self.subTest(record=record is not None):
                checkpoint, reasons = build(model="material_b", object_pose_m=LAGGING,
                                            convergence=record)
                self.assertIsNone(checkpoint)
                self.assertTrue(any(CHECKPOINT_UNAVAILABLE in r for r in reasons))

    def test_saved_pose_must_be_the_converged_observation(self):
        checkpoint, reasons = build(model="material_b", object_pose_m=LAGGING,
                                    convergence=converged(NEAR))
        self.assertIsNone(checkpoint)
        self.assertTrue(any("수렴 관측과" in r for r in reasons))

    def test_converged_checkpoint_carries_the_record(self):
        record = converged(NEAR)
        checkpoint, reasons = build(model="material_b", object_pose_m=NEAR,
                                    convergence=record)
        self.assertEqual(reasons, [])
        self.assertEqual(checkpoint["convergence"], record)
        self.assertEqual(checkpoint["object_pose_m"], [round(v, 6) for v in NEAR])

    def test_object_not_held_needs_no_convergence(self):
        checkpoint, reasons = build(held=False, object_pose_m=ON_CONVEYOR,
                                    convergence=None)
        self.assertEqual(reasons, [])
        self.assertEqual(checkpoint["object_state"], OBJECT_CONVEYOR)


class CaptureAndRecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = SimulationDemoState(Path(tmp.name) / "s.json")
        # STOP 판정은 먼저 기록된다(reset_required).
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_b",
                              result=stopped_result(), final_pose_m=LAGGING,
                              restored=None)
        self.saved = dict(demo.RUN_INFO)
        self.addCleanup(lambda: (demo.RUN_INFO.clear(),
                                 demo.RUN_INFO.update(self.saved)))
        self.stages = [types.SimpleNamespace(stage=name) for name in (
            "home_start", "pick_approach", "pre_grasp", "gripper_open_before_grasp",
            "grasp_approach", "gripper_close", "lift", "place_approach",
            "place_descend", "gripper_open_release", "retreat", "home_end")]

    def capture(self, convergence):
        cp = build()[0]
        fixture = types.SimpleNamespace(held=lambda: ("material_b",),
                                        pose_of=self.fail_pose_of)
        client = types.SimpleNamespace(
            snapshot=lambda: types.SimpleNamespace(content_hash="h" * 64))
        observation = types.SimpleNamespace(
            positions=cp["joint_state"]["positions"], valid=True,
            observed_at=cp["joint_state"]["observed_at"])
        checkpoint = demo.capture_stop_checkpoint(
            mode="forward", model="material_b", object_id="mat_b",
            source_id="loc_pallet_2", destination_id="loc_conveyor",
            origin_slot_id="loc_pallet_2", stages=self.stages,
            stopped_stage="place_approach",
            stage_records=[{"stage": s.stage, "reached": True}
                           for s in self.stages[:7]],
            confirmed=True, stop_execution_id="simstop_b",
            stop_requested_at=cp["joint_state"]["observed_at"] - 1.0,
            observation=observation, gripper_joint="robotiq_85_left_knuckle_joint",
            fixture=fixture, client=client, scene_hash_at_stop="h" * 64,
            conveyor_zone=None, origin_home_m=(0.5, 0.0, 0.84),
            convergence=convergence)
        demo.record_stop_checkpoint(self.state, checkpoint)
        return checkpoint

    @staticmethod
    def fail_pose_of(*args, **kwargs):
        raise AssertionError("든 자재의 저장 pose는 수렴 관측값이어야 한다(다시 읽지 않음)")

    def test_unconverged_records_unavailable_and_keeps_reset(self):
        record = {**converged(LAGGING, ok=False, error=0.083392),
                  "expected_pose_m": list(EXPECTED), "settle_elapsed_s": 5.0}
        self.assertIsNone(self.capture(record))
        status = self.state.status()
        self.assertFalse(status["checkpoint_available"])
        self.assertTrue(status["simulation_demo_reset_required"])
        self.assertEqual(status["objects"]["material_b"]["state"], STOPPED_UNRESTORED)
        row = status["checkpoint_unavailable"]["material_b"]
        self.assertEqual(row["reason_code"], CHECKPOINT_UNAVAILABLE)
        for key in ("expected_pose_m", "observed_pose_m", "error_m", "samples",
                    "settle_elapsed_s"):
            self.assertIsNotNone(row["convergence"][key], key)
        self.assertEqual(demo.RUN_INFO["stop_checkpoint"]["reason_code"],
                         CHECKPOINT_UNAVAILABLE)

    def test_converged_creates_checkpoint_with_record(self):
        record = {**converged(NEAR), "expected_pose_m": list(EXPECTED)}
        checkpoint = self.capture(record)
        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["object_pose_m"], [round(v, 6) for v in NEAR])
        status = self.state.status()
        self.assertTrue(status["checkpoint_available"])
        self.assertEqual(status["checkpoint"]["convergence"]["samples"], 2)
        self.assertEqual(status["checkpoint_unavailable"], {})
        self.assertEqual(demo.RUN_INFO["stop_checkpoint"]["convergence"], record)


if __name__ == "__main__":
    unittest.main(verbosity=2)
