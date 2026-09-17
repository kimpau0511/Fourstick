"""세션의 브라우저 경계 (md/웹UI_구조.md "다중 세션 격리").

세션 격리는 서버 식별자만으로 끝나지 않는다. 브라우저 쪽 경계를 함께 본다.

  1. 새 탭은 새 세션이다
  2. **탭 복제**는 sessionStorage를 복사해 같은 session_id를 보낸다 —
     클라이언트로 분리한다
  3. 일반 새로고침은 같은 세션을 유지한다
  4. 탭을 닫아도 진행 중인 실행을 즉시 취소하지 않는다(만료 정책에 따른다)
  5. 전체 STOP은 활성 실행 전체에 적용된다
  6. 전체 STOP 결과는 영향받은 세션에 전달되지만, 다른 세션의 상세는 노출되지
     않는다
  7. 특정 실행 취소는 소유 세션의 그 실행에만 적용된다
  8. 전체 STOP과 실행 취소의 상태 전이·ReasonCode가 구분된다
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode
from integration.asgi_client import WebSocketSession
from integration.test_session_isolation import IsolationCase
from storage.records import SessionStatus


class ClientCase(IsolationCase):
    """세션 A·B 위에 클라이언트(탭) 개념을 더해 본다."""

    async def claim(self, session_id: str, client_id: str | None = None):
        body = {} if client_id is None else {"client_id": client_id}
        return await self.client.post(f"/v1/sessions/{session_id}/clients", body)

    def events_path(self, session_id: str, client_id: str) -> str:
        return f"/v1/events?session_id={session_id}&client_id={client_id}"


class TestTabDuplication(ClientCase):
    """2. 복제된 탭은 같은 session_id를 보낸다. 클라이언트로 분리한다."""

    async def test_new_session_comes_with_a_client_id(self):
        payload = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        self.assertTrue(payload["client_id"].startswith("cli_"))
        self.assertGreater(payload["client_grace_sec"], 0)

    async def test_duplicated_tab_is_refused_while_the_original_is_live(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab-1"})).json()
        session_id, client_id = first["session_id"], first["client_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, client_id)
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            # 복제된 탭: session_id는 복사됐지만 client_id는 메모리에만 있어 없다.
            response = await self.claim(session_id)
            self.assertEqual(response.status, 409)
            self.assertEqual(
                response.json()["reason_code"],
                ReasonCode.SESSION_CLIENT_CONFLICT.value,
            )

    async def test_duplicated_tab_socket_is_refused_too(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab-1"})).json()
        session_id, client_id = first["session_id"], first["client_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, client_id)
        ) as original:
            await original.wait_for(lambda m: m.get("type") == "hello")
            async with WebSocketSession(
                self.app, self.events_path(session_id, "cli_copied")
            ) as clone:
                message = await clone.wait_for(lambda m: m.get("type") == "error")
                self.assertEqual(
                    message["reason_code"],
                    ReasonCode.SESSION_CLIENT_CONFLICT.value,
                )
                await asyncio.sleep(0.05)
                self.assertTrue(clone.closed)

    async def test_the_clone_gets_its_own_session_and_stays_separate(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab-1"})).json()
        async with WebSocketSession(
            self.app, self.events_path(first["session_id"], first["client_id"])
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            mine = await self.plan(first["session_id"])
            # 복제 탭이 새 세션을 받는다.
            clone = (await self.client.post("/v1/sessions", {"origin": "clone"})).json()
            self.assertNotEqual(clone["session_id"], first["session_id"])
            restored = (await self.client.get(
                f"/v1/state?session_id={clone['session_id']}"
            )).json()
            self.assertIsNone(restored["plan"])
            # 원래 탭의 계획은 그대로 남는다.
            still = (await self.client.get(
                f"/v1/state?session_id={first['session_id']}"
            )).json()
            self.assertEqual(still["plan"]["plan_id"], mine["plan"]["plan_id"])

    async def test_known_client_id_is_reusable_as_a_heartbeat(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        again = (await self.claim(
            first["session_id"], first["client_id"]
        )).json()
        self.assertTrue(again["reused"])
        self.assertEqual(again["client_id"], first["client_id"])

    async def test_unknown_client_id_is_refused(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        response = await self.claim(first["session_id"], "cli_invented")
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.SESSION_CLIENT_UNKNOWN.value
        )


class TestGraceWindowRace(ClientCase):
    """등록과 WebSocket 연결 사이(유예 구간)의 경쟁 조건.

    유예는 `ServerConfig.client_grace_sec`가 유일한 출처다 — 테스트도 그 값을
    읽고, 브라우저는 `/v1/config`에서 받는다(숫자를 중복하지 않는다).
    """

    async def test_grace_is_configured_not_hardcoded(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertEqual(
            payload["session"]["client_grace_sec"], self.config.client_grace_sec
        )
        created = (await self.client.post("/v1/sessions", {})).json()
        self.assertEqual(created["client_grace_sec"], self.config.client_grace_sec)
        # 화면 코드가 숫자를 따로 갖고 있지 않다.
        backend = (
            ROOT / "html" / "static" / "js" / "backend-http.js"
        ).read_text(encoding="utf-8")
        self.assertIn("client_grace_sec", backend)
        self.assertNotIn("graceSec = 5", backend)

    async def test_claim_during_the_grace_window_is_refused(self):
        """등록 직후(WS 연결 전)에도 다른 탭이 세션을 가져가지 못한다."""
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        self.assertGreater(self.config.client_grace_sec, 0)
        response = await self.claim(first["session_id"])   # WS 연결 전이다
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.SESSION_CLIENT_CONFLICT.value
        )

    async def test_socket_during_the_grace_window_is_refused(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        async with WebSocketSession(
            self.app, self.events_path(first["session_id"], "cli_copied")
        ) as clone:
            message = await clone.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(
                message["reason_code"], ReasonCode.SESSION_CLIENT_CONFLICT.value
            )

    async def test_simultaneous_duplicate_and_refresh_leave_one_owner(self):
        """복제와 새로고침이 동시에 일어나도 세션 소유자는 하나다.

        같은 session_id로 두 이어받기가 경쟁하면 한쪽만 성공하고, 성공한 쪽만
        이벤트를 구독할 수 있다.
        """
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id = first["session_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, first["client_id"])
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
        await asyncio.sleep(0.05)      # 새로고침으로 구독이 끊겼다

        # 복제 탭과 새로고침이 동시에 이어받기를 시도한다.
        first_try, second_try = await asyncio.gather(
            self.claim(session_id), self.claim(session_id)
        )
        statuses = sorted([first_try.status, second_try.status])
        self.assertEqual(statuses, [200, 409])
        winner = first_try if first_try.status == 200 else second_try
        loser = second_try if first_try.status == 200 else first_try
        self.assertEqual(
            loser.json()["reason_code"], ReasonCode.SESSION_CLIENT_CONFLICT.value
        )

        # 이긴 쪽만 구독할 수 있다.
        client_id = winner.json()["client_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, client_id)
        ) as owner:
            hello = await owner.wait_for(lambda m: m.get("type") == "hello")
            self.assertEqual(hello["session_id"], session_id)
            async with WebSocketSession(
                self.app, self.events_path(session_id, "cli_other")
            ) as other:
                message = await other.wait_for(lambda m: m.get("type") == "error")
                self.assertEqual(
                    message["reason_code"],
                    ReasonCode.SESSION_CLIENT_CONFLICT.value,
                )
        self.assertEqual(self.app.api.clients_of(session_id), [])

    async def test_many_simultaneous_claims_leave_one_winner(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id = first["session_id"]
        api = self.app.api
        api.detach_client(session_id=session_id, client_id=first["client_id"])
        results = await asyncio.gather(*[self.claim(session_id) for _ in range(6)])
        winners = [r for r in results if r.status == 200]
        self.assertEqual(len(winners), 1)
        self.assertTrue(all(
            r.json()["reason_code"] == ReasonCode.SESSION_CLIENT_CONFLICT.value
            for r in results if r.status != 200
        ))

    async def test_expired_grace_lets_the_session_be_reclaimed(self):
        """유예가 지나고 구독도 없으면 이어받을 수 있다(실제 새로고침 실패 복구)."""
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        api = self.app.api
        later = api.now() + self.config.client_grace_sec + 1
        api.now = lambda: later
        response = await self.claim(first["session_id"])
        self.assertEqual(response.status, 200)
        self.assertNotEqual(response.json()["client_id"], first["client_id"])


class TestRefreshKeepsTheSession(ClientCase):
    """3. 일반 새로고침은 기존 세션을 유지한다."""

    async def test_reclaim_after_the_socket_closes_keeps_the_session(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id = first["session_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, first["client_id"])
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            bundle = await self.plan(session_id)
        # 소켓이 닫혔다 = 새로고침. 같은 세션을 이어받는다.
        await asyncio.sleep(0.05)
        again = await self.claim(session_id)
        self.assertEqual(again.status, 200)
        payload = again.json()
        self.assertEqual(payload["session_id"], session_id)
        self.assertNotEqual(payload["client_id"], first["client_id"])
        restored = (await self.client.get(f"/v1/state?session_id={session_id}")).json()
        self.assertEqual(restored["plan"]["plan_id"], bundle["plan"]["plan_id"])

    async def test_session_stays_active_after_the_tab_closes(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id = first["session_id"]
        async with WebSocketSession(
            self.app, self.events_path(session_id, first["client_id"])
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
        await asyncio.sleep(0.05)
        record = self.runtime.repository.get_session(session_id)
        self.assertIs(record.status, SessionStatus.ACTIVE)
        self.assertEqual(self.app.api.clients_of(session_id), [])


class TestClosedTabDoesNotCancelExecution(ClientCase):
    """4. 닫힌 탭의 세션은 즉시 실행을 취소하지 않는다."""

    slow_adapter = True

    async def test_execution_survives_the_tab_closing(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id, client_id = first["session_id"], first["client_id"]
        mine = await self.approved_plan(session_id)
        running = asyncio.create_task(self.execute(session_id, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)

        async with WebSocketSession(
            self.app, self.events_path(session_id, client_id)
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
        # 탭이 닫혔다. 실행은 계속되고 결과가 나온다.
        result = (await running).json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["final"]["state"], "completed")
        record = self.runtime.repository.get_session(session_id)
        self.assertIs(record.status, SessionStatus.ACTIVE)

    async def test_expiry_policy_is_what_ends_the_session(self):
        first = (await self.client.post("/v1/sessions", {"origin": "tab"})).json()
        session_id = first["session_id"]
        api = self.app.api
        later = api.now() + self.config.session_idle_timeout_sec + 1
        api.now = lambda: later
        response = await self.client.get(f"/v1/state?session_id={session_id}")
        self.assertEqual(response.status, 409)
        self.assertIs(
            self.runtime.repository.get_session(session_id).status,
            SessionStatus.EXPIRED,
        )


class TestGlobalStopReachesEverySession(ClientCase):
    """5·6. 전체 STOP은 활성 실행 전체에 적용되고, 상세는 소유 세션에만 간다."""

    slow_adapter = True

    async def test_stop_marks_every_active_execution(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        stop = (await self.client.post("/v1/stop", {"session_id": self.a})).json()
        self.assertEqual(stop["affected_execution_count"], 1)
        self.assertEqual(len(stop["your_execution_ids"]), 1)
        result = (await running).json()
        self.assertEqual(
            result["final"]["reason_code"], ReasonCode.EXEC_STOPPED.value
        )
        self.assertEqual(stop["your_execution_ids"], [result["execution_id"]])

    async def test_other_sessions_see_the_stop_without_details(self):
        mine = await self.approved_plan(self.a)
        async with WebSocketSession(
            self.app, f"/v1/events?session_id={self.b}"
        ) as watcher:
            await watcher.wait_for(lambda m: m.get("type") == "hello")
            running = asyncio.create_task(self.execute(self.a, mine))
            await asyncio.to_thread(self.adapter.entered.wait, 2.0)
            await self.client.post("/v1/stop", {"session_id": self.b})
            result = (await running).json()
            events = [
                m for m in await watcher.messages(timeout=0.3)
                if m.get("type") == "stop"
            ]
            self.assertTrue(events)
            for event in events:
                payload = event["payload"]
                self.assertEqual(event["scope"], "global")
                # 다른 세션의 실행 식별자·계획·승인이 들어 있지 않다.
                self.assertNotIn("your_execution_ids", payload)
                blob = str(payload)
                self.assertNotIn(result["execution_id"], blob)
                self.assertNotIn(mine["plan"]["plan_id"], blob)
                self.assertNotIn(mine["request_id"], blob)
                self.assertIn("affected_execution_count", payload)

    async def test_the_owning_session_gets_its_own_execution_ids(self):
        mine = await self.approved_plan(self.a)
        async with WebSocketSession(
            self.app, f"/v1/events?session_id={self.a}"
        ) as owner:
            await owner.wait_for(lambda m: m.get("type") == "hello")
            running = asyncio.create_task(self.execute(self.a, mine))
            await asyncio.to_thread(self.adapter.entered.wait, 2.0)
            await self.client.post("/v1/stop", {"session_id": self.b})
            result = (await running).json()
            detailed = [
                m for m in await owner.messages(timeout=0.3)
                if m.get("type") == "stop" and m.get("scope") == "session"
            ]
            self.assertTrue(detailed)
            self.assertIn(
                result["execution_id"],
                detailed[-1]["payload"]["your_execution_ids"],
            )

    async def test_stop_applies_to_several_active_executions(self):
        """활성 실행이 여러 개일 때도 전체 정지가 모두에 적용된다.

        로봇이 하나라 실행 잠금이 동시 실행을 1개로 제한하므로, 계약 동작을
        보기 위해 활성 목록에 두 실행을 직접 넣는다. 정지는 활성 목록 전체를
        대상으로 하고, 상세는 소유 세션별로만 나간다.
        """
        api = self.app.api
        with api._flag_lock:
            api._active_executions.update({
                "exec_a1": self.a, "exec_a2": self.a, "exec_b1": self.b,
            })
        async with WebSocketSession(
            self.app, f"/v1/events?session_id={self.a}"
        ) as sa, WebSocketSession(
            self.app, f"/v1/events?session_id={self.b}"
        ) as sb:
            await sa.wait_for(lambda m: m.get("type") == "hello")
            await sb.wait_for(lambda m: m.get("type") == "hello")
            stop = (await self.client.post("/v1/stop", {"session_id": self.a})).json()

            self.assertEqual(stop["affected_execution_count"], 3)
            self.assertEqual(stop["your_execution_ids"], ["exec_a1", "exec_a2"])
            # 세션마다 자기 실행만 받는다.
            a_event = await sa.wait_for(
                lambda m: m.get("type") == "stop" and m.get("scope") == "session"
            )
            b_event = await sb.wait_for(
                lambda m: m.get("type") == "stop" and m.get("scope") == "session"
            )
            self.assertEqual(
                a_event["payload"]["your_execution_ids"], ["exec_a1", "exec_a2"]
            )
            self.assertEqual(
                b_event["payload"]["your_execution_ids"], ["exec_b1"]
            )
            # 전체 범위 이벤트에는 식별자가 없다.
            for socket in (sa, sb):
                globals_ = [
                    m for m in await socket.messages(timeout=0.1)
                    if m.get("type") == "stop" and m.get("scope") == "global"
                ]
                self.assertTrue(globals_)
                for event in globals_:
                    blob = str(event["payload"])
                    for execution_id in ("exec_a1", "exec_a2", "exec_b1"):
                        self.assertNotIn(execution_id, blob)
        with api._flag_lock:
            api._active_executions.clear()

    async def test_stop_latches_until_a_new_plan_is_accepted(self):
        """전체 정지는 래치다.

        계약(`core/stop_contract.reset_for_new_plan`)이 정한 해제 지점은
        **새 계획 수락**이다. 실행마다 래치를 풀면 STOP 이후 첫 실행이 이유 없이
        진행된다(forstick에서 실제로 있었던 문제의 반대 방향).
        """
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        await self.client.post("/v1/stop", {})
        first = (await running).json()
        self.assertEqual(
            first["final"]["reason_code"], ReasonCode.EXEC_STOPPED.value
        )

        # 래치가 걸려 있어 재실행은 거부된다.
        refused = await self.execute(self.a, mine)
        self.assertEqual(refused.status, 409)
        self.assertEqual(
            refused.json()["reason_code"], ReasonCode.EXEC_STOPPED.value
        )
        self.assertEqual(
            len(self.runtime.repository.executions_for_request(mine["request_id"])), 1
        )

        # 새 계획을 요청하면 래치가 풀린다.
        fresh = await self.approved_plan(self.a)
        allowed = (await self.execute(self.a, fresh)).json()
        self.assertTrue(allowed["ok"])

    async def test_new_plan_does_not_clear_the_latch_while_something_runs(self):
        """진행 중인 실행이 있으면 새 계획이 래치를 풀지 않는다."""
        api = self.app.api
        with api._flag_lock:
            api._stop_requested = True
            api._active_executions["exec_other"] = self.b
        await self.plan(self.a)
        with api._flag_lock:
            still_latched = api._stop_requested
            api._active_executions.clear()
            api._stop_requested = False
        self.assertTrue(still_latched)

    async def test_stop_without_any_active_execution_reports_zero(self):
        stop = (await self.client.post("/v1/stop", {})).json()
        self.assertEqual(stop["affected_execution_count"], 0)
        self.assertEqual(stop["your_execution_ids"], [])


class TestStopAndCancelAreDistinct(ClientCase):
    """7·8. 취소는 소유 세션의 그 실행에만. 전이·ReasonCode가 구분된다."""

    slow_adapter = True

    async def _interrupt(self, kind: str) -> dict:
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        execution_id = self.app.api.running_execution_id
        if kind == "stop":
            await self.client.post("/v1/stop", {"session_id": self.a})
        else:
            await self.client.post(
                f"/v1/executions/{execution_id}/cancel", {"session_id": self.a}
            )
        result = (await running).json()
        trace = self.runtime.repository.trace(result["execution_id"])
        return {
            "result": result,
            "states": [t.to_state.value for t in trace.transitions],
            "reasons": [
                None if t.reason is None else t.reason.value
                for t in trace.transitions
            ],
        }

    async def test_global_stop_uses_exec_stopped(self):
        out = await self._interrupt("stop")
        self.assertEqual(
            out["result"]["final"]["reason_code"], ReasonCode.EXEC_STOPPED.value
        )
        self.assertIn(ReasonCode.EXEC_STOPPED.value, out["reasons"])
        self.assertNotIn(ReasonCode.EXEC_CANCELED.value, out["reasons"])
        self.assertEqual(out["states"][-1], "stopped")
        self.assertIn("stopping", out["states"])

    async def test_execution_cancel_uses_exec_canceled(self):
        out = await self._interrupt("cancel")
        self.assertEqual(
            out["result"]["final"]["reason_code"], ReasonCode.EXEC_CANCELED.value
        )
        self.assertIn(ReasonCode.EXEC_CANCELED.value, out["reasons"])
        self.assertNotIn(ReasonCode.EXEC_STOPPED.value, out["reasons"])
        self.assertEqual(out["states"][-1], "stopped")
        self.assertEqual(out["result"]["interrupted"], ReasonCode.EXEC_CANCELED.value)

    async def test_cancel_does_not_block_the_next_execution(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        execution_id = self.app.api.running_execution_id
        await self.client.post(
            f"/v1/executions/{execution_id}/cancel", {"session_id": self.a}
        )
        await running
        again = (await self.execute(self.a, mine)).json()
        self.assertTrue(again["ok"])

    async def test_cancel_of_another_sessions_execution_is_refused(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        execution_id = self.app.api.running_execution_id
        response = await self.client.post(
            f"/v1/executions/{execution_id}/cancel", {"session_id": self.b}
        )
        self.assertEqual(response.status, 404)
        result = (await running).json()
        self.assertTrue(result["ok"])       # 취소되지 않고 끝까지 진행했다

    async def test_cancel_event_goes_only_to_the_owner(self):
        mine = await self.approved_plan(self.a)
        async with WebSocketSession(
            self.app, f"/v1/events?session_id={self.b}"
        ) as other:
            await other.wait_for(lambda m: m.get("type") == "hello")
            running = asyncio.create_task(self.execute(self.a, mine))
            await asyncio.to_thread(self.adapter.entered.wait, 2.0)
            execution_id = self.app.api.running_execution_id
            await self.client.post(
                f"/v1/executions/{execution_id}/cancel", {"session_id": self.a}
            )
            await running
            cancels = [
                m for m in await other.messages(timeout=0.2)
                if m.get("type") == "cancel"
            ]
            self.assertEqual(cancels, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
