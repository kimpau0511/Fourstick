"""재개는 중단된 이송의 **기록 칸**에 놓는다 (2026-09-24 실측 회귀).

파지 후 STOP → resume에서 slot_2로 가던 B가 slot_1에 놓였다. 서버가 재개에
`--slot`을 넘기지 않았고, 재개 문맥이 칸 자세를 결속하지 않았다.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_jobs import build_argv  # noqa: E402

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_resume_slot", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

MATERIAL = {"model": "material_b", "support_model": "pallet_2"}


class FakeState:
    def __init__(self, record):
        self.record = record

    def status(self):
        return {"objects": {} if self.record is None else {"material_b": self.record}}


class ResumeArgvTest(unittest.TestCase):
    def test_resume_actions_carry_the_recorded_slot(self):
        self.assertEqual(build_argv("resume", MATERIAL, "ck1", "slot_2")[-2:],
                         ["--slot", "slot_2"])
        self.assertEqual(build_argv("resume_preflight", MATERIAL, None, "slot_2")[-2:],
                         ["--slot", "slot_2"])
        self.assertNotIn("--slot", build_argv("resume", MATERIAL, "ck1", None))


class ResumeSlotTest(unittest.TestCase):
    def args(self, slot):
        return types.SimpleNamespace(slot=slot)

    def test_recorded_slot_is_used(self):
        state = FakeState({"state": "stopped_unrestored", "slot": "slot_2"})
        self.assertEqual(demo.resume_slot(self.args(None), state, "material_b"),
                         ("slot_2", None))
        self.assertEqual(demo.resume_slot(self.args("slot_2"), state, "material_b"),
                         ("slot_2", None))

    def test_mismatch_refuses(self):
        state = FakeState({"state": "stopped_unrestored", "slot": "slot_2"})
        slot, problem = demo.resume_slot(self.args("slot_1"), state, "material_b")
        self.assertIsNone(slot)
        self.assertIn("slot_2", problem)

    def test_legacy_record_without_slot(self):
        state = FakeState({"state": "stopped_unrestored"})
        self.assertEqual(demo.resume_slot(self.args(None), state, "material_b"),
                         (None, None))

    def test_context_binds_slot_before_building_stages(self):
        source = DEMO_PATH.read_text(encoding="utf-8")
        start = source.index("def build_resume_context(")
        body = source[start:source.index("\ndef ", start + 10)]
        self.assertLess(body.index("apply_slot("), body.index("build_stages(steps"))
        for name in ("def resume_preflight(", "def resume_checkpoint("):
            start = source.index(name)
            body = source[start:source.index("\ndef ", start + 10)]
            self.assertIn("slot=slot", body, name)


if __name__ == "__main__":
    unittest.main()
