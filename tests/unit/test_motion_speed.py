"""단계 이동 시간 정책·웹 속도 설정·실행기 연결 (2026-10-06)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.motion_speed import MotionSpeedPolicy  # noqa: E402
from core.policy import PolicyError  # noqa: E402
from server.api import ApiError  # noqa: E402
from server.sim_demo_jobs import SimDemoJobs, SimDemoJobError  # noqa: E402
from server.sim_demo_motion import MotionSettings  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL, FakePopen  # noqa: E402

MOTION_PATH = ROOT / "config/workcell/fr3_2f85_workcell_motion.json"
CONFIG = json.loads(MOTION_PATH.read_text(encoding="utf-8"))
LIMITS = {f"j{i}": v for i, v in enumerate((3.15, 3.15, 3.15, 3.2, 3.2, 3.2), start=1)}
HOME = {name: 0.0 for name in LIMITS}

_spec = importlib.util.spec_from_file_location(
    "demo_for_motion_speed", ROOT / "scripts/demo_workcell_pick_place.py")
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


def policy() -> MotionSpeedPolicy:
    return MotionSpeedPolicy.from_config(CONFIG)


class PolicyTest(unittest.TestCase):
    def test_registered_config_loads(self):
        p = policy()
        self.assertEqual((p.min_percent, p.default_percent, p.max_percent, p.step_percent),
                         (0, 100, 100, 10))
        self.assertLessEqual(p.max_fraction_of_joint_limit, 1.0)

    def test_config_without_provenance_is_refused(self):
        with self.assertRaises(PolicyError):
            MotionSpeedPolicy.from_config({k: v for k, v in CONFIG.items()
                                           if k != "provenance"})

    def test_percent_must_be_on_the_grid(self):
        p = policy()
        self.assertEqual(p.check_percent(70), 70)
        for bad in (-10, 5, 55, 110, "50", 50.0, True, None):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                p.check_percent(bad)

    def test_largest_joint_move_sets_the_time(self):
        p = policy()
        target = dict(HOME, j1=2.0, j4=0.1)
        allowed = 3.15 * p.max_fraction_of_joint_limit * 0.5
        expected = max(p.peak_factor * 2.0 / allowed,
                       math.sqrt((10 * math.sqrt(3) / 3) * 2.0 / (allowed * p.acceleration_factor_per_sec)))
        self.assertAlmostEqual(p.arm_seconds(HOME, target, LIMITS, 50), expected)
        self.assertLess(p.arm_seconds(HOME, target, LIMITS, 100), expected)
        for percent in (10, 50, 100):
            seconds = p.arm_seconds(HOME, target, LIMITS, percent)
            self.assertLessEqual(p.peak_factor * 2 / seconds, 3.15 * .8 * percent / 100 + 1e-9)
            self.assertLessEqual((10 * math.sqrt(3) / 3) * 2 / seconds**2, 3.15 * .5 * .8 * percent / 100 + 1e-9)

    def test_zero_is_stored_but_never_used_in_duration_division(self):
        p = policy()
        self.assertEqual(p.check_percent(0), 0)
        with self.assertRaises(PolicyError):
            p.arm_seconds(HOME, dict(HOME, j1=1), LIMITS, 0)
        with self.assertRaises(PolicyError):
            p.gripper_seconds(0, .7, 0)

    def test_peak_velocity_stays_within_the_allowed_fraction(self):
        p = policy()
        target = dict(HOME, j2=2.5)
        seconds = p.arm_seconds(HOME, target, LIMITS, p.max_percent)
        peak = p.peak_factor * 2.5 / seconds
        self.assertLessEqual(peak, 3.15 * p.max_fraction_of_joint_limit + 1e-9)

    def test_small_moves_keep_the_minimum_time(self):
        p = policy()
        self.assertEqual(p.arm_seconds(HOME, dict(HOME, j1=0.001), LIMITS, 100),
                         p.min_arm_seconds)
        self.assertEqual(p.gripper_seconds(0.4, 0.4, 100), p.min_gripper_seconds)

    def test_missing_limit_or_start_is_refused(self):
        p = policy()
        with self.assertRaises(PolicyError):
            p.arm_seconds(HOME, {"j7": 0.1}, LIMITS, 50)
        with self.assertRaises(PolicyError):
            p.arm_seconds({}, {"j1": 0.1}, LIMITS, 50)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.settings = MotionSettings(policy(), self.tmp / "motion.json", clock=lambda: 1.0)

    def test_default_until_saved(self):
        self.assertEqual(self.settings.percent(), 100)
        saved = self.settings.set_percent(80)
        self.assertEqual(saved["speed_percent"], 80)
        self.assertEqual(saved["applies_to"], "next_job")
        self.assertEqual(self.settings.percent(), 80)

    def test_failed_atomic_write_preserves_saved_value(self):
        from unittest.mock import patch
        self.settings.set_percent(30)
        with patch("server.sim_demo_motion.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.settings.set_percent(80)
        self.assertEqual(self.settings.percent(), 30)

    def test_out_of_policy_value_is_not_saved(self):
        self.settings.set_percent(30)
        with self.assertRaises(PolicyError):
            self.settings.set_percent(120)
        self.assertEqual(self.settings.percent(), 30)

    def test_broken_or_foreign_file_is_not_invented(self):
        for content in ('{broken', '{"speed_percent": 999}', '[]'):
            self.settings.path.write_text(content)
            with self.assertRaises(PolicyError):
                self.settings.percent()


class JobsArgvTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.popen = FakePopen()

    def jobs(self, motion):
        return SimDemoJobs(workcell=WORKCELL, state_path=self.tmp / "state.json",
                           jobs_dir=self.tmp / "jobs", stop_request=self.tmp / "stop.json",
                           popen=self.popen, environ={"PATH": "/usr/bin"}, motion=motion)

    def test_saved_speed_is_passed_to_the_next_job(self):
        settings = MotionSettings(policy(), self.tmp / "motion.json")
        settings.set_percent(70)
        job = self.jobs(settings).start("transfer", "material_b")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--speed-percent") + 1], "70")
        self.assertEqual(job["speed_percent"], 70)

    def test_without_settings_the_executor_keeps_fixed_times(self):
        job = self.jobs(None).start("transfer", "material_b")
        self.assertNotIn("--speed-percent", self.popen.calls[-1]["argv"])
        self.assertIsNone(job["speed_percent"])


    def test_zero_blocks_launch_without_stop(self):
        settings = MotionSettings(policy(), self.tmp / "motion.json")
        settings.set_percent(0)
        with self.assertRaises(SimDemoJobError) as caught:
            self.jobs(settings).start("transfer", "material_b")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.popen.calls, [])
        self.assertFalse((self.tmp / "stop.json").exists())

    def test_running_job_keeps_speed_and_next_job_reads_new_speed(self):
        settings = MotionSettings(policy(), self.tmp / "motion.json")
        settings.set_percent(50)
        jobs = self.jobs(settings)
        first = jobs.start("transfer", "material_b")
        settings.set_percent(80)
        self.assertEqual(jobs.running()["speed_percent"], 50)
        self.assertEqual(first["argv"][first["argv"].index("--speed-percent")+1], "50")
        self.popen.procs[-1].code = 0
        jobs.running()
        second = jobs.start("transfer", "material_a")
        self.assertEqual(second["speed_percent"], 80)

    def test_general_snapshot_overrides_later_setting(self):
        settings = MotionSettings(policy(), self.tmp / "motion.json")
        settings.set_percent(50)
        snapshot = settings.snapshot()
        settings.set_percent(0)
        jobs = self.jobs(settings)
        job = jobs.start("transfer", "material_b", speed_percent=snapshot)
        self.assertEqual(job["speed_percent"], 50)


def body(payload):
    async def read(_receive):
        return payload
    return read


class RouteTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = MotionSettings(policy(), Path(tmp.name) / "motion.json")
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=types.SimpleNamespace(motion=settings))

    def call(self, method, payload=None):
        from server.routes import sim_demo

        ctx = types.SimpleNamespace(runtime=self.runtime, read_body=body(payload or {}))
        return asyncio.run(sim_demo.handle(ctx, method, "/v1/sim-demo/motion", None, {}))

    def test_get_and_set(self):
        status, _, raw = self.call("GET")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["speed_percent"], 100)
        status, _, raw = self.call("POST", {"speed_percent": 90})
        self.assertEqual(json.loads(raw)["speed_percent"], 90)

    def test_bad_value_is_400_and_does_not_change_storage(self):
        for value in (-10, 110, 45, True, "50", 50.0):
            with self.subTest(value=value), self.assertRaises(ApiError) as caught:
                self.call("POST", {"speed_percent": value})
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(json.loads(self.call("GET")[2])["speed_percent"], 100)

    def test_zero_is_supported_by_server(self):
        self.call("POST", {"speed_percent": 0})
        self.assertEqual(json.loads(self.call("GET")[2])["speed_percent"], 0)

    def test_storage_failure_is_not_reported_as_success(self):
        from unittest.mock import patch
        with patch("server.sim_demo_motion.os.replace", side_effect=OSError("fixture")):
            with self.assertRaises(ApiError) as caught:
                self.call("POST", {"speed_percent": 80})
        self.assertEqual(caught.exception.status, 503)

    def test_unavailable_without_policy(self):
        self.runtime.sim_demo_jobs.motion = None
        with self.assertRaises(ApiError) as caught:
            self.call("GET")
        self.assertEqual(caught.exception.status, 503)


class FakeTransport:
    def __init__(self, positions, valid=True):
        self.positions, self.valid = positions, valid

    def joint_observation(self, timeout_sec, *, after=None):
        return types.SimpleNamespace(valid=self.valid, positions=dict(self.positions))


class ExecutorTimingTest(unittest.TestCase):
    def test_without_argument_uses_declared_policy(self):
        timing = demo.StageTiming()
        target = dict(HOME, j1=.01)
        self.assertAlmostEqual(timing.arm(FakeTransport(HOME), target),
                               policy().arm_seconds(HOME, target, LIMITS, 100))
        self.assertEqual(timing.report()["mode"], "joint_velocity")

    def test_time_comes_from_observed_joints(self):
        timing = demo.StageTiming()
        timing.configure(100)
        p = policy()
        start = dict(HOME, robotiq_85_left_knuckle_joint=0.0)
        seconds = timing.arm(FakeTransport(start), dict(HOME, j1=1.0))
        self.assertAlmostEqual(seconds, p.arm_seconds(HOME, dict(HOME, j1=1.0), LIMITS, 100))
        self.assertAlmostEqual(timing.gripper(FakeTransport(start), 0.7),
                               p.gripper_seconds(0.0, 0.7, 100))
        self.assertEqual([s["source"] for s in timing.report()["stages"]],
                         ["computed", "computed"])

    def test_no_observation_blocks_instead_of_fixed_time(self):
        timing = demo.StageTiming()
        timing.configure(100)
        with self.assertRaises(PolicyError):
            timing.arm(FakeTransport({}, valid=False), dict(HOME, j1=.1))
        with self.assertRaises(PolicyError):
            timing.gripper(FakeTransport({}, valid=False), .7)

    def test_bad_percent_is_refused(self):
        with self.assertRaises(PolicyError):
            demo.StageTiming().configure(55)

    def test_argument_is_registered(self):
        args = demo.build_parser().parse_args(["s", "o", "--speed-percent", "60"])
        self.assertEqual(args.speed_percent, 60)
        self.assertIsNone(demo.build_parser().parse_args(["s", "o"]).speed_percent)


class GoalObservationTest(unittest.TestCase):
    def test_elapsed_time_does_not_replace_complete_valid_observation(self):
        for positions, valid in (({"j1": 0}, True), ({"j1": 0, "j2": float("nan")}, True),
                                 ({"j1": 0, "j2": 0}, False)):
            observation = types.SimpleNamespace(positions=positions, valid=valid)
            self.assertTrue(math.isnan(demo.observed_goal_error(observation, {"j1": 0, "j2": 0})))
        observation = types.SimpleNamespace(positions={"j1": 0, "j2": .5}, valid=True)
        self.assertEqual(demo.observed_goal_error(observation, {"j1": 0, "j2": .5}), 0)


class TrajectoryTest(unittest.TestCase):
    def test_same_duration_and_rest_boundary_derivatives_are_sent(self):
        from unittest.mock import patch
        from robots.fr3_gazebo.ros_transport import RosWorkcellTransport
        builtin = types.ModuleType("builtin_interfaces.msg")
        builtin.Duration = lambda **kw: types.SimpleNamespace(**kw)
        trajectory = types.ModuleType("trajectory_msgs.msg")
        trajectory.JointTrajectoryPoint = types.SimpleNamespace
        t = RosWorkcellTransport(world_name="fixture", gz_partition="fixture", ros_domain_id=44)
        t.joint_observation = lambda timeout, **kw: types.SimpleNamespace(valid=True, positions={"j1": 0.2})
        seconds = policy().arm_seconds({"j1": .2}, {"j1": 1.2}, {"j1": 3.15}, 50)
        with patch.dict(sys.modules, {"builtin_interfaces.msg": builtin, "trajectory_msgs.msg": trajectory}):
            points = t._trajectory_points(["j1"], [1.2], seconds, 20)
            self.assertEqual([points[0].positions, points[-1].positions], [[.2], [1.2]])
            self.assertEqual([points[0].velocities, points[-1].velocities], [[0], [0]])
            self.assertEqual([points[0].accelerations, points[-1].accelerations], [[0], [0]])
            self.assertGreater(points[2].velocities[0], 0)
            for point in points:
                self.assertLessEqual(abs(point.velocities[0]), 3.15*.8*.5+1e-9)
                self.assertLessEqual(abs(point.accelerations[0]), 3.15*.5*.8*.5+1e-9)
            self.assertAlmostEqual(points[-1].time_from_start.sec + points[-1].time_from_start.nanosec / 1e9, seconds)
            t.joint_observation = lambda timeout, **kw: types.SimpleNamespace(valid=False, positions={})
            self.assertIsNone(t._trajectory_points(["j1"], [1.2], seconds, 20))



if __name__ == "__main__":
    unittest.main()
