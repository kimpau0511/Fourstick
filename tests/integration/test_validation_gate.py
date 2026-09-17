"""실행 관문과 검증 이력 (md/개발플랜.md 6-04·6-05).

계획 → 승인 → 실행 경로에서 확인하는 것:

- Capability 사전 검사와 기하 검사가 실행 전에 선다
- 기하 검사 구현체가 없으면 ASK이고, **ASK로는 실행 허가가 나지 않는다**
- 승인은 검증 당시 조건(plan_hash·robot·Profile·Policy·snapshot·검증 결과)에
  묶이고, 하나라도 바뀌면 재검증·재승인을 요구한다
- 검증 이력이 append-only로 남고, 환경 데이터 본문은 담기지 않는다
- Fake 실행 환경 값이 모순되게 저장되지 않는다

실제 로봇 수치는 쓰지 않는다. 개발용 Fake 셀과 합성 Validator만 쓴다.
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.geometry import (
    GeometryDecision,
    GeometryReason,
    GeometryVerdict,
)
from core.reason_codes import ReasonCode
from integration.test_session_isolation import IsolationCase
from robots.fake.geometry import DevFakeGeometryValidator, FakeCell
from storage.records import (
    FAKE_ADAPTER_KIND,
    ExecutionRecord,
    ValidationDecision,
    check_execution_environment,
)
from storage.repository import IntegrityViolation


class BlockingValidator:
    """충돌을 선언하는 시험용 구현체."""

    validator_id = "blocking-test"
    validator_version = "1.0"

    def check(self, request):
        return GeometryVerdict(
            decision=GeometryDecision.BLOCK, validator_id=self.validator_id,
            validator_version=self.validator_version, input_complete=True,
            started_at=request.checked_at, finished_at=request.checked_at,
            reasons=(GeometryReason(
                ReasonCode.GEOMETRY_COLLISION, "시험용 충돌 선언",
            ),),
            snapshot_id=request.snapshot.snapshot_id,
            snapshot_version=request.snapshot.snapshot_version,
            snapshot_hash=request.snapshot.content_hash,
            frame_id=request.frame_id,
        )


class GateCase(IsolationCase):
    def runs(self, plan_id: str, kind: str | None = None):
        return self.runtime.repository.validation_runs_for_plan(
            plan_id, validator_kind=kind
        )


class TestGateIsPartOfThePlanResponse(GateCase):
    async def test_plan_reports_capability_and_geometry(self):
        payload = await self.plan(self.a)
        validation = payload["validation"]
        self.assertEqual(validation["decision"], "allow")
        self.assertEqual(validation["capability"]["status"], "pass")
        self.assertEqual(validation["geometry"]["decision"], "allow")
        self.assertEqual(
            validation["geometry"]["validator_id"], "dev-fake-geometry"
        )
        self.assertTrue(payload["executable"])
        codes = {rule["code"] for rule in payload["safety"]["rules"]}
        self.assertIn("E-CAP-001", codes)
        self.assertIn("E-GEOM-001", codes)

    async def test_bindings_are_reported_for_the_screen(self):
        payload = await self.plan(self.a)
        bindings = payload["validation"]["bindings"]
        for key in ("plan_hash", "robot_id", "capability_profile_id",
                    "capability_profile_version", "policy_id", "policy_version",
                    "snapshot_id", "snapshot_version", "snapshot_hash", "frame_id"):
            with self.subTest(key=key):
                self.assertIsNotNone(bindings[key])

    async def test_symbolic_plan_marks_numeric_rules_not_applicable(self):
        payload = await self.plan(self.a)
        table = {r["code"]: r for r in payload["safety"]["rules"]}
        for code in ("E-CAP-005", "E-CAP-006", "E-CAP-007", "E-CAP-008"):
            with self.subTest(code=code):
                self.assertEqual(table[code]["status"], "not_applicable")


class TestGeometryBlocksAndAsks(GateCase):
    async def test_no_validator_means_ask_and_no_execution(self):
        self.runtime.geometry_validator = None
        payload = await self.plan(self.a)
        self.assertEqual(payload["validation"]["decision"], "ask")
        self.assertFalse(payload["executable"])
        self.assertEqual(
            payload["validation"]["geometry"]["reasons"][0]["reason_code"],
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value,
        )
        approved = (await self.decide(self.a, payload)).json()
        self.assertTrue(approved["ok"])            # 승인 기록은 남는다
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value,
        )
        self.assertEqual(
            self.runtime.repository.executions_for_request(payload["request_id"]), ()
        )

    async def test_ask_execution_attempt_is_recorded(self):
        self.runtime.geometry_validator = None
        payload = await self.plan(self.a)
        await self.decide(self.a, payload)
        await self.execute(self.a, payload)
        gates = self.runs(payload["plan"]["plan_id"], "gate")
        self.assertGreaterEqual(len(gates), 3)     # 계획·승인·실행 시도
        self.assertTrue(
            all(run.decision is ValidationDecision.ASK for run in gates)
        )
        self.assertTrue(all(not run.input_complete for run in gates))

    async def test_collision_blocks_execution(self):
        self.runtime.geometry_validator = BlockingValidator()
        payload = await self.plan(self.a)
        self.assertEqual(payload["validation"]["decision"], "block")
        self.assertFalse(payload["executable"])
        await self.decide(self.a, payload)
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.GEOMETRY_COLLISION.value
        )

    async def test_workspace_violation_has_its_own_code(self):
        cell = FakeCell.from_catalog(
            self.runtime.resource_catalog, frame_id=self.runtime.frame_id,
            blocked_locations=frozenset({"loc_conveyor"}),
        )
        self.runtime.geometry_cell = cell
        self.runtime.geometry_validator = DevFakeGeometryValidator(cell)
        payload = await self.plan(self.a)
        self.assertEqual(payload["validation"]["decision"], "block")
        self.assertEqual(
            payload["validation"]["geometry"]["reasons"][0]["reason_code"],
            ReasonCode.GEOMETRY_WORKSPACE_VIOLATION.value,
        )

    async def test_expired_snapshot_is_ask(self):
        """관측 시각보다 한참 뒤에 검사하면 snapshot이 만료된다.

        어댑터가 관측을 새로 만들기 때문에 유효기간만 줄이면 만료가 되지 않는다.
        검사 시각(주입된 시계)을 앞으로 옮겨 만료를 만든다.
        """
        api = self.app.api
        later = api.now() + self.runtime.freshness_policy.environment_max_age_sec + 60
        api.now = lambda: later
        payload = await self.plan(self.a)
        geometry = payload["validation"]["geometry"]
        self.assertEqual(geometry["decision"], "ask")
        self.assertEqual(
            geometry["reasons"][0]["reason_code"],
            ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED.value,
        )
        self.assertFalse(payload["executable"])

    async def test_missing_environment_is_ask(self):
        self.runtime.geometry_cell = None      # snapshot 제공자 없음
        payload = await self.plan(self.a)
        geometry = payload["validation"]["geometry"]
        self.assertEqual(geometry["decision"], "ask")
        self.assertEqual(
            geometry["reasons"][0]["reason_code"],
            ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE.value,
        )


class TestApprovalIsBoundToValidation(GateCase):
    async def test_approval_records_every_binding(self):
        payload = await self.plan(self.a)
        approval = (await self.decide(self.a, payload)).json()
        record = self.runtime.repository.get_approval(approval["approval_id"])
        bindings = payload["validation"]["bindings"]
        self.assertEqual(record.plan_hash, bindings["plan_hash"])
        self.assertEqual(record.robot_id, bindings["robot_id"])
        self.assertEqual(record.profile_id, bindings["capability_profile_id"])
        self.assertEqual(
            record.profile_version, bindings["capability_profile_version"]
        )
        self.assertEqual(record.policy_id, bindings["policy_id"])
        self.assertEqual(record.policy_version, bindings["policy_version"])
        self.assertEqual(record.snapshot_id, bindings["snapshot_id"])
        self.assertEqual(record.snapshot_version, bindings["snapshot_version"])
        self.assertEqual(record.snapshot_hash, bindings["snapshot_hash"])
        self.assertTrue(record.validation_run_id)
        run = self.runtime.repository.get_validation_run(record.validation_run_id)
        self.assertEqual(run.validator_kind, "gate")
        self.assertIs(run.decision, ValidationDecision.ALLOW)

    async def test_profile_change_after_approval_requires_reapproval(self):
        payload = await self.approved_plan(self.a)
        # 승인 후 Profile 버전이 바뀐다.
        self.runtime.profile = dataclasses.replace(
            self.runtime.profile, profile_version="changed-9.9"
        )
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.ROBOT_PROFILE_MISMATCH.value
        )
        self.assertEqual(
            self.runtime.repository.executions_for_request(payload["request_id"]), ()
        )

    async def test_policy_change_after_approval_requires_reapproval(self):
        payload = await self.approved_plan(self.a)
        self.runtime.safety_policy = dataclasses.replace(
            self.runtime.safety_policy, policy_version="changed-policy-9.9"
        )
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"], ReasonCode.CONFIG_VERSION_MISMATCH.value
        )

    async def test_environment_change_after_approval_requires_reapproval(self):
        payload = await self.approved_plan(self.a)
        # 셀 선언이 바뀌면 snapshot 지문이 달라진다.
        cell = FakeCell.from_catalog(
            self.runtime.resource_catalog, frame_id=self.runtime.frame_id,
            cell_version="cell-changed-2.0",
        )
        self.runtime.geometry_cell = cell
        self.runtime.geometry_validator = DevFakeGeometryValidator(cell)
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.EXEC_ENVIRONMENT_CHANGED.value,
        )

    async def test_reapproval_after_the_change_allows_execution(self):
        payload = await self.approved_plan(self.a)
        cell = FakeCell.from_catalog(
            self.runtime.resource_catalog, frame_id=self.runtime.frame_id,
            cell_version="cell-changed-2.0",
        )
        self.runtime.geometry_cell = cell
        self.runtime.geometry_validator = DevFakeGeometryValidator(cell)
        self.assertEqual((await self.execute(self.a, payload)).status, 409)
        # 다시 승인하면 새 조건에 묶인 승인이 생기고 실행할 수 있다.
        again = (await self.decide(self.a, payload)).json()
        self.assertTrue(again["ok"])
        result = (await self.execute(self.a, payload)).json()
        self.assertTrue(result["ok"])
        record = self.runtime.repository.get_execution(result["execution_id"])
        self.assertEqual(record.approval_id, again["approval_id"])

    async def test_approval_without_validation_run_cannot_execute(self):
        """0012 이전 승인처럼 검증 근거가 없는 승인은 재사용하지 않는다."""
        payload = await self.plan(self.a)
        approval = (await self.decide(self.a, payload)).json()
        repo = self.runtime.repository
        with repo._tx() as conn:
            conn.execute(
                "UPDATE plan_approvals SET validation_run_id = NULL"
                " WHERE approval_id = ?",
                (approval["approval_id"],),
            )
        response = await self.execute(self.a, payload)
        self.assertEqual(response.status, 409)
        self.assertEqual(
            response.json()["reason_code"],
            ReasonCode.SAFETY_APPROVAL_REQUIRED.value,
        )


class TestValidationRunsAreAppendOnly(GateCase):
    async def test_every_validator_kind_is_recorded(self):
        payload = await self.plan(self.a)
        kinds = {run.validator_kind for run in self.runs(payload["plan"]["plan_id"])}
        self.assertEqual(
            kinds, {"safety", "consistency", "capability", "geometry", "gate"}
        )

    async def test_runs_carry_identifiers_and_timing(self):
        payload = await self.plan(self.a)
        run = self.runs(payload["plan"]["plan_id"], "geometry")[-1]
        self.assertEqual(run.request_id, payload["request_id"])
        self.assertEqual(run.plan_id, payload["plan"]["plan_id"])
        self.assertEqual(run.plan_hash, payload["plan"]["plan_hash"])
        self.assertEqual(run.session_id, self.a)
        self.assertEqual(run.validator_id, "dev-fake-geometry")
        self.assertTrue(run.validator_version)
        self.assertTrue(run.snapshot_id)
        self.assertTrue(run.snapshot_hash)
        self.assertGreaterEqual(run.duration_ms, 0)
        self.assertGreaterEqual(run.finished_at, run.started_at)
        self.assertIsNone(run.execution_id)

    async def test_execution_revalidation_names_the_execution(self):
        payload = await self.approved_plan(self.a)
        result = (await self.execute(self.a, payload)).json()
        runs = self.runtime.repository.validation_runs_for_execution(
            result["execution_id"]
        )
        self.assertTrue(runs)
        self.assertTrue(all(r.execution_id == result["execution_id"] for r in runs))
        self.assertIn("gate", {r.validator_kind for r in runs})

    async def test_history_grows_and_is_never_rewritten(self):
        payload = await self.plan(self.a)
        first = len(self.runs(payload["plan"]["plan_id"]))
        await self.decide(self.a, payload)
        second = len(self.runs(payload["plan"]["plan_id"]))
        self.assertGreater(second, first)
        run = self.runs(payload["plan"]["plan_id"])[0]
        with self.assertRaises(IntegrityViolation):
            self.runtime.repository.append_validation_run(
                dataclasses.replace(run, detail="다른 내용")
            )

    async def test_no_environment_payload_is_copied_into_the_rows(self):
        payload = await self.plan(self.a)
        run = self.runs(payload["plan"]["plan_id"], "geometry")[-1]
        blob = str(run.to_dict())
        # 지문과 식별자만 남는다. 좌표·메시·점군 같은 본문은 없다.
        self.assertIn(run.snapshot_hash, blob)
        for banned in ("vertices", "mesh", "pointcloud", "collision_objects"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, blob)
        self.assertLess(len(blob), 2000)

    async def test_allow_with_incomplete_input_cannot_be_stored(self):
        payload = await self.plan(self.a)
        run = self.runs(payload["plan"]["plan_id"], "gate")[-1]
        with self.assertRaises(ValueError):
            dataclasses.replace(
                run, validation_run_id="vr_forced", input_complete=False,
            )


class TestFakeEnvironmentCannotContradict(GateCase):
    async def test_fake_execution_is_always_simulated(self):
        payload = await self.approved_plan(self.a)
        result = (await self.execute(self.a, payload)).json()
        record = self.runtime.repository.get_execution(result["execution_id"])
        self.assertEqual(record.adapter_kind, FAKE_ADAPTER_KIND)
        self.assertTrue(record.is_simulated)
        self.assertIsNone(record.environment_confirmed_at)

    async def test_runtime_reports_fake_even_with_a_real_profile(self):
        """Profile이 실제 것이어도 어댑터가 Fake면 실행은 시뮬레이션이다."""
        self.runtime.robot_configured = True
        self.assertEqual(self.runtime.adapter_kind, FAKE_ADAPTER_KIND)
        self.assertTrue(self.runtime.is_simulated)
        kind, simulated, confirmed = self.runtime.execution_environment(
            connected=True, now=1.0
        )
        self.assertEqual(kind, FAKE_ADAPTER_KIND)
        self.assertTrue(simulated)
        self.assertIsNone(confirmed)

    def test_real_adapter_needs_a_confirmed_connection_for_real(self):
        kind, simulated, confirmed = self.runtime.execution_environment(
            connected=False, now=5.0
        )
        self.runtime.declared_adapter_kind = "some_real_adapter"
        self.runtime._adapter = None
        kind, simulated, confirmed = self.runtime.execution_environment(
            connected=False, now=5.0
        )
        self.assertEqual(kind, "some_real_adapter")
        self.assertIsNone(simulated)      # 근거가 없으면 unknown
        self.assertIsNone(confirmed)
        kind, simulated, confirmed = self.runtime.execution_environment(
            connected=True, now=5.0
        )
        self.assertFalse(simulated)       # 연결이 확인되면 real
        self.assertEqual(confirmed, 5.0)

    def test_contradictions_are_refused_by_the_record_contract(self):
        cases = [
            dict(adapter_kind="fake", is_simulated=False,
                 environment_confirmed_at=None),
            dict(adapter_kind="fake", is_simulated=None,
                 environment_confirmed_at=None),
            dict(adapter_kind="real_arm", is_simulated=False,
                 environment_confirmed_at=None),
            dict(adapter_kind=None, is_simulated=True,
                 environment_confirmed_at=None),
        ]
        for case in cases:
            with self.subTest(**case):
                with self.assertRaises(ValueError):
                    check_execution_environment(**case)

    def test_allowed_combinations_pass(self):
        ok = [
            dict(adapter_kind="fake", is_simulated=True,
                 environment_confirmed_at=None),
            dict(adapter_kind="real_arm", is_simulated=False,
                 environment_confirmed_at=1.0),
            dict(adapter_kind="real_arm", is_simulated=None,
                 environment_confirmed_at=None),
            dict(adapter_kind=None, is_simulated=None,
                 environment_confirmed_at=None),
        ]
        for case in ok:
            with self.subTest(**case):
                check_execution_environment(**case)

    def test_execution_record_rejects_contradictions(self):
        with self.assertRaises(ValueError):
            ExecutionRecord(
                execution_id="exec_x", request_id="req_x", plan_id="plan_x",
                plan_hash="h", attempt_no=1, adapter_id="a", robot_id="r",
                profile_id="p", profile_version="1", policy_id="pol",
                policy_version="1", schema_version="2.0", started_at=1.0,
                adapter_kind="fake", is_simulated=False,
            )

    async def test_sqlite_refuses_contradictory_rows(self):
        """레코드 계약을 우회해도 저장에서 막힌다(마이그레이션 0011 트리거)."""
        payload = await self.approved_plan(self.a)
        result = (await self.execute(self.a, payload)).json()
        repo = self.runtime.repository
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError) as ctx:
            with repo._tx() as conn:
                conn.execute(
                    "UPDATE executions SET is_simulated = 0 WHERE execution_id = ?",
                    (result["execution_id"],),
                )
        self.assertIn("모순", str(ctx.exception))
        record = repo.get_execution(result["execution_id"])
        self.assertTrue(record.is_simulated)      # 바뀌지 않았다

    async def test_counts_stay_split_by_environment(self):
        payload = await self.approved_plan(self.a)
        await self.execute(self.a, payload)
        counts = self.runtime.repository.execution_environment_counts()
        self.assertEqual(counts, {"simulated": 1, "real": 0, "unknown": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)
