"""해석 미리보기(`server/sim_demo_preview.py`) — 운영 상태를 바꾸지 않는다."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import eval_sim_commands as E  # noqa: E402
from server.sim_demo_preview import preview  # noqa: E402


class PreviewHasNoSideEffects(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.live, self.live_popen = E.build(Path(self._tmp.name), {}, None)
        self.state_file = Path(self.live.sim_demo_jobs.state.path)
        self.state_before = (self.state_file.read_bytes() if self.state_file.is_file() else None)

    def tearDown(self):
        self._tmp.cleanup()

    def assert_live_untouched(self):
        self.assertEqual(self.live_popen.calls, [], "운영 작업이 시작됐다")
        after = self.state_file.read_bytes() if self.state_file.is_file() else None
        self.assertEqual(after, self.state_before, "운영 상태 파일이 바뀌었다")
        self.assertEqual(self.live.sim_demo_goals.environment().get("blocked_slots") or [], [])

    def test_executable_command_is_only_previewed(self):
        status, result = preview(self.live, {"utterance": "A자재를 컨베이어로 옮겨줘",
                                             "source": "text", "session_id": "s1"})
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"], "CONFIRM")
        self.assertTrue(result["preview"])
        self.assertFalse(result["would_start_job"])
        self.assertTrue(result["would_require_confirmation"])
        self.assert_live_untouched()

    def test_environment_utterance_does_not_change_live_environment(self):
        preview(self.live, {"utterance": "2번 칸 고장났어", "source": "text", "session_id": "s1"})
        self.assert_live_untouched()

    def test_confirm_card_is_not_created_in_live_store(self):
        _, result = preview(self.live, {"utterance": "B자재를 컨베이어 2번에 놓아줘",
                                        "source": "text", "session_id": "s1"})
        self.assertEqual(result["decision"], "CONFIRM")
        self.assertIsNone(self.live.sim_demo_confirm.current())
        self.assert_live_untouched()


if __name__ == "__main__":
    unittest.main()
