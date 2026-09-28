"""시뮬레이션 시연의 **외부 정지 요청**(웹 STOP) 단위 검증.

- 요청 파일은 이 실행이 시작된 뒤 쓰인 것만 인정하고, 처리한 요청만 지운다
- transport `_send(should_stop=...)`: 정지 요청이면 결과를 기다리지 않고 돌아오고
  goal은 추적 목록에 남는다(cancel_all이 취소). 없으면 기존 동작
- resume 루프: 이동 중 외부 정지 → 기존 STOP 절차(cancel·정지 확인·수렴·새
  체크포인트), 단계 전 외부 정지 → 이동 명령 없이 STOP 절차

**ROS 없이** 돈다. ROS 메시지 모듈은 스텁이다.
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

from robots.fr3_gazebo.ros_transport import (  # noqa: E402
    STOP_REQUESTED_DETAIL,
    RosWorkcellTransport,
)
from tests.unit.test_ros_transport_goal_tracking import FakeHandle  # noqa: E402
from tests.unit.test_simulation_demo_resume_run import (  # noqa: E402
    FakeTransport,
    ResumeRunBase,
)

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_external_stop", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


class ExternalStopRequestTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "stop.json"

    def write(self, request_id, at):
        self.path.write_text(json.dumps({"request_id": request_id,
                                         "requested_at": at}), encoding="utf-8")

    def test_only_requests_after_start_count(self):
        monitor = demo.ExternalStopRequest(self.path, since=100.0)
        self.assertFalse(monitor.requested())
        self.write("old", 99.0)
        self.assertFalse(monitor.requested())
        self.write("new", 101.0)
        self.assertTrue(monitor.requested())
        self.assertEqual(monitor.seen["request_id"], "new")

    def test_consume_removes_only_the_handled_request(self):
        monitor = demo.ExternalStopRequest(self.path, since=0.0)
        self.write("a", 1.0)
        self.assertTrue(monitor.requested())
        self.write("b", 2.0)   # 처리 중 새 요청
        monitor.consume()
        self.assertTrue(self.path.exists())
        self.write("a", 1.0)
        monitor.consume()
        self.assertFalse(self.path.exists())

    def test_module_monitor_is_inert_until_main_starts(self):
        self.assertEqual(demo.EXTERNAL_STOP.since, float("inf"))


def install_ros_message_stubs(test):
    """_send가 함수 안에서 import하는 ROS 메시지 모듈의 스텁."""
    names = ("builtin_interfaces", "builtin_interfaces.msg", "control_msgs",
             "control_msgs.action", "trajectory_msgs", "trajectory_msgs.msg")
    saved = {name: sys.modules.get(name) for name in names}

    class Duration:
        def __init__(self, **kwargs):
            pass

    class Goal:
        def __init__(self):
            self.trajectory = types.SimpleNamespace(joint_names=[], points=[])

    builtin = types.ModuleType("builtin_interfaces.msg")
    builtin.Duration = Duration
    control = types.ModuleType("control_msgs.action")
    control.FollowJointTrajectory = types.SimpleNamespace(Goal=Goal)
    trajectory = types.ModuleType("trajectory_msgs.msg")
    trajectory.JointTrajectoryPoint = lambda: types.SimpleNamespace()
    sys.modules.update({
        "builtin_interfaces": types.ModuleType("builtin_interfaces"),
        "builtin_interfaces.msg": builtin,
        "control_msgs": types.ModuleType("control_msgs"),
        "control_msgs.action": control,
        "trajectory_msgs": types.ModuleType("trajectory_msgs"),
        "trajectory_msgs.msg": trajectory})

    def restore():
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    test.addCleanup(restore)


class FakeClient:
    def __init__(self, handle):
        self.handle = handle

    def wait_for_server(self, timeout_sec):
        return True

    def send_goal_async(self, goal):
        future = types.SimpleNamespace(done=lambda: True,
                                       result=lambda: self.handle)
        return future


class TransportShouldStopTest(unittest.TestCase):
    def setUp(self):
        install_ros_message_stubs(self)
        self.transport = RosWorkcellTransport(world_name="w", gz_partition="p",
                                              ros_domain_id=44)
        self.transport._ensure_node = lambda: None
        self.handle = FakeHandle(1)
        self.handle.accepted = True

    def test_stop_request_returns_without_waiting_and_keeps_goal_tracked(self):
        calls = {"n": 0}

        def should_stop():
            calls["n"] += 1
            return calls["n"] >= 3

        started = time.monotonic()
        outcome = self.transport._send(FakeClient(self.handle), ["j1"], [0.1],
                                       6.0, 2.0, should_stop=should_stop)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(outcome.detail, STOP_REQUESTED_DETAIL)
        self.assertFalse(outcome.result_received)
        self.assertEqual(self.transport.live_goals(), 1)

    def test_without_should_stop_waits_for_result_as_before(self):
        result = types.SimpleNamespace(status=4,
                                       result=types.SimpleNamespace(error_code=0))
        self.handle.result_future.finish(result)
        outcome = self.transport._send(FakeClient(self.handle), ["j1"], [0.1],
                                       6.0, 2.0)
        self.assertTrue(outcome.result_received)
        self.assertEqual(outcome.error_code, 0)


class ResumeExternalStopTest(ResumeRunBase):
    def test_external_stop_during_motion_runs_the_stop_procedure(self):
        state = {"after_first": False}
        transport = FakeTransport(self.cp["joint_state"]["positions"])
        original = transport.send_arm

        def send_arm(target, seconds, timeout, **kwargs):
            result = original(target, seconds, timeout, **kwargs)
            state["after_first"] = True   # 첫 이동 중 웹 STOP이 왔다
            return result

        transport.send_arm = send_arm
        ctx = self.context(transport=transport)
        ctx.external_stop = lambda: state["after_first"]
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_stopped")
        self.assertEqual(ctx.commands, ["send_arm", "cancel_all"])
        self.assertNotIn("send_arm_async", ctx.commands)
        self.assertTrue(report["new_checkpoint_id"])
        self.assertEqual(self.state.status()["checkpoint_id"],
                         report["new_checkpoint_id"])
        self.assertEqual(self.latch.latched()["stage"], "place_approach")

    def test_external_stop_before_stage_sends_no_motion(self):
        ctx = self.context()
        ctx.external_stop = lambda: True
        report = demo.run_resume(ctx, checkpoint_id=self.cp["checkpoint_id"])
        self.assertEqual(report["status"], "resume_stopped")
        self.assertEqual(ctx.commands, ["cancel_all"])


class LoopWiringTest(unittest.TestCase):
    def test_every_motion_send_in_forward_and_return_watches_external_stop(self):
        import ast

        tree = ast.parse(DEMO_PATH.read_text(encoding="utf-8"))
        for name in ("main", "return_held_to_origin"):
            function = next(n for n in tree.body
                            if isinstance(n, ast.FunctionDef) and n.name == name)
            sends = [c for c in ast.walk(function) if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute)
                     and c.func.attr in ("send_arm", "send_gripper")]
            with self.subTest(function=name):
                self.assertTrue(sends)
                for call in sends:
                    self.assertIn("should_stop", {k.arg for k in call.keywords})


if __name__ == "__main__":
    unittest.main(verbosity=2)
