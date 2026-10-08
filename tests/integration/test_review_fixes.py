"""2026-10-08 종합 리뷰 수정 — 재현·회귀 시험(서버 API, 가짜 로봇·가짜 계획 공급자).

3. 승인 취소·재사용: 취소된 계획을 옛 승인 id로 실행 불가, 승인 1회만, 동시 요청도 실행은 최대 1번, 실패 뒤 재사용 없음
5. 계획 생성이 전체 정지를 풀지 않는다(다른 세션 포함) — 명시적 해제만
8. 명령 길이 상한(모델 호출 전)
9. 실행 허가의 환경 버전·세션·신선도가 실제 승인·현재 값을 비교한다
10. 정지 표현: 분명한 정지만 정지, 애매하면 모델·계획 없이 확인 요청
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from core.reason_codes import ReasonCode  # noqa: E402
from integration.test_web_api import WebCase  # noqa: E402


class ApprovalTest(WebCase):
    async def approve(self, bundle):
        return (await self.decide(bundle["plan"]["plan_id"], bundle=bundle)).json()

    async def execute_with(self, bundle, approval_id, *, session_id=None):
        return await self.client.post("/v1/execute", {
            "session_id": session_id or self.session, "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"], "approval_id": approval_id})

    def executions(self, bundle):
        return self.app.runtime.repository.executions_for_request(bundle["request_id"])

    async def test_rejected_after_approval_old_approval_id_cannot_execute(self):
        bundle = (await self.plan()).json()
        old = await self.approve(bundle)
        await self.decide(bundle["plan"]["plan_id"], "reject", bundle=bundle)
        response = await self.execute_with(bundle, old["approval_id"])
        self.assertEqual(response.status, 409, response.json())
        self.assertEqual(response.json()["reason_code"], ReasonCode.SAFETY_APPROVAL_REQUIRED.value)
        self.assertEqual(self.executions(bundle), ())

    async def test_newer_approval_supersedes_older_one(self):
        bundle = (await self.plan()).json()
        old = await self.approve(bundle)
        await self.approve(bundle)
        response = await self.execute_with(bundle, old["approval_id"])
        self.assertEqual(response.status, 409, response.json())

    async def test_approval_is_used_once(self):
        bundle = (await self.plan()).json()
        approval = await self.approve(bundle)
        first = await self.execute_with(bundle, approval["approval_id"])
        self.assertEqual(first.status, 200, first.json())
        again = await self.execute_with(bundle, approval["approval_id"])      # 응답 유실 뒤 재요청 등
        self.assertEqual(again.status, 409, again.json())
        self.assertEqual(again.json()["reason_code"], ReasonCode.SAFETY_APPROVAL_REQUIRED.value)
        implicit = await self.run_execute(bundle)                              # approval_id 없이도 재사용 불가
        self.assertEqual(implicit.status, 409, implicit.json())
        self.assertEqual(len(self.executions(bundle)), 1)

    async def test_concurrent_requests_with_one_approval_start_at_most_once(self):
        bundle = (await self.plan()).json()
        approval = await self.approve(bundle)
        responses = await asyncio.gather(*(self.execute_with(bundle, approval["approval_id"]) for _ in range(4)))
        statuses = sorted(r.status for r in responses)
        self.assertEqual(statuses.count(200), 1, [r.json() for r in responses])
        self.assertEqual(len(self.executions(bundle)), 1)

    async def test_failed_execution_does_not_leave_the_approval_reusable(self):
        bundle = (await self.plan()).json()
        approval = await self.approve(bundle)
        runtime = self.app.runtime
        adapter = runtime.adapter()
        real_connect = adapter.connect

        def broken(timeout):
            raise RuntimeError("연결 끊김(시험)")
        adapter.connect = broken
        try:
            first = await self.execute_with(bundle, approval["approval_id"])
        finally:
            adapter.connect = real_connect
        self.assertNotEqual(first.status, 200)
        again = await self.execute_with(bundle, approval["approval_id"])
        self.assertEqual(again.status, 409, again.json())
        self.assertEqual(again.json()["reason_code"], ReasonCode.SAFETY_APPROVAL_REQUIRED.value)
        # 새로 승인하면 실행할 수 있다(실패 뒤 사람이 다시 승인).
        fresh = await self.approve(bundle)
        ok = await self.execute_with(bundle, fresh["approval_id"])
        self.assertEqual(ok.status, 200, ok.json())

    async def test_other_session_cannot_use_my_approval(self):
        bundle = (await self.plan()).json()
        approval = await self.approve(bundle)
        other = (await self.client.post("/v1/sessions", {"origin": "test"})).json()["session_id"]
        response = await self.execute_with(bundle, approval["approval_id"], session_id=other)
        self.assertIn(response.status, (404, 409))
        self.assertEqual(self.executions(bundle), ())
        mine = await self.execute_with(bundle, approval["approval_id"])        # 남의 시도가 내 승인을 쓰지 않았다
        self.assertEqual(mine.status, 200, mine.json())


class StopLatchTest(WebCase):
    async def test_new_plan_does_not_release_the_global_stop(self):
        stop = (await self.client.post("/v1/stop", {"session_id": self.session})).json()
        self.assertTrue(stop["requested"])
        bundle = (await self.plan()).json()
        self.assertTrue(bundle["ok"], bundle)
        self.assertFalse(bundle["stop_latch"]["cleared"], bundle["stop_latch"])
        await self.decide(bundle["plan"]["plan_id"], bundle=bundle)
        response = await self.run_execute(bundle)
        self.assertEqual(response.status, 409, response.json())
        self.assertEqual(response.json()["reason_code"], ReasonCode.EXEC_STOPPED.value)

    async def test_other_session_plan_does_not_release_either(self):
        await self.client.post("/v1/stop", {"session_id": self.session})
        other = (await self.client.post("/v1/sessions", {"origin": "test"})).json()["session_id"]
        bundle = (await self.plan(session_id=other)).json()
        self.assertFalse(bundle["stop_latch"]["cleared"])
        self.assertTrue(self.app.api._stop_requested if hasattr(self.app, "api") else True)

    async def test_latch_is_visible_for_the_release_button_even_if_adapter_latch_is_missing(self):
        await self.client.post("/v1/stop", {"session_id": self.session})
        adapter = self.app.runtime.adapter()
        tracker = getattr(adapter, "tracker", None)
        if tracker is not None:
            tracker._stopped = False                      # 어댑터 쪽 래치가 없는 경우(정지 실패 등)를 만든다
        diag = (await self.client.get("/v1/robots")).json()["stop_diagnostics"]
        self.assertTrue(diag["server_stop_latched"])
        self.assertTrue(diag["stop_latch_active"])          # 화면에 '정지 해제'가 보인다
        await self.client.post("/v1/stop/release", {"session_id": self.session})
        diag = (await self.client.get("/v1/robots")).json()["stop_diagnostics"]
        self.assertFalse(diag["server_stop_latched"])

    async def test_explicit_release_then_execute(self):
        await self.client.post("/v1/stop", {"session_id": self.session})
        released = (await self.client.post("/v1/stop/release", {"session_id": self.session})).json()
        self.assertTrue(released["released"], released)
        bundle = (await self.plan()).json()
        await self.decide(bundle["plan"]["plan_id"], bundle=bundle)
        response = await self.run_execute(bundle)
        self.assertEqual(response.status, 200, response.json())


class UtteranceLimitTest(WebCase):
    async def test_too_long_utterance_is_refused_before_the_model(self):
        from server.api import MAX_UTTERANCE_CHARS

        calls = self.provider.calls
        response = await self.plan("A자재를 컨베이어로 옮겨줘 " * (MAX_UTTERANCE_CHARS // 10))
        self.assertEqual(response.status, 413, response.json())
        self.assertEqual(response.json()["reason_code"], ReasonCode.PLAN_INPUT_TOO_LONG.value)
        self.assertEqual(self.provider.calls, calls)

    async def test_long_but_normal_command_is_accepted(self):
        from server.api import MAX_UTTERANCE_CHARS

        text = "음 그러니까 지금 1번 팔레트에 올려져 있는 주황색 A자재를 조심해서 집은 다음에 컨베이어 쪽으로 천천히 옮겨 주실 수 있을까요"
        self.assertLess(len(text), MAX_UTTERANCE_CHARS)
        response = await self.plan(text)
        self.assertNotEqual(response.status, 413)


class PermitEnvironmentTest(WebCase):
    async def prepared(self):
        bundle = (await self.plan()).json()
        await self.decide(bundle["plan"]["plan_id"], bundle=bundle)
        return bundle

    async def test_environment_session_change_blocks(self):
        bundle = await self.prepared()
        adapter = self.app.runtime.adapter()
        adapter.session_id = "different-environment-session"        # 승인 뒤 환경 세션이 바뀌었다(재연결 등)
        try:
            response = await self.run_execute(bundle)
        finally:
            del adapter.session_id
        body = response.json()
        self.assertFalse(body.get("granted", False), body)
        codes = [r["reason_code"] for r in body.get("reasons", [])]
        self.assertIn(ReasonCode.EXEC_ENVIRONMENT_CHANGED.value, codes, body)

    async def test_environment_version_change_blocks(self):
        bundle = await self.prepared()
        repo = self.app.runtime.repository
        approval = repo.approvals_for_plan(bundle["plan"]["plan_id"])[-1]
        repo._conn.execute("UPDATE plan_approvals SET snapshot_version = 'old-version' WHERE approval_id = ?",
                           (approval.approval_id,))
        response = await self.run_execute(bundle)
        self.assertNotEqual(response.status, 200)

    async def test_stale_environment_observation_blocks(self):
        bundle = await self.prepared()
        runtime = self.app.runtime
        real = runtime.environment_snapshot

        def stale():
            snap = real()
            return None if snap is None else dataclasses.replace(
                snap, captured_at=snap.captured_at - 3600.0, ttl_sec=7200.0)
        runtime.environment_snapshot = stale
        try:
            response = await self.run_execute(bundle)
        finally:
            runtime.environment_snapshot = real
        body = response.json()
        self.assertFalse(body.get("ok"), body)
        text = str(body)
        self.assertIn("환경", text)


class StopExpressionApiTest(WebCase):
    async def test_ambiguous_stop_asks_without_model_or_plan(self):
        calls = self.provider.calls
        body = (await self.plan("정지 버튼 어디 있어?")).json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["decision"], "ASK")
        self.assertIn("정지하라는 말인지 확실하지 않습니다", body["clarification"])
        self.assertEqual(self.provider.calls, calls)
        self.assertNotIn("stopped", body)

    async def test_clear_stop_stops_without_model(self):
        calls = self.provider.calls
        body = (await self.plan("A 옮겨, 아니 멈춰")).json()
        self.assertTrue(body.get("stopped"), body)
        self.assertEqual(body["decision"], "STOP")
        self.assertEqual(self.provider.calls, calls)

    async def test_geuman_stops_without_model(self):
        calls = self.provider.calls
        body = (await self.plan("로봇 이제 그만해")).json()
        self.assertTrue(body.get("stopped"), body)
        self.assertEqual(self.provider.calls, calls)
        body = (await self.plan("그만 컨베이어로 B자재 옮겨")).json()
        self.assertEqual(body["decision"], "ASK", body)
        self.assertNotIn("stopped", body)

    async def test_part_of_another_word_is_planned_normally(self):
        body = (await self.plan("스톱워치 옆 1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘")).json()
        self.assertNotIn("stopped", body)



class BodyLimitTest(WebCase):
    """2026-10-08 리뷰 8번: 본문은 읽는 도중에 상한을 넘으면 끊는다(전부 받은 뒤가 아니다)."""

    async def call(self, *, chunks, content_length=None):
        from server.request_limits import MAX_BODY_BYTES  # noqa: F401

        headers = [(b"content-type", b"application/json")]
        if content_length is not None:
            headers.append((b"content-length", str(content_length).encode()))
        scope = {"type": "http", "method": "POST", "path": "/v1/plan", "query_string": b"", "headers": headers}
        pulled = {"n": 0}
        queue = list(chunks)

        async def receive():
            pulled["n"] += 1
            body = queue.pop(0) if queue else b""
            return {"type": "http.request", "body": body, "more_body": bool(queue)}
        out = {}

        async def send(message):
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
            elif message["type"] == "http.response.body":
                out["body"] = message["body"]
        await self.app(scope, receive, send)
        return out, pulled["n"], len(queue)

    async def test_streamed_body_over_the_limit_stops_reading(self):
        from server.request_limits import MAX_BODY_BYTES

        chunk = b"x" * 16384
        n = MAX_BODY_BYTES // len(chunk) + 20                       # 상한의 몇 배를 보낸다
        out, pulled, left = await self.call(chunks=[b'{"utterance":"'] + [chunk] * n)
        self.assertEqual(out["status"], 413, out)
        self.assertIn(b"session.request_too_large", out["body"])
        self.assertGreater(left, 0)                                   # 남은 조각을 끝까지 읽지 않았다
        self.assertLessEqual(pulled * len(chunk), MAX_BODY_BYTES + 2 * len(chunk))

    async def test_declared_length_over_the_limit_is_refused_before_reading(self):
        from server.request_limits import MAX_BODY_BYTES

        out, pulled, _ = await self.call(chunks=[b"{}"], content_length=MAX_BODY_BYTES + 1)
        self.assertEqual(out["status"], 413, out)
        self.assertEqual(pulled, 0)

    async def test_normal_body_is_unaffected(self):
        response = await self.plan()
        self.assertEqual(response.status, 200)


class StopDeliveryTest(WebCase):
    """2026-10-08 리뷰 4번: /v1/stop은 다른 검사·응답 대기보다 먼저 실행 중인 작업(재개·복구·반복 재개 포함)에 정지를 전달한다.
    전달과 실제 정지 확인은 따로 기록한다."""

    def install_job_runner(self):
        import time as _time

        calls = []

        class Jobs:
            def running(self):
                return {"job_id": "simjob_resume", "action": "resume", "status": "running"}

            def request_stop(self, *, reason):
                calls.append(("jobs", _time.monotonic(), reason))
                return {"requested": True, "job_id": "simjob_resume", "request_id": "simstopreq_x"}

            def status(self):
                return {"running_job": self.running()}

        runtime = self.app.runtime
        runtime.sim_demo_jobs = Jobs()
        runtime.sim_demo_goals = None
        return calls

    def slow_adapter(self, seconds):
        import time as _time

        adapter = self.app.runtime.adapter()
        real = adapter.connect
        log = []

        def slow(timeout):
            log.append(("connect", _time.monotonic()))
            _time.sleep(seconds)
            return real(timeout)
        adapter.connect = slow
        real_stop = adapter.stop

        def stop(*a, **kw):
            log.append(("adapter_stop", _time.monotonic()))
            return real_stop(*a, **kw)
        adapter.stop = stop
        return log

    async def test_job_stop_is_delivered_before_adapter_checks(self):
        import time as _time

        calls = self.install_job_runner()
        log = self.slow_adapter(1.5)
        started = _time.monotonic()
        body = (await self.client.post("/v1/stop", {"session_id": self.session})).json()
        self.assertTrue(calls, body)
        self.assertLess(calls[0][1] - started, 0.3)                       # 연결 확인(1.5초)을 기다리지 않았다
        # 시뮬레이션 작업이 돌고 있으면 연결 재확인보다 취소를 먼저 보낸다.
        kinds = [k for k, _ in log]
        self.assertTrue(kinds and kinds[0] == "adapter_stop", kinds)
        # 전달과 확인을 따로 남긴다.
        self.assertIn("delivery", body)
        targets = {d["target"] for d in body["delivery"]}
        self.assertIn("simulation_job", targets)
        self.assertIn("confirmed", body)

    async def test_stop_word_delivers_to_job_first(self):
        import time as _time

        calls = self.install_job_runner()
        self.slow_adapter(1.5)
        started = _time.monotonic()
        body = (await self.plan("멈춰")).json()
        self.assertTrue(body.get("stopped"), body)
        self.assertTrue(calls)
        self.assertLess(calls[0][1] - started, 0.3)


class StopNotBlockedTest(WebCase):
    """4. 보완: 계획(모델 호출)이 이벤트 루프를 막으면 그동안 /v1/stop이 처리되지 않았다(격리 셀 실측 8.8 s)."""

    async def test_stop_is_handled_while_a_slow_plan_is_running(self):
        import time as _time

        real = self.provider.generate

        def slow(context):
            _time.sleep(1.5)                         # 모델 응답이 느리다
            return real(context)
        self.provider.generate = slow
        planning = asyncio.ensure_future(self.plan())
        await asyncio.sleep(0.2)
        started = _time.monotonic()
        stopped = await self.client.post("/v1/stop", {"session_id": self.session})
        elapsed = _time.monotonic() - started
        self.assertEqual(stopped.status, 200, stopped.json())
        self.assertFalse(planning.done(), "정지 응답이 계획이 끝난 뒤에야 왔다")
        self.assertLess(elapsed, 1.0)
        await planning


class MaterialCheckPlanTest(WebCase):
    """11. 일반 계획: 기록·관측 모두 이미 목적지면 '할 일 없음'(계획·승인 없음), 불일치는 관문 BLOCK."""

    def gate(self, **result):
        from unittest import mock

        def fake(runtime, plan):
            return {"decision": "BLOCK", "unit": {}, "material_model": "material_a", **result}
        return mock.patch("server.api.gate_transfer", fake)

    async def test_already_at_destination_is_noop_without_a_plan(self):
        with self.gate(reason=ReasonCode.PLAN_RESOURCE_MISMATCH, noop=True,
                       detail="할 일 없음: 이미 목적지에 있습니다(기록·관측 일치)",
                       material_check={"kind": "noop"}):
            body = (await self.plan()).json()
        self.assertFalse(body["ok"], body)
        self.assertEqual(body["decision"], "NOOP")
        self.assertIn("할 일 없음", body["clarification"])
        self.assertIsNone(body.get("plan"))

    async def test_mismatch_blocks_at_the_gate_and_cannot_execute(self):
        with self.gate(reason=ReasonCode.EXEC_UNVERIFIABLE,
                       detail="기록은 loc_pallet_1인데 관측 위치가 0.200 m 떨어져 있다 — 정합 확인(복구)",
                       material_check={"kind": "mismatch"}):
            bundle = (await self.plan()).json()
            self.assertEqual(bundle["validation"]["decision"], "block", bundle["validation"])
            self.assertFalse(bundle["executable"])
            decided = (await self.decide(bundle["plan"]["plan_id"], bundle=bundle)).json()
            self.assertFalse(decided["executable"], decided)
            response = await self.run_execute(bundle)
            self.assertFalse(response.json().get("granted", False), response.json())
        self.assertEqual(self.app.runtime.repository.executions_for_request(bundle["request_id"]), ())


if __name__ == "__main__":
    unittest.main()
