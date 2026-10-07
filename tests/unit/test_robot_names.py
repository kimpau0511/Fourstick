"""로봇 이름·호출어 설정(server/robot_names.py) — 기본값·검증·저장 유지."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.robot_names import RobotNameError, RobotNames  # noqa: E402


class RobotNamesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "robot_settings.json"

    def test_default_is_jini(self):
        got = RobotNames(self.path).get("fr3")
        self.assertEqual((got["name"], got["wake_word"], got["is_default"]), ("지니", "지니야", True))

    def test_save_persists_and_makes_wake_word(self):
        RobotNames(self.path).set("fr3", " 알파 ")
        got = RobotNames(self.path).get("fr3")                     # 새로 읽어도(= 새로고침·재시작) 유지
        self.assertEqual((got["name"], got["wake_word"], got["is_default"]), ("알파", "알파야", False))
        self.assertEqual(RobotNames(self.path).get("other")["name"], "지니")   # 로봇마다 따로

    def test_validation(self):
        names = RobotNames(self.path)
        for bad in ("", "   ", "가", "가나다라마", "jini", "지 니", "지니1", None, 12):
            with self.subTest(bad=bad), self.assertRaises(RobotNameError):
                names.set("fr3", bad)
        for good in ("지니", "알파", "로봇이", "가나다라"):
            self.assertEqual(names.set("fr3", good)["name"], good)


if __name__ == "__main__":
    unittest.main(verbosity=2)
