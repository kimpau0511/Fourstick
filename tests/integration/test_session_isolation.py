"""다중 세션 격리와 동시성 (md/웹UI_구조.md "세션 격리").

서버는 "현재 계획"을 추정하지 않는다. 모든 작업이 명시적인 식별자
(session_id, request_id, plan_id, plan_hash, approval_id, execution_id)로만
조회·처리된다는 것을 확인한다.

확인 대상 열 가지:

  1. 두 세션이 각자 다른 계획을 만든다
  2. 한 세션의 승인을 다른 세션이 쓰지 못한다
  3. 같은 plan_id에 오래된 plan_hash로 승인하지 못한다
  4. 승인 뒤 계획 내용이 바뀌면 그 승인으로 실행하지 못한다
  5. 두 실행의 WebSocket 이벤트가 섞이지 않는다
  6. 새로고침 복원이 자신의 실행만 돌려준다
  7. 같은 요청의 중복 실행이 경쟁해도 하나만 실행된다
  8. 실행 중 전체 STOP이 세션 격리에 막히지 않는다
  9. 실행 중 특정 execution만 취소된다(전체 정지와 다른 계약)
 10. SQLite 다중 스레드 접근과 트랜잭션 원자성

로봇은 개발용 Fake Adapter다. 실제 로봇 실행이 아니다 — 기록의
`adapter_kind`/`is_simulated`가 이를 보존하는지도 함께 본다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan, TaskStep
from integration.asgi_client import HttpClient, WebSocketSession
from integration.test_web_api import ScriptedProvider, TRANSFER_STEPS, TRANSFER_UTTERANCE
from integration.test_storage_roundtrip import inference
from server.api import ApiError
from server.asgi import Application
from server.config import ServerConfig
from server.runtime import FeatureState, build_runtime
from storage.records import RequestRecord, SessionRecord, SessionStatus
from storage.repository import IntegrityViolation
from storage.sqlite.repository import SqliteRepository


class SlowAdapter:
    """모션 스킬마다 잠깐 머무는 어댑터 감싸개.

    실행 중간 상태(전체 정지·취소·중복 실행 경쟁)를 실제로 만들기 위한 것이다.
    지연 외에는 원래 어댑터 그대로 위임한다.
    """

    def __init__(self, inner, delay: float = 0.08):
        self._inner = inner
        self._delay = delay
        self.entered = threading.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _slow(self, name, *args):
        self.entered.set()
        threading.Event().wait(self._delay)
        return getattr(self._inner, name)(*args)

    def home(self, timeout):
        return self._slow("home", timeout)

    def move(self, to, timeout):
        return self._slow("move", to, timeout)

    def pick(self, obj, source, timeout):
        return self._slow("pick", obj, source, timeout)

    def place(self, obj, target, timeout):
        return self._slow("place", obj, target, timeout)


class IsolationCase(unittest.IsolatedAsyncioTestCase):
    """세션 두 개가 같은 서버(같은 DB, 같은 로봇 하나)를 쓴다."""

    slow_adapter = False

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dataclasses.replace(
            ServerConfig.from_env(),
            db_path=Path(self.tmp.name) / "web.sqlite3",
            enable_stt=False, llm_config_name="__absent__.json",
        )
        self.runtime = build_runtime(self.config)
        self.runtime.provider = ScriptedProvider([{"steps": TRANSFER_STEPS}])
        self.runtime.planning = FeatureState(True, "scripted")
        self.app = Application(runtime=self.runtime, config=self.config)
        self.addCleanup(self.runtime.repository.close)
        self.client = HttpClient(self.app)
        if self.slow_adapter:
            self.adapter = SlowAdapter(self.runtime.adapter())
            self.runtime._adapter = self.adapter
        self.a = await self.new_session("tab-a")
        self.b = await self.new_session("tab-b")

    async def new_session(self, origin: str) -> str:
        response = await self.client.post("/v1/sessions", {"origin": origin})
        return response.json()["session_id"]

    async def plan(self, session_id: str, utterance: str = TRANSFER_UTTERANCE) -> dict:
        return (await self.client.post(
            "/v1/plan", {"session_id": session_id, "utterance": utterance}
        )).json()

    async def decide(self, session_id: str, bundle: dict, decision="approve",
                     *, plan_hash=None):
        return await self.client.post("/v1/decision", {
            "session_id": session_id, "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"],
            "plan_hash": plan_hash or bundle["plan"]["plan_hash"],
            "decision": decision,
        })

    async def execute(self, session_id: str, bundle: dict, **extra):
        return await self.client.post("/v1/execute", {
            "session_id": session_id, "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"], **extra,
        })

    async def approved_plan(self, session_id: str, utterance=TRANSFER_UTTERANCE) -> dict:
        bundle = await self.plan(session_id, utterance)
        approval = (await self.decide(session_id, bundle)).json()
        bundle["approval_id"] = approval["approval_id"]
        return bundle


class TestSeparatePlans(IsolationCase):
    """1. 두 세션이 각자 다른 계획을 만든다 — 하나가 다른 하나를 덮지 않는다."""

    async def test_each_session_keeps_its_own_request_and_plan(self):
        first = await self.plan(self.a, TRANSFER_UTTERANCE)
        second = await self.plan(self.b, "1번 팔레트에서 B자재를 집어서 컨베이어에 올려줘")
        self.assertNotEqual(first["request_id"], second["request_id"])
        self.assertNotEqual(first["plan"]["plan_id"], second["plan"]["plan_id"])

        repo = self.runtime.repository
        self.assertEqual(
            [r.request_id for r in repo.requests_for_session(self.a)],
            [first["request_id"]],
        )
        self.assertEqual(
            [r.request_id for r in repo.requests_for_session(self.b)],
            [second["request_id"]],
        )

    async def test_a_new_request_does_not_overwrite_the_other_session(self):
        first = await self.plan(self.a)
        await self.plan(self.b)
        await self.plan(self.b, "1번 팔레트에서 B자재를 집어서 컨베이어에 올려줘")
        restored = (await self.client.get(f"/v1/state?session_id={self.a}")).json()
        self.assertEqual(restored["request_id"], first["request_id"])
        self.assertEqual(restored["plan"]["plan_id"], first["plan"]["plan_id"])

    async def test_other_sessions_plan_is_not_readable(self):
        mine = await self.plan(self.a)
        response = await self.client.get(
            f"/v1/plan?session_id={self.b}&request_id={mine['request_id']}"
            f"&plan_id={mine['plan']['plan_id']}"
        )
        self.assertEqual(response.status, 404)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.PLAN_UNKNOWN_RESOURCE.value,
        )

    async def test_identifiers_are_required_not_guessed(self):
        """식별자 없이 오는 요청은 거절한다 — '마지막 계획'을 추정하지 않는다."""
        for path, payload in (
            ("/v1/plan", {"utterance": TRANSFER_UTTERANCE}),
            ("/v1/decision", {"session_id": self.a, "decision": "approve"}),
            ("/v1/execute", {"session_id": self.a}),
        ):
            with self.subTest(path=path):
                response = await self.client.post(path, payload)
                self.assertEqual(response.status, 400)
        self.assertEqual((await self.client.get("/v1/state")).status, 400)
        self.assertEqual((await self.client.get("/v1/plan")).status, 400)


class TestCrossSessionApproval(IsolationCase):
    """2. 한 세션의 승인을 다른 세션이 쓰지 못한다."""

    async def test_other_session_cannot_approve_my_plan(self):
        mine = await self.plan(self.a)
        response = await self.decide(self.b, mine)
        self.assertEqual(response.status, 404)
        self.assertEqual(self.runtime.repository.approvals_for_plan(
            mine["plan"]["plan_id"]), ())

    async def test_other_session_cannot_execute_my_approved_plan(self):
        mine = await self.approved_plan(self.a)
        response = await self.execute(self.b, mine)
        self.assertEqual(response.status, 404)
        self.assertEqual(
            self.runtime.repository.executions_for_request(mine["request_id"]), ()
        )

    async def test_approval_of_another_session_is_not_usable(self):
        """소유 확인을 지나도 승인 주체가 다르면 실행에 쓸 수 없다.

        이 검사는 `bundle_for`의 소유 확인과 **다른 층**이다. 승인 기록의
        session_id를 직접 확인한다.
        """
        mine = await self.approved_plan(self.a)
        plan_hash = mine["plan"]["plan_hash"]
        with self.assertRaises(ApiError) as ctx:
            self.app.api._usable_approval(
                session_id=self.b, request_id=mine["request_id"],
                plan_id=mine["plan"]["plan_id"], plan_hash=plan_hash,
                approval_id=None,
            )
        self.assertIs(ctx.exception.reason, ReasonCode.SAFETY_APPROVAL_REQUIRED)
        self.assertIn("다른 세션", ctx.exception.message)

    async def test_approval_id_from_another_session_is_refused(self):
        other = await self.approved_plan(self.a)
        mine = await self.plan(self.b)
        response = await self.execute(
            self.b, mine, approval_id=other["approval_id"]
        )
        self.assertIn(response.status, (404, 409))
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.SAFETY_APPROVAL_REQUIRED.value,
        )

    async def test_history_marks_which_decisions_are_mine(self):
        mine = await self.approved_plan(self.a)
        bundle = (await self.client.get(
            f"/v1/plan?session_id={self.a}&request_id={mine['request_id']}"
            f"&plan_id={mine['plan']['plan_id']}"
        )).json()
        self.assertTrue(all(row["own_session"] for row in bundle["approvals"]))


class TestStalePlanHash(IsolationCase):
    """3. 같은 plan_id에 오래된 plan_hash로 승인하지 못한다."""

    async def test_wrong_hash_is_refused(self):
        bundle = await self.plan(self.a)
        response = await self.decide(self.a, bundle, plan_hash="0" * 64)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_HASH_MISMATCH.value
        )
        self.assertEqual(
            self.runtime.repository.approvals_for_plan(bundle["plan"]["plan_id"]), ()
        )

    async def test_hash_of_a_different_plan_is_refused(self):
        """plan_hash는 내용 기준이다 — 내용이 다른 계획의 해시는 쓸 수 없다."""
        first = await self.plan(self.a)
        self.runtime.provider = ScriptedProvider([
            {"steps": TRANSFER_STEPS[:2] + [{"skill": "home", "args": {}}],
             "terminal_hold": "obj_a"}
        ])
        second = await self.plan(
            self.a, "1번 팔레트에서 A자재를 집어서 들고 있어"
        )
        self.assertNotEqual(
            first["plan"]["plan_hash"], second["plan"]["plan_hash"]
        )
        response = await self.decide(
            self.a, first, plan_hash=second["plan"]["plan_hash"]
        )
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_HASH_MISMATCH.value
        )

    async def test_missing_hash_is_refused(self):
        bundle = await self.plan(self.a)
        response = await self.client.post("/v1/decision", {
            "session_id": self.a, "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"], "decision": "approve",
        })
        self.assertEqual(response.status, 400)


class TestPlanTamperedAfterApproval(IsolationCase):
    """4. 승인 뒤 계획 내용이 바뀌면 그 승인은 쓸 수 없다."""

    def _tamper(self, plan_id: str, *, keep_hash_consistent: bool) -> None:
        """저장된 계획 본문을 바꾼다(시험용 직접 조작).

        `keep_hash_consistent=True`면 plan_hash 열까지 다시 계산해 넣는다 —
        저장소 자체로는 어긋남을 알 수 없는 상태를 만들어, 승인 기록의
        plan_hash 대조가 실제로 동작하는지 본다.
        """
        repo = self.runtime.repository
        stored = repo.get_plan(plan_id)
        row = repo._conn.execute(
            "SELECT plan_json FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        raw = json.loads(row["plan_json"])
        raw["steps"][1]["args"]["object"] = "obj_b"   # 다른 물체로 바꿔치기
        plan_json = json.dumps(raw, ensure_ascii=False)
        if keep_hash_consistent:
            # 해시는 계약 함수로 다시 계산한다(시험 코드가 규칙을 복제하지 않는다).
            new_plan = TaskPlan(
                plan_id=raw["plan_id"], robot_id=raw["robot_id"],
                profile_id=raw["profile_id"],
                profile_version=raw["profile_version"],
                steps=tuple(TaskStep(s["skill"], s.get("args", {}))
                            for s in raw["steps"]),
                created_at=raw["created_at"], ttl_sec=raw["ttl_sec"],
                schema_version=raw["schema_version"],
                utterance=raw.get("utterance", ""),
                terminal_hold=raw.get("terminal_hold"),
            )
            new_hash = new_plan.plan_hash()
        else:
            new_hash = stored.plan_hash            # 내용과 어긋난 채로 둔다
        with repo._tx() as conn:
            conn.execute(
                "UPDATE plans SET plan_json = ?, plan_hash = ? WHERE plan_id = ?",
                (plan_json, new_hash, plan_id),
            )

    async def test_stored_hash_that_no_longer_matches_content_blocks_everything(self):
        mine = await self.approved_plan(self.a)
        self._tamper(mine["plan"]["plan_id"], keep_hash_consistent=False)
        response = await self.execute(self.a, mine)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_HASH_MISMATCH.value
        )

    async def test_approval_does_not_carry_over_to_changed_content(self):
        mine = await self.approved_plan(self.a)
        self._tamper(mine["plan"]["plan_id"], keep_hash_consistent=True)
        response = await self.execute(self.a, mine)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_HASH_MISMATCH.value
        )
        self.assertEqual(
            self.runtime.repository.executions_for_request(mine["request_id"]), ()
        )

    async def test_reapproval_is_needed_and_is_a_new_record(self):
        mine = await self.approved_plan(self.a)
        self._tamper(mine["plan"]["plan_id"], keep_hash_consistent=True)
        fresh = (await self.client.get(
            f"/v1/plan?session_id={self.a}&request_id={mine['request_id']}"
            f"&plan_id={mine['plan']['plan_id']}"
        )).json()
        self.assertNotEqual(fresh["plan"]["plan_hash"], mine["plan"]["plan_hash"])
        again = (await self.decide(self.a, fresh)).json()
        self.assertTrue(again["ok"])
        history = self.runtime.repository.approvals_for_plan(mine["plan"]["plan_id"])
        self.assertEqual(len(history), 2)          # append-only
        self.assertEqual(history[0].plan_hash, mine["plan"]["plan_hash"])
        self.assertEqual(history[1].plan_hash, fresh["plan"]["plan_hash"])


class TestEventsDoNotMix(IsolationCase):
    """5. 두 실행의 WebSocket 이벤트가 섞이지 않는다."""

    async def test_execution_events_go_only_to_the_owning_session(self):
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.a}") as sa, \
                   WebSocketSession(self.app, f"/v1/events?session_id={self.b}") as sb:
            await sa.wait_for(lambda m: m.get("type") == "hello")
            await sb.wait_for(lambda m: m.get("type") == "hello")

            mine = await self.approved_plan(self.a)
            final = (await self.execute(self.a, mine)).json()
            started = await sa.wait_for(lambda m: m.get("type") == "execution_started")
            self.assertEqual(
                started["payload"]["execution_id"], final["execution_id"]
            )
            await sa.wait_for(lambda m: m.get("type") == "execution_final")

            others = [
                m for m in await sb.messages(timeout=0.2)
                if m.get("type") in ("execution_started", "step", "state",
                                     "execution_final", "approval")
            ]
            self.assertEqual(others, [])

    async def test_each_event_names_its_session_and_execution(self):
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.a}") as sa:
            await sa.wait_for(lambda m: m.get("type") == "hello")
            mine = await self.approved_plan(self.a)
            result = (await self.execute(self.a, mine)).json()
            steps = [m for m in await sa.messages(timeout=0.3)
                     if m.get("type") == "step"]
            self.assertTrue(steps)
            for message in steps:
                self.assertEqual(message["session_id"], self.a)
                self.assertEqual(message["execution_id"], result["execution_id"])

    async def test_global_stop_reaches_every_subscriber(self):
        """전체 정지는 로봇 하나를 공유하는 모든 세션에 간다."""
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.a}") as sa, \
                   WebSocketSession(self.app, f"/v1/events?session_id={self.b}") as sb:
            await sa.wait_for(lambda m: m.get("type") == "hello")
            await sb.wait_for(lambda m: m.get("type") == "hello")
            await self.client.post("/v1/stop", {"session_id": self.a})
            for socket in (sa, sb):
                event = await socket.wait_for(lambda m: m.get("type") == "stop")
                self.assertEqual(event["scope"], "global")

    async def test_socket_without_a_session_is_refused(self):
        async with WebSocketSession(self.app, "/v1/events") as socket:
            message = await socket.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(
                message["reason_code"], ReasonCode.CONFIG_MISSING.value
            )
            await asyncio.sleep(0.05)
            self.assertTrue(socket.closed)

    async def test_socket_of_an_ended_session_is_refused(self):
        await self.client.request("DELETE", f"/v1/sessions/{self.b}")
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.b}") as socket:
            message = await socket.wait_for(lambda m: m.get("type") == "error")
            self.assertEqual(message["reason_code"], ReasonCode.PLAN_EXPIRED.value)


class TestRefreshRestoresOwnWork(IsolationCase):
    """6. 새로고침 복원이 자신의 실행만 돌려준다."""

    async def test_state_lists_only_my_executions(self):
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        theirs = await self.plan(self.b)

        restored_a = (await self.client.get(f"/v1/state?session_id={self.a}")).json()
        restored_b = (await self.client.get(f"/v1/state?session_id={self.b}")).json()
        self.assertEqual(
            [e["execution_id"] for e in restored_a["executions"]],
            [result["execution_id"]],
        )
        self.assertEqual(restored_b["executions"], [])
        self.assertEqual(restored_b["plan"]["plan_id"], theirs["plan"]["plan_id"])

    async def test_execution_status_is_queried_by_execution_id(self):
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        own = await self.client.get(
            f"/v1/executions/{result['execution_id']}?session_id={self.a}"
        )
        self.assertEqual(own.status, 200)
        self.assertEqual(own.json()["execution_id"], result["execution_id"])
        other = await self.client.get(
            f"/v1/executions/{result['execution_id']}?session_id={self.b}"
        )
        self.assertEqual(other.status, 404)

    async def test_ended_session_is_not_restored_as_current(self):
        mine = await self.approved_plan(self.a)
        await self.execute(self.a, mine)
        await self.client.request("DELETE", f"/v1/sessions/{self.a}")
        response = await self.client.get(f"/v1/state?session_id={self.a}")
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_EXPIRED.value
        )

    async def test_idle_expired_session_is_not_restored_as_current(self):
        mine = await self.plan(self.a)
        api = self.app.api
        # 유휴 한계를 넘긴 시점으로 시계를 옮긴다(실제 시간을 기다리지 않는다).
        later = api.now() + self.config.session_idle_timeout_sec + 1
        api.now = lambda: later
        response = await self.client.get(f"/v1/state?session_id={self.a}")
        self.assertEqual(response.status, 409)
        record = self.runtime.repository.get_session(self.a)
        self.assertIs(record.status, SessionStatus.EXPIRED)
        # 계획 기록 자체는 남는다 — 복원 대상만 아니다.
        self.assertTrue(self.runtime.repository.get_plan(mine["plan"]["plan_id"]))

    async def test_a_session_id_the_browser_invents_gets_nothing(self):
        """브라우저가 보낸 값을 그대로 믿지 않는다."""
        response = await self.client.get("/v1/state?session_id=sess_made_up")
        self.assertEqual(response.status, 404)


class TestDuplicateExecutionRace(IsolationCase):
    """7. 같은 요청의 중복 실행 경쟁 — 로봇은 하나다."""

    slow_adapter = True

    async def test_only_one_execution_starts(self):
        mine = await self.approved_plan(self.a)
        first, second = await asyncio.gather(
            self.execute(self.a, mine), self.execute(self.a, mine)
        )
        statuses = sorted([first.status, second.status])
        self.assertEqual(statuses, [200, 409])
        refused = first if first.status == 409 else second
        self.assertEqual(
            refused.json()["reason_code"], ReasonCode.EXEC_GOAL_REJECTED.value
        )
        rows = self.runtime.repository.executions_for_request(mine["request_id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attempt_no, 1)

    async def test_two_sessions_cannot_run_at_the_same_time(self):
        mine = await self.approved_plan(self.a)
        theirs = await self.approved_plan(self.b)
        first, second = await asyncio.gather(
            self.execute(self.a, mine), self.execute(self.b, theirs)
        )
        self.assertEqual(sorted([first.status, second.status]), [200, 409])
        executions = [
            *self.runtime.repository.executions_for_session(self.a),
            *self.runtime.repository.executions_for_session(self.b),
        ]
        self.assertEqual(len(executions), 1)

    async def test_retry_gets_a_new_execution_id_and_attempt_no(self):
        mine = await self.approved_plan(self.a)
        first = (await self.execute(self.a, mine)).json()
        second = (await self.execute(self.a, mine)).json()
        self.assertNotEqual(first["execution_id"], second["execution_id"])
        self.assertEqual([first["attempt_no"], second["attempt_no"]], [1, 2])
        # 기존 실행 기록은 그대로다.
        record = self.runtime.repository.get_execution(first["execution_id"])
        self.assertEqual(record.attempt_no, 1)


class TestGlobalStopDuringExecution(IsolationCase):
    """8. 실행 중 전체 STOP. 세션 격리 때문에 막히지 않는다."""

    slow_adapter = True

    async def test_stop_without_any_session_still_works(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        stop = (await self.client.post("/v1/stop", {})).json()
        self.assertEqual(stop["scope"], "global")
        result = (await running).json()
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["final"]["reason_code"], ReasonCode.EXEC_STOPPED.value
        )
        self.assertFalse(result["final"]["task_succeeded"])

    async def test_stop_from_another_session_stops_the_running_execution(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        stop = (await self.client.post("/v1/stop", {"session_id": self.b})).json()
        self.assertEqual(stop["scope"], "global")
        # 영향받은 실행은 다른 세션의 것이므로 **건수만** 알려준다.
        self.assertEqual(stop["affected_execution_count"], 1)
        self.assertEqual(stop["your_execution_ids"], [])
        result = (await running).json()
        self.assertEqual(
            result["final"]["reason_code"], ReasonCode.EXEC_STOPPED.value
        )

    async def test_stop_of_an_expired_session_is_not_blocked(self):
        api = self.app.api
        later = api.now() + self.config.session_idle_timeout_sec + 1
        api.now = lambda: later
        await self.client.get(f"/v1/state?session_id={self.b}")   # 만료 처리
        self.assertIs(
            self.runtime.repository.get_session(self.b).status,
            SessionStatus.EXPIRED,
        )
        stop = (await self.client.post("/v1/stop", {"session_id": self.b})).json()
        self.assertEqual(stop["scope"], "global")
        self.assertIn("confirmed", stop)

    async def test_stopped_execution_is_recorded_as_stopped_not_completed(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        await self.client.post("/v1/stop", {})
        result = (await running).json()
        trace = self.runtime.repository.trace(result["execution_id"])
        states = [t.to_state.value for t in trace.transitions]
        self.assertIn("stopping", states)
        self.assertEqual(states[-1], "stopped")
        self.assertNotIn("completed", states)


class TestPerExecutionCancel(IsolationCase):
    """9. 특정 실행 취소는 전체 정지와 다른 계약이다."""

    slow_adapter = True

    async def test_cancel_stops_only_that_execution(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        execution_id = self.app.api.running_execution_id
        self.assertIsNotNone(execution_id)
        cancel = (await self.client.post(
            f"/v1/executions/{execution_id}/cancel", {"session_id": self.a}
        )).json()
        self.assertEqual(cancel["scope"], "execution")
        self.assertTrue(cancel["was_running"])
        result = (await running).json()
        self.assertEqual(
            result["final"]["reason_code"], ReasonCode.EXEC_CANCELED.value
        )
        # 전체 정지 요청은 남지 않는다 — 다음 실행이 막히지 않는다.
        follow_up = (await self.execute(self.a, mine)).json()
        self.assertTrue(follow_up["ok"])
        self.assertEqual(follow_up["attempt_no"], 2)

    async def test_another_session_cannot_cancel_my_execution(self):
        mine = await self.approved_plan(self.a)
        running = asyncio.create_task(self.execute(self.a, mine))
        await asyncio.to_thread(self.adapter.entered.wait, 2.0)
        execution_id = self.app.api.running_execution_id
        response = await self.client.post(
            f"/v1/executions/{execution_id}/cancel", {"session_id": self.b}
        )
        self.assertEqual(response.status, 404)
        result = (await running).json()
        self.assertTrue(result["ok"])          # 계속 진행해 끝났다

    async def test_cancel_of_a_finished_execution_is_recorded_not_faked(self):
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        cancel = (await self.client.post(
            f"/v1/executions/{result['execution_id']}/cancel",
            {"session_id": self.a},
        )).json()
        self.assertFalse(cancel["was_running"])
        self.assertIn("이미 끝난 실행", cancel["detail"])


class TestFakeExecutionIsLabeled(IsolationCase):
    """개발용 Fake 실행을 실제 로봇 실행으로 집계하지 않는다."""

    async def test_execution_record_keeps_the_adapter_kind(self):
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        record = self.runtime.repository.get_execution(result["execution_id"])
        self.assertEqual(record.adapter_kind, "fake")
        self.assertTrue(record.is_simulated)
        self.assertEqual(result["adapter_kind"], "fake")
        self.assertTrue(result["is_simulated"])

    async def test_state_and_status_report_it_too(self):
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        status = (await self.client.get(
            f"/v1/executions/{result['execution_id']}?session_id={self.a}"
        )).json()
        self.assertTrue(status["is_simulated"])
        restored = (await self.client.get(f"/v1/state?session_id={self.a}")).json()
        self.assertTrue(restored["executions"][0]["is_simulated"])

    async def test_completed_fake_run_is_still_marked_simulated(self):
        """completed라도 실제 로봇 실행이 아니다."""
        mine = await self.approved_plan(self.a)
        result = (await self.execute(self.a, mine)).json()
        self.assertTrue(result["final"]["task_succeeded"])
        self.assertEqual(result["final"]["state"], "completed")
        self.assertTrue(result["is_simulated"])

    async def test_counts_are_split_by_execution_environment(self):
        """세는 지점에서부터 나눈다 — Fake 실행이 실제 실행 수에 섞이지 않는다."""
        mine = await self.approved_plan(self.a)
        await self.execute(self.a, mine)
        counts = (await self.client.get("/health")).json()["executions"]
        self.assertEqual(counts, {"simulated": 1, "real": 0, "unknown": 0})
        self.assertEqual(
            self.runtime.repository.execution_environment_counts()["real"], 0
        )

    async def test_config_and_health_declare_the_simulator(self):
        config = (await self.client.get("/v1/config")).json()
        self.assertTrue(config["robot"]["is_simulated"])
        self.assertFalse(config["robot"]["configured"])
        health = (await self.client.get("/health")).json()
        self.assertTrue(health["robot"]["is_simulated"])


class TestSqliteConcurrency(unittest.TestCase):
    """10. SQLite 다중 스레드 접근과 트랜잭션 원자성."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = SqliteRepository(
            str(Path(self.tmp.name) / "concurrent.sqlite3"), now=0.0
        )
        self.addCleanup(self.repo.close)

    def _session(self, index: int) -> str:
        session_id = f"sess_c{index}"
        self.repo.create_session(SessionRecord(
            session_id=session_id, created_at=1.0, last_seen_at=1.0,
            status=SessionStatus.ACTIVE,
            schema_version=TASK_PLAN_SCHEMA_VERSION, origin="test",
        ))
        return session_id

    def test_threadsafety_is_serialized_mode(self):
        """모듈이 직렬화 모드여야 연결 하나를 여러 스레드가 쓸 수 있다."""
        self.assertEqual(sqlite3.threadsafety, 3)

    def test_many_threads_write_requests_without_loss_or_corruption(self):
        sessions = [self._session(i) for i in range(4)]
        errors: list[Exception] = []

        def write(index: int) -> None:
            try:
                self.repo.save_request(RequestRecord(
                    request_id=f"req_{index}",
                    utterance=f"요청 {index}",
                    schema_version=TASK_PLAN_SCHEMA_VERSION,
                    created_at=float(index),
                    session_id=sessions[index % len(sessions)],
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(80)))
        self.assertEqual(errors, [])
        total = sum(
            len(self.repo.requests_for_session(s)) for s in sessions
        )
        self.assertEqual(total, 80)
        for index, session_id in enumerate(sessions):
            rows = self.repo.requests_for_session(session_id)
            self.assertTrue(all(r.session_id == session_id for r in rows))
        self.assertEqual(
            self.repo._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
        )

    def test_concurrent_readers_and_writers_see_consistent_rows(self):
        session_id = self._session(0)
        seen: list[int] = []

        def write(index: int) -> None:
            self.repo.save_request(RequestRecord(
                request_id=f"req_rw_{index}", utterance=f"요청 {index}",
                schema_version=TASK_PLAN_SCHEMA_VERSION,
                created_at=float(index), session_id=session_id,
            ))

        def read(_: int) -> None:
            rows = self.repo.requests_for_session(session_id)
            seen.append(len(rows))
            for row in rows:                       # 읽은 행이 온전해야 한다
                self.assertTrue(row.request_id.startswith("req_rw_"))
                self.assertEqual(row.session_id, session_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write, i) for i in range(40)]
            futures += [pool.submit(read, i) for i in range(40)]
            for future in futures:
                future.result()
        self.assertEqual(len(self.repo.requests_for_session(session_id)), 40)
        self.assertTrue(seen)

    def test_a_failing_transaction_leaves_no_partial_rows(self):
        """두 표에 함께 넣는 트랜잭션이 실패하면 아무것도 남지 않는다."""
        session_id = self._session(0)
        self.repo.save_request(RequestRecord(
            request_id="req_first", utterance="먼저 저장된 요청",
            schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=1.0,
            session_id=session_id,
        ))
        record = inference(
            stt_inference_id="stt_dup", session_id=session_id,
            request_id="req_first", final_adopted=True,
        )
        self.repo.append_stt_inference(record)     # 같은 id를 먼저 차지한다

        with self.assertRaises(IntegrityViolation):
            self.repo.save_request_with_stt_inference(
                RequestRecord(
                    request_id="req_partial", utterance="부분 저장 시험",
                    schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=5.0,
                    selected_stt_inference_id="stt_dup", session_id=session_id,
                ),
                dataclasses.replace(record, request_id="req_partial"),
            )
        rows = self.repo._conn.execute(
            "SELECT COUNT(*) FROM requests WHERE request_id = ?", ("req_partial",)
        ).fetchone()[0]
        self.assertEqual(rows, 0)                  # 요청만 남지 않았다
        self.assertEqual(
            len(self.repo.stt_inferences_for_session(session_id)), 1
        )

    def test_concurrent_approvals_are_all_kept_append_only(self):
        """같은 계획에 여러 스레드가 결정을 남겨도 모두 보존된다."""
        from core.task_plan import TaskPlan, TaskStep
        from storage.records import ApprovalDecision, ApprovalRecord

        session_id = self._session(0)
        self.repo.save_request(RequestRecord(
            request_id="req_apv", utterance="승인 경쟁",
            schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=1.0,
            session_id=session_id,
        ))
        plan = TaskPlan(
            "plan_apv", "fake_1", "fixture", "1.0",
            (TaskStep("home"),), created_at=1.0, ttl_sec=600.0,
        )
        self.repo.save_plan("req_apv", plan, 1.0)

        def approve(index: int) -> None:
            self.repo.append_approval(ApprovalRecord(
                approval_id=f"apv_{index}", plan_id="plan_apv",
                plan_hash=plan.plan_hash(), request_id="req_apv",
                decision=(ApprovalDecision.APPROVED if index % 2
                          else ApprovalDecision.REJECTED),
                decided_at=float(index),
                schema_version=TASK_PLAN_SCHEMA_VERSION,
                session_id=session_id,
            ))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(approve, range(30)))
        history = self.repo.approvals_for_plan("plan_apv")
        self.assertEqual(len(history), 30)
        self.assertEqual(
            [h.decided_at for h in history], sorted(h.decided_at for h in history)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
