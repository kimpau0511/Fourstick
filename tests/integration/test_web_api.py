"""웹 API 통합 검증 (md/개발플랜.md 7단계).

실제 LLM 서버를 쓰지 않는다 — 계획 공급자는 test double이다. 확인 대상은
**흐름과 저장**이다.

- 계획 생성이 실행하지 않는다
- 승인 없이 실행되지 않는다
- 요청↔계획 리소스 불일치가 실행 전에 차단된다
- 승인 기록이 append-only다
- STOP이 계획 경로를 거치지 않고, 미확인을 성공으로 표시하지 않는다
- 새로고침 복원(`/v1/state`)이 저장소에서 읽는다
- 기능이 없으면 사용 불가로 표시된다(Mock 성공을 만들지 않는다)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.constants import TASK_PLAN_SCHEMA_VERSION
from integration.asgi_client import HttpClient, WebSocketSession
from core.reason_codes import ReasonCode
from planning.output_parser import ClarificationNeeded, draft_from_output
from planning.plan_provider import PlanningMode, PlanProviderError
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION
from server.asgi import Application
from server.config import ServerConfig
from server.runtime import FeatureState, build_runtime
from storage.records import ApprovalDecision

TRANSFER_STEPS = [
    {"skill": "move", "args": {"to": "loc_pallet_1"}},
    {"skill": "pick", "args": {"object": "obj_a", "from": "loc_pallet_1"}},
    {"skill": "move", "args": {"to": "loc_conveyor"}},
    {"skill": "place", "args": {"object": "obj_a", "to": "loc_conveyor"}},
    {"skill": "home", "args": {}},
]
TRANSFER_UTTERANCE = "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"


class ScriptedProvider:
    """정해진 출력을 돌려주는 공급자. 실제 모델을 쓰지 않는다."""

    provider_id = "scripted"
    model_name = "scripted-model"
    is_mock = False
    prompt_template_version = PROMPT_TEMPLATE_VERSION
    output_schema_version = OUTPUT_SCHEMA_VERSION

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def generate(self, context):
        value = self._outputs[min(self.calls, len(self._outputs) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return draft_from_output(
            {
                "output_schema_version": OUTPUT_SCHEMA_VERSION,
                "result": "plan",
                "steps": value.get("steps", TRANSFER_STEPS),
                "terminal_hold": value.get("terminal_hold"),
            },
            provider_id=self.provider_id, model_id=self.model_name,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            mode=PlanningMode.JSON_SCHEMA, latency_sec=0.01,
        )


class WebCase(unittest.IsolatedAsyncioTestCase):
    """각 테스트가 새 DB와 새 앱을 쓴다."""

    provider_outputs: list = [{"steps": TRANSFER_STEPS}]
    planning_available = True

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = dataclasses.replace(
            ServerConfig.from_env(),
            db_path=Path(self.tmp.name) / "web.sqlite3",
            enable_stt=False,
            # 존재하지 않는 이름을 줘서 실제 모델 서버를 부르지 않게 한다.
            llm_config_name="__absent__.json",
        )
        runtime = build_runtime(config)
        self.provider = ScriptedProvider(self.provider_outputs)
        if self.planning_available:
            runtime.provider = self.provider
            runtime.planning = FeatureState(True, "scripted")
        self.app = Application(runtime=runtime, config=config)
        self.addCleanup(runtime.repository.close)
        self.client = HttpClient(self.app)
        # 세션은 서버가 만든다. 모든 요청이 명시적으로 session_id를 보낸다.
        self.session = (await self.client.post(
            "/v1/sessions", {"origin": "test"}
        )).json()["session_id"]

    async def plan(self, utterance=TRANSFER_UTTERANCE, session_id=None):
        return await self.client.post("/v1/plan", {
            "session_id": session_id or self.session, "utterance": utterance,
        })

    async def decide(self, plan_id, decision="approve", *, session_id=None,
                     request_id=None, plan_hash=None, bundle=None):
        """승인·거부는 session_id·request_id·plan_id·plan_hash를 모두 명시한다."""
        if bundle is not None:
            request_id = bundle["request_id"]
            plan_hash = bundle["plan"]["plan_hash"]
        return await self.client.post("/v1/decision", {
            "session_id": session_id or self.session,
            "request_id": request_id, "plan_id": plan_id,
            "plan_hash": plan_hash, "decision": decision,
        })

    async def run_execute(self, bundle, *, session_id=None):
        return await self.client.post("/v1/execute", {
            "session_id": session_id or self.session,
            "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"],
        })


class TestStaticAndConfig(WebCase):
    async def test_index_is_served(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Robot Command Console", response.text)
        self.assertIn("/static/js/main.js", response.text)

    async def test_assets_are_served_with_types(self):
        for path, fragment in (
            ("/static/js/main.js", "javascript"),
            ("/static/app.css", "css"),
            ("/static/pcm-worklet.js", "javascript"),
            ("/static/icon.svg", "svg"),
        ):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn(fragment, response.headers["content-type"])

    async def test_directory_escape_is_refused(self):
        response = await self.client.get("/static/../server/api.py")
        self.assertIn(response.status, (403, 404))

    async def test_unknown_path_is_404_json(self):
        response = await self.client.get("/nope")
        self.assertEqual(response.status, 404)
        self.assertIn("reason_code", response.json())

    async def test_config_never_exposes_the_model_address_or_credentials(self):
        payload = (await self.client.get("/v1/config")).json()
        blob = json.dumps(payload, ensure_ascii=False)
        for banned in ("localhost:8000", "http://", "api_key", "Authorization",
                       "bearer"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned.lower(), blob.lower())

    async def test_config_reports_policies_and_catalogs(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertEqual(payload["schema_version"], TASK_PLAN_SCHEMA_VERSION)
        self.assertTrue(payload["policies"]["safety"]["policy_version"])
        self.assertEqual(payload["policies"]["safety"]["required_final_skill"], "home")
        self.assertTrue(payload["catalogs"]["locations"])
        self.assertTrue(payload["catalogs"]["skills"])

    async def test_robot_without_a_real_profile_is_reported_unconfigured(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertFalse(payload["robot"]["configured"])
        self.assertEqual(payload["robot"]["kind"], "fake")
        # 실제 로봇 수치를 만들어 내지 않는다.
        self.assertIsNotNone(payload["robot"]["profile_id"])

    async def test_health_reports_feature_availability(self):
        payload = (await self.client.get("/health")).json()
        self.assertTrue(payload["db"]["available"])
        self.assertFalse(payload["features"]["stt"]["available"])
        self.assertTrue(payload["features"]["stt"]["detail"])


class TestUnderspecifiedPlaceIsAsked(WebCase):
    """놓기 요청인데 물체가 없으면, 모델이 이동 계획을 내도 ASK로 되돌린다(8-15).

    dev_098 "1번 팔레트에 내려놔" 유형. 결정론적 게이트가 false-PASS를 막는다.
    개발셋 근거만 쓴다.
    """

    # 모델이 놓기 요청을 단순 이동 계획으로 바꿔 낸 상황을 재현한다.
    provider_outputs = [{"steps": [{"skill": "move", "args": {"to": "loc_pallet_1"}},
                                   {"skill": "home", "args": {}}]}]

    async def test_place_without_object_becomes_ask_not_pass(self):
        payload = (await self.plan(utterance="1번 팔레트에 내려놔")).json()
        self.assertTrue(payload["ok"])            # 계획 자체는 생성됨(이동 계획)
        self.assertFalse(payload["executable"])   # 그러나 실행 가능이 아니다
        self.assertEqual(payload["validation"]["decision"], "ask")
        self.assertEqual(payload["validation"]["reason_code"],
                         ReasonCode.PLAN_CLARIFICATION_REQUIRED.value)

    async def test_move_request_is_not_affected(self):
        payload = (await self.plan(utterance="1번 팔레트로 이동해줘")).json()
        self.assertTrue(payload["executable"])
        self.assertEqual(payload["validation"]["decision"], "allow")


class TestPlanFlow(WebCase):
    async def test_plan_creates_records_but_does_not_execute(self):
        response = await self.plan()
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["plan"]["steps"]), 5)
        self.assertTrue(payload["executable"])
        self.assertEqual(payload["safety"]["decision"], "allow")
        self.assertEqual(payload["consistency"]["status"], "consistent")
        # 실행 기록이 없다 — 계획 생성이 실행하지 않는다.
        repo = self.app.runtime.repository
        self.assertEqual(repo.executions_for_request(payload["request_id"]), ())

    async def test_plan_stores_request_plan_and_attempt(self):
        payload = (await self.plan()).json()
        repo = self.app.runtime.repository
        request = repo.get_request(payload["request_id"])
        self.assertEqual(request.utterance, TRANSFER_UTTERANCE)
        plan = repo.get_plan(payload["plan"]["plan_id"])
        self.assertEqual(plan.plan_hash, payload["plan"]["plan_hash"])
        attempts = repo.planning_attempts_for_request(payload["request_id"])
        self.assertTrue(attempts)
        self.assertEqual(attempts[-1].planning_attempt_id,
                         payload["planning_attempt_id"])

    async def test_validation_is_stored_with_the_consistency_rule(self):
        payload = (await self.plan()).json()
        repo = self.app.runtime.repository
        records = repo.validations_for_plan(payload["plan"]["plan_id"])
        self.assertTrue(records)
        codes = {r.rule_code for r in records[-1].rule_results}
        self.assertIn("E-REQ-001", codes)
        self.assertIn("E-SEQ-002", codes)

    async def test_empty_utterance_is_refused(self):
        response = await self.client.post(
            "/v1/plan", {"session_id": self.session, "utterance": "  "}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_SLOT_INCOMPLETE.value
        )


class TestApprovalRecordsWhatTheUserSaw(WebCase):
    """승인 기록에 화면 값(plan_hash·로봇·Profile 버전)이 남는다 (6-06 / 7-06).

    승인자 계정은 없다 — 인증이 없는 단계이므로 세션까지만 기록한다.
    """

    async def approve_with(self, bundle, **over):
        body = {
            "session_id": self.session, "request_id": bundle["request_id"],
            "plan_id": bundle["plan"]["plan_id"],
            "plan_hash": bundle["plan"]["plan_hash"],
            "robot_id": bundle["plan"]["robot_id"],
            "profile_id": bundle["plan"]["profile_id"],
            "profile_version": bundle["plan"]["profile_version"],
            "decision": "approve",
        }
        body.update(over)
        return await self.client.post("/v1/decision", body)

    async def test_approval_stores_robot_and_profile_version(self):
        bundle = (await self.plan()).json()
        payload = (await self.approve_with(bundle)).json()
        self.assertEqual(payload["robot_id"], bundle["plan"]["robot_id"])
        self.assertEqual(payload["profile_version"], bundle["plan"]["profile_version"])
        self.assertEqual(payload["verified_against_view"], {
            "plan_hash": True, "robot_id": True,
            "profile_id": True, "profile_version": True,
        })
        record = self.app.runtime.repository.get_approval(payload["approval_id"])
        self.assertEqual(record.robot_id, bundle["plan"]["robot_id"])
        self.assertEqual(record.profile_id, bundle["plan"]["profile_id"])
        self.assertEqual(record.profile_version, bundle["plan"]["profile_version"])
        self.assertIsNotNone(record.session_id)
        self.assertTrue(record.decided_at > 0)

    async def test_profile_version_from_another_screen_is_refused(self):
        bundle = (await self.plan()).json()
        response = await self.approve_with(bundle, profile_version="9.9")
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.ROBOT_PROFILE_MISMATCH.value
        )
        self.assertEqual(
            self.app.runtime.repository.approvals_for_plan(
                bundle["plan"]["plan_id"]), ()
        )

    async def test_robot_from_another_screen_is_refused(self):
        bundle = (await self.plan()).json()
        response = await self.approve_with(bundle, robot_id="other_robot")
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.ROBOT_PROFILE_MISMATCH.value
        )

    async def test_approval_without_view_values_says_it_was_not_compared(self):
        """값을 안 보낸 경우를 '대조했다'로 기록하지 않는다."""
        bundle = (await self.plan()).json()
        payload = (await self.decide(
            bundle["plan"]["plan_id"], bundle=bundle
        )).json()
        self.assertEqual(payload["verified_against_view"], {
            "plan_hash": True, "robot_id": False,
            "profile_id": False, "profile_version": False,
        })
        # 기록에는 계획의 값이 들어간다(추정이 아니라 계획에서 복제).
        self.assertEqual(payload["profile_id"], bundle["plan"]["profile_id"])

    async def test_history_shows_profile_of_each_decision(self):
        bundle = (await self.plan()).json()
        await self.approve_with(bundle, decision="reject")
        payload = (await self.approve_with(bundle)).json()
        self.assertEqual(len(payload["history"]), 2)
        for row in payload["history"]:
            self.assertEqual(row["profile_version"], bundle["plan"]["profile_version"])


class TestMismatchIsBlocked(WebCase):
    #: "3번 팔레트"는 카탈로그에 없다. 모델이 1번으로 치환한 계획을 돌려준다.
    provider_outputs = [{"steps": TRANSFER_STEPS}]

    async def test_substituted_resource_blocks_execution(self):
        payload = (await self.plan("3번 팔레트에서 A자재를 집어줘")).json()
        self.assertTrue(payload["ok"])            # 계획은 만들어졌다
        self.assertFalse(payload["executable"])   # 실행은 막힌다
        self.assertEqual(payload["consistency"]["status"], "mismatch")
        self.assertEqual(
            payload["consistency"]["reason_code"],
            ReasonCode.PLAN_RESOURCE_MISMATCH.value,
        )
        self.assertIn("loc_pallet_1", payload["consistency"]["only_in_plan"])

    async def test_execute_is_refused_even_after_approval(self):
        payload = (await self.plan("3번 팔레트에서 A자재를 집어줘")).json()
        approved = (await self.decide(
            payload["plan"]["plan_id"], bundle=payload
        )).json()
        self.assertTrue(approved["ok"])
        response = await self.run_execute(payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_RESOURCE_MISMATCH.value
        )
        repo = self.app.runtime.repository
        self.assertEqual(repo.executions_for_request(payload["request_id"]), ())

    async def test_ui_can_show_the_difference(self):
        payload = (await self.plan("3번 팔레트에서 A자재를 집어줘")).json()
        consistency = payload["consistency"]
        surfaces = [r["surface"] for r in consistency["request_resources"]]
        self.assertIn("a자재", surfaces)
        self.assertTrue(consistency["detail"])
        self.assertTrue(consistency["plan_resources"])


class TestApprovalAndExecution(WebCase):
    async def test_execute_without_approval_is_refused(self):
        payload = (await self.plan()).json()
        response = await self.run_execute(payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.SAFETY_APPROVAL_REQUIRED.value
        )

    async def test_rejected_plan_cannot_execute(self):
        payload = (await self.plan()).json()
        await self.decide(payload["plan"]["plan_id"], "reject", bundle=payload)
        response = await self.run_execute(payload)
        self.assertEqual(response.status, 409)

    async def test_approval_history_is_append_only(self):
        payload = (await self.plan()).json()
        plan_id = payload["plan"]["plan_id"]
        first = (await self.decide(plan_id, "reject", bundle=payload)).json()
        second = (await self.decide(plan_id, "approve", bundle=payload)).json()
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(len(second["history"]), 2)
        repo = self.app.runtime.repository
        stored = repo.approvals_for_plan(plan_id)
        self.assertEqual([a.decision for a in stored],
                         [ApprovalDecision.REJECTED, ApprovalDecision.APPROVED])
        # 첫 결정이 남아 있다 — 재승인이 덮지 않는다.
        self.assertEqual(stored[0].approval_id, first["approval_id"])

    async def test_approved_plan_executes_and_records_everything(self):
        payload = (await self.plan()).json()
        await self.decide(payload["plan"]["plan_id"], bundle=payload)
        response = await self.run_execute(payload)
        self.assertEqual(response.status, 200)
        result = response.json()
        self.assertTrue(result["ok"])
        self.assertTrue(result["granted"])
        self.assertEqual(len(result["steps"]), 5)
        self.assertTrue(result["final"]["task_succeeded"])
        self.assertEqual(result["final"]["state"], "completed")

        repo = self.app.runtime.repository
        executions = repo.executions_for_request(payload["request_id"])
        self.assertEqual(len(executions), 1)
        trace = repo.trace(executions[0].execution_id)
        self.assertTrue(trace.transitions)
        self.assertTrue(trace.results)
        self.assertEqual(executions[0].attempt_no, 1)

    async def test_final_result_exposes_all_five_axes(self):
        payload = (await self.plan()).json()
        await self.decide(payload["plan"]["plan_id"], bundle=payload)
        final = (await self.run_execute(payload)).json()["final"]
        for axis in ("state", "request_accepted", "motion_completed",
                     "target_reached", "task_succeeded"):
            with self.subTest(axis=axis):
                self.assertIn(axis, final)
        self.assertIn("recoverable", final)
        self.assertIn("hold", final)

    async def test_unknown_plan_id_is_404(self):
        response = await self.client.post("/v1/execute", {
            "session_id": self.session, "request_id": "req_x", "plan_id": "plan_x",
        })
        self.assertEqual(response.status, 404)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_UNKNOWN_RESOURCE.value
        )


class TestStop(WebCase):
    async def test_stop_works_without_a_plan(self):
        """정지는 계획·LLM 경로를 거치지 않는다."""
        response = await self.client.post("/v1/stop")
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertIn(payload["state"], ("confirmed", "unconfirmed"))
        self.assertEqual(self.provider.calls, 0)

    async def test_stop_reports_confirmation_separately_from_request(self):
        payload = (await self.client.post("/v1/stop")).json()
        self.assertIn("requested", payload)
        self.assertIn("confirmed", payload)
        # 확인되지 않은 정지를 ok로 표시하지 않는다.
        self.assertEqual(payload["ok"], payload["confirmed"])

    async def test_confirmed_stop_is_not_reported_as_task_success(self):
        """정지 확인은 작업 성공이 아니다. 두 축을 섞지 않는다."""
        payload = (await self.client.post("/v1/stop")).json()
        self.assertTrue(payload["confirmed"])
        self.assertEqual(payload["confirm_result"]["state"], "stopped")
        self.assertFalse(payload["confirm_result"]["task_succeeded"])
        self.assertIsNone(payload["reason_code"])

    async def test_unconfirmed_stop_carries_a_reason_code(self):
        runtime = self.app.runtime

        class Broken:
            def connect(self, timeout_sec):
                raise RuntimeError("연결 끊김")

        runtime.registry = type(runtime.registry)()
        runtime.registry.register(
            "fake_dev", runtime.profile, lambda rid, prof: Broken()
        )
        payload = (await self.client.post("/v1/stop")).json()
        self.assertFalse(payload["confirmed"])
        self.assertEqual(payload["state"], "unconfirmed")
        self.assertEqual(
            payload["reason_code"], ReasonCode.EXEC_STOP_UNCONFIRMED.value
        )


class TestStateRestore(WebCase):
    async def test_state_is_empty_before_any_plan(self):
        payload = (await self.client.get(f"/v1/state?session_id={self.session}")).json()
        self.assertIsNone(payload["plan"])

    async def test_state_restores_plan_approval_and_execution(self):
        plan_payload = (await self.plan()).json()
        plan_id = plan_payload["plan"]["plan_id"]
        await self.decide(plan_id, bundle=plan_payload)
        await self.run_execute(plan_payload)

        payload = (await self.client.get(f"/v1/state?session_id={self.session}")).json()
        self.assertEqual(payload["plan"]["plan_id"], plan_id)
        self.assertEqual(len(payload["approvals"]), 1)
        self.assertEqual(len(payload["executions"]), 1)
        execution = payload["executions"][0]
        self.assertTrue(execution["transitions"])
        self.assertTrue(execution["results"])
        # 복원된 결과도 5축을 그대로 담는다.
        self.assertIn("task_succeeded", execution["results"][-1])


class TestFeatureUnavailable(WebCase):
    planning_available = False

    async def test_planning_is_reported_unavailable_not_faked(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertFalse(payload["features"]["planning"]["available"])
        self.assertTrue(payload["features"]["planning"]["detail"])

    async def test_plan_request_is_refused_with_a_reason(self):
        response = await self.plan()
        self.assertEqual(response.status, 503)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_LLM_UNAVAILABLE.value
        )

    async def test_stop_still_works_when_planning_is_down(self):
        response = await self.client.post("/v1/stop")
        self.assertEqual(response.status, 200)


class TestPlanFailurePaths(WebCase):
    provider_outputs = [
        PlanProviderError(ReasonCode.PLAN_LLM_TIMEOUT, "느리다"),
    ]

    async def test_timeout_is_reported_as_recoverable(self):
        response = await self.plan()
        self.assertEqual(response.status, 422)
        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["recoverable"], True)
        self.assertIn(payload["reason_code"], (
            ReasonCode.PLAN_LLM_TIMEOUT.value,
            ReasonCode.PLAN_LLM_RETRY_EXHAUSTED.value,
        ))


class TestClarificationPath(WebCase):
    provider_outputs = [ClarificationNeeded("어느 팔레트인지 알 수 없다")]

    async def test_clarification_is_surfaced_to_the_user(self):
        payload = (await self.plan("그거 저기로 옮겨줘")).json()
        self.assertFalse(payload["ok"])
        self.assertEqual(
            payload["reason_code"], ReasonCode.PLAN_CLARIFICATION_REQUIRED.value
        )
        self.assertEqual(payload["clarification"], "어느 팔레트인지 알 수 없다")
        self.assertTrue(payload["recoverable"])


class TestEventSocket(WebCase):
    async def test_socket_sends_hello_with_health(self):
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.session}") as socket:
            hello = await socket.wait_for(lambda m: m.get("type") == "hello")
            self.assertTrue(socket.accepted)
            self.assertIn("features", hello["payload"])

    async def test_execution_events_are_pushed(self):
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.session}") as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            payload = (await self.plan()).json()
            plan_id = payload["plan"]["plan_id"]
            await self.decide(plan_id, bundle=payload)
            await self.run_execute(payload)
            started = await socket.wait_for(
                lambda m: m.get("type") == "execution_started"
            )
            self.assertEqual(started["payload"]["plan_id"], plan_id)
            state_event = await socket.wait_for(lambda m: m.get("type") == "state")
            self.assertIn("to_state", state_event["payload"])
            final = await socket.wait_for(lambda m: m.get("type") == "execution_final")
            self.assertTrue(final["payload"]["final"]["task_succeeded"])

    async def test_stop_event_is_pushed(self):
        async with WebSocketSession(self.app, f"/v1/events?session_id={self.session}") as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            await self.client.post("/v1/stop")
            event = await socket.wait_for(lambda m: m.get("type") == "stop")
            self.assertIn("confirmed", event["payload"])

    async def test_unknown_socket_path_is_closed(self):
        async with WebSocketSession(self.app, "/v1/nope") as socket:
            await asyncio.sleep(0.05)
            self.assertTrue(socket.closed)
            self.assertFalse(socket.accepted)


class TestSttSocketUnavailable(WebCase):
    """STT 모델이 없으면 **사용할 수 없는 상태로 알린다.** 흉내 내지 않는다."""

    async def test_socket_reports_unavailable_and_closes(self):
        async with WebSocketSession(self.app, f"/v1/stt?session_id={self.session}") as socket:
            message = await socket.wait_for(lambda m: m.get("kind") == "error")
            self.assertEqual(
                message["reason_code"], ReasonCode.STT_BACKEND_UNAVAILABLE.value
            )
            self.assertTrue(message["detail"])
            await asyncio.sleep(0.05)
            self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
