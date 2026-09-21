"""전역 STOP의 호출 순서 단위 검증.

진행 중인 실행이 있으면 그 실행이 이미 연결을 확인한 어댑터다. 전역 STOP은
연결 재확인(world·컨트롤러·모델 조회)보다 **취소를 먼저** 보내야 한다. 실행이
없거나 어댑터가 준비되지 않았으면 기존 흐름(연결 확인 → 정지)과 실패 처리를
그대로 따른다.

**ROS 없이** 실제 `Fr3GazeboAdapter.stop()`을 호출 기록 스텁 전송 위에서 돌린다.
정지 확인(안정 창)은 이 검증의 대상이 아니므로 결과를 고정한다.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from robots.fr3_gazebo.transport import GoalOutcome
from server.api import Api
from server.config import ServerConfig
from server.runtime import build_runtime
from tests.unit.test_fr3_gazebo_adapter import StubTransport, build


class RecordingTransport(StubTransport):
    """호출 순서를 남기는 전송 스텁."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls: list[str] = []
        self.cancel_error: Exception | None = None

    def connect(self, timeout_sec):
        self.calls.append("connect")
        return super().connect(timeout_sec)

    def status(self, timeout_sec):
        self.calls.append("status")
        return super().status(timeout_sec)

    def cancel_all(self, timeout_sec):
        self.calls.append("cancel_all")
        if self.cancel_error is not None:
            raise self.cancel_error
        return super().cancel_all(timeout_sec)


def confirmed(timeout_sec):
    return ExecutionResult(
        state=ExecutionState.STOPPED, request_accepted=True,
        task_succeeded=None, verified=True, evidence={"stop_verdict": "stop_confirmed"})


class StopApiCase(unittest.TestCase):
    """공용 준비. 테스트를 두지 않는다."""

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
        self.api = Api(runtime=self.runtime)

    def _prepared_adapter(self):
        """실행이 쓰던 어댑터처럼, 이미 만들어지고 연결된 어댑터를 넣는다."""
        adapter, transport, resources, _ = build(RecordingTransport(world=""))
        transport.world_name = resources.world_name
        self.assertTrue(adapter.connect(1.0).request_accepted)
        transport.calls.clear()
        adapter.confirm_stopped = confirmed
        self.runtime._adapter = adapter
        return adapter, transport

    def _running(self, execution_id="exec_running", session_id="sess_a"):
        with self.api._flag_lock:
            self.api._active_executions[execution_id] = session_id


class StopOrderTest(StopApiCase):

    def test_prepared_adapter_cancels_first_without_connect(self):
        adapter, transport = self._prepared_adapter()
        self._running()
        payload = self.api.stop(session_id="sess_a")
        self.assertEqual(transport.calls[0], "cancel_all")
        self.assertNotIn("connect", transport.calls)
        self.assertNotIn("status", transport.calls)
        self.assertTrue(payload["requested"])
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["affected_execution_count"], 1)
        self.assertEqual(payload["your_execution_ids"], ["exec_running"])
        self.assertTrue(adapter.tracker.stopped)

    def test_no_running_execution_keeps_connect_before_cancel(self):
        _, transport = self._prepared_adapter()
        payload = self.api.stop()
        self.assertEqual(transport.calls[:2], ["connect", "cancel_all"])
        self.assertEqual(payload["affected_execution_count"], 0)
        self.assertTrue(payload["requested"])

    def test_unprepared_adapter_keeps_safe_failure(self):
        class Broken:
            def connect(self, timeout_sec):
                raise RuntimeError("연결 끊김")

        self.runtime.registry = type(self.runtime.registry)()
        self.runtime.registry.register(
            self.runtime.robot_id, self.runtime.profile, lambda rid, prof: Broken())
        self.runtime._adapter = None
        payload = self.api.stop()
        self.assertFalse(payload["requested"])
        self.assertFalse(payload["confirmed"])
        self.assertEqual(payload["state"], "unconfirmed")
        self.assertEqual(payload["reason_code"], ReasonCode.EXEC_STOP_UNCONFIRMED.value)

    def test_cancel_all_exception_keeps_error_handling(self):
        _, transport = self._prepared_adapter()
        transport.cancel_error = RuntimeError("취소 서비스 없음")
        self._running()
        payload = self.api.stop(session_id="sess_a")
        self.assertEqual(transport.calls, ["cancel_all"])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["requested"])
        self.assertFalse(payload["confirmed"])
        self.assertEqual(payload["state"], "unconfirmed")
        self.assertEqual(payload["reason_code"], ReasonCode.EXEC_STOP_UNCONFIRMED.value)
        self.assertIn("정지 요청을 보내지 못했다", payload["detail"])
        # 실패해도 전역 정지 래치는 걸려 있다.
        self.assertTrue(self.api._stop_requested)

    def test_cancel_without_ack_still_goes_to_confirmation(self):
        adapter, transport = self._prepared_adapter()
        transport.cancel_outcome = GoalOutcome(
            accepted=True, cancel_ack=False, goals_canceling=0,
            detail="일부 goal의 취소 ACK를 받지 못했다")
        seen = []
        adapter.confirm_stopped = lambda t: (seen.append("confirm"), confirmed(t))[1]
        self._running()
        payload = self.api.stop(session_id="sess_a")
        self.assertEqual(transport.calls[0], "cancel_all")
        self.assertEqual(seen, ["confirm"])
        self.assertFalse(payload["stop_result"]["evidence"]["cancel_ack"])


class StopLatchExecutionIdTest(StopApiCase):
    """전역 STOP이 멈춘 실행 id를 래치에 남긴다 — 정확히 하나일 때만."""

    def test_single_execution_id_is_latched(self):
        adapter, _ = self._prepared_adapter()
        self._running("exec_only", "sess_a")
        self.api.stop(session_id="sess_a")
        diag = adapter.stop_diagnostics()
        self.assertTrue(diag["stop_latch_active"])
        self.assertEqual(diag["stop_latch_execution_id"], "exec_only")

    def test_no_execution_latches_null(self):
        adapter, _ = self._prepared_adapter()
        self.api.stop()
        diag = adapter.stop_diagnostics()
        self.assertTrue(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])

    def test_multiple_executions_latch_null(self):
        adapter, _ = self._prepared_adapter()
        self._running("exec_a", "sess_a")
        self._running("exec_b", "sess_b")
        payload = self.api.stop(session_id="sess_a")
        self.assertEqual(payload["affected_execution_count"], 2)
        diag = adapter.stop_diagnostics()
        self.assertTrue(diag["stop_latch_active"])
        self.assertIsNone(diag["stop_latch_execution_id"])

    def test_adapter_without_execution_id_parameter_is_called_as_before(self):
        seen = []

        class Legacy:
            def stop(self, timeout_sec):
                seen.append(("stop", timeout_sec))
                return ExecutionResult(state=ExecutionState.STOPPING,
                                       request_accepted=True, evidence={})

            def confirm_stopped(self, timeout_sec):
                return confirmed(timeout_sec)

        self.runtime._adapter = Legacy()
        self._running("exec_only", "sess_a")
        payload = self.api.stop(session_id="sess_a")
        self.assertEqual(len(seen), 1)
        self.assertTrue(payload["requested"])
        self.assertTrue(payload["confirmed"])


if __name__ == "__main__":
    unittest.main()
