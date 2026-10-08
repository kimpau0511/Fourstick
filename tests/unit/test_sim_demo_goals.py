"""Deterministic tests for constrained simulation-demo goals and routes."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.api import ApiError  # noqa: E402
from server.sim_demo_goals import (  # noqa: E402
    GOAL_RETURN_ALL,
    SimDemoGoalError,
    SimDemoGoals,
)
from server.sim_demo_jobs import SimDemoJobs  # noqa: E402
from tests.unit.test_sim_demo_web import (  # noqa: E402
    GRASP,
    WORKCELL,
    JobsBase,
    body,
)
from tests.unit.test_simulation_demo_checkpoint import completed_result  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)


class GoalsBase(JobsBase):
    def setUp(self):
        super().setUp()
        self.jobs = SimDemoJobs(
            workcell=WORKCELL,
            grasp_config=GRASP,
            state_path=self.state_path,
            jobs_dir=self.tmp / "goal-jobs",
            stop_request=self.tmp / "goal-stop.json",
            popen=self.popen,
            environ={"PATH": "/usr/bin"},
        )
        self.state = SimulationDemoState(self.state_path)
        self.goals = SimDemoGoals(
            self.jobs, clock=lambda: 123.0, sleep=lambda _: None,
            auto_run=False,
        )

    def hold(self, model: str, slot: str) -> None:
        self.state.record_run(
            policy=POLICY_DEMO_HOLD,
            model=model,
            result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75),
            restored=None,
            slot=slot,
        )

    def clear(self, model: str) -> None:
        self.state.record_return(
            model,
            completed=True,
            stop_requested=False,
            final_pose_m=(0.0, 0.0, 0.0),
            origin_home_m=(0.0, 0.0, 0.0),
        )


class SimDemoGoalsTest(GoalsBase):
    def test_create_requires_fixed_goal_and_orders_return_plan_by_slot(self):
        self.hold("material_a", "slot_3")
        self.hold("material_c", "slot_1")
        self.hold("material_b", "slot_2")

        with self.assertRaises(SimDemoGoalError) as caught:
            self.goals.create("move_everything")
        self.assertEqual(caught.exception.status, 400)

        goal = self.goals.create(GOAL_RETURN_ALL)
        self.assertEqual(goal["status"], "planned")
        self.assertTrue(goal["confirmation_required"])
        self.assertEqual(
            [(row["step"], row["action"], row["material"], row["from"])
             for row in goal["plan"]],
            [(1, "return", "material_c", "slot_1"),
             (2, "return", "material_b", "slot_2"),
             (3, "return", "material_a", "slot_3")],
        )
        self.assertEqual(self.popen.calls, [])

    def test_confirm_holds_cell_manager_and_denies_general_lease(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)

        confirmed = self.goals.confirm(goal["goal_id"], "confirm")

        self.assertEqual(confirmed["status"], "running")
        active = self.jobs.cell_execution.current()
        self.assertIsNotNone(active)
        self.assertEqual(active.owner, "sim_demo_goal")
        self.assertEqual(active.operation_id, goal["goal_id"])
        self.assertIsNone(self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_waiting"))
        self.assertTrue(self.jobs.release_goal(goal["goal_id"]))

    def test_confirm_rejects_changed_state_and_releases_reservation(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)
        self.hold("material_b", "slot_2")

        with self.assertRaises(SimDemoGoalError) as caught:
            self.goals.confirm(goal["goal_id"], "confirm")

        self.assertEqual(caught.exception.status, 409)
        self.assertIn("상태가 바뀌었다", str(caught.exception))
        self.assertIsNone(self.jobs.cell_execution.current())
        self.assertIsNone(self.jobs.status()["goal_reservation"])
        self.assertEqual(self.popen.calls, [])

    def test_manual_run_completes_two_steps_after_each_state_record_disappears(self):
        self.hold("material_b", "slot_2")
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")
        observations = []

        def finish_current(_seconds):
            call_no = len(self.popen.calls)
            running = self.goals.get(goal_id)
            material = running["plan"][call_no - 1]["material"]
            observations.append((call_no, running["progress"]["completed"],
                                 material in self.state.objects()))
            self.clear(material)
            self.popen.procs[call_no - 1].code = 0

        self.goals._sleep = finish_current
        self.goals.run(goal_id)

        result = self.goals.get(goal_id)
        self.assertEqual(observations, [(1, 0, True), (2, 1, True)])
        self.assertEqual([call["argv"][2] for call in self.popen.calls],
                         ["material_b", "material_a"])  # 2026-09-25: A(1번)의 복귀 경로가 2번 칸을 스친다(측정) → 2번의 B가 먼저
        self.assertEqual([row["status"] for row in result["plan"]],
                         ["completed", "completed"])
        self.assertEqual(result["progress"],
                         {"completed": 2, "total": 2, "percent": 100})
        self.assertEqual(result["status"], "completed")

    def test_failed_state_verification_fails_without_starting_next_step(self):
        self.hold("material_a", "slot_1")
        self.hold("material_b", "slot_2")
        goal = self.goals.create(GOAL_RETURN_ALL)
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")

        def process_exits_but_record_remains(_seconds):
            self.popen.procs[0].code = 0

        self.goals._sleep = process_exits_but_record_remains
        self.goals.run(goal_id)

        result = self.goals.get(goal_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["progress"]["completed"], 0)
        self.assertEqual(result["plan"][0]["status"], "failed")
        self.assertEqual(result["plan"][1]["status"], "pending")
        self.assertTrue(result["plan"][0]["result"]["still_on_conveyor"])
        self.assertEqual(len(self.popen.calls), 1)

    def test_step_with_record_observation_mismatch_does_not_start(self):
        """2026-10-08 리뷰 11번: 목표 단계도 시작 전에 기록과 관측을 함께 본다."""
        from server.material_check import check_material_start

        self.hold("material_a", "slot_1")
        moved = self.view.sample()
        moved["materials"]["material_a"] = [0.15, -0.5, 0.75, 0, 0, 0, 1]      # 기록은 1번 칸, 관측은 다른 곳
        view = types.SimpleNamespace(sample=lambda: moved)
        self.goals.material_check = lambda m, s, d: check_material_start(self.jobs, view, m, s, d,
                                                                         log_dir=self.tmp)
        goal = self.goals.create(GOAL_RETURN_ALL)
        self.goals.confirm(goal["goal_id"], "confirm")
        self.goals.run(goal["goal_id"])
        result = self.goals.get(goal["goal_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("정합", result["plan"][0]["result"]["detail"])
        self.assertEqual(self.popen.calls, [])

    def test_stop_sets_stopping_and_asks_jobs_to_stop(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")
        reasons = []

        def request_stop(*, reason):
            reasons.append(reason)
            return {"requested": False, "detail": "no child yet"}

        self.jobs.request_stop = request_stop
        response = self.goals.request_stop(goal_id)

        self.assertTrue(response["requested"])
        self.assertEqual(reasons, ["sim_demo_goal_stop"])
        self.assertEqual(self.goals.get(goal_id)["status"], "stopping")
        self.assertTrue(self.goals.get(goal_id)["stop_requested"])
        self.goals.run(goal_id)
        self.assertEqual(self.goals.get(goal_id)["status"], "stopped")

    def test_goal_lease_is_released_only_after_terminal_outcome(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")
        released_at = []
        real_release = self.jobs.release_goal

        def release_goal(releasing_goal_id):
            released_at.append(self.goals.get(releasing_goal_id)["status"])
            return real_release(releasing_goal_id)

        self.jobs.release_goal = release_goal

        def finish(_seconds):
            self.assertEqual(self.goals.get(goal_id)["status"], "running")
            self.assertIsNotNone(self.jobs.cell_execution.current())
            self.clear("material_a")
            self.popen.procs[0].code = 0

        self.goals._sleep = finish
        self.goals.run(goal_id)

        self.assertEqual(released_at, ["completed"])
        self.assertIsNone(self.jobs.cell_execution.current())
        lease = self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_after_goal")
        self.assertIsNotNone(lease)
        self.jobs.cell_execution.release(lease)


class SimDemoGoalRoutesTest(GoalsBase):
    def setUp(self):
        super().setUp()
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=self.jobs,
            sim_demo_goals=self.goals,
            sim_demo_disabled_reason=None,
            sim_view=self.view,
        )

    def call(self, method: str, path: str, payload=None):
        from server.routes import sim_demo

        ctx = types.SimpleNamespace(
            runtime=self.runtime,
            read_body=body(payload or {}),
        )
        return asyncio.run(sim_demo.handle(ctx, method, path, None, {}))

    def test_create_get_confirm_and_stop_routes(self):
        self.hold("material_b", "slot_2")
        self.hold("material_a", "slot_1")

        # 2026-10-08 리뷰 2번: 목표 API는 세션 필수 — 만든 세션만 확인한다.
        status, _, raw = self.call(
            "POST", "/v1/sim-demo/goals", {"goal": GOAL_RETURN_ALL, "session_id": "s1"})
        created = json.loads(raw)
        goal_id = created["goal_id"]
        self.assertEqual(status, 201)
        self.assertEqual([row["material"] for row in created["plan"]],
                         ["material_b", "material_a"])  # 2026-09-25: A(1번)의 복귀 경로가 2번 칸을 스친다(측정) → 2번의 B가 먼저

        status, _, raw = self.call("GET", f"/v1/sim-demo/goals/{goal_id}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["goal_id"], goal_id)

        with self.assertRaises(ApiError) as other:
            self.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm", "session_id": "s2"})
        self.assertEqual(other.exception.status, 409)
        status, _, raw = self.call(
            "POST", f"/v1/sim-demo/goals/{goal_id}/confirm",
            {"action": "confirm", "session_id": "s1"})
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["status"], "running")

        status, _, raw = self.call(
            "POST", f"/v1/sim-demo/goals/{goal_id}/stop")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["requested"])
        self.assertEqual(self.goals.get(goal_id)["status"], "stopping")
        self.goals.run(goal_id)

    def test_routes_translate_invalid_fixed_goal_and_missing_goal(self):
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/v1/sim-demo/goals", {"goal": "arbitrary", "session_id": "s1"})
        self.assertEqual(caught.exception.status, 400)

        with self.assertRaises(ApiError) as caught:
            self.call("GET", "/v1/sim-demo/goals/simgoal_missing")
        self.assertEqual(caught.exception.status, 404)

    def test_text_command_creates_ordered_fixed_goal_without_child_job(self):
        self.hold("material_b", "slot_2")
        self.hold("material_a", "slot_1")

        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "컨베이어에 있는 자재를 모두 제자리에 가져다놔",
        })
        payload = json.loads(raw)

        self.assertEqual((status, payload["decision"], payload["intent"]),
                         (200, "CONFIRM_GOAL", GOAL_RETURN_ALL))
        self.assertEqual(payload["confirmation"]["kind"], "goal")
        self.assertEqual(payload["confirmation"]["action"], "confirm")
        self.assertEqual(payload["confirmation"]["goal_id"],
                         payload["goal"]["goal_id"])
        self.assertEqual(payload["confirmation"]["summary"],
                         payload["goal"]["summary"])
        self.assertEqual(
            [row["material"] for row in payload["confirmation"]["plan"]],
            ["material_b", "material_a"])  # 2026-09-25: A(1번)의 복귀 경로가 2번 칸을 스친다(측정) → 2번의 B가 먼저
        self.assertEqual(self.popen.calls, [], "confirmation 전에 child job이 생겼다")

    def test_stt_goal_cancel_calls_goal_endpoint_and_moves_nothing(self):
        self.hold("material_a", "slot_1")
        _, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "stt_final",
            "raw_transcript": "컨베이어 자재 전부 원위치로 돌려 놔",
            "utterance": "컨베이어 자재 전부 원위치로 돌려 놔",
        })
        planned = json.loads(raw)
        goal_id = planned["confirmation"]["goal_id"]

        status, _, raw = self.call(
            "POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "cancel"})

        self.assertEqual((status, json.loads(raw)["status"]), (202, "cancelled"))
        self.assertEqual(self.popen.calls, [])
        self.assertIsNone(self.jobs.cell_execution.current())

    def test_text_goal_confirm_uses_existing_endpoint(self):
        self.hold("material_a", "slot_1")
        _, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "컨베이어의 모든 자재를 원래 자리로 복귀해",
        })
        goal_id = json.loads(raw)["confirmation"]["goal_id"]

        status, _, raw = self.call(
            "POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})

        self.assertEqual((status, json.loads(raw)["status"]), (202, "running"))
        self.assertEqual(self.popen.calls, [])  # auto_run=False; endpoint only confirmed it

    def test_ambiguous_and_unsupported_collective_goals_do_not_fall_through(self):
        cases = (
            ("자재를 모두 원래 자리로 옮겨", 200, "ASK"),
            ("컨베이어의 모든 자재를 검사대로 옮겨", 409, "BLOCK"),
        )
        for utterance, expected_status, decision in cases:
            with self.subTest(utterance=utterance):
                status, _, raw = self.call("POST", "/v1/sim-demo/command", {
                    "mode": "simulation_demo", "source": "text",
                    "utterance": utterance,
                })
                payload = json.loads(raw)
                self.assertEqual((status, payload["decision"]),
                                 (expected_status, decision))
                self.assertIsNone(payload["job"])
                self.assertIsNone(payload["confirmation"])
        self.assertEqual(self.popen.calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
