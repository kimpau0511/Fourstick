"""`--restore-only` 성공 뒤 stale STOP 래치 해제 조건 단위 검증.

- 셀 전체가 정리됐을 때만 해제(B 실측과 같은 성공 복구)
- 슬롯 pose 불일치·관측 불가 / 고정 자재 남음·판별 불가 / 체크포인트 남음 /
  다른 자재 미해결(reset_required) / active goal·관측 불가 / scene 변경·읽기 실패 /
  판정 중 새 STOP → 래치 유지
- 복구가 확인되지 않으면 해제를 시도하지도 않는다

**ROS·Gazebo 없이** 돈다.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    build,
    stopped_result,
)

DEMO_PATH = ROOT / "scripts/demo_workcell_pick_place.py"
_spec = importlib.util.spec_from_file_location("demo_for_latch_clear", DEMO_PATH)
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)

SLOT_B = (0.5, 0.0, 0.84)
SCENE = "5" * 64
MATERIALS = ("material_a", "material_b", "material_c")


class LatchClearTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state = SimulationDemoState(self.tmp / "state.json")
        self.latch = demo.StopLatchFile(self.tmp / "latch.json")
        self.latch.latch({"stop_execution_id": "simstop_b", "stage": "place_approach",
                          "scene_hash": SCENE})

    def run_clear(self, *, pose=SLOT_B, statics=None, goals=0, scene=SCENE,
                  on_goals=None):
        statics = statics or {}

        def observe_goals():
            if on_goals:
                on_goals()
            return {"active": goals}

        return demo.clear_latch_after_restore(
            model="material_b", slot_home_m=SLOT_B, latch=self.latch,
            demo_state=self.state, materials=MATERIALS,
            observe_pose=lambda name: pose,
            observe_static=lambda name: statics.get(name, False),
            observe_goals=observe_goals, scene_hash=lambda: scene)

    def assert_kept(self, result, text):
        self.assertFalse(result["cleared"])
        self.assertTrue(any(text in r for r in result["reasons"]), result["reasons"])
        self.assertIsNotNone(self.latch.latched())

    def test_clean_cell_after_successful_restore_clears_latch(self):
        result = self.run_clear(pose=(0.5, 0.0, 0.839999))
        self.assertTrue(result["cleared"], result["reasons"])
        self.assertIsNone(self.latch.latched())
        evidence = result["evidence"]
        self.assertEqual(evidence["latch"]["stop_execution_id"], "simstop_b")
        self.assertLessEqual(evidence["slot_gap_m"], 0.02)
        self.assertEqual(evidence["active_goals"], 0)
        self.assertFalse(evidence["reset_required"])

    def test_pose_mismatch_or_unobserved_keeps_latch(self):
        self.assert_kept(self.run_clear(pose=(0.53, 0.0, 0.84)), "원래 슬롯")
        self.assert_kept(self.run_clear(pose=None), "pose를 관측하지 못했다")

    def test_held_or_unknown_attachment_keeps_latch(self):
        self.assert_kept(self.run_clear(statics={"material_a": True}), "고정된 자재")
        self.assert_kept(self.run_clear(statics={"material_c": None}), "고정 여부")

    def test_checkpoint_keeps_latch(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=stopped_result(), final_pose_m=(0.4, 0, 1),
                              restored=None)
        self.state.record_checkpoint(build()[0])
        self.assert_kept(self.run_clear(), "체크포인트")

    def test_other_unresolved_material_keeps_global_latch(self):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_c",
                              result=stopped_result(), final_pose_m=(0.4, 0, 1),
                              restored=None)
        result = self.run_clear()
        self.assert_kept(result, "material_c")
        self.assertEqual(result["evidence"]["unresolved_objects"], ["material_c"])

    def test_active_goal_or_unobserved_goals_keeps_latch(self):
        self.assert_kept(self.run_clear(goals=1), "active goal이 1개")
        self.assert_kept(self.run_clear(goals=None), "active goal 수")

    def test_scene_change_or_unreadable_keeps_latch(self):
        self.assert_kept(self.run_clear(scene="x" * 64), "scene이 정지 시점과 다르다")
        self.assert_kept(self.run_clear(scene=None), "scene hash를 다시 읽지 못했다")

    def test_new_stop_during_check_keeps_new_latch(self):
        def new_stop():
            self.latch.latch({"stop_execution_id": "simstop_new", "scene_hash": SCENE})

        result = self.run_clear(on_goals=new_stop)
        self.assert_kept(result, "래치가 바뀌었다")
        self.assertEqual(self.latch.latched()["stop_execution_id"], "simstop_new")

    def test_missing_latch_reports_nothing_to_clear(self):
        self.latch.path.unlink()
        result = self.run_clear()
        self.assertFalse(result["cleared"])
        self.assertEqual(result["reasons"], ["래치가 없다"])


class RestoreOnlyWiringTest(unittest.TestCase):
    def test_clear_is_attempted_only_after_verified_restore(self):
        source = ast.unparse(next(
            n for n in ast.parse(DEMO_PATH.read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "main"))
        restore = source.index("event = fixture.restore(model)")
        mark = source.index("demo_state.mark_restored(model, verified=event.verified)")
        guard = source.index("if event.verified and StopLatchFile(LATCH).latched()"
                             " is not None:")
        call = source.index("clear_latch_after_restore_live(")
        self.assertLess(restore, mark)
        self.assertLess(mark, guard)
        self.assertLess(guard, call)

    def test_general_release_and_resume_rules_unchanged(self):
        source = DEMO_PATH.read_text(encoding="utf-8")
        # 일반 해제(StopLatchFile.release)와 resume(run_resume)은 이 경로를 부르지 않는다.
        for name in ("run_resume", "return_held_to_origin"):
            body = ast.unparse(next(
                n for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef) and n.name == name))
            self.assertNotIn("clear_latch_after_restore", body)
        release = ast.unparse(next(
            n for n in ast.walk(ast.parse(source))
            if isinstance(n, ast.ClassDef) and n.name == "StopLatchFile"))
        self.assertNotIn("clear_latch_after_restore", release)


if __name__ == "__main__":
    unittest.main(verbosity=2)
