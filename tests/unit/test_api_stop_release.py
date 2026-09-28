"""전체 정지 래치 **해제**의 계약 (`Api.release_stop`).

확인하는 것:
 - 해제는 `core/stop_contract`의 `reset_for_new_plan`을 그대로 부른다
   (`plan`이 쓰는 해제 지점과 **같은 곳**이다 — 두 번째 경로를 만들지 않는다)
 - **살아 있는 동작 위에서는 풀지 않는다** — 진행 중인 실행이나 시뮬레이션
   작업이 있으면 거부하고 사유를 돌려준다
 - 어댑터가 해제를 거부하면 그 거부를 성공으로 바꾸지 않는다
 - 풀린 뒤에는 전역 정지 플래그도 내려간다

ROS 없이 돈다 — 기존 STOP 단위 검증과 같은 스텁 위에서 돌린다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.unit.test_api_stop_order import StopApiCase  # noqa: E402


class ReleaseStopTest(StopApiCase):

    def setUp(self):
        super().setUp()
        self.adapter, _ = self._prepared_adapter()

    def _latched(self):
        """정지를 걸어 래치가 선 상태를 만든다."""
        self.api.stop()
        self.assertTrue(self.adapter.tracker.stopped)
        with self.api._flag_lock:
            self.assertTrue(self.api._stop_requested)

    def test_release_clears_latch_and_flag(self):
        self._latched()
        payload = self.api.release_stop()
        self.assertTrue(payload["released"])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["scope"], "global")
        self.assertFalse(self.adapter.tracker.stopped)
        with self.api._flag_lock:
            self.assertFalse(self.api._stop_requested)

    def test_running_execution_refuses_release(self):
        self._latched()
        self._running()
        payload = self.api.release_stop()
        self.assertFalse(payload["released"])
        self.assertIn("진행 중인 실행", payload["detail"])
        # 거부됐으면 래치는 그대로다 — 반쯤 푼 상태를 만들지 않는다.
        self.assertTrue(self.adapter.tracker.stopped)
        with self.api._flag_lock:
            self.assertTrue(self.api._stop_requested)

    def test_running_simulation_job_refuses_release(self):
        self._latched()

        class Jobs:
            def status(self):
                return {"running_job": {"job_id": "simjob_1"}}

        self.runtime.sim_demo_jobs = Jobs()
        payload = self.api.release_stop()
        self.assertFalse(payload["released"])
        self.assertIn("시뮬레이션 작업", payload["detail"])
        self.assertTrue(self.adapter.tracker.stopped)

    def test_job_status_failure_does_not_release(self):
        """상태를 못 읽었다는 것은 '돌고 있지 않다'는 근거가 아니다."""
        self._latched()

        class Broken:
            def status(self):
                raise RuntimeError("상태 파일 없음")

        self.runtime.sim_demo_jobs = Broken()
        payload = self.api.release_stop()
        self.assertFalse(payload["released"])
        self.assertIn("읽지 못했다", payload["detail"])
        self.assertTrue(self.adapter.tracker.stopped)

    def test_adapter_refusal_is_not_reported_as_success(self):
        self._latched()

        class NoReset:
            tracker = None

        self.runtime._adapter = NoReset()
        payload = self.api.release_stop()
        self.assertFalse(payload["released"])
        self.assertFalse(payload["ok"])
        with self.api._flag_lock:
            self.assertTrue(self.api._stop_requested)

    def test_release_uses_the_same_contract_as_a_new_plan(self):
        """해제 지점을 새로 만들지 않았다 — 둘 다 reset_stop_latch를 부른다."""
        calls = []
        original = self.runtime.reset_stop_latch
        self.runtime.reset_stop_latch = lambda: (calls.append("reset") or (True, "풀었다"))
        try:
            self._latched()
            payload = self.api.release_stop()
        finally:
            self.runtime.reset_stop_latch = original
        self.assertEqual(calls, ["reset"])
        self.assertTrue(payload["released"])
        self.assertEqual(payload["detail"], "풀었다")

    def test_release_emits_an_event(self):
        seen = []
        self.api.emit = lambda event, **kw: seen.append((event.get("type"), kw))
        self._latched()
        self.api.release_stop()
        kinds = [t for t, _ in seen]
        self.assertIn("stop_released", kinds)


if __name__ == "__main__":
    unittest.main(verbosity=2)
