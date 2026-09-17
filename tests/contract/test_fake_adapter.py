"""Fake Adapter 검증 (md/개발플랜.md 2-02~2-05).

필수 통과 기준은 `md/STOP테스트_이관대조표.md`의 "전송 계층으로 분류한 항목"이다.
아래 TestTransportLayerCriteria가 그 5개를 하나씩 확인한다.

시뮬레이터·실제 시간 없이 끝난다(시각과 표본은 모두 주입값).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.capability_profile import CapabilityProfile, GripperSpec, JointLimit
from core.constants import ATOMIC_SKILLS
from core.execution_state import ExecutionState
from core.frames import ANGLE_UNIT, FrameKind, Vector3
from core.policy import StopPolicy
from core.reason_codes import ReasonCode
from core.stop_contract import JointSample
from robots.fake.adapter import CHANNEL_GRIPPER, CHANNEL_MOTION, FakeRobotAdapter, FakeWorld
from robots.fake.transport import FakeTransport, GoalStatus, Scenario, SendOutcome

J = ("j1", "j2")
TIMEOUT = 1.0


def make_profile() -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="fixture",
        profile_version="1.0",
        dof=2,
        joint_limits=tuple(
            JointLimit(n, -3.0, 3.0, 1.0, ANGLE_UNIT, "revolute") for n in J
        ),
        frames={FrameKind.BASE: "base", FrameKind.TOOL: "tool"},
        tcp_offset=Vector3(0.0, 0.0, 0.1),
        work_radius_m=0.8,
        payload_kg=3.0,
        supported_skills=ATOMIC_SKILLS,
        gripper=GripperSpec("g", 0.02, 0.55, ANGLE_UNIT, 0.0286, 50.0, fully_open_margin=0.05),
    )


def make_policy() -> StopPolicy:
    return StopPolicy(
        policy_version="fixture",
        position_tolerance={CHANNEL_MOTION: 0.02, CHANNEL_GRIPPER: 0.001},
        hold_sec=0.5,
        cancel_ack_timeout_sec=5.0,
        max_sample_gap_sec=0.3,
        provenance={
            "hold_sec": "fixture",
            "cancel_ack_timeout_sec": "fixture",
            "max_sample_gap_sec": "fixture",
        },
    )


def steady(n: int = 12, step: float = 0.1):
    return [JointSample(i * step, {"j1": 1.0, "j2": 1.0}) for i in range(n)]


def moving(n: int = 12, step: float = 0.1):
    return [JointSample(i * step, {"j1": 1.0 + i * 0.05, "j2": 1.0}) for i in range(n)]


def make_adapter(scenarios=None, world=None, connect=True) -> FakeRobotAdapter:
    a = FakeRobotAdapter(
        "fake_1",
        make_profile(),
        stop_policy=make_policy(),
        transport=FakeTransport(scenarios or {}),
        world=world or FakeWorld(samples=steady()),
    )
    if connect:
        a.connect(TIMEOUT)
    return a


def only_pending(adapter, channel: str) -> str:
    """해당 채널의 유일한 pending 요청 id. request_id는 이제 일련번호가 붙은
    식별자이므로 테스트가 문자열을 만들어 쓰지 않고 추적 상태에서 읽는다."""
    pending = adapter.tracker.snapshot(channel).pending
    assert len(pending) == 1, f"pending이 1개여야 한다: {pending}"
    return next(iter(pending))


class TestTransportLayerCriteria(unittest.TestCase):
    """대조표의 전송 계층 5개 요구사항.

    요구사항은 5개지만 **실행 경로가 다르면 테스트를 따로 둔다** — T2는 수락
    응답 future와 결과 future가 서로 다른 경로이고, T4는 채택된 goal과 orphan이
    서로 다른 경로다. 같은 요구사항으로 묶었다는 이유로 한 쪽만 확인하면
    다른 경로의 회귀를 놓친다.
    """

    def test_T1_send_is_called_exactly_once_on_timeout(self):
        """T1 — 전송 응답이 늦어도 재전송하지 않는다."""
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.TIMEOUT_NEVER)})
        res = a.home(TIMEOUT)
        self.assertEqual(len(a.transport.log.sends), 1)
        self.assertIs(res.reason, ReasonCode.EXEC_SEND_TIMEOUT)
        self.assertFalse(res.verified)
        self.assertEqual(len(a.tracker.snapshot(CHANNEL_MOTION).pending), 1)

    def test_T2a_callback_is_attached_to_the_accept_future(self):
        """T2 경로 1 — 전송 응답 future에 콜백을 건다(4차 원본)."""
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.TIMEOUT_THEN_ACCEPT)})
        a.home(TIMEOUT)
        rid = only_pending(a, CHANNEL_MOTION)
        fut = a.transport.accept_future(rid)
        self.assertIsNotNone(fut)
        self.assertTrue(fut.callbacks, "수락 응답 future에 콜백이 걸려야 한다")

        ref = a.transport.deliver_late_accept(CHANNEL_MOTION, rid)
        snap = a.tracker.snapshot(CHANNEL_MOTION)
        self.assertEqual(snap.pending, set())
        self.assertEqual(snap.active, ref)
        self.assertEqual(len(a.transport.log.sends), 1)   # 여전히 1번

    def test_T2b_callback_is_attached_to_the_result_future(self):
        """T2 경로 2 — 결과 future에도 콜백을 건다(6차 원본)."""
        a = make_adapter({CHANNEL_MOTION: Scenario(result_timeout=True)})
        a.home(TIMEOUT)
        ref = a.tracker.snapshot(CHANNEL_MOTION).active
        fut = a.transport.result_future(ref)
        self.assertIsNotNone(fut)
        self.assertTrue(fut.callbacks, "결과 future에 콜백이 걸려야 한다")
        # 결과가 도착하면 그 콜백이 추적을 정리한다.
        a.transport.deliver_result(ref)
        self.assertIsNone(a.tracker.snapshot(CHANNEL_MOTION).active)

    def test_T3_adapter_cancels_itself_on_result_timeout(self):
        """T3 — 결과 대기 타임아웃이면 스스로 취소를 시도하고 추적을 유지한다."""
        a = make_adapter({CHANNEL_MOTION: Scenario(result_timeout=True)})
        res = a.home(TIMEOUT)
        self.assertIs(res.reason, ReasonCode.EXEC_RESULT_TIMEOUT)
        self.assertTrue(a.transport.log.cancels)
        self.assertIsNotNone(a.tracker.snapshot(CHANNEL_MOTION).active)

    def test_T4a_adopted_goal_result_is_subscribed(self):
        """T4 경로 1 — 늦게 채택된 goal의 결과를 구독한다(6차 원본)."""
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.TIMEOUT_THEN_ACCEPT)})
        a.home(TIMEOUT)
        rid = only_pending(a, CHANNEL_MOTION)
        adopted = a.transport.deliver_late_accept(CHANNEL_MOTION, rid)
        self.assertEqual(a.tracker.snapshot(CHANNEL_MOTION).active, adopted)
        self.assertIn(adopted, a.transport.log.result_subscriptions)
        self.assertNotIn(adopted, a.transport.log.cancels)   # 정지 상태가 아니면 취소하지 않는다

    def test_T4b_orphan_goal_result_is_subscribed(self):
        """T4 경로 2 — orphan이 된 goal의 결과도 구독한다(5차 원본)."""
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.TIMEOUT_THEN_ACCEPT)})
        a.home(TIMEOUT)
        rid = only_pending(a, CHANNEL_MOTION)
        a.stop(TIMEOUT)                                    # 정지 후 늦은 수락 -> orphan
        orphan = a.transport.deliver_late_accept(CHANNEL_MOTION, rid)
        self.assertEqual(a.tracker.snapshot(CHANNEL_MOTION).orphans, [orphan])
        self.assertIn(orphan, a.transport.log.result_subscriptions)
        self.assertIn(orphan, a.transport.log.cancels)      # orphan은 취소도 시도

    def test_T5_tracking_is_retained_when_result_callback_raises(self):
        """T5 — 결과 콜백이 예외를 던져도 추적을 유지한다."""
        a = make_adapter({CHANNEL_MOTION: Scenario(raise_in_result_callback=True)})
        a.home(TIMEOUT)
        self.assertTrue(a.callback_errors)
        self.assertIsNotNone(a.tracker.snapshot(CHANNEL_MOTION).active)


class TestNormalOperation(unittest.TestCase):
    """2-02 — 정상 동작."""

    def test_connect_is_accepted_but_not_a_task_success(self):
        a = make_adapter(connect=False)
        res = a.connect(TIMEOUT)
        self.assertTrue(res.request_accepted)
        self.assertFalse(res.task_succeeded)
        self.assertIsNone(res.reason)

    def test_skills_complete_and_reach_target(self):
        a = make_adapter()
        for res in (a.home(TIMEOUT), a.move("loc_a", TIMEOUT)):
            self.assertTrue(res.motion_completed)
            self.assertTrue(res.target_reached)

    def test_pick_then_place_updates_observed_hold(self):
        a = make_adapter()
        a.pick("obj_a", "loc_a", TIMEOUT)
        self.assertEqual(a.state().held_object, "obj_a")
        a.place("obj_a", "loc_b", TIMEOUT)
        self.assertIsNone(a.state().held_object)

    def test_check_passes_when_connected_and_state_valid(self):
        self.assertTrue(make_adapter().check().request_accepted)


class TestRejectionAndFailures(unittest.TestCase):
    """2-03 — 거부·타임아웃·연결 손실·상태 stale."""

    def test_goal_rejection_is_reported(self):
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.REJECT)})
        res = a.home(TIMEOUT)
        self.assertFalse(res.request_accepted)
        self.assertIs(res.reason, ReasonCode.EXEC_GOAL_REJECTED)

    def test_connection_loss_is_reported(self):
        a = make_adapter()
        a.transport.set_scenario(CHANNEL_MOTION, Scenario(connected=False))
        res = a.home(TIMEOUT)
        self.assertIs(res.reason, ReasonCode.ROBOT_CONNECTION_LOST)

    def test_connect_fails_when_transport_is_down(self):
        a = make_adapter({CHANNEL_MOTION: Scenario(connected=False)}, connect=False)
        self.assertIs(a.connect(TIMEOUT).reason, ReasonCode.ROBOT_CONNECTION_LOST)

    def test_skill_before_connect_is_rejected(self):
        a = make_adapter(connect=False)
        self.assertIs(a.home(TIMEOUT).reason, ReasonCode.ROBOT_NOT_CONNECTED)

    def test_invalid_state_fails_check(self):
        a = make_adapter(world=FakeWorld(samples=steady(), state_valid=False))
        a.connect(TIMEOUT)
        self.assertIs(a.check().reason, ReasonCode.ROBOT_STATE_UNAVAILABLE)

    def test_state_snapshot_reports_staleness(self):
        a = make_adapter(world=FakeWorld(samples=steady(), observed_at=10.0))
        snap = a.state()
        self.assertTrue(snap.is_stale(now=12.0, max_age_sec=1.0))
        self.assertFalse(snap.is_stale(now=10.5, max_age_sec=1.0))

    def test_canceled_and_aborted_results_are_failures(self):
        for status, reason in (
            (GoalStatus.CANCELED, ReasonCode.EXEC_CANCELED),
            (GoalStatus.ABORTED, ReasonCode.EXEC_ABORTED),
        ):
            with self.subTest(status=status):
                a = make_adapter({CHANNEL_MOTION: Scenario(result_status=status)})
                res = a.home(TIMEOUT)
                self.assertFalse(res.task_succeeded)
                self.assertIs(res.reason, reason)


class TestPickPlaceUnverifiable(unittest.TestCase):
    """2-04 — pick/place 실패·확인 불가."""

    def test_unobservable_hold_does_not_claim_a_grasp(self):
        a = make_adapter(world=FakeWorld(samples=steady(), hold_observed=False))
        a.connect(TIMEOUT)
        a.pick("obj_a", "loc_a", TIMEOUT)
        snap = a.state()
        self.assertFalse(snap.hold_observed)
        self.assertIsNone(snap.held_object)   # 관측 불가를 파지로 바꾸지 않는다

    def test_aborted_pick_does_not_set_hold(self):
        a = make_adapter({CHANNEL_GRIPPER: Scenario(result_status=GoalStatus.ABORTED)})
        res = a.pick("obj_a", "loc_a", TIMEOUT)
        self.assertFalse(res.task_succeeded)
        self.assertIsNone(a.state().held_object)

    def test_place_does_not_clear_hold_when_it_fails(self):
        a = make_adapter()
        a.pick("obj_a", "loc_a", TIMEOUT)
        a.transport.set_scenario(CHANNEL_GRIPPER, Scenario(result_status=GoalStatus.ABORTED))
        a.place("obj_a", "loc_b", TIMEOUT)
        self.assertEqual(a.state().held_object, "obj_a")


class TestStopFlow(unittest.TestCase):
    """2-05 — pending/active/orphan goal과 STOP 성공·실패."""

    def test_stop_request_alone_is_not_confirmation(self):
        a = make_adapter()
        a.home(TIMEOUT)
        res = a.stop(TIMEOUT)
        self.assertIs(res.state, ExecutionState.STOPPING)
        self.assertFalse(res.task_succeeded)

    def test_stop_is_confirmed_when_cancel_and_measurement_agree(self):
        a = make_adapter()
        a.home(TIMEOUT)
        a.transport.deliver_result(a.tracker.snapshot(CHANNEL_MOTION).active or "x")
        a.stop(TIMEOUT)
        res = a.confirm_stopped(TIMEOUT)
        self.assertIs(res.state, ExecutionState.STOPPED)

    def test_stop_is_unconfirmed_while_joints_keep_moving(self):
        a = make_adapter(world=FakeWorld(samples=moving()))
        a.connect(TIMEOUT)
        a.home(TIMEOUT)
        a.stop(TIMEOUT)
        res = a.confirm_stopped(TIMEOUT)
        self.assertIs(res.state, ExecutionState.UNKNOWN)
        self.assertIs(res.reason, ReasonCode.EXEC_STOP_UNCONFIRMED)

    def test_stop_is_unconfirmed_without_samples(self):
        a = make_adapter(world=FakeWorld(samples=[]))
        a.connect(TIMEOUT)
        a.stop(TIMEOUT)
        self.assertIs(a.confirm_stopped(TIMEOUT).state, ExecutionState.UNKNOWN)

    def test_stop_is_unconfirmed_when_cancel_ack_is_missing(self):
        # 추적 중인 goal이 남아 있어야 취소 ACK가 판정에 관여한다. 결과가 즉시
        # 도착해 정리되면 취소할 대상이 없어 실측만으로 확인되는 것이 정상이다.
        a = make_adapter({CHANNEL_MOTION: Scenario(result_timeout=True, cancel_ack=False)})
        a.home(TIMEOUT)
        self.assertIsNotNone(a.tracker.snapshot(CHANNEL_MOTION).active)
        a.stop(TIMEOUT)
        res = a.confirm_stopped(TIMEOUT)
        self.assertIs(res.state, ExecutionState.UNKNOWN)
        self.assertIs(res.reason, ReasonCode.EXEC_STOP_UNCONFIRMED)

    def test_stop_is_confirmed_by_measurement_when_nothing_is_tracked(self):
        """취소할 goal이 없으면 실측으로 판정한다 — 무조건 True가 아니다."""
        a = make_adapter({CHANNEL_MOTION: Scenario(cancel_ack=False)})
        a.home(TIMEOUT)                       # 결과가 즉시 도착해 추적이 비워진다
        self.assertIsNone(a.tracker.snapshot(CHANNEL_MOTION).active)
        a.stop(TIMEOUT)
        self.assertIs(a.confirm_stopped(TIMEOUT).state, ExecutionState.STOPPED)

    def test_pending_request_blocks_stop_confirmation(self):
        a = make_adapter({CHANNEL_MOTION: Scenario(send=SendOutcome.TIMEOUT_NEVER)})
        a.home(TIMEOUT)
        a.stop(TIMEOUT)
        self.assertIs(a.confirm_stopped(TIMEOUT).state, ExecutionState.UNKNOWN)

    def test_skills_are_refused_after_stop(self):
        a = make_adapter()
        a.stop(TIMEOUT)
        res = a.move("loc_a", TIMEOUT)
        self.assertFalse(res.request_accepted)
        self.assertIs(res.reason, ReasonCode.EXEC_STOPPED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
