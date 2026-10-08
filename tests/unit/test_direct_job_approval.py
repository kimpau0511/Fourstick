"""2026-10-08 리뷰 2번: 직접 작업·목표 실행 API에도 세션·안전 검증·승인을 적용한다.

- POST /v1/sim-demo/jobs: 세션 필수. 움직이는 동작은 **확인 카드만** 만든다(작업 0건). 작업은 /v1/sim-demo/confirm(같은 세션,
  한 번만, 만료·상태 변경·정지 래치 재검사)으로만 시작한다. 서버 판정(자재별 가능 동작·체크포인트)에 맞지 않으면 바로 거절한다.
- POST /v1/sim-demo/goals·/goals/{id}/confirm: 세션 필수, 만든 세션만 확인, 정지 래치면 거절.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from server.api import Api, ApiError  # noqa: E402
import test_sim_demo_web as base  # noqa: E402

body = base.body


class DirectJobApprovalTest(base.RoutesTest):
    # 이 파일에서는 이 클래스의 시험만 돈다(물려받은 RoutesTest 시험은 원래 파일에서 돈다).
    for _name in [n for n in dir(base.RoutesTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def setUp(self):
        super().setUp()
        from server.sim_demo_goals import SimDemoGoals

        self.runtime.sim_demo_goals = SimDemoGoals(self.jobs, auto_run=False)
        self.api = Api(runtime=self.runtime)
        self.runtime.sim_demo_jobs = self.jobs
        self.session = self.api.create_session(origin="test")["session_id"]
        self.other = self.api.create_session(origin="test")["session_id"]

    def call(self, method, path, payload=None, module=None):
        from server.routes import sim_demo

        ctx = types.SimpleNamespace(runtime=self.runtime, read_body=body(payload or {}), api=self.api)
        return asyncio.run((module or sim_demo).handle(ctx, method, path, None, {}))

    def post(self, path, payload):
        try:
            status, _, raw = self.call("POST", path, payload)
            return status, json.loads(raw)
        except ApiError as exc:
            return exc.status, {"reason_code": None if exc.reason is None else exc.reason.value, "error": str(exc)}

    def test_direct_start_without_session_is_refused(self):
        status, _ = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c"})
        self.assertIn(status, (400, 404))
        self.assertEqual(self.popen.calls, [])

    def test_direct_start_only_creates_a_confirmation(self):
        status, payload = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                           "session_id": self.session})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["decision"], "CONFIRM")
        self.assertTrue(payload["confirmation"]["token"])
        self.assertIsNone(payload.get("job"))
        self.assertEqual(self.popen.calls, [])                   # 승인 전에는 작업 0건

    def test_confirm_starts_exactly_once(self):
        _, offer = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                   "session_id": self.session})
        token = offer["confirmation"]["token"]
        status, started = self.post("/v1/sim-demo/confirm", {"token": token, "action": "confirm",
                                                             "session_id": self.session})
        self.assertEqual(status, 202, started)
        self.assertEqual(started["decision"], "RUN")
        again, _ = self.post("/v1/sim-demo/confirm", {"token": token, "action": "confirm",
                                                      "session_id": self.session})
        self.assertEqual(again, 409)
        self.assertEqual(len(self.popen.calls), 1)

    def test_other_session_cannot_confirm_and_does_not_consume(self):
        _, offer = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                   "session_id": self.session})
        token = offer["confirmation"]["token"]
        status, refused = self.post("/v1/sim-demo/confirm", {"token": token, "action": "confirm",
                                                             "session_id": self.other})
        self.assertEqual(status, 409, refused)
        self.assertEqual(self.popen.calls, [])
        status, started = self.post("/v1/sim-demo/confirm", {"token": token, "action": "confirm",
                                                             "session_id": self.session})
        self.assertEqual(status, 202, started)

    def test_global_stop_latch_blocks_offer_and_confirm(self):
        _, offer = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                   "session_id": self.session})
        with self.api._flag_lock:
            self.api._stop_requested = True
        status, payload = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                          "session_id": self.session})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["reason_code"], "exec.stopped")
        status, payload = self.post("/v1/sim-demo/confirm", {"token": offer["confirmation"]["token"],
                                                             "action": "confirm", "session_id": self.session})
        self.assertEqual(status, 409, payload)
        self.assertEqual(self.popen.calls, [])

    def test_stop_arriving_between_check_and_start_reaches_the_new_job(self):
        """확인은 이벤트 루프 밖에서 돈다 — 정지 검사 뒤·시작 전에 들어온 전체 정지도 새 작업에 전달돼야 한다."""
        _, offer = self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": "material_c",
                                                   "session_id": self.session})
        real_start = self.jobs.start

        def start_then_global_stop(*args, **kwargs):
            job = real_start(*args, **kwargs)
            with self.api._flag_lock:                            # 시작 직후 /v1/stop이 들어왔다(그때는 실행 중 작업 없음)
                self.api._stop_requested = True
            return job
        self.jobs.start = start_then_global_stop
        status, payload = self.post("/v1/sim-demo/confirm", {"token": offer["confirmation"]["token"],
                                                             "action": "confirm", "session_id": self.session})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["confirm_rejection"], "stopped")
        self.assertTrue(payload["stop"]["requested"], payload["stop"])
        self.assertTrue(self.jobs.job(payload["job"]["job_id"])["stop_requested"])

    def test_command_and_confirm_need_a_server_session(self):
        """세션 없는 명령이 만든 카드는 아무 세션이나 확인할 수 있었다 — 명령·확인 모두 서버 발급 세션이 필요하다."""
        for path, payload in (("/v1/sim-demo/command", {"utterance": "C 자재를 컨베이어로 옮겨줘", "source": "text",
                                                         "mode": "simulation_demo"}),
                              ("/v1/sim-demo/confirm", {"token": "simconfirm_x", "action": "confirm"}),
                              ("/v1/sim-demo/confirm", {"token": "simconfirm_x", "action": "confirm",
                                                        "session_id": "sess_not_issued"})):
            with self.subTest(path=path, session=payload.get("session_id")):
                status, body_ = self.post(path, payload)
                self.assertIn(status, (400, 404), body_)
        self.assertEqual(self.popen.calls, [])

    def test_action_the_server_does_not_allow_is_refused_up_front(self):
        status, payload = self.post("/v1/sim-demo/jobs", {"action": "return", "material": "material_c",
                                                          "session_id": self.session})
        self.assertEqual(status, 409, payload)                   # 컨베이어에 없는 자재의 복귀
        self.assertEqual(self.popen.calls, [])

    def test_read_only_reconcile_needs_only_a_session(self):
        status, _ = self.post("/v1/sim-demo/reconcile", {"session_id": self.session})
        self.assertEqual(status, 202)

    def test_goal_api_requires_session_and_owner(self):
        goals = getattr(self.runtime, "sim_demo_goals", None)
        if goals is None:
            self.skipTest("목표 실행기가 없는 설정")
        status, _ = self.post("/v1/sim-demo/goals", {"goal": "return_all_to_origin"})
        self.assertIn(status, (400, 404))
        status, goal = self.post("/v1/sim-demo/goals", {"goal": "return_all_to_origin", "session_id": self.session})
        self.assertIn(status, (200, 201), goal)
        status, refused = self.post(f"/v1/sim-demo/goals/{goal['goal_id']}/confirm",
                                    {"action": "confirm", "session_id": self.other})
        self.assertEqual(status, 409, refused)

    def test_goal_confirm_blocked_by_stop_latch(self):
        goals = getattr(self.runtime, "sim_demo_goals", None)
        if goals is None:
            self.skipTest("목표 실행기가 없는 설정")
        _, goal = self.post("/v1/sim-demo/goals", {"goal": "return_all_to_origin", "session_id": self.session})
        with self.api._flag_lock:
            self.api._stop_requested = True
        status, payload = self.post(f"/v1/sim-demo/goals/{goal['goal_id']}/confirm",
                                    {"action": "confirm", "session_id": self.session})
        self.assertEqual(status, 409, payload)


if __name__ == "__main__":
    unittest.main()
