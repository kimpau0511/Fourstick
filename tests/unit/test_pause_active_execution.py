"""시뮬레이션 보기의 '일시정지' — 이 세션에서 실행 중인 작업만 취소한다(전체 정지 아님, 2026-10-07)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


class PauseActiveExecutionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests.integration.test_web_api import WebCase

        self.case = WebCase()
        await WebCase.asyncSetUp(self.case)
        self.addAsyncCleanup(self._cleanup)
        self.api = self.case.app.api

    async def _cleanup(self):
        self.case.doCleanups()

    async def pause(self, session_id):
        return await self.case.client.post("/v1/executions/active/cancel", {"session_id": session_id})

    async def test_only_this_sessions_running_execution_is_cancelled(self):
        other = (await self.case.client.post("/v1/sessions", {"origin": "test"})).json()["session_id"]
        cancelled = []
        self.api.cancel_execution = lambda *, session_id, execution_id: cancelled.append((session_id, execution_id))
        self.api._active_executions.update({"exec_mine": self.case.session, "exec_other": other})
        body = (await self.pause(self.case.session)).json()
        self.assertTrue(body["requested"], body)
        self.assertEqual(body["execution_ids"], ["exec_mine"])
        self.assertEqual(cancelled, [(self.case.session, "exec_mine")])     # 다른 세션 실행은 건드리지 않는다
        self.assertFalse(self.api._stop_requested)                          # 전체 정지가 아니다

    async def test_nothing_running_says_so(self):
        body = (await self.pause(self.case.session)).json()
        self.assertEqual((body["requested"], body["execution_ids"]), (False, []))
        self.assertIn("실행 중인 작업이 없습니다", body["detail"])

    async def test_unknown_session_is_refused(self):
        response = await self.pause("sess_unknown")
        self.assertGreaterEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
