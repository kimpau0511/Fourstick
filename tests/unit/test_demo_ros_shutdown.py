"""시뮬레이션 시연의 ROS 종료 순서와 원본 로그 보존 단위 검증.

- 종료 순서: transport.disconnect → fixture·node 정리 → rclpy.shutdown
- disconnect 예외·미초기화에서도 shutdown까지 간다
- rclpy는 스크립트가 transport보다 먼저 올린다(transport가 소유하면
  disconnect가 shutdown까지 해 순서가 깨진다)
- 정방향·복귀 종료 경로가 모두 이 정리 함수를 쓴다
- fd 1·2 원본 출력 전체가 실행별 로그 파일에 남고 보고서에 경로가 적힌다

**ROS·Gazebo 없이** 돈다. rclpy·transport·node는 기록용 대역이다.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.ros_transport import RosWorkcellTransport  # noqa: E402

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_shutdown", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


class Recorder:
    def __init__(self):
        self.calls: list[str] = []


class FakeRclpy:
    def __init__(self, calls, *, ok=True, fail=False):
        self.calls, self._ok, self._fail = calls, ok, fail

    def ok(self):
        return self._ok

    def shutdown(self):
        self.calls.append("rclpy.shutdown")
        if self._fail:
            raise RuntimeError("shutdown 실패")
        self._ok = False


def thing(calls, label, method, *, fail=False):
    def action():
        calls.append(label)
        if fail:
            raise RuntimeError(f"{label} 실패")
    return types.SimpleNamespace(**{method: action})


class ShutdownOrderTest(unittest.TestCase):
    def run_shutdown(self, *, transport=True, disconnect_fails=False,
                     node_fails=False, ok=True):
        rec = Recorder()
        errors = demo.shutdown_ros(
            transport=(thing(rec.calls, "transport.disconnect", "disconnect",
                             fail=disconnect_fails) if transport else None),
            fixture=thing(rec.calls, "fixture.close", "close"),
            node=thing(rec.calls, "node.destroy_node", "destroy_node",
                       fail=node_fails),
            rclpy_module=FakeRclpy(rec.calls, ok=ok))
        return rec.calls, errors

    def test_order_is_disconnect_cleanup_shutdown(self):
        calls, errors = self.run_shutdown()
        self.assertEqual(calls, ["transport.disconnect", "fixture.close",
                                 "node.destroy_node", "rclpy.shutdown"])
        self.assertEqual(errors, [])

    def test_disconnect_exception_still_reaches_shutdown(self):
        calls, errors = self.run_shutdown(disconnect_fails=True)
        self.assertEqual(calls, ["transport.disconnect", "fixture.close",
                                 "node.destroy_node", "rclpy.shutdown"])
        self.assertEqual(len(errors), 1)
        self.assertIn("transport.disconnect", errors[0])

    def test_cleanup_exception_still_reaches_shutdown(self):
        calls, errors = self.run_shutdown(node_fails=True)
        self.assertEqual(calls[-1], "rclpy.shutdown")
        self.assertIn("node.destroy_node", errors[0])

    def test_missing_transport_still_reaches_shutdown(self):
        calls, errors = self.run_shutdown(transport=False)
        self.assertEqual(calls, ["fixture.close", "node.destroy_node",
                                 "rclpy.shutdown"])
        self.assertEqual(errors, [])

    def test_transport_without_disconnect_is_skipped(self):
        rec = Recorder()
        demo.shutdown_ros(transport=object(), fixture=None, node=None,
                          rclpy_module=FakeRclpy(rec.calls))
        self.assertEqual(rec.calls, ["rclpy.shutdown"])

    def test_already_shut_down_rclpy_is_not_shut_down_again(self):
        calls, errors = self.run_shutdown(ok=False)
        self.assertNotIn("rclpy.shutdown", calls)
        self.assertEqual(errors, [])

    def test_real_transport_disconnect_uninitialised_is_safe(self):
        transport = RosWorkcellTransport(world_name="w", gz_partition="p",
                                         ros_domain_id=44)
        rec = Recorder()
        errors = demo.shutdown_ros(transport=transport, fixture=None, node=None,
                                   rclpy_module=FakeRclpy(rec.calls))
        self.assertEqual(errors, [])
        self.assertEqual(rec.calls, ["rclpy.shutdown"])

    def test_real_transport_disconnect_stops_spin_before_script_cleanup(self):
        """transport가 rclpy를 소유하지 않으면 disconnect는 shutdown하지 않는다."""
        rec = Recorder()
        transport = RosWorkcellTransport(world_name="w", gz_partition="p",
                                         ros_domain_id=44)
        transport._executor = thing(rec.calls, "executor.shutdown", "shutdown")
        transport._node = thing(rec.calls, "transport_node.destroy_node",
                                "destroy_node")
        transport._started_rclpy = False
        demo.shutdown_ros(
            transport=transport,
            fixture=thing(rec.calls, "fixture.close", "close"),
            node=thing(rec.calls, "node.destroy_node", "destroy_node"),
            rclpy_module=FakeRclpy(rec.calls))
        self.assertEqual(rec.calls, [
            "executor.shutdown", "transport_node.destroy_node",
            "fixture.close", "node.destroy_node", "rclpy.shutdown"])


class ScriptInitialisesRclpyFirstTest(unittest.TestCase):
    """`scene_and_transport`가 transport 연결 **전에** rclpy를 올린다."""

    def test_rclpy_init_precedes_transport_connect(self):
        calls: list[str] = []
        state = {"ok": False}
        rclpy = types.ModuleType("rclpy")
        rclpy.ok = lambda: state["ok"]

        def init():
            calls.append("rclpy.init")
            state["ok"] = True
        rclpy.init = init
        node_mod = types.ModuleType("rclpy.node")
        node_mod.Node = lambda name: types.SimpleNamespace(
            name=name, destroy_node=lambda: None)

        class Transport:
            def __init__(self, **kwargs):
                pass

            def connect(self, timeout):
                calls.append(f"transport.connect(rclpy.ok={state['ok']})")
                return types.SimpleNamespace(world_present=True, detail="")

        transport_mod = types.ModuleType("robots.fr3_gazebo.ros_transport")
        transport_mod.RosWorkcellTransport = Transport
        client_mod = types.ModuleType("robots.moveit.ros_client")

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            def wait(self, timeout_sec):
                return True
        client_mod.RosPlanningSceneClient = Client
        saved = {name: sys.modules.get(name) for name in (
            "rclpy", "rclpy.node", "robots.fr3_gazebo.ros_transport",
            "robots.moveit.ros_client")}
        sys.modules.update({"rclpy": rclpy, "rclpy.node": node_mod,
                            "robots.fr3_gazebo.ros_transport": transport_mod,
                            "robots.moveit.ros_client": client_mod})
        try:
            resources = types.SimpleNamespace(
                world_name="w", gz_partition="p", ros_domain_id=44,
                workcell_id="c", workcell_version="v")
            demo.scene_and_transport(resources, {})
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertEqual(calls, ["rclpy.init", "transport.connect(rclpy.ok=True)"])


class ExitPathsUseShutdownRosTest(unittest.TestCase):
    """정방향(`main`)·복귀(`return_held_to_origin`) 종료 경로."""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(DEMO_PATH.read_text(encoding="utf-8"))
        cls.functions = {node.name: node for node in tree.body
                         if isinstance(node, ast.FunctionDef)}

    def calls_in(self, name):
        return [node for node in ast.walk(self.functions[name])
                if isinstance(node, ast.Call)]

    def test_each_exit_path_calls_shutdown_ros_with_all_three(self):
        for name in ("main", "return_held_to_origin"):
            with self.subTest(path=name):
                found = [c for c in self.calls_in(name)
                         if isinstance(c.func, ast.Name)
                         and c.func.id == "shutdown_ros"]
                self.assertEqual(len(found), 1)
                keywords = {k.arg: getattr(k.value, "id", None)
                            for k in found[0].keywords}
                self.assertEqual(keywords, {"transport": "transport",
                                            "fixture": "fixture", "node": "node"})

    def test_no_direct_rclpy_shutdown_or_node_destroy_in_exit_paths(self):
        for name in ("main", "return_held_to_origin"):
            with self.subTest(path=name):
                attrs = [c.func.attr for c in self.calls_in(name)
                         if isinstance(c.func, ast.Attribute)]
                self.assertNotIn("destroy_node", attrs)
                direct = [c for c in self.calls_in(name)
                          if isinstance(c.func, ast.Attribute)
                          and c.func.attr == "shutdown"
                          and getattr(c.func.value, "id", "") == "rclpy"]
                self.assertEqual(direct, [])

    def test_shutdown_happens_after_the_verdict_is_recorded(self):
        """정리는 판정 기록 뒤다 — 정리 결과가 판정을 바꾸지 않는다."""
        body = ast.unparse(self.functions["return_held_to_origin"])
        self.assertLess(body.index("demo_state.record_return("),
                        body.index("shutdown_ros("))
        main = ast.unparse(self.functions["main"])
        self.assertLess(main.index("write(args.out, result)"),
                        main.index("shutdown_ros("))


class RawLogTest(unittest.TestCase):
    def test_all_fd_output_is_kept_including_thread_exceptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = textwrap.dedent(f"""
                import importlib.util, os, sys, threading
                from pathlib import Path
                sys.path.insert(0, {str(ROOT)!r})
                spec = importlib.util.spec_from_file_location("d", {str(DEMO_PATH)!r})
                d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
                info = d.start_raw_log(Path({tmp!r}), label="test")
                print("stdout-line")
                print("stderr-line", file=sys.stderr)
                os.write(2, b"[INFO] fd2-direct\\n")
                def boom():
                    raise RuntimeError("spin-thread-error")
                t = threading.Thread(target=boom, name="spin"); t.start(); t.join()
                print("PATH=" + info["path"])
                sys.stdout.flush(); sys.stderr.flush()
                os._exit(0)
            """)
            done = subprocess.run([sys.executable, "-c", child],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            # 화면(원래 fd)으로도 그대로 나간다.
            self.assertIn("stdout-line", done.stdout)
            self.assertIn("stderr-line", done.stderr)
            logs = list(Path(tmp).glob("*_test.log"))
            self.assertEqual(len(logs), 1)
            text = logs[0].read_text(encoding="utf-8")
            for token in ("stdout-line", "stderr-line", "[INFO] fd2-direct",
                          "Exception in thread", "RuntimeError: spin-thread-error",
                          "Traceback (most recent call last)"):
                self.assertIn(token, text)

    def test_report_carries_raw_log_path(self):
        from core.reason_codes import ReasonCode
        from validation.simulation_e2e import not_started

        saved = dict(demo.RAW_LOG)
        demo.RAW_LOG.update(path=str(ROOT / "reports/workcell/sim_demo_logs/x.log"),
                            detail="stdout·stderr 원본 전체")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "r.json"
                demo.write(out, not_started(
                    scenario="s", object_id="o", support_id="p", target_id="t",
                    reason=ReasonCode.EXEC_SIM_OBJECT_ABSENT, detail="d"))
                payload = json.loads(out.read_text(encoding="utf-8"))
        finally:
            demo.RAW_LOG.clear()
            demo.RAW_LOG.update(saved)
        self.assertEqual(payload["raw_log_path"],
                         "reports/workcell/sim_demo_logs/x.log")
        self.assertIs(payload["is_simulated"], True)

    def test_missing_tee_does_not_block_the_run(self):
        from unittest import mock

        saved = dict(demo.RAW_LOG)
        try:
            with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
                    demo.subprocess, "Popen", side_effect=FileNotFoundError("tee")):
                info = demo.start_raw_log(Path(tmp), label="x")
            self.assertIsNone(info["path"])
            self.assertIn("시작하지 못했다", info["detail"])
        finally:
            demo.RAW_LOG.clear()
            demo.RAW_LOG.update(saved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
