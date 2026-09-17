"""STOP 계약 회귀 테스트 (forstick STOP 테스트 6종 이관).

forstick에서 Codex 리뷰 6라운드에 걸쳐 발견된 경합·오보고 경로를 forstick2
계약(core.stop_contract) 기준으로 다시 작성했다. 라운드별 원본 의도:

  1차 test_gripper_stop_fixes      — 채널별 방향/단위, 실패·취소를 성공으로 반환
  2차 test_stop_confirm_races      — 채널 허용치 혼용, 락 경합 시 미확인 True
  3차 test_stop_e2e_tracking       — 대기로 실제 확인 대체, 결과 타임아웃에 추적 유실
  4차 test_stop_no_resend_races    — 재전송 후 늦은 응답으로 추적 유실
  5차 test_orphan_goal_lifecycle   — orphan 취소만 하고 추적 안 함
  6차 test_late_accept_lifecycle   — 채택된 늦은 goal 정리 누락, pending 독립 검사 누락

외부 의존성 없이 끝나며 실제 시간을 기다리지 않는다(표본 시각은 주입값).
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.policy import PolicyError, StopPolicy
from core.stop_contract import (
    GoalTracker,
    JointSample,
    StopContractError,
    confirm_motion_stopped,
)

ARM, GRIP = "channel_a", "channel_b"
J = ("j1", "j2")


def make_policy(**over) -> StopPolicy:
    kw = dict(
        policy_version="fixture",
        position_tolerance={ARM: 0.02, GRIP: 0.001},
        hold_sec=0.5,
        cancel_ack_timeout_sec=5.0,
        max_sample_gap_sec=0.3,
        provenance={
            "hold_sec": "fixture",
            "cancel_ack_timeout_sec": "fixture",
            "max_sample_gap_sec": "fixture",
        },
    )
    kw.update(over)
    return StopPolicy(**kw)


def steady(start: float = 0.0, n: int = 12, step: float = 0.1, value: float = 1.0):
    """정지 표본. step 간격으로 같은 값이 계속 온다."""
    return [JointSample(start + i * step, {"j1": value, "j2": value}) for i in range(n)]


def moving(start: float = 0.0, n: int = 12, step: float = 0.1, drift: float = 0.05):
    return [
        JointSample(start + i * step, {"j1": 1.0 + i * drift, "j2": 1.0})
        for i in range(n)
    ]


def always(value: bool):
    return lambda _h: value


class TestChannelSeparation(unittest.TestCase):
    """1·2차: 채널별 단위·허용치를 섞어 쓰지 않는다."""

    def test_tolerance_is_per_channel(self):
        pol = make_policy()
        self.assertNotEqual(pol.tolerance_for(ARM), pol.tolerance_for(GRIP))

    def test_unknown_channel_does_not_borrow_another_tolerance(self):
        with self.assertRaises(PolicyError):
            make_policy().tolerance_for("channel_c")

    def test_tracker_rejects_channel_without_tolerance(self):
        with self.assertRaises(PolicyError):
            GoalTracker([ARM, "channel_c"], make_policy())

    def test_tight_channel_rejects_drift_that_loose_channel_accepts(self):
        # 같은 표본이라도 허용치가 다르면 판정이 달라야 한다.
        samples = [
            JointSample(0.0, {"j1": 1.0, "j2": 1.0}),
            JointSample(0.2, {"j1": 1.005, "j2": 1.0}),
            JointSample(0.4, {"j1": 1.005, "j2": 1.0}),
            JointSample(0.6, {"j1": 1.005, "j2": 1.0}),
        ]
        loose = confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3)
        tight = confirm_motion_stopped(samples, J, 0.001, 0.5, 0.3)
        self.assertTrue(loose)
        self.assertFalse(tight)


class TestMeasuredStopConfirmation(unittest.TestCase):
    """3차 + R10: 대기나 ACK가 실측 확인을 대체하지 못한다."""

    def test_steady_samples_confirm_stop(self):
        self.assertTrue(confirm_motion_stopped(steady(), J, 0.02, 0.5, 0.3))

    def test_moving_samples_do_not_confirm_stop(self):
        self.assertFalse(confirm_motion_stopped(moving(), J, 0.02, 0.5, 0.3))

    def test_no_samples_do_not_confirm_stop(self):
        self.assertFalse(confirm_motion_stopped([], J, 0.02, 0.5, 0.3))

    def test_invalid_sample_resets_stability(self):
        samples = steady(n=4) + [JointSample(0.4, {"j1": 1.0, "j2": 1.0}, valid=False)] + steady(0.5, 2)
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_missing_joint_resets_stability(self):
        samples = [JointSample(i * 0.1, {"j1": 1.0}) for i in range(12)]
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_nan_resets_stability(self):
        samples = steady(n=3) + [JointSample(0.3, {"j1": float("nan"), "j2": 1.0})] + steady(0.4, 3)
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_infinite_resets_stability(self):
        samples = steady(n=3) + [JointSample(0.3, {"j1": float("inf"), "j2": 1.0})] + steady(0.4, 3)
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_sample_gap_resets_stability(self):
        # 0.3s 상한을 넘는 공백 뒤에는 연속 정지를 처음부터 다시 센다.
        samples = steady(n=3, step=0.1) + steady(5.0, 3, step=0.1)
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_repeated_same_timestamp_does_not_accumulate(self):
        # 같은 stamp가 계속 들어오면 정지 시간이 쌓이지 않아야 한다.
        samples = [JointSample(0.0, {"j1": 1.0, "j2": 1.0}) for _ in range(50)]
        self.assertFalse(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_clock_going_backwards_is_ignored(self):
        samples = steady(n=3) + [JointSample(0.05, {"j1": 1.0, "j2": 1.0})] + steady(0.3, 4)
        # 역행 표본을 새 관측으로 세지 않으므로 정상 구간만으로 판정된다.
        self.assertTrue(confirm_motion_stopped(samples, J, 0.02, 0.5, 0.3))

    def test_unstable_then_stable_confirms_after_hold_window(self):
        """초반에 흔들리다 이후 고정되면, 그 이후 구간만으로 hold window를 채워 True."""
        wobble = [JointSample(i * 0.1, {"j1": 1.0 + (i % 2) * 0.05, "j2": 1.0}) for i in range(4)]
        settled = steady(0.4, 8, step=0.1)
        self.assertTrue(confirm_motion_stopped(wobble + settled, J, 0.02, 0.5, 0.3))

    def test_empty_joint_list_is_a_config_error(self):
        with self.assertRaises(StopContractError):
            confirm_motion_stopped(steady(), (), 0.02, 0.5, 0.3)


class TestSendAndAcceptRaces(unittest.TestCase):
    """1·4차: 전송 전 STOP, 재전송 금지, 늦은 수락."""

    def setUp(self):
        self.t = GoalTracker([ARM, GRIP], make_policy())

    def test_send_is_refused_after_stop(self):
        self.t.request_stop()
        self.assertFalse(self.t.begin_send(ARM, "r1"))
        self.assertEqual(self.t.snapshot(ARM).pending, set())

    def test_send_timeout_keeps_tracking_and_does_not_resend(self):
        self.assertTrue(self.t.begin_send(ARM, "r1"))
        self.t.on_send_timeout(ARM, "r1")
        # pending이 남아 있어야 한다 — 재전송으로 새 요청을 만들지 않는다.
        self.assertEqual(self.t.snapshot(ARM).pending, {"r1"})

    def test_send_timeout_for_untracked_request_is_an_error(self):
        with self.assertRaises(StopContractError):
            self.t.on_send_timeout(ARM, "never_sent")

    def test_late_accept_while_stopped_becomes_orphan(self):
        self.t.begin_send(ARM, "r1")
        self.t.request_stop()
        self.assertEqual(self.t.on_accept(ARM, "r1", "g1"), "orphan")
        snap = self.t.snapshot(ARM)
        self.assertIsNone(snap.active)
        self.assertEqual(snap.orphans, ["g1"])
        self.assertEqual(snap.pending, set())

    def test_late_accept_does_not_overwrite_existing_active(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")
        self.assertEqual(self.t.on_accept(ARM, "r2", "g2"), "orphan")
        snap = self.t.snapshot(ARM)
        self.assertEqual(snap.active, "g1")   # 기존 추적을 잃지 않는다
        self.assertEqual(snap.orphans, ["g2"])

    def test_channels_are_independent(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.assertIsNone(self.t.snapshot(GRIP).active)


class TestResultAndOrphanLifecycle(unittest.TestCase):
    """3·5·6차: 결과 타임아웃, 동일성 확인, orphan 정리."""

    def setUp(self):
        self.t = GoalTracker([ARM], make_policy())

    def test_result_timeout_does_not_clear_handle(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.on_result_timeout(ARM, "g1")
        self.assertEqual(self.t.snapshot(ARM).active, "g1")

    def test_result_clears_active_only_by_identity(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.on_result(ARM, "g_other")
        self.assertEqual(self.t.snapshot(ARM).active, "g1")
        self.t.on_result(ARM, "g1")
        self.assertIsNone(self.t.snapshot(ARM).active)

    def test_orphan_removed_only_on_definitive_result(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")
        self.t.on_accept(ARM, "r2", "g2")
        self.assertEqual(self.t.snapshot(ARM).orphans, ["g2"])
        self.t.on_result(ARM, "g2")
        self.assertEqual(self.t.snapshot(ARM).orphans, [])

    def test_adopted_late_goal_can_be_cleaned_up(self):
        # 6차: 채택된 늦은 goal도 결과로 정리돼야 한다.
        self.t.begin_send(ARM, "r1")
        self.assertEqual(self.t.on_accept(ARM, "r1", "g1"), "active")
        self.t.on_result(ARM, "g1")
        self.assertIsNone(self.t.snapshot(ARM).active)


class TestStopConfirmation(unittest.TestCase):
    """2·5·6차: 정지 판정 규칙 (R8, R9)."""

    def setUp(self):
        self.t = GoalTracker([ARM], make_policy())

    def _confirm(self, cancel_ok: bool, samples):
        return self.t.confirm_stop(ARM, J, always(cancel_ok), lambda: samples)

    def test_no_goals_falls_back_to_measured_confirmation(self):
        self.assertTrue(self._confirm(True, steady()))
        self.assertFalse(self._confirm(True, moving()))

    def test_cancel_ack_alone_is_not_enough(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        # ACK는 왔지만 관절은 계속 움직인다 -> 미확인
        self.assertFalse(self._confirm(True, moving()))

    def test_measured_stop_alone_is_not_enough_when_cancel_fails(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.assertFalse(self._confirm(False, steady()))

    def test_active_goal_confirmed_when_both_hold(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.assertTrue(self._confirm(True, steady()))

    def test_pending_alone_blocks_confirmation(self):
        # 5·6차 핵심: handle이 없어도 pending이 있으면 미확인이다.
        self.t.begin_send(ARM, "r1")
        self.assertEqual(self.t.snapshot(ARM).pending, {"r1"})
        self.assertFalse(self._confirm(True, steady()))

    def test_pending_blocks_confirmation_even_with_confirmed_handles(self):
        # 6차: handle 확인이 성공해도 별도 pending이 있으면 미확인이다.
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")   # 아직 수락 여부 모름
        self.assertFalse(self._confirm(True, steady()))

    def test_orphans_are_included_in_cancel_and_confirm(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")
        self.t.on_accept(ARM, "r2", "g2")   # orphan
        tried: list = []
        ok = self.t.confirm_stop(
            ARM, J, lambda h: (tried.append(h), True)[1], lambda: steady()
        )
        self.assertTrue(ok)
        self.assertEqual(set(tried), {"g1", "g2"})   # 둘 다 취소 시도

    def test_orphan_cancel_failure_blocks_confirmation(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")
        self.t.on_accept(ARM, "r2", "g2")
        ok = self.t.confirm_stop(
            ARM, J, lambda h: h != "g2", lambda: steady()
        )
        self.assertFalse(ok)

    def test_stop_can_be_retried_and_retries_cancel_again(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        calls: list = []
        self.t.confirm_stop(ARM, J, lambda h: (calls.append(h), False)[1], lambda: steady())
        self.t.confirm_stop(ARM, J, lambda h: (calls.append(h), True)[1], lambda: steady())
        self.assertEqual(calls, ["g1", "g1"])   # 재요청에서 같은 goal을 다시 취소


class TestStopConfirmationTrackingRetained(unittest.TestCase):
    """5차: 정지 확인이 실패해도 추적을 잃지 않는다. 정리는 결과 수신 전담."""

    def setUp(self):
        self.t = GoalTracker([ARM], make_policy())
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.begin_send(ARM, "r2")
        self.t.on_accept(ARM, "r2", "g2")   # orphan

    def test_failed_confirmation_keeps_active_and_orphan(self):
        ok = self.t.confirm_stop(ARM, J, always(False), lambda: steady())
        self.assertFalse(ok)
        snap = self.t.snapshot(ARM)
        self.assertEqual(snap.active, "g1")
        self.assertEqual(snap.orphans, ["g2"])

    def test_successful_confirmation_does_not_remove_orphan(self):
        """확인이 성공해도 목록에서 지우지 않는다 — 제거는 결과 수신에서만."""
        ok = self.t.confirm_stop(ARM, J, always(True), lambda: steady())
        self.assertTrue(ok)
        self.assertEqual(self.t.snapshot(ARM).orphans, ["g2"])
        self.t.on_result(ARM, "g2")
        self.assertEqual(self.t.snapshot(ARM).orphans, [])

    def test_orphan_cancel_success_with_pending_still_blocks(self):
        """6차: orphan 취소가 성공해도 별도 pending이 있으면 미확인이다."""
        self.t.begin_send(ARM, "r3")   # 수락 여부 불명
        self.assertFalse(self.t.confirm_stop(ARM, J, always(True), lambda: steady()))


class TestLateAcceptWhenNotStopped(unittest.TestCase):
    """4차: 정지 상태가 아니면 늦게 수락된 goal을 취소하지 않고 채택한다."""

    def test_adopted_goal_is_active_and_not_orphaned(self):
        t = GoalTracker([ARM], make_policy())
        t.begin_send(ARM, "r1")
        self.assertEqual(t.on_accept(ARM, "r1", "g1"), "active")
        snap = t.snapshot(ARM)
        self.assertEqual(snap.active, "g1")
        self.assertEqual(snap.orphans, [])   # 취소 대상으로 넘기지 않는다
        self.assertEqual(snap.pending, set())


class TestMultiChannelStop(unittest.TestCase):
    """1차: 한 채널에 goal이 없어도 다른 채널의 정지를 건너뛰지 않는다."""

    def setUp(self):
        self.t = GoalTracker([ARM, GRIP], make_policy())

    def test_channel_without_goals_is_confirmed_by_measurement(self):
        self.t.begin_send(GRIP, "r1")
        self.t.on_accept(GRIP, "r1", "g1")
        # ARM에는 goal이 없다 -> 실측으로 판정해야 한다(무조건 True 아님).
        self.assertTrue(self.t.confirm_stop(ARM, J, always(True), lambda: steady()))
        self.assertFalse(self.t.confirm_stop(ARM, J, always(True), lambda: moving()))

    def test_channel_with_goals_is_not_skipped(self):
        self.t.begin_send(GRIP, "r1")
        self.t.on_accept(GRIP, "r1", "g1")
        tried: list = []
        self.t.confirm_stop(GRIP, J, lambda h: (tried.append(h), True)[1], lambda: steady())
        self.assertEqual(tried, ["g1"])   # 취소를 실제로 시도했다


class TestConcurrentStopConfirmation(unittest.TestCase):
    """2차: 동시에 들어온 정지 확인 요청 둘 다 실제 판정을 받아야 한다.
    어느 한쪽이 확인 없이 즉시 True로 새치기하면 안 된다."""

    def test_both_callers_get_a_real_verdict(self):
        t = GoalTracker([ARM], make_policy())
        t.begin_send(ARM, "r1")
        t.on_accept(ARM, "r1", "g1")
        results: list[bool] = []
        cancel_calls: list = []
        barrier = threading.Barrier(2)

        def caller():
            barrier.wait()
            results.append(
                t.confirm_stop(
                    ARM, J, lambda h: (cancel_calls.append(h), True)[1], lambda: steady()
                )
            )

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
            self.assertFalse(th.is_alive())

        self.assertEqual(len(results), 2)          # 둘 다 결과를 받음
        self.assertEqual(len(cancel_calls), 2)     # 둘 다 실제로 취소를 시도함
        self.assertTrue(all(results))


class TestAcceptAtomicity(unittest.TestCase):
    """6차: pending 해제와 active/orphan 등록이 하나의 임계구역이어야 한다."""

    class CountingLock:
        def __init__(self, inner):
            self._inner, self.acquires = inner, 0

        def acquire(self, *a, **kw):
            self.acquires += 1
            return self._inner.acquire(*a, **kw)

        def release(self):
            return self._inner.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()
            return False

    def test_on_accept_takes_the_lock_exactly_once(self):
        t = GoalTracker([ARM], make_policy())
        t.begin_send(ARM, "r1")
        counting = self.CountingLock(threading.RLock())
        t._lock = counting          # 계약 검증 목적의 의도적 내부 접근
        t.on_accept(ARM, "r1", "g1")
        self.assertEqual(counting.acquires, 1)


class TestStopLatchAndReset(unittest.TestCase):
    """forstick에서 실제로 겪은 "정지 래치가 안 풀려 모든 실행이 거부" 문제."""

    def setUp(self):
        self.t = GoalTracker([ARM], make_policy())

    def test_stop_latches_until_explicit_reset(self):
        self.t.request_stop()
        self.assertTrue(self.t.stopped)
        self.assertFalse(self.t.begin_send(ARM, "r1"))
        self.t.reset_for_new_plan()
        self.assertFalse(self.t.stopped)
        self.assertTrue(self.t.begin_send(ARM, "r2"))

    def test_reset_is_refused_while_goals_are_tracked(self):
        self.t.begin_send(ARM, "r1")
        self.t.on_accept(ARM, "r1", "g1")
        self.t.request_stop()
        with self.assertRaises(StopContractError):
            self.t.reset_for_new_plan()

    def test_reset_is_refused_while_pending_remains(self):
        self.t.begin_send(ARM, "r1")
        self.t.request_stop()
        with self.assertRaises(StopContractError):
            self.t.reset_for_new_plan()


class TestAtomicity(unittest.TestCase):
    """1·2·4차: 상태 변경이 하나의 임계구역에서 일어나는지."""

    def test_concurrent_sends_and_stop_leave_consistent_state(self):
        t = GoalTracker([ARM], make_policy())
        accepted: list[str] = []
        barrier = threading.Barrier(5)

        def sender(i: int):
            barrier.wait()
            if t.begin_send(ARM, f"r{i}"):
                accepted.append(t.on_accept(ARM, f"r{i}", f"g{i}"))

        def stopper():
            barrier.wait()
            t.request_stop()

        threads = [threading.Thread(target=sender, args=(i,)) for i in range(4)]
        threads.append(threading.Thread(target=stopper))
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=5.0)
            self.assertFalse(th.is_alive())

        snap = t.snapshot(ARM)
        # active는 최대 1개이고, 수락된 나머지는 전부 orphan으로 보존된다.
        active_count = 1 if snap.active is not None else 0
        self.assertLessEqual(active_count, 1)
        self.assertEqual(active_count + len(snap.orphans), len(accepted))
        self.assertTrue(t.stopped)


if __name__ == "__main__":
    unittest.main(verbosity=2)
