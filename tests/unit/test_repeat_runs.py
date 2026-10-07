"""반복 작업(server/repeat_runs.py) — 순서·회차·회차 후 종료·실패 중단·정지·일시정지/재개·중복 방지·셀 예약·재시작.

일반 모드 API(create_plan·decide·execute)는 가짜다 — 반복이 그 경로를 **어떤 순서·인자로** 부르는지,
결과에 따라 어떻게 멈추는지를 본다. 실제 계획·관문·이송 실행은 격리 복제 셀 시험에서 따로 본다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.cell_execution import CellExecutionManager  # noqa: E402
from server.repeat_runs import RepeatRunError, RepeatRuns  # noqa: E402

ORIGIN = {"mat_a": "loc_pallet_1", "mat_b": "loc_pallet_2", "mat_c": "loc_pallet_3"}
MODEL = {"mat_a": "material_a", "mat_b": "material_b", "mat_c": "material_c"}
NAME = {"mat_a": "A자재", "mat_b": "B자재", "mat_c": "C자재"}


class FakeWorld:
    def __init__(self):
        self.where = dict(ORIGIN)
        self.records: dict[str, dict] = {}
        self.running_job = None

    def facts(self):
        mats = [{"id": rid, "model": MODEL[rid], "name": NAME[rid], "colors": [], "tokens": [],
                 "location": self.where[rid], "origin": ORIGIN[rid]} for rid in ("mat_a", "mat_b", "mat_c")]
        locs = [{"id": i, "name": n} for i, n in (("loc_pallet_1", "1번 팔레트"), ("loc_pallet_2", "2번 팔레트"),
                                                  ("loc_pallet_3", "3번 팔레트"), ("loc_conveyor", "컨베이어"))]
        used = sum(1 for v in self.where.values() if v == "loc_conveyor")
        return {"materials": mats, "locations": locs, "free_conveyor_slots": ["s"] * (3 - used),
                "state_error": None}


class FakeJobs:
    def __init__(self, world):
        self.world = world

    def status(self):
        return {"running_job": self.world.running_job, "recovery_required": None,
                "state": {"objects": self.world.records, "checkpoint": None},
                "materials": [{"model": MODEL[r], "record": self.world.records.get(MODEL[r]),
                               "actions": {"resume": False}} for r in MODEL]}


class FakeApi:
    def __init__(self, world, cell):
        self.world, self.cell = world, cell
        self._flag_lock = threading.Lock()
        self._stop_requested = False
        self._active_executions = {}
        self.calls = []
        self.on_execute = None          # (n, intent) -> 결과 dict 또는 None(성공)
        self.intents = {}

    def require_session(self, session_id):
        if session_id != "sess":
            from server.api import ApiError
            raise ApiError(404, None, "세션 없음")

    def create_plan(self, *, session_id, utterance, intent, keep_stop_latch):
        n = len(self.intents) + 1
        self.calls.append(("plan", utterance, intent.material.resource_id, intent.source.resource_id,
                           intent.destination.resource_id, keep_stop_latch))
        self.intents[f"req{n}"] = intent
        return {"ok": True, "executable": True, "request_id": f"req{n}",
                "plan": {"plan_id": f"plan{n}", "plan_hash": f"h{n}"}}

    def decide(self, *, session_id, request_id, plan_id, plan_hash, approve, note):
        self.calls.append(("decide", plan_id, approve))
        return {"approval_id": f"appr_{plan_id}"}

    def execute(self, *, session_id, request_id, plan_id, approval_id, reservation):
        intent = self.intents[request_id]
        self.calls.append(("execute", plan_id, approval_id, reservation))
        # 셀이 예약돼 있으면 예약 토큰 없이는 임대를 못 받는다.
        assert self.cell.try_acquire(owner="intruder", operation_id="x") is None
        lease = self.cell.try_acquire(owner="general_execute", operation_id=plan_id, reservation=reservation)
        assert lease is not None, "반복의 예약 토큰으로는 임대를 받아야 한다"
        try:
            if self.on_execute:
                out = self.on_execute(len([c for c in self.calls if c[0] == "execute"]), intent)
                if out is not None:
                    return out
            self.world.where[intent.material.resource_id] = intent.destination.resource_id
            return {"ok": True, "execution_id": f"ex_{plan_id}", "final": {"task_succeeded": True}}
        finally:
            self.cell.release(lease)

    def cancel_active_execution(self, *, session_id):
        self.calls.append(("cancel_active", session_id))
        return {"requested": True}


class InlineRuns(RepeatRuns):
    """스레드 없이 같은 흐름을 돌린다(결정적 시험)."""

    def __init__(self, world, **kw):
        self.world = world
        super().__init__(**kw)

    def _facts(self):
        return self.world.facts()

    def _spawn(self):
        self._loop()


class RepeatRunsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = FakeWorld()
        self.cell = CellExecutionManager()
        self.api = FakeApi(self.world, self.cell)
        self.runtime = type("R", (), {"sim_demo_jobs": FakeJobs(self.world), "cell_execution": self.cell})()
        self.path = Path(self.tmp.name) / "repeat_runs.json"
        self.runs = self.make()

    def make(self):
        return InlineRuns(self.world, api=self.api, runtime=self.runtime, path=self.path, poll_sec=0)

    def start(self, materials, count):
        preview = self.runs.preview(session_id="sess", materials=materials, count=count)
        self.assertTrue(preview["ok"], preview)
        return preview, self.runs.start(session_id="sess", token=preview["token"])

    def steps(self):
        return [(c[2], c[4]) for c in self.api.calls if c[0] == "plan"]

    def test_preview_validates_and_orders_a_b_c(self):
        for bad in (0, -1, "2.5", "abc", 101, True, None):
            with self.subTest(count=bad), self.assertRaises(RepeatRunError):
                self.runs.preview(session_id="sess", materials=["mat_a"], count=bad)
        with self.assertRaises(RepeatRunError):
            self.runs.preview(session_id="sess", materials=[], count=1)
        with self.assertRaises(RepeatRunError):
            self.runs.preview(session_id="sess", materials=["mat_x"], count=1)
        p = self.runs.preview(session_id="sess", materials=["mat_c", "mat_a"], count="2")
        self.assertEqual([m["name"] for m in p["materials"]], ["A자재", "C자재"])   # A → B → C 순서
        self.assertEqual(p["sequence"], ["A자재 컨베이어로 이송", "A자재 원래 자리 복귀",
                                         "C자재 컨베이어로 이송", "C자재 원래 자리 복귀"])
        self.assertEqual((p["count"], p["total_steps"]), (2, 8))
        self.assertEqual(self.api.calls, [])                                         # 미리보기는 실행하지 않는다

    def test_wrong_initial_position_is_refused_without_moving(self):
        self.world.where["mat_b"] = "loc_conveyor"
        p = self.runs.preview(session_id="sess", materials=["mat_b"], count=1)
        self.assertFalse(p["ok"])
        self.assertNotIn("token", p)
        self.assertTrue(any("원래 자리" in x and "움직이지 않습니다" in x for x in p["problems"]))
        self.assertEqual(self.api.calls, [])

    def test_single_material_repeats_transfer_then_return(self):
        _, run = self.start(["mat_a"], 2)
        run = self.runs.view()
        self.assertEqual(run["state"], "completed", run)
        self.assertEqual(self.steps(), [("mat_a", "loc_conveyor"), ("mat_a", "loc_pallet_1")] * 2)
        plans = [c for c in self.api.calls if c[0] == "plan"]
        self.assertTrue(all(c[5] is True for c in plans))                            # 단계 계획은 정지 래치를 풀지 않는다
        self.assertIn("[반복 작업 1/2회] A자재 컨베이어로 이송", plans[0][1])
        self.assertEqual(run["label"], "2/2회 · 완료")
        self.assertIsNone(self.cell.reservation())                                    # 끝나면 예약을 푼다

    def test_multiple_materials_round_order(self):
        self.start(["mat_b", "mat_a"], 3)
        self.assertEqual(self.steps(), [("mat_a", "loc_conveyor"), ("mat_a", "loc_pallet_1"),
                                        ("mat_b", "loc_conveyor"), ("mat_b", "loc_pallet_2")] * 3)
        self.assertEqual(self.runs.view()["state"], "completed")

    def test_finish_after_current_round(self):
        def on_execute(n, intent):
            if n == 2:                                                     # 1회차 도중 요청
                self.runs.finish_after_round(self.runs.view()["run_id"])
        self.api.on_execute = on_execute
        self.start(["mat_a", "mat_b"], 3)
        run = self.runs.view()
        self.assertEqual(run["state"], "completed")
        self.assertEqual(len(self.steps()), 4)                             # 선택한 자재 전부의 이송·복귀까지
        self.assertIn("현재 회차 후 종료", run["reason"])

    def test_failure_stops_without_retry(self):
        self.api.on_execute = lambda n, intent: ({"ok": False, "execution_id": "e2",
                                                  "final": {"task_succeeded": False, "reason_code": "exec.unverifiable",
                                                            "evidence": {"detail": "최종 위치를 관측하지 못했다"}}}
                                                 if n == 2 else None)
        self.start(["mat_a"], 3)
        run = self.runs.view()
        self.assertEqual(run["state"], "failed")
        self.assertEqual(len([c for c in self.api.calls if c[0] == "execute"]), 2)   # 다시 시도하지 않는다
        self.assertIn("최종 위치를 관측하지 못했다", run["reason"])
        self.assertIn("자동으로 다시 시도하지 않습니다", run["reason"])

    def test_global_stop_during_step_and_between_steps(self):
        self.api.on_execute = lambda n, intent: ({"ok": False, "interrupted": "exec.stopped", "execution_id": "e",
                                                  "final": {}} if n == 2 else None)
        self.start(["mat_a"], 2)
        self.assertEqual(self.runs.view()["state"], "stopped")
        self.assertEqual(len([c for c in self.api.calls if c[0] == "execute"]), 2)

        self.world.__init__()
        self.api.calls.clear()
        self.api.intents.clear()

        def stop_between(n, intent):
            if n == 1:
                with self.api._flag_lock:
                    self.api._stop_requested = True
        self.api.on_execute = stop_between
        self.start(["mat_a"], 2)
        self.assertEqual(self.runs.view()["state"], "stopped")
        self.assertEqual(len([c for c in self.api.calls if c[0] == "execute"]), 1)   # 정지 뒤 다음 단계 없음

    def test_pause_mid_step_then_resume_between_steps_and_duplicate_guards(self):
        def pause_on_second(n, intent):
            if n == 2:
                self.runs.pause(self.runs.view()["run_id"])
                return {"ok": False, "interrupted": "exec.canceled", "execution_id": "e2", "final": {}}
            return None
        self.api.on_execute = pause_on_second
        _, run = self.start(["mat_a"], 1)
        run = self.runs.view()
        self.assertEqual(run["state"], "paused")
        self.assertIn(("cancel_active", "sess"), self.api.calls)          # 이 반복의 실행만 취소
        self.assertIn("일시정지", run["label"])
        self.assertIsNotNone(self.cell.reservation())                      # 일시정지 중에도 셀은 반복의 것
        # 일시정지 중 새 반복은 시작하지 않는다(중복 방지)
        self.world.where["mat_a"] = "loc_pallet_1"
        p = self.runs.preview(session_id="sess", materials=["mat_b"], count=1)
        with self.assertRaises(RepeatRunError):
            self.runs.start(session_id="sess", token=p.get("token") or "x")
        # 재개(기록상 정지 지점 없음 → 그 단계를 다시 계획해 이어서)
        self.world.where["mat_a"] = "loc_conveyor"
        self.api.on_execute = None
        self.runs.resume(run["run_id"])
        self.assertEqual(self.runs.view()["state"], "completed")

    def test_resume_refused_when_stopped_where_it_cannot_continue(self):
        def pause_first(n, intent):
            self.runs.pause(self.runs.view()["run_id"])
            self.world.records["material_a"] = {"state": "stopped_unrestored"}
            return {"ok": False, "interrupted": "exec.canceled", "execution_id": "e1", "final": {}}
        self.api.on_execute = pause_first
        _, run = self.start(["mat_a"], 1)
        run = self.runs.view()
        with self.assertRaises(RepeatRunError) as caught:
            self.runs.resume(run["run_id"])
        self.assertIn("복구", caught.exception.detail)
        self.runs.cancel(run["run_id"])
        self.assertEqual(self.runs.view()["state"], "cancelled")
        self.assertIsNone(self.cell.reservation())

    def test_token_is_single_use_and_session_bound(self):
        p = self.runs.preview(session_id="sess", materials=["mat_a"], count=1)
        self.runs.start(session_id="sess", token=p["token"])
        with self.assertRaises(RepeatRunError):
            self.runs.start(session_id="sess", token=p["token"])

    def test_unexpected_position_before_a_step_stops_without_moving(self):
        def move_b_onto_origin(n, intent):
            if n == 1:
                self.world.where["mat_a"] = "loc_conveyor"
                self.world.where["mat_b"] = "loc_pallet_1"          # 누가 A의 원래 자리를 차지했다
                return {"ok": True, "execution_id": "e1", "final": {"task_succeeded": True}}
            return None
        self.api.on_execute = move_b_onto_origin
        self.start(["mat_a"], 1)
        run = self.runs.view()
        self.assertEqual(run["state"], "failed")
        self.assertIn("B자재", run["reason"])
        self.assertEqual(len([c for c in self.api.calls if c[0] == "execute"]), 1)   # 복귀를 실행하지 않았다

    def test_server_restart_marks_active_run_interrupted(self):
        self.path.write_text(json.dumps({"run": {"run_id": "rep_x", "state": "running", "steps": [
            {"round": 1, "material": "mat_a", "model": "material_a", "name": "A자재", "phase": "transfer"}],
            "cursor": 0, "count": 1, "materials": [{"id": "mat_a"}], "history": []}}), encoding="utf-8")
        runs = self.make()
        run = runs.view()
        self.assertEqual(run["state"], "interrupted")
        self.assertIn("서버가 다시 시작돼", run["reason"])
        self.assertEqual(self.api.calls, [])                                # 이어서 하지 않는다

    def test_interrupted_run_keeps_cell_locked_until_verified(self):
        """중단 표시만으로 잠금을 풀지 않는다 — 이어받은 실행의 임대가 있어도 예약하고, 확인이 통과해야만 푼다."""
        self.path.write_text(json.dumps({"run": {"run_id": "rep_x", "state": "running", "steps": [
            {"round": 1, "material": "mat_b", "model": "material_b", "name": "B자재", "phase": "transfer"}],
            "cursor": 0, "count": 1, "materials": [{"id": "mat_b"}], "history": []}}), encoding="utf-8")
        recovered = self.cell.try_acquire(owner="sim_demo_goal", operation_id="genexec_old")   # 재시작 뒤 이어받은 실행
        verdicts = [{"ok": False, "checks": [{"name": "process", "ok": False, "detail": "실행 프로세스가 아직 동작 중"}],
                     "notes": []},
                    {"ok": True, "checks": [{"name": "process", "ok": True, "detail": "없음"}], "notes": []}]
        runs = InlineRuns(self.world, api=self.api, runtime=self.runtime, path=self.path, poll_sec=0,
                          quiet_check=lambda: verdicts.pop(0))
        run = runs.view()
        self.assertEqual((run["state"], run["lock_held"]), ("interrupted", True))
        self.assertEqual(self.cell.reservation().owner, "repeat_interrupted")
        self.cell.release(recovered)                                       # 이어받은 실행이 끝나도
        self.assertIsNone(self.cell.try_acquire(owner="general_execute", operation_id="p"))   # 일반 실행은 여전히 막힌다
        preview = runs.preview(session_id="sess", materials=["mat_a"], count=1)
        self.assertFalse(preview["ok"])
        self.assertTrue(any("상태 확인 후 잠금 해제" in p for p in preview["problems"]), preview["problems"])
        with self.assertRaises(RepeatRunError):
            runs.resume("rep_x")
        run = runs.verify("rep_x")                                         # 확인 실패 → 잠금 유지
        self.assertTrue(run["lock_held"])
        self.assertFalse(run["check"]["ok"])
        self.assertIsNotNone(self.cell.reservation())
        # 확인 전 서버가 또 재시작해도 잠금은 그대로 다시 잡힌다.
        self.cell.unreserve(self.cell.reservation())
        runs = InlineRuns(self.world, api=self.api, runtime=self.runtime, path=self.path, poll_sec=0,
                          quiet_check=lambda: verdicts.pop(0))
        self.assertTrue(runs.view()["lock_held"])
        self.assertEqual(self.cell.reservation().owner, "repeat_interrupted")
        run = runs.verify("rep_x")                                         # 확인 통과 → 해제
        self.assertFalse(run["lock_held"])
        self.assertIsNone(self.cell.reservation())
        self.assertEqual(run["state"], "interrupted")                      # 이어서 하지 않는다
        self.assertEqual(self.api.calls, [])
        with self.assertRaises(RepeatRunError):
            runs.verify("rep_x")
        self.assertTrue(runs.preview(session_id="sess", materials=["mat_a"], count=1)["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
