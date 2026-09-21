"""E2E item 14 하위 스크립트 호출 단위 검증.

하위 스크립트는 실제로 실행하지 않는다 — `subprocess.run` 자리에 기록용
대역을 넣어, 부모의 서버 주소가 정확히 전달되는지와 실패 원인이 보고서
구조에 남는지만 본다.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "verify_pick_place_sim_e2e", ROOT / "scripts/verify_pick_place_sim_e2e.py")
e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e2e)

BASE = "http://127.0.0.1:8094"


class FakeRun:
    """호출을 기록하고 정해 둔 결과를 돌려준다."""

    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def __call__(self, args, **kwargs):
        self.calls.append({"args": list(args), **kwargs})
        code, out, err = self.results.pop(0)
        return subprocess.CompletedProcess(args, code, stdout=out, stderr=err)


class BaseIsPassedTest(unittest.TestCase):
    def setUp(self):
        self.run = FakeRun([
            (0, "a\nb\n  reports/workcell/outcome_contract.json 기록 — 7/7 통과\n", ""),
            (0, "x\n\n시연 6/6 통과 — reports/workcell/demo_commands.json\n", ""),
        ])
        self.result = e2e.run_regression_subscripts(BASE, run=self.run)
        self.outcome_call, self.demo_call = self.run.calls

    def test_outcome_gets_base_argument(self):
        args = self.outcome_call["args"]
        self.assertTrue(args[1].endswith("scripts/verify_workcell_outcome.py"))
        self.assertIn("--base", args)
        self.assertEqual(args[args.index("--base") + 1], BASE)

    def test_demo_gets_web_base_env(self):
        self.assertTrue(self.demo_call["args"][0].endswith("scripts/demo_workcell_commands.sh"))
        self.assertEqual(self.demo_call["env"]["FORSTICK2_WEB_BASE"], BASE)

    def test_output_is_captured_as_text_with_timeout(self):
        for call in self.run.calls:
            self.assertTrue(call["capture_output"])
            self.assertTrue(call["text"])
            self.assertEqual(call["timeout"], 900)

    def test_success_rule_is_unchanged(self):
        self.assertTrue(self.result["outcome_ok"])
        self.assertTrue(self.result["demo_ok"])
        self.assertEqual(self.result["outcome"]["exit_code"], 0)
        self.assertEqual(len(self.result["outcome"]["stdout_tail"]), 3)
        self.assertIn("7/7 통과", self.result["outcome"]["stdout_tail"][-1])


class FailureIsRecordedTest(unittest.TestCase):
    def test_exit_code_and_stderr_are_kept(self):
        run = FakeRun([
            (2, "", "서버에 연결할 수 없다: http://127.0.0.1:8093 — refused\n"),
            (2, "", "[demo] 웹 서버가 없다: http://127.0.0.1:8093\n"
                    "[demo] 먼저 띄운다: FORSTICK2_PORT=8093 ./scripts/run_web_workcell.sh\n"),
        ])
        result = e2e.run_regression_subscripts(BASE, run=run)
        self.assertFalse(result["outcome_ok"])
        self.assertFalse(result["demo_ok"])
        self.assertEqual(result["outcome"]["exit_code"], 2)
        self.assertEqual(result["outcome"]["stdout_tail"], [])
        self.assertIn("서버에 연결할 수 없다", result["outcome"]["stderr_tail"][-1])
        self.assertEqual(result["demo"]["exit_code"], 2)
        self.assertEqual(len(result["demo"]["stderr_tail"]), 2)
        self.assertIn("웹 서버가 없다", result["demo"]["stderr_tail"][0])

    def test_nonzero_exit_fails_even_with_pass_text(self):
        run = FakeRun([(1, "7/7 통과\n", ""), (0, "시연 5/6 통과\n", "")])
        result = e2e.run_regression_subscripts(BASE, run=run)
        self.assertFalse(result["outcome_ok"])
        self.assertFalse(result["demo_ok"])

    def test_stderr_keeps_last_three_lines(self):
        run = FakeRun([(1, "", "1\n2\n3\n4\n5\n"), (1, "", "")])
        result = e2e.run_regression_subscripts(BASE, run=run)
        self.assertEqual(result["outcome"]["stderr_tail"], ["3", "4", "5"])
        self.assertEqual(result["demo"]["stderr_tail"], [])


if __name__ == "__main__":
    unittest.main()
