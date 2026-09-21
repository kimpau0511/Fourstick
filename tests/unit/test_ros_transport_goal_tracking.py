"""ROS 전송 계층의 goal 추적 정리 단위 검증.

**ROS 없이** `RosWorkcellTransport`의 추적 목록만 본다. goal handle·결과
future를 결정적 스텁으로 바꿔, terminal 결과가 온 goal만 정확히 한 번 빠지는지
확인한다. 이동·정지 명령을 보내지 않는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo.adapter import StopLatch, transport_live_goals
from robots.fr3_gazebo.ros_transport import RosWorkcellTransport

# action_msgs/GoalStatus
UNKNOWN, ACCEPTED, EXECUTING, CANCELING = 0, 1, 2, 3
SUCCEEDED, CANCELED, ABORTED = 4, 5, 6


class FakeFuture:
    """결과를 나중에 채우는 future. 콜백은 채울 때 부른다."""

    def __init__(self):
        self._callbacks = []
        self._done = False
        self._result = None

    def add_done_callback(self, callback):
        if self._done:
            callback(self)
        else:
            self._callbacks.append(callback)

    def done(self):
        return self._done

    def result(self):
        return self._result

    def finish(self, result):
        self._done, self._result = True, result
        for callback in self._callbacks:
            callback(self)


class Response:
    def __init__(self, status):
        self.status = status


class GoalId:
    def __init__(self, n):
        self.uuid = [n] * 16


class FakeHandle:
    def __init__(self, n):
        self.goal_id = GoalId(n)
        self.result_future = FakeFuture()
        self.cancel_future = FakeFuture()

    def get_result_async(self):
        return self.result_future

    def cancel_goal_async(self):
        return self.cancel_future


class CancelResponse:
    def __init__(self, count):
        self.goals_canceling = [object()] * count


def transport():
    t = RosWorkcellTransport(world_name="w", gz_partition="p", ros_domain_id=44)
    t._node = object()      # _ensure_node가 ROS를 올리지 않게 한다
    return t


class TerminalGoalCleanupTest(unittest.TestCase):

    def _tracked(self, n=1):
        t = transport()
        handle = FakeHandle(n)
        t._track(handle)
        self.assertEqual(t.live_goals(), 1)
        return t, handle

    def test_succeeded_removes_goal(self):
        t, handle = self._tracked()
        handle.result_future.finish(Response(SUCCEEDED))
        self.assertEqual(t.live_goals(), 0)

    def test_canceled_removes_goal(self):
        t, handle = self._tracked()
        handle.result_future.finish(Response(CANCELED))
        self.assertEqual(t.live_goals(), 0)

    def test_aborted_removes_goal(self):
        t, handle = self._tracked()
        handle.result_future.finish(Response(ABORTED))
        self.assertEqual(t.live_goals(), 0)

    def test_non_terminal_status_keeps_goal(self):
        for status in (UNKNOWN, ACCEPTED, EXECUTING, CANCELING, None, 99):
            with self.subTest(status=status):
                t, handle = self._tracked()
                removed = t._on_goal_result(handle, FakeFutureDone(Response(status)))
                self.assertFalse(removed)
                self.assertEqual(t.live_goals(), 1)

    def test_unreadable_result_keeps_goal(self):
        t, handle = self._tracked()
        self.assertFalse(t._on_goal_result(handle, FailingFuture()))
        self.assertEqual(t.live_goals(), 1)

    def test_duplicate_terminal_event_removes_once(self):
        t = transport()
        first, second = FakeHandle(1), FakeHandle(2)
        t._track(first)
        t._track(second)
        handle_done = FakeFutureDone(Response(SUCCEEDED))
        self.assertTrue(t._on_goal_result(first, handle_done))
        self.assertFalse(t._on_goal_result(first, handle_done))
        self.assertFalse(t._on_goal_result(first, FakeFutureDone(Response(CANCELED))))
        self.assertEqual(t.live_goals(), 1)
        self.assertIs(t._handles[0], second)

    def test_other_goal_terminal_keeps_active_goal(self):
        t = transport()
        old, active = FakeHandle(1), FakeHandle(2)
        t._track(old)
        t._track(active)
        old.result_future.finish(Response(SUCCEEDED))
        self.assertEqual(t.live_goals(), 1)
        self.assertIs(t._handles[0], active)
        # 추적하지 않는 goal의 terminal 결과도 활성 goal을 빼지 않는다.
        stranger = FakeHandle(9)
        self.assertFalse(t._on_goal_result(stranger, FakeFutureDone(Response(ABORTED))))
        self.assertEqual(t.live_goals(), 1)
        self.assertIs(t._handles[0], active)

    def test_result_already_done_is_still_removed(self):
        t = transport()
        handle = FakeHandle(1)
        handle.result_future.finish(Response(SUCCEEDED))
        t._track(handle)
        self.assertEqual(t.live_goals(), 0)


class StopThenNextPlanTest(unittest.TestCase):
    """STOP 뒤 goal이 모두 terminal이면 정지 래치가 풀린다."""

    def test_cancel_ack_keeps_goal_until_terminal(self):
        t = transport()
        handle = FakeHandle(1)
        t._track(handle)
        latch = StopLatch(lambda: transport_live_goals(t))
        latch.latch()
        handle.cancel_future.finish(CancelResponse(1))
        outcome = t.cancel_all(1.0)
        self.assertTrue(outcome.cancel_ack)
        self.assertEqual(outcome.goals_canceling, 1)
        # 취소 ACK는 terminal이 아니다 — 아직 추적한다. 래치는 풀리지 않는다.
        self.assertEqual(t.live_goals(), 1)
        with self.assertRaises(RuntimeError):
            latch.reset_for_new_plan()
        self.assertTrue(latch.stopped)
        # CANCELED 결과가 오면 빠지고, 다음 계획의 래치 해제가 막히지 않는다.
        handle.result_future.finish(Response(CANCELED))
        self.assertEqual(t.live_goals(), 0)
        latch.reset_for_new_plan()
        self.assertFalse(latch.stopped)

    def test_earlier_finished_goals_do_not_block_release(self):
        t = transport()
        done = [FakeHandle(n) for n in (1, 2, 3)]
        for handle in done:
            t._track(handle)
            handle.result_future.finish(Response(SUCCEEDED))
        moving = FakeHandle(4)
        t._track(moving)
        latch = StopLatch(lambda: transport_live_goals(t))
        latch.latch()
        moving.cancel_future.finish(CancelResponse(1))
        outcome = t.cancel_all(1.0)
        # 이미 끝난 goal에는 취소를 보내지 않는다 — 추적 중인 1개만.
        self.assertEqual(outcome.goals_canceling, 1)
        moving.result_future.finish(Response(CANCELED))
        latch.reset_for_new_plan()
        self.assertFalse(latch.stopped)


class FakeFutureDone:
    def __init__(self, result):
        self._result = result

    def result(self):
        return self._result


class FailingFuture:
    def result(self):
        raise RuntimeError("결과를 읽을 수 없다")


if __name__ == "__main__":
    unittest.main()
