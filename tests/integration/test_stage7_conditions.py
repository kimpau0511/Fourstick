"""7단계 완료 조건과 6-07·6-08 대조 (md/개발플랜.md).

7단계 완료 조건:
- UI와 서버·DB의 plan / execution / robot ID가 일치한다
- **사용자가 누르지 않은 자동 실행 0건**
- STOP 성공과 정지 미확인이 구분되어 표시된다
- 정상·BLOCK·ASK·STOP 흐름이 통과한다

6-07(실행 직전 재검증)·6-08(PASS/BLOCK/ASK와 이유 코드 API 계약):
- TTL·plan_hash·Profile·Policy·환경 snapshot·컨트롤러·로봇 상태·승인 유효성
- 세 판정과 이유 코드가 **API 왕복 후에도 그대로** 나온다

Fake Adapter 실행을 실제 로봇 검증으로 세지 않는다 — 모든 실행 기록이
`is_simulated=true`임을 함께 확인한다.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode
from integration.asgi_client import WebSocketSession
from integration.test_session_isolation import IsolationCase
from robots.fake.geometry import DevFakeGeometryValidator, FakeCell
from storage.records import ValidationDecision
from validation.execution_permit import PermitContext, check_execution_permit
from validation.safety_validator import evaluate


class TestIdentifiersMatchAcrossUiServerDb(IsolationCase):
    """7단계: UI가 받은 ID = 서버 응답 ID = DB 행의 ID."""

    async def test_plan_execution_robot_ids_match_everywhere(self):
        bundle = await self.approved_plan(self.a)
        result = (await self.execute(self.a, bundle)).json()
        repo = self.runtime.repository

        plan_id = bundle["plan"]["plan_id"]
        request_id = bundle["request_id"]
        execution_id = result["execution_id"]
        robot_id = bundle["plan"]["robot_id"]

        # 서버 응답끼리
        self.assertEqual(result["plan_id"], plan_id)
        self.assertEqual(result["request_id"], request_id)

        # DB 행
        plan_row = repo.get_plan(plan_id)
        self.assertEqual(plan_row.request_id, request_id)
        self.assertEqual(plan_row.plan.robot_id, robot_id)
        execution = repo.get_execution(execution_id)
        self.assertEqual(execution.plan_id, plan_id)
        self.assertEqual(execution.request_id, request_id)
        self.assertEqual(execution.robot_id, robot_id)
        self.assertEqual(execution.plan_hash, bundle["plan"]["plan_hash"])
        approval = repo.get_approval(execution.approval_id)
        self.assertEqual(approval.plan_id, plan_id)
        self.assertEqual(approval.robot_id, execution.robot_id)

        # Registry 응답의 robot_id
        robots = (await self.client.get("/v1/robots")).json()["robots"]
        self.assertIn(robot_id, robots)

        # 복원(UI가 새로고침 때 받는 것)
        restored = (await self.client.get(f"/v1/state?session_id={self.a}")).json()
        self.assertEqual(restored["plan"]["plan_id"], plan_id)
        self.assertEqual(restored["executions"][-1]["execution_id"], execution_id)
        self.assertEqual(restored["executions"][-1]["plan_id"], plan_id)

        # Fake 실행임을 유지한다
        self.assertTrue(execution.is_simulated)

    async def test_execution_status_endpoint_uses_the_same_ids(self):
        bundle = await self.approved_plan(self.a)
        result = (await self.execute(self.a, bundle)).json()
        status = (await self.client.get(
            f"/v1/executions/{result['execution_id']}?session_id={self.a}"
        )).json()
        self.assertEqual(status["execution_id"], result["execution_id"])
        self.assertEqual(status["plan_id"], bundle["plan"]["plan_id"])
        self.assertEqual(status["request_id"], bundle["request_id"])
        self.assertEqual(status["plan_hash"], bundle["plan"]["plan_hash"])


class TestNoExecutionWithoutAUserPress(IsolationCase):
    """7단계: 사용자가 누르지 않은 자동 실행 0건."""

    async def test_planning_never_executes(self):
        bundle = await self.plan(self.a)
        self.assertEqual(
            self.runtime.repository.executions_for_request(bundle["request_id"]), ()
        )
        self.assertEqual(
            self.runtime.repository.execution_environment_counts(),
            {"simulated": 0, "real": 0, "unknown": 0},
        )

    async def test_approval_never_executes(self):
        bundle = await self.approved_plan(self.a)
        self.assertEqual(
            self.runtime.repository.executions_for_request(bundle["request_id"]), ()
        )

    async def test_restore_never_executes(self):
        bundle = await self.approved_plan(self.a)
        for _ in range(3):
            await self.client.get(f"/v1/state?session_id={self.a}")
            await self.client.get(
                f"/v1/plan?session_id={self.a}&request_id={bundle['request_id']}"
                f"&plan_id={bundle['plan']['plan_id']}"
            )
        self.assertEqual(
            self.runtime.repository.executions_for_request(bundle["request_id"]), ()
        )

    async def test_event_subscription_never_executes(self):
        bundle = await self.approved_plan(self.a)
        async with WebSocketSession(
            self.app, f"/v1/events?session_id={self.a}"
        ) as socket:
            await socket.wait_for(lambda m: m.get("type") == "hello")
            await asyncio.sleep(0.1)
        self.assertEqual(
            self.runtime.repository.executions_for_request(bundle["request_id"]), ()
        )

    async def test_stop_never_starts_an_execution(self):
        await self.approved_plan(self.a)
        await self.client.post("/v1/stop", {"session_id": self.a})
        self.assertEqual(
            self.runtime.repository.execution_environment_counts()["simulated"], 0
        )

    async def test_exactly_one_execution_per_execute_call(self):
        bundle = await self.approved_plan(self.a)
        await self.execute(self.a, bundle)
        await self.execute(self.a, bundle)
        rows = self.runtime.repository.executions_for_request(bundle["request_id"])
        self.assertEqual(len(rows), 2)
        self.assertEqual([r.attempt_no for r in rows], [1, 2])


class TestPermitRechecksEverything(IsolationCase):
    """6-07: 실행 직전에 TTL·hash·Profile·Policy·환경·로봇 상태를 다시 본다."""

    def permit_context(self, plan, **over):
        runtime = self.runtime
        policy_id, policy_version = runtime.policy_binding
        snapshot = runtime.environment_snapshot()
        marker = f"{runtime.robot_id}:env"
        kw = dict(
            now=self.app.api.now(), robot_ready=True,
            robot_state_observed_at=self.app.api.now(), robot_state_valid=True,
            environment_observed_at=self.app.api.now(),
            recorded_environment_version=1, current_environment_version=1,
            recorded_environment_session=marker, current_environment_session=marker,
            recorded_plan_hash=plan.plan_hash(),
            profile_id=plan.profile_id, profile_version=plan.profile_version,
            supported_skills=runtime.profile.supported_skills,
            recorded_policy_id=policy_id, current_policy_id=policy_id,
            recorded_policy_version=policy_version,
            current_policy_version=policy_version,
            recorded_snapshot_id=snapshot.snapshot_id,
            current_snapshot_id=snapshot.snapshot_id,
            recorded_snapshot_hash=snapshot.content_hash,
            current_snapshot_hash=snapshot.content_hash,
        )
        kw.update(over)
        return PermitContext(**kw)

    async def check(self, **over):
        bundle = await self.plan(self.a)
        plan = self.runtime.repository.get_plan(bundle["plan"]["plan_id"]).plan
        rules = evaluate(plan, self.runtime.safety_policy, self.runtime.resource_catalog)
        return check_execution_permit(
            plan, rules, self.permit_context(plan, **over),
            self.runtime.freshness_policy,
        )

    async def test_all_conditions_satisfied_grants(self):
        permit = await self.check()
        self.assertTrue(permit.granted, permit.reasons)

    async def test_expired_ttl_is_refused(self):
        permit = await self.check(now=self.app.api.now() + 10_000)
        self.assertFalse(permit.granted)
        self.assertIn(ReasonCode.PLAN_EXPIRED, permit.reason_codes())

    async def test_changed_hash_is_refused(self):
        permit = await self.check(recorded_plan_hash="0" * 64)
        self.assertIn(ReasonCode.PLAN_HASH_MISMATCH, permit.reason_codes())

    async def test_changed_profile_is_refused(self):
        permit = await self.check(profile_version="9.9")
        self.assertIn(ReasonCode.ROBOT_PROFILE_MISMATCH, permit.reason_codes())

    async def test_changed_policy_is_refused(self):
        permit = await self.check(current_policy_version="changed-9.9")
        self.assertIn(ReasonCode.CONFIG_VERSION_MISMATCH, permit.reason_codes())

    async def test_changed_snapshot_is_refused(self):
        permit = await self.check(current_snapshot_hash="f" * 64)
        self.assertIn(ReasonCode.EXEC_ENVIRONMENT_CHANGED, permit.reason_codes())

    async def test_unknown_snapshot_is_insufficient_data_not_pass(self):
        permit = await self.check(current_snapshot_hash=None)
        self.assertFalse(permit.granted)
        self.assertIn(ReasonCode.SAFETY_INSUFFICIENT_DATA, permit.reason_codes())

    async def test_controller_not_ready_is_refused(self):
        permit = await self.check(robot_ready=False)
        self.assertIn(ReasonCode.ROBOT_NOT_CONNECTED, permit.reason_codes())

    async def test_unknown_controller_state_is_refused(self):
        permit = await self.check(robot_ready=None)
        self.assertIn(ReasonCode.ROBOT_STATE_UNAVAILABLE, permit.reason_codes())

    async def test_stale_robot_state_is_refused(self):
        permit = await self.check(
            robot_state_observed_at=self.app.api.now() - 3_600
        )
        self.assertIn(ReasonCode.ROBOT_STATE_STALE, permit.reason_codes())

    async def test_changed_environment_version_is_refused(self):
        permit = await self.check(current_environment_version=2)
        self.assertIn(ReasonCode.EXEC_ENVIRONMENT_CHANGED, permit.reason_codes())

    async def test_permit_decision_is_stored_with_reasons(self):
        bundle = await self.approved_plan(self.a)
        result = (await self.execute(self.a, bundle)).json()
        permits = self.runtime.repository.permits_for_plan(bundle["plan"]["plan_id"])
        self.assertTrue(permits)
        self.assertEqual(permits[-1].permit_id, result["permit_id"])
        self.assertTrue(permits[-1].granted)
        self.assertEqual(permits[-1].plan_hash, bundle["plan"]["plan_hash"])

    async def test_refused_permit_is_stored_too(self):
        """허가가 거부된 경우도 기록에 남는다 — 이유와 함께."""
        bundle = await self.approved_plan(self.a)
        # 계획 TTL만 지나게 만든다. 환경 관측이 먼저 만료되면 관문에서 막혀
        # 허가 단계까지 가지 않으므로, 신선도 한계를 넉넉히 둔다.
        self.runtime.freshness_policy = dataclasses.replace(
            self.runtime.freshness_policy,
            robot_state_max_age_sec=100_000.0, environment_max_age_sec=100_000.0,
        )
        api = self.app.api
        later = api.now() + self.config.plan_ttl_sec + 10
        api.now = lambda: later
        response = await self.execute(self.a, bundle)
        permits = self.runtime.repository.permits_for_plan(bundle["plan"]["plan_id"])
        self.assertTrue(permits)
        self.assertFalse(permits[-1].granted)
        codes = {r.reason for r in permits[-1].reasons}
        self.assertTrue(codes)
        self.assertIn(response.status, (200, 409))


class TestThreeDecisionsRoundTripThroughTheApi(IsolationCase):
    """6-08: PASS / BLOCK / ASK와 이유 코드가 API 왕복 후에도 같다."""

    async def test_pass_round_trip(self):
        bundle = await self.plan(self.a)
        self.assertEqual(bundle["safety"]["decision"], "allow")
        self.assertEqual(bundle["validation"]["decision"], "allow")
        self.assertTrue(bundle["executable"])
        fetched = (await self.client.get(
            f"/v1/plan?session_id={self.a}&request_id={bundle['request_id']}"
            f"&plan_id={bundle['plan']['plan_id']}"
        )).json()
        self.assertEqual(fetched["validation"]["decision"], "allow")
        self.assertEqual(
            [r["code"] for r in fetched["safety"]["rules"]],
            [r["code"] for r in bundle["safety"]["rules"]],
        )

    async def test_block_round_trip_keeps_the_reason_code(self):
        bundle = await self.plan(self.a, "3번 팔레트에서 A자재를 집어줘")
        self.assertEqual(bundle["validation"]["decision"], "block")
        self.assertEqual(
            bundle["consistency"]["reason_code"],
            ReasonCode.PLAN_RESOURCE_MISMATCH.value,
        )
        await self.decide(self.a, bundle)          # 승인해도 실행은 막힌다
        response = await self.execute(self.a, bundle)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.PLAN_RESOURCE_MISMATCH.value
        )
        runs = self.runtime.repository.validation_runs_for_plan(
            bundle["plan"]["plan_id"], validator_kind="gate"
        )
        self.assertIs(runs[-1].decision, ValidationDecision.BLOCK)
        self.assertEqual(
            runs[-1].reason_code, ReasonCode.PLAN_RESOURCE_MISMATCH
        )

    async def test_ask_round_trip_keeps_the_reason_code(self):
        self.runtime.geometry_validator = None
        bundle = await self.plan(self.a)
        self.assertEqual(bundle["validation"]["decision"], "ask")
        self.assertEqual(
            bundle["validation"]["reason_code"],
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value,
        )
        await self.decide(self.a, bundle)          # 승인해도 실행은 막힌다
        response = await self.execute(self.a, bundle)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value,
        )
        runs = self.runtime.repository.validation_runs_for_plan(
            bundle["plan"]["plan_id"], validator_kind="gate"
        )
        self.assertIs(runs[-1].decision, ValidationDecision.ASK)

    async def test_every_rule_row_has_a_status_and_optional_reason(self):
        bundle = await self.plan(self.a, "3번 팔레트에서 A자재를 집어줘")
        statuses = {"pass", "block", "insufficient_data", "not_applicable"}
        for rule in bundle["safety"]["rules"]:
            with self.subTest(code=rule["code"]):
                self.assertIn(rule["status"], statuses)
                if rule["status"] in ("block", "insufficient_data"):
                    self.assertIsNotNone(rule["reason_code"])
                    # 이유 코드는 계약에 있는 값이어야 한다.
                    ReasonCode(rule["reason_code"])

    async def test_stop_confirmed_and_unconfirmed_are_distinct(self):
        confirmed = (await self.client.post("/v1/stop", {"session_id": self.a})).json()
        self.assertTrue(confirmed["confirmed"])
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertIsNone(confirmed["reason_code"])

        # 어댑터를 떼면 정지를 확인할 수 없다 — 성공으로 표시하지 않는다.
        self.runtime.robot_id = None
        unconfirmed = (await self.client.post("/v1/stop", {})).json()
        self.assertFalse(unconfirmed["confirmed"])
        self.assertEqual(unconfirmed["state"], "unconfirmed")
        self.assertEqual(
            unconfirmed["reason_code"], ReasonCode.ROBOT_NOT_REGISTERED.value
        )


class TestVersionLinkageSurvivesApiRoundTrip(IsolationCase):
    """validation_run · approval · execution의 Profile·Policy·snapshot 연결."""

    async def test_links_are_consistent_after_a_full_round_trip(self):
        bundle = await self.plan(self.a)
        approval = (await self.decide(self.a, bundle)).json()
        result = (await self.execute(self.a, bundle)).json()
        repo = self.runtime.repository

        approval_row = repo.get_approval(approval["approval_id"])
        gate_run = repo.get_validation_run(approval_row.validation_run_id)
        execution = repo.get_execution(result["execution_id"])
        execution_runs = repo.validation_runs_for_execution(result["execution_id"])

        # 승인 ↔ 검증 실행
        self.assertEqual(gate_run.plan_id, approval_row.plan_id)
        self.assertEqual(gate_run.plan_hash, approval_row.plan_hash)
        self.assertEqual(gate_run.profile_id, approval_row.profile_id)
        self.assertEqual(gate_run.profile_version, approval_row.profile_version)
        self.assertEqual(gate_run.policy_id, approval_row.policy_id)
        self.assertEqual(gate_run.policy_version, approval_row.policy_version)
        self.assertEqual(gate_run.snapshot_id, approval_row.snapshot_id)
        self.assertEqual(gate_run.snapshot_hash, approval_row.snapshot_hash)

        # 승인 ↔ 실행
        self.assertEqual(execution.approval_id, approval_row.approval_id)
        self.assertEqual(execution.plan_hash, approval_row.plan_hash)
        self.assertEqual(execution.profile_id, approval_row.profile_id)
        self.assertEqual(execution.profile_version, approval_row.profile_version)

        # 실행 직전 재검증도 같은 버전을 가리킨다
        self.assertTrue(execution_runs)
        for run in execution_runs:
            self.assertEqual(run.profile_version, approval_row.profile_version)
            self.assertEqual(run.policy_version, approval_row.policy_version)
            if run.validator_kind in ("geometry", "gate"):
                self.assertEqual(run.snapshot_hash, approval_row.snapshot_hash)

    async def test_api_reports_the_same_versions_it_stored(self):
        bundle = await self.plan(self.a)
        approval = (await self.decide(self.a, bundle)).json()
        bindings = bundle["validation"]["bindings"]
        self.assertEqual(approval["profile_version"],
                         bindings["capability_profile_version"])
        self.assertEqual(approval["policy_version"], bindings["policy_version"])
        self.assertEqual(approval["snapshot_id"], bindings["snapshot_id"])
        history = (await self.client.get(
            f"/v1/plan?session_id={self.a}&request_id={bundle['request_id']}"
            f"&plan_id={bundle['plan']['plan_id']}"
        )).json()["approvals"]
        self.assertEqual(history[-1]["validation_run_id"],
                         approval["validation_run_id"])
        self.assertEqual(history[-1]["policy_version"], bindings["policy_version"])

    async def test_changed_snapshot_breaks_the_link_and_is_refused(self):
        bundle = await self.approved_plan(self.a)
        cell = FakeCell.from_catalog(
            self.runtime.resource_catalog, frame_id=self.runtime.frame_id,
            cell_version="cell-linked-2.0",
        )
        self.runtime.geometry_cell = cell
        self.runtime.geometry_validator = DevFakeGeometryValidator(cell)
        response = await self.execute(self.a, bundle)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.EXEC_ENVIRONMENT_CHANGED.value,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
