"""계획 → 검증 → 실행 → 결과 검증 통합 흐름 (Fake Adapter 기준).

terminal_hold가 실제로 연결되는지 확인한다. 세 단계가 분리돼 있어야 한다.

  1. 계획 검증 (validation/safety_validator.py E-HOLD-003)
     — 스텝을 따라가면 계획이 명시한 목표 종료 상태에 도달하는가
  2. 실행 허가 (validation/execution_permit.py)
     — 지금 실행해도 되는가
  3. 결과 검증 (validation/outcome_verifier.py)
     — 실제로 그 상태로 끝났는가(실측 관측과 대조)

1단계 통과를 작업 성공으로 세지 않는다. 시뮬레이터·실제 시간 없이 끝난다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.constants import ATOMIC_SKILLS
from core.execution_state import ExecutionState
from core.policy import FreshnessPolicy, SafetyPolicy
from core.reason_codes import ReasonCode
from core.resource_catalog import (
    ResourceCatalog,
    ResourceEntry,
    ResourceKind,
)
from core.task_plan import TaskPlan, TaskStep
from robots.fake.adapter import FakeRobotAdapter, FakeWorld
from robots.fake.transport import FakeTransport, GoalStatus, Scenario
from robots.registry import RegistryError, RobotRegistry
from validation.execution_permit import PermitContext, check_execution_permit
from validation.outcome_verifier import finalize_plan_result, verify_terminal_hold
from validation.safety_validator import (
    ResourceCatalog,
    RuleStatus,
    SafetyDecision,
    aggregate,
    evaluate,
)

from tests.contract.test_fake_adapter import make_policy, make_profile, steady

TIMEOUT = 1.0
SAFETY = SafetyPolicy("fixture", 12,
                       {"max_steps": "fixture",
                        "required_final_skill": "fixture"},
                       required_final_skill="home")
FRESH = FreshnessPolicy(
    "fixture", 1.0, 3.0,
    {"robot_state_max_age_sec": "fixture", "environment_max_age_sec": "fixture"},
)

def make_catalog(
    locations=("loc_a", "loc_b", "loc_c"), objects=("obj_a", "obj_b"),
    *, catalog_version="fixture-1.0",
) -> ResourceCatalog:
    """시험용 카탈로그. 별칭은 id 자체로 둔다 — 여기서 검증하려는 것은
    별칭 해석이 아니라 인자 대조다(별칭 해석은 test_slot_extractor.py)."""
    entries = [
        ResourceEntry(resource_id=r, kind=ResourceKind.LOCATION,
                      display_name=r, aliases=(r,))
        for r in locations
    ] + [
        ResourceEntry(resource_id=r, kind=ResourceKind.OBJECT,
                      display_name=r, aliases=(r,))
        for r in objects
    ]
    return ResourceCatalog(catalog_version=catalog_version, entries=tuple(entries))


CATALOG = make_catalog(locations=("loc_a", "loc_b"), objects=("obj_a",))

TRANSFER = (
    TaskStep("home"),
    TaskStep("move", {"to": "loc_a"}),
    TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
    TaskStep("move", {"to": "loc_b"}),
    TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
    TaskStep("home"),
)
CARRY = (
    TaskStep("home"),
    TaskStep("move", {"to": "loc_a"}),
    TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
    TaskStep("home"),
)


def make_plan(steps, terminal_hold=None) -> TaskPlan:
    return TaskPlan(
        "plan_1", "fake_1", "fixture", "1.0", steps,
        created_at=1_000.0, ttl_sec=60.0, terminal_hold=terminal_hold,
    )


def make_registry(**adapter_kw) -> RobotRegistry:
    reg = RobotRegistry()
    reg.register(
        "fake_1",
        make_profile(),
        lambda rid, prof: FakeRobotAdapter(
            rid, prof,
            stop_policy=make_policy(),
            transport=FakeTransport(adapter_kw.get("scenarios") or {}),
            world=adapter_kw.get("world") or FakeWorld(samples=steady()),
        ),
    )
    return reg


def run_plan(adapter, plan: TaskPlan) -> tuple[bool, bool]:
    """스텝을 순서대로 실행한다. (모션 완료, 목표 도달) 반환."""
    motion_completed = target_reached = True
    for step in plan.steps:
        if step.skill == "home":
            res = adapter.home(TIMEOUT)
        elif step.skill == "move":
            res = adapter.move(step.args["to"], TIMEOUT)
        elif step.skill == "pick":
            res = adapter.pick(step.args["object"], step.args["from"], TIMEOUT)
        elif step.skill == "place":
            res = adapter.place(step.args["object"], step.args["to"], TIMEOUT)
        else:
            continue
        motion_completed = motion_completed and res.motion_completed
        target_reached = target_reached and res.target_reached
    return motion_completed, target_reached


def permit_context(plan: TaskPlan, **over) -> PermitContext:
    kw = dict(
        now=1_001.0, robot_ready=True, robot_state_observed_at=1_000.9,
        robot_state_valid=True, environment_observed_at=1_000.5,
        recorded_environment_version=1, current_environment_version=1,
        recorded_environment_session="s", current_environment_session="s",
        recorded_plan_hash=plan.plan_hash(), profile_id="fixture",
        profile_version="1.0", supported_skills=ATOMIC_SKILLS,
    )
    kw.update(over)
    return PermitContext(**kw)


class TestRegistryWiring(unittest.TestCase):
    def test_registry_creates_the_adapter_for_the_plan(self):
        reg = make_registry()
        adapter = reg.create("fake_1")
        self.assertEqual(adapter.robot_id, "fake_1")
        reg.require_skills("fake_1", {s.skill for s in TRANSFER})
        reg.require_profile("fake_1", "fixture", "1.0")

    def test_unregistered_robot_is_refused_before_execution(self):
        with self.assertRaises(RegistryError) as ctx:
            make_registry().create("nope")
        self.assertIs(ctx.exception.reason, ReasonCode.ROBOT_NOT_REGISTERED)

    def test_catalog_is_the_source_for_ui_lists(self):
        entry = make_registry().catalog()["fake_1"]
        self.assertEqual(entry["profile_id"], "fixture")
        self.assertEqual(set(entry["supported_skills"]), set(ATOMIC_SKILLS))


class TestTerminalHoldWiredEndToEnd(unittest.TestCase):
    """계획의 목표 종료 상태가 실측 관측과 대조되는지."""

    def _flow(self, steps, terminal_hold, **adapter_kw):
        plan = make_plan(steps, terminal_hold)
        results = evaluate(plan, SAFETY, CATALOG)
        reg = make_registry(**adapter_kw)
        adapter = reg.create("fake_1")
        adapter.connect(TIMEOUT)
        permit = check_execution_permit(plan, results, permit_context(plan), FRESH)
        motion, target = (False, False)
        if permit.granted:
            motion, target = run_plan(adapter, plan)
        outcome = verify_terminal_hold(plan, adapter.state())
        final = finalize_plan_result(
            outcome, motion_completed=motion, target_reached=target
        )
        return aggregate(results), permit, outcome, final

    def test_transfer_plan_succeeds_and_ends_empty_handed(self):
        decision, permit, outcome, final = self._flow(TRANSFER, None)
        self.assertIs(decision, SafetyDecision.ALLOW)
        self.assertTrue(permit.granted)
        self.assertTrue(outcome.verified)
        self.assertTrue(outcome.matched)
        self.assertTrue(final.task_succeeded)
        self.assertIs(final.state, ExecutionState.COMPLETED)

    def test_carry_plan_succeeds_while_still_holding(self):
        """쥔 채 종료가 목표인 계획은 정상이다."""
        decision, permit, outcome, final = self._flow(CARRY, "obj_a")
        self.assertIs(decision, SafetyDecision.ALLOW)
        self.assertTrue(final.task_succeeded)
        self.assertEqual(outcome.observed, "obj_a")

    def test_plan_validation_catches_intent_mismatch_before_execution(self):
        """1단계에서 잡히면 실행 허가가 나지 않는다."""
        plan = make_plan(CARRY, None)          # 이송 목표인데 쥔 채 끝나는 계획
        results = evaluate(plan, SAFETY, CATALOG)
        hold = next(r for r in results if r.code == "E-HOLD-003")
        self.assertIs(hold.status, RuleStatus.BLOCK)
        permit = check_execution_permit(plan, results, permit_context(plan), FRESH)
        self.assertFalse(permit.granted)
        self.assertIn(ReasonCode.EXEC_PERMIT_DENIED, permit.reason_codes())

    def test_object_dropped_during_execution_is_caught_by_outcome_check(self):
        """계획은 맞았는데 실행 중 물체를 놓친 경우 — 3단계에서만 잡힌다."""
        world = FakeWorld(samples=steady())
        decision, permit, outcome, final = self._flow(
            CARRY, "obj_a",
            world=world,
            scenarios={"gripper": Scenario(result_status=GoalStatus.ABORTED)},
        )
        self.assertIs(decision, SafetyDecision.ALLOW)   # 계획 자체는 문제 없음
        self.assertTrue(permit.granted)
        self.assertTrue(outcome.verified)
        self.assertFalse(outcome.matched)               # 실측은 목표와 다름
        self.assertFalse(final.task_succeeded)
        self.assertIs(final.reason, ReasonCode.EXEC_TASK_FAILED)
        self.assertEqual(final.evidence["terminal_hold_expected"], "obj_a")
        self.assertIsNone(final.evidence["terminal_hold_observed"])

    def test_unobservable_hold_ends_as_unverifiable_not_success(self):
        decision, permit, outcome, final = self._flow(
            TRANSFER, None, world=FakeWorld(samples=steady(), hold_observed=False)
        )
        self.assertFalse(outcome.verified)
        self.assertFalse(final.task_succeeded)
        self.assertIs(final.state, ExecutionState.UNKNOWN)
        self.assertIs(final.reason, ReasonCode.EXEC_UNVERIFIABLE)

    def test_motion_failure_does_not_become_success_even_when_hold_matches(self):
        decision, permit, outcome, final = self._flow(
            TRANSFER, None, scenarios={"motion": Scenario(result_status=GoalStatus.ABORTED)}
        )
        self.assertTrue(outcome.matched)                # 손은 비어 있음(목표와 일치)
        self.assertFalse(final.task_succeeded)          # 그러나 모션이 실패했다
        self.assertIs(final.reason, ReasonCode.EXEC_GOAL_NOT_REACHED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
