"""실행 결과 판정 계약 — 파지 관측을 요구하는 계획의 구분 (8-09).

확인하는 것:

- `home`·빈손 `move`·`stop`은 파지 관측을 요구하지 않는다
- 물체 보유 상태가 목표에 포함된 계획만 요구한다
- 판정은 **명시적 선언**(`terminal_hold`, 카탈로그 `arg_kinds`)에서 나온다.
  스킬 이름으로 분기하지 않는다
- 요구되지 않는 계획에서 **grasp 관측 API를 호출하지 않는다**
- 파지 관측 없음이 home/move 성공을 막지 않고, pick/place 성공으로도 바뀌지 않는다
- 정지는 관측으로 확인된 뒤에만 STOPPED다
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_skill_catalog
from core.execution_state import ExecutionState
from core.task_plan import TaskPlan, TaskStep
from robots.base.robot_adapter import RobotStateSnapshot
from validation.outcome_verifier import (
    StopOutcome,
    finalize_plan_result,
    hold_requirement,
    verify_terminal_hold,
)

CATALOG = load_skill_catalog(json.loads(
    (ROOT / "examples/config/valid_skill_catalog.json").read_text(encoding="utf-8")))


def plan(steps, *, terminal_hold=None) -> TaskPlan:
    return TaskPlan(
        plan_id="plan_1", robot_id="r", profile_id="p", profile_version="1",
        steps=tuple(TaskStep(skill=s, args=dict(a)) for s, a in steps),
        created_at=0.0, ttl_sec=600.0,
        terminal_hold=terminal_hold,
    )


def observed(*, held=None, hold_observed=True, valid=True) -> RobotStateSnapshot:
    return RobotStateSnapshot(
        state=ExecutionState.IDLE, observed_at=1.0,
        joint_positions={}, joint_velocities={}, valid=valid,
        held_object=held, hold_observed=hold_observed)


class TestHoldRequirement(unittest.TestCase):
    def test_home_only_plan_does_not_require_hold_observation(self):
        requirement = hold_requirement(plan([("home", {})]), CATALOG)
        self.assertFalse(requirement.required)
        self.assertIsNone(requirement.terminal_hold)
        self.assertEqual(requirement.object_steps, ())

    def test_empty_handed_move_plan_does_not_require_hold_observation(self):
        requirement = hold_requirement(
            plan([("move", {"to": "loc_pallet_1"}), ("home", {})]), CATALOG)
        self.assertFalse(requirement.required)

    def test_stop_plan_does_not_require_hold_observation(self):
        requirement = hold_requirement(plan([("stop", {})]), CATALOG)
        self.assertFalse(requirement.required)

    def test_pick_plan_requires_hold_observation(self):
        requirement = hold_requirement(
            plan([("move", {"to": "loc_pallet_1"}),
                  ("pick", {"object": "obj_a", "from": "loc_pallet_1"})],
                 terminal_hold="obj_a"), CATALOG)
        self.assertTrue(requirement.required)
        self.assertEqual(requirement.terminal_hold, "obj_a")

    def test_pick_and_place_plan_requires_hold_observation_even_when_ending_empty(self):
        """놓고 끝나는 계획도 **보유 상태가 목표에 포함된다** — 확인해야 한다."""
        requirement = hold_requirement(
            plan([("move", {"to": "loc_pallet_1"}),
                  ("pick", {"object": "obj_a", "from": "loc_pallet_1"}),
                  ("move", {"to": "loc_conveyor"}),
                  ("place", {"object": "obj_a", "to": "loc_conveyor"}),
                  ("home", {})]), CATALOG)
        self.assertTrue(requirement.required)
        self.assertIsNone(requirement.terminal_hold)
        # 근거는 카탈로그가 물체로 선언한 인자다(스킬 이름이 아니다).
        self.assertIn("물체(object)로 선언한 인자", requirement.basis)
        self.assertEqual([row[1] for row in requirement.object_steps],
                         ["pick", "place"])

    def test_terminal_hold_alone_requires_observation(self):
        requirement = hold_requirement(
            plan([("home", {})], terminal_hold="obj_a"), CATALOG)
        self.assertTrue(requirement.required)
        self.assertIn("terminal_hold", requirement.basis)

    def test_unknown_skill_is_conservatively_required(self):
        """모르는 스킬을 '관측 불필요'로 넘기지 않는다."""
        empty = load_skill_catalog({
            "catalog_version": "only-home",
            "entries": [{"skill": "home", "description": "홈", "arg_kinds": {}}],
        })
        requirement = hold_requirement(
            plan([("move", {"to": "loc_pallet_1"})]), empty)
        self.assertTrue(requirement.required)
        self.assertIn("선언되지 않은", requirement.basis)

    def test_basis_is_recorded_in_the_payload(self):
        requirement = hold_requirement(plan([("home", {})]), CATALOG)
        payload = requirement.to_dict()
        self.assertFalse(payload["hold_required"])
        self.assertTrue(payload["basis"])
        self.assertEqual(payload["object_steps"], [])


class TestFinalizeWithoutHoldObservation(unittest.TestCase):
    """파지 관측이 없어도 home/move는 성공으로 판정된다."""

    def setUp(self):
        self.plan = plan([("move", {"to": "loc_pallet_1"}), ("home", {})])
        self.requirement = hold_requirement(self.plan, CATALOG)
        # 관측을 하지 않았다 — snapshot=None.
        self.outcome = verify_terminal_hold(self.plan, None)

    def test_motion_observed_is_success(self):
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=True)
        self.assertTrue(result.task_succeeded)
        self.assertEqual(result.state, ExecutionState.COMPLETED)
        self.assertIsNone(result.reason)
        self.assertFalse(result.evidence["hold_requirement"]["hold_required"])
        self.assertTrue(result.evidence["planning_scene_revalidated"])

    def test_goal_not_reached_is_failure_not_unverifiable(self):
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=False,
            requirement=self.requirement, scene_revalidated=True)
        self.assertFalse(result.task_succeeded)
        self.assertTrue(result.verified)
        self.assertEqual(result.reason.value, "exec.goal_not_reached")

    def test_scene_revalidation_failure_blocks_success(self):
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=False)
        self.assertFalse(result.task_succeeded)
        self.assertEqual(result.reason.value, "geometry.collision")

    def test_missing_requirement_keeps_the_conservative_behaviour(self):
        """요구 여부를 주지 않으면 예전처럼 파지 관측을 요구한다."""
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=True)
        self.assertFalse(result.task_succeeded)
        self.assertEqual(result.reason.value, "exec.unverifiable")


class TestFinalizeWithHoldObservation(unittest.TestCase):
    """파지 관측이 요구되는 계획은 관측 없이 성공하지 않는다."""

    def setUp(self):
        self.plan = plan([("move", {"to": "loc_pallet_1"}),
                          ("pick", {"object": "obj_a", "from": "loc_pallet_1"})],
                         terminal_hold="obj_a")
        self.requirement = hold_requirement(self.plan, CATALOG)

    def test_no_observation_stays_unverifiable(self):
        outcome = verify_terminal_hold(self.plan, observed(hold_observed=False))
        result = finalize_plan_result(
            outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=True)
        self.assertFalse(result.task_succeeded)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason.value, "exec.unverifiable")

    def test_snapshot_none_stays_unverifiable(self):
        """요구되는데 관측을 하지 않았으면 성공으로 바뀌지 않는다."""
        outcome = verify_terminal_hold(self.plan, None)
        result = finalize_plan_result(
            outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=True)
        self.assertFalse(result.task_succeeded)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason.value, "exec.unverifiable")

    def test_observed_match_is_success(self):
        outcome = verify_terminal_hold(self.plan, observed(held="obj_a"))
        result = finalize_plan_result(
            outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=True)
        self.assertTrue(result.task_succeeded)

    def test_observed_mismatch_is_failure(self):
        outcome = verify_terminal_hold(self.plan, observed(held=None))
        result = finalize_plan_result(
            outcome, motion_completed=True, target_reached=True,
            requirement=self.requirement, scene_revalidated=True)
        self.assertFalse(result.task_succeeded)
        self.assertTrue(result.verified)
        self.assertEqual(result.reason.value, "exec.task_failed")


class TestStopVerdict(unittest.TestCase):
    def setUp(self):
        self.plan = plan([("stop", {})])
        self.requirement = hold_requirement(self.plan, CATALOG)
        self.outcome = verify_terminal_hold(self.plan, None)

    def test_confirmed_stop_is_stopped_and_verified(self):
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=False,
            requirement=self.requirement,
            stop=StopOutcome(requested=True, confirmed=True,
                             detail="관측 확인"),
            scene_revalidated=None)
        self.assertEqual(result.state, ExecutionState.STOPPED)
        self.assertTrue(result.verified)
        self.assertEqual(result.reason.value, "exec.stopped")
        # 정지는 작업 성공이 아니다(계약 6번).
        self.assertFalse(result.task_succeeded)
        self.assertTrue(result.evidence["stop"]["stop_confirmed"])

    def test_unconfirmed_stop_is_not_verified(self):
        result = finalize_plan_result(
            self.outcome, motion_completed=True, target_reached=False,
            requirement=self.requirement,
            stop=StopOutcome(requested=True, confirmed=False,
                             detail="속도가 남아 있다"))
        self.assertFalse(result.verified)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertFalse(result.evidence["stop"]["stop_confirmed"])

    def test_stop_verdict_wins_over_hold_requirement(self):
        """물체를 든 계획이 정지로 끝나도 정지 관측이 기준이다."""
        held_plan = plan([("pick", {"object": "obj_a", "from": "loc_pallet_1"}),
                          ("stop", {})], terminal_hold="obj_a")
        requirement = hold_requirement(held_plan, CATALOG)
        self.assertTrue(requirement.required)
        result = finalize_plan_result(
            verify_terminal_hold(held_plan, None),
            motion_completed=True, target_reached=False,
            requirement=requirement,
            stop=StopOutcome(requested=True, confirmed=True))
        self.assertEqual(result.state, ExecutionState.STOPPED)
        self.assertTrue(result.verified)


class TestGraspApiIsNotCalledWhenNotRequired(unittest.TestCase):
    """요구되지 않는 계획에서 **grasp 관측 API를 호출하지 않는다.**

    호출자(`server/api.py`)가 `requirement.required`일 때만 `adapter.state()`를
    부르는 것을 모사해, 부르지 않는 경로가 실제로 성공 판정에 닿는지 본다.
    """

    class SpyAdapter:
        def __init__(self):
            self.state_calls = 0

        def state(self):
            self.state_calls += 1
            return observed(hold_observed=False)

    def _finalize(self, task_plan):
        adapter = self.SpyAdapter()
        requirement = hold_requirement(task_plan, CATALOG)
        snapshot = adapter.state() if requirement.required else None
        outcome = verify_terminal_hold(task_plan, snapshot)
        result = finalize_plan_result(
            outcome, motion_completed=True, target_reached=True,
            requirement=requirement, scene_revalidated=True)
        return adapter, result

    def test_pure_motion_plan_never_touches_grasp_observation(self):
        for steps in ([("home", {})],
                      [("move", {"to": "loc_pallet_1"}), ("home", {})],
                      [("stop", {})]):
            with self.subTest(steps=steps):
                adapter, result = self._finalize(plan(steps))
                self.assertEqual(adapter.state_calls, 0)
                # 그리고 그 결과가 성공을 막지 않는다.
                if steps != [("stop", {})]:
                    self.assertTrue(result.task_succeeded)

    def test_object_plan_does_observe(self):
        adapter, result = self._finalize(
            plan([("pick", {"object": "obj_a", "from": "loc_pallet_1"})],
                 terminal_hold="obj_a"))
        self.assertEqual(adapter.state_calls, 1)
        # 관측 수단이 없으므로 확인 불가로 남는다 — 성공으로 바뀌지 않는다.
        self.assertFalse(result.task_succeeded)
        self.assertEqual(result.reason.value, "exec.unverifiable")


if __name__ == "__main__":
    unittest.main()
