"""정지 래치·추적 goal 진단값(읽기 전용) 단위 검증.

- 어댑터 진단값이 추적 goal 수(terminal 정리 반영)와 래치 상태·실행 id를
  정확히 보이는지.
- `/v1/robots`가 진단값을 담되, 어댑터를 만들거나 연결·관측·취소·해제를
  일으키지 않는지.

**ROS 없이** 돈다. 이동·정지 명령을 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.ros_transport import RosWorkcellTransport
from server.config import ServerConfig
from server.routes import robot
from server.runtime import build_runtime
from tests.unit.test_api_stop_order import RecordingTransport
from tests.unit.test_fr3_gazebo_adapter import build
from tests.unit.test_ros_transport_goal_tracking import (
    ABORTED, CANCELED, EXECUTING, FakeHandle, Response,
)


def ros_transport():
    t = RosWorkcellTransport(world_name="w", gz_partition="p", ros_domain_id=44)
    t._node = object()      # _ensure_node가 ROS를 올리지 않게 한다
    return t


class TrackedGoalCountTest(unittest.TestCase):

    def test_zero_and_one_tracked_goal(self):
        transport = ros_transport()
        adapter, _, _, _ = build(transport)
        self.assertEqual(adapter.stop_diagnostics()["tracked_goal_count"], 0)
        handle = FakeHandle(1)
        transport._track(handle)
        self.assertEqual(adapter.stop_diagnostics()["tracked_goal_count"], 1)
        # 진행 중 상태는 그대로 1이다.
        transport._on_goal_result(handle, types.SimpleNamespace(
            result=lambda: Response(EXECUTING)))
        self.assertEqual(adapter.stop_diagnostics()["tracked_goal_count"], 1)
        handle.result_future.finish(Response(CANCELED))
        self.assertEqual(adapter.stop_diagnostics()["tracked_goal_count"], 0)

    def test_only_the_terminal_goal_leaves_the_count(self):
        transport = ros_transport()
        adapter, _, _, _ = build(transport)
        first, second = FakeHandle(1), FakeHandle(2)
        transport._track(first)
        transport._track(second)
        first.result_future.finish(Response(ABORTED))
        self.assertEqual(adapter.stop_diagnostics()["tracked_goal_count"], 1)


class StopLatchDiagnosticsTest(unittest.TestCase):

    def test_inactive_by_default(self):
        adapter, _, _, _ = build()
        diag = adapter.stop_diagnostics()
        self.assertFalse(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])
        self.assertTrue(diag["is_simulated"])
        self.assertFalse(diag["real_hardware"])

    def test_active_with_execution_id_then_released(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        adapter.stop(1.0, execution_id="exec_abc")
        diag = adapter.stop_diagnostics()
        self.assertTrue(diag["stop_latch_active"])
        self.assertEqual(diag["stop_latch_execution_id"], "exec_abc")
        adapter.tracker.reset_for_new_plan()
        diag = adapter.stop_diagnostics()
        self.assertFalse(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])

    def test_active_without_execution_id_is_null(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        adapter.stop(1.0)
        diag = adapter.stop_diagnostics()
        self.assertTrue(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])

    def test_reading_does_not_release_or_cancel(self):
        adapter, transport, _, _ = build(RecordingTransport(world=""))
        transport.world_name = adapter._resources.world_name
        adapter.connect(1.0)
        adapter.stop(1.0, execution_id="exec_abc")
        transport.calls.clear()
        for _ in range(3):
            adapter.stop_diagnostics()
        self.assertEqual(transport.calls, [])
        self.assertTrue(adapter.tracker.stopped)


class RobotsRouteTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = dataclasses.replace(
            ServerConfig.from_env(),
            db_path=Path(tmp.name) / "web.sqlite3",
            enable_stt=False,
            llm_config_name="__absent__.json",
        )
        self.runtime = build_runtime(config)
        self.addCleanup(self.runtime.repository.close)
        self.ctx = types.SimpleNamespace(
            runtime=self.runtime, repository=self.runtime.repository)

    def get(self) -> dict:
        response = asyncio.run(robot.handle(self.ctx, "GET", "/v1/robots", None, {}))
        self.assertIsNotNone(response)
        status, _, body = response
        self.assertEqual(status, 200)
        return json.loads(body)["stop_diagnostics"]

    def test_no_adapter_is_not_created(self):
        # 진단값 읽기 자체가 어댑터를 만들지 않는지 본다. (같은 응답의 다른 키인
        # `declared`는 기존대로 runtime.adapter()를 부른다 — 이 검증의 대상이 아니다.)
        created = []
        self.runtime.adapter = lambda: created.append(1)
        self.runtime._adapter = None
        diag = robot._stop_diagnostics(self.runtime)
        self.assertEqual(created, [])
        self.assertIsNone(self.runtime._adapter)
        self.assertFalse(diag["adapter_instantiated"])
        self.assertEqual(diag["tracked_goal_count"], 0)
        self.assertFalse(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])
        self.assertEqual(self.get()["adapter_instantiated"], False)

    def test_existing_adapter_values_without_external_calls(self):
        adapter, transport, _, _ = build(RecordingTransport(world=""))
        transport.world_name = adapter._resources.world_name
        adapter.connect(1.0)
        adapter.stop(1.0, execution_id="exec_5143165dd177")
        transport.calls.clear()
        self.runtime._adapter = adapter
        diag = self.get()
        self.assertTrue(diag["adapter_instantiated"])
        self.assertEqual(diag["tracked_goal_count"], 0)
        self.assertTrue(diag["stop_latch_active"])
        self.assertEqual(diag["stop_latch_execution_id"], "exec_5143165dd177")
        self.assertTrue(diag["is_simulated"])
        self.assertFalse(diag["real_hardware"])
        self.assertEqual(transport.calls, [])
        self.assertTrue(adapter.tracker.stopped)

    def test_adapter_without_diagnostics_reports_unavailable(self):
        self.runtime._adapter = object()
        diag = self.get()
        self.assertTrue(diag["adapter_instantiated"])
        self.assertFalse(diag["available"])
        self.assertIsNone(diag["tracked_goal_count"])


if __name__ == "__main__":
    unittest.main()
