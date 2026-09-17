"""Adapter 적합성 테스트 (md/개발플랜.md 2-06).

**모든** Robot Adapter가 공통으로 통과해야 하는 테스트다. 새 로봇을 추가하면
`ADAPTERS`에 한 줄만 더해 같은 검사를 받는다 — 로봇마다 다른 테스트를 쓰면
규칙이 갈라지기 때문이다(계획.md 27장의 "새 로봇 추가 시 공통 코드 수정 0건").

여기서 확인하는 것은 계약 준수다. 로봇 고유 값이나 동작 품질은 보지 않는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.capability_profile import CapabilityProfile
from core.constants import ATOMIC_SKILLS
from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState, TERMINAL_STATES
from core.policy import StopPolicy
from core.reason_codes import ReasonCode
from robots.base.robot_adapter import RobotAdapter, RobotStateSnapshot
from robots.fake.adapter import CHANNEL_GRIPPER, CHANNEL_MOTION, FakeRobotAdapter, FakeWorld
from robots.fake.transport import FakeTransport, Scenario, SendOutcome

from tests.contract.test_fake_adapter import make_policy, make_profile, moving, steady

TIMEOUT = 1.0

AdapterFactory = Callable[[], RobotAdapter]


def _fake_factory() -> RobotAdapter:
    a = FakeRobotAdapter(
        "conformance",
        make_profile(),
        stop_policy=make_policy(),
        transport=FakeTransport(),
        world=FakeWorld(samples=steady()),
    )
    a.connect(TIMEOUT)
    return a


#: 새 Adapter가 생기면 여기에 (이름, 팩토리)를 추가한다.
ADAPTERS: tuple[tuple[str, AdapterFactory], ...] = (("fake", _fake_factory),)


class AdapterConformance(unittest.TestCase):
    """각 Adapter에 대해 같은 검사를 반복한다."""

    def each(self):
        for name, factory in ADAPTERS:
            with self.subTest(adapter=name):
                yield factory()

    # ── 인터페이스 ──────────────────────────────────────────────────────
    def test_is_a_robot_adapter(self):
        for a in self.each():
            self.assertIsInstance(a, RobotAdapter)
            self.assertTrue(a.robot_id)
            self.assertIsInstance(a.profile, CapabilityProfile)

    def test_declares_supported_skills_through_the_profile(self):
        for a in self.each():
            for skill in ATOMIC_SKILLS:
                self.assertEqual(a.supports(skill), a.profile.supports(skill))

    # ── 결과 계약 ───────────────────────────────────────────────────────
    def test_every_operation_returns_an_execution_result(self):
        for a in self.each():
            for res in (
                a.check(),
                a.home(TIMEOUT),
                a.move("loc_a", TIMEOUT),
                a.pick("obj_a", "loc_a", TIMEOUT),
                a.place("obj_a", "loc_b", TIMEOUT),
                a.stop(TIMEOUT),
                a.cancel(TIMEOUT),
                a.confirm_stopped(TIMEOUT),
            ):
                self.assertIsInstance(res, ExecutionResult)

    def test_operations_never_raise_for_normal_inputs(self):
        """실패를 예외가 아니라 결과로 표현한다."""
        for a in self.each():
            try:
                a.home(TIMEOUT)
                a.move("loc_a", TIMEOUT)
                a.stop(TIMEOUT)
                a.confirm_stopped(TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"정상 입력에서 예외가 발생했다: {exc!r}")

    def test_state_snapshot_shape(self):
        for a in self.each():
            snap = a.state()
            self.assertIsInstance(snap, RobotStateSnapshot)
            self.assertIsInstance(snap.valid, bool)
            self.assertIsInstance(snap.hold_observed, bool)
            self.assertIn(snap.state, set(ExecutionState))

    # ── 정지 계약 ───────────────────────────────────────────────────────
    def test_stop_request_does_not_claim_confirmation(self):
        for a in self.each():
            a.home(TIMEOUT)
            res = a.stop(TIMEOUT)
            self.assertIs(res.state, ExecutionState.STOPPING)
            self.assertFalse(res.task_succeeded)

    def test_skills_are_refused_after_stop(self):
        for a in self.each():
            a.stop(TIMEOUT)
            res = a.move("loc_a", TIMEOUT)
            self.assertFalse(res.request_accepted)
            self.assertIsNotNone(res.reason)

    def test_confirm_stopped_ends_in_stopped_or_unknown(self):
        for a in self.each():
            a.stop(TIMEOUT)
            res = a.confirm_stopped(TIMEOUT)
            self.assertIn(res.state, {ExecutionState.STOPPED, ExecutionState.UNKNOWN})
            self.assertIn(res.state, TERMINAL_STATES)

    def test_unconfirmed_stop_is_never_reported_as_success(self):
        """계측이 안 되면 확인 불가로 남아야 한다 — 성공으로 바뀌지 않는다."""
        for name, factory in ADAPTERS:
            with self.subTest(adapter=name):
                a = factory()
                if isinstance(a, FakeRobotAdapter):
                    a.world.samples = moving()          # 계속 움직이는 관측
                a.home(TIMEOUT)
                a.stop(TIMEOUT)
                res = a.confirm_stopped(TIMEOUT)
                self.assertFalse(res.task_succeeded)
                if res.state is not ExecutionState.STOPPED:
                    self.assertFalse(res.verified)
                    self.assertIsNotNone(res.reason)

    # ── 실패 표현 ───────────────────────────────────────────────────────
    def test_failure_results_carry_a_reason_code(self):
        for a in self.each():
            a.disconnect()
            res = a.home(TIMEOUT)
            self.assertFalse(res.request_accepted)
            self.assertIsInstance(res.reason, ReasonCode)

    def test_unverifiable_results_are_not_success(self):
        for name, factory in ADAPTERS:
            with self.subTest(adapter=name):
                a = factory()
                if isinstance(a, FakeRobotAdapter):
                    a.transport.set_scenario(
                        CHANNEL_MOTION, Scenario(send=SendOutcome.TIMEOUT_NEVER)
                    )
                res = a.home(TIMEOUT)
                if not res.verified:
                    self.assertFalse(res.task_succeeded)
                    self.assertIsNotNone(res.reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
