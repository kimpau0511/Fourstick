"""장면 조회 진단·재질의(2026-10-07) — server/runtime.Runtime.environment_snapshot, robots/moveit/scene_log.

- 응답 대기 시간 초과일 때만 **한 번** 다시 묻는다(짧은 한도). 그래도 실패면 None(기하 ASK) — 예전 장면을 쓰지 않는다.
- 다른 예외는 다시 묻지 않는다. 실패해도 원인(예외 종류·내용·요청)은 진단 기록에 남는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from robots.moveit import scene_log  # noqa: E402
from server.runtime import SCENE_RETRY_TIMEOUT_SEC, Runtime  # noqa: E402


class Snap:
    def __init__(self, tag):
        self.tag = tag

    def to_environment(self):
        return {"env": self.tag}


class Client:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def snapshot(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def run(client):
    fake = SimpleNamespace(planning_scene_client=lambda: client)
    token = scene_log.SCENE_CONTEXT.set({"request_id": "req_t", "plan_id": "plan_t", "purpose": "create_plan"})
    try:
        before = len(scene_log.recent(200))
        env = Runtime.environment_snapshot(fake)
        rows = scene_log.recent(200)[before:]
    finally:
        scene_log.SCENE_CONTEXT.reset(token)
    return env, rows


class SceneQueryRetryTest(unittest.TestCase):
    def test_first_success_no_retry(self):
        client = Client([Snap("a")])
        env, rows = run(client)
        self.assertEqual(env, {"env": "a"})
        self.assertEqual(client.calls, [{}])
        self.assertEqual(rows, [])

    def test_timeout_then_one_short_retry_recovers(self):
        client = Client([TimeoutError("/get_planning_scene 응답 없음(10.0s, sq1)"), Snap("fresh")])
        env, rows = run(client)
        self.assertEqual(env, {"env": "fresh"})                  # 다시 물은 **새** 장면
        self.assertEqual(client.calls, [{}, {"timeout_sec": SCENE_RETRY_TIMEOUT_SEC}])
        self.assertEqual([r["kind"] for r in rows], ["snapshot_failed", "snapshot_recovered"])
        self.assertEqual(rows[0]["request_id"], "req_t")          # 어느 요청의 조회였는지
        self.assertIn("응답 없음", rows[0]["detail"])

    def test_two_timeouts_give_none_and_never_a_third_try(self):
        client = Client([TimeoutError("t1"), TimeoutError("t2"), Snap("never")])
        env, rows = run(client)
        self.assertIsNone(env)                                    # 기하 검사는 ASK — 실행 불가
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([(r["kind"], r["attempt"], r["will_retry"]) for r in rows],
                         [("snapshot_failed", 1, True), ("snapshot_failed", 2, False)])

    def test_other_errors_are_not_retried_but_recorded(self):
        client = Client([RuntimeError("scene 메시지 해석 실패"), Snap("never")])
        env, rows = run(client)
        self.assertIsNone(env)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual((rows[0]["error"], rows[0]["will_retry"]), ("RuntimeError", False))

    def test_log_line_goes_to_its_own_handler(self):
        with self.assertLogs("forstick2.scene", level="WARNING") as cm:
            run(Client([TimeoutError("t1"), TimeoutError("t2")]))
        self.assertTrue(all(line.split(":", 2)[2].startswith("scene_query {") for line in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
