"""저장·조회·API 왕복 검증 (md/개발플랜.md 2-07, md/저장소_설계.md).

요구 15의 항목을 하나씩 확인한다.
  plan_hash / 상태와 전이 순서 / ReasonCode / 성공·실패·확인불가 구분 /
  최종 결과 5축 / Profile·Policy·schema 버전 / 재시도별 실행 이력

저장 백엔드는 표준 라이브러리 sqlite3다. PostgreSQL 구현과 양쪽 호환 테스트는
`md/저장소_설계.md`의 "PostgreSQL 도입 체크리스트"에 미완료로 기록했다.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.execution_result import ExecutionResult, rejected, success, unverifiable
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan, TaskStep
from server.schemas import ExecutionResultModel, TaskPlanModel, parse_task_plan
from storage.records import (
    PermitReasonRecord,
    PlanningAttemptRecord,
    PlanningAttemptStatus,
    PlanningPayload,
    SttInferenceRecord,
    PermitRecord,
    RequestRecord,
    RuleResultRecord,
    ValidationRecord,
)
from storage.repository import CorruptedRecord, IntegrityViolation, StorageError
from storage.sqlite.repository import SqliteRepository
from storage.sqlite.schema import LATEST_VERSION as LATEST_SCHEMA_VERSION

STEPS = (
    TaskStep("home"),
    TaskStep("move", {"to": "loc_a"}),
    TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
    TaskStep("home"),
)


def make_plan(**over) -> TaskPlan:
    kw = dict(
        plan_id="plan_1", robot_id="robot_a", profile_id="profile_a",
        profile_version="1.0", steps=STEPS, created_at=1_000.0, ttl_sec=60.0,
        utterance="obj_a를 집어서 들고 있어", terminal_hold="obj_a",
    )
    kw.update(over)
    return TaskPlan(**kw)


def all_result_shapes() -> dict[str, ExecutionResult]:
    return {
        "success": success({"lift_m": 0.012}),
        "unverifiable": unverifiable(ReasonCode.EXEC_UNVERIFIABLE, {"why": "센서 없음"}),
        "rejected": rejected(ReasonCode.EXEC_PERMIT_DENIED, {"detail": "안전 판정 ASK"}),
        "stop_unconfirmed": unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, {}),
        "task_failed": ExecutionResult(
            state=ExecutionState.FAILED, request_accepted=True, motion_completed=True,
            target_reached=True, task_succeeded=False, verified=True,
            reason=ReasonCode.EXEC_TASK_FAILED,
            evidence={"terminal_hold_expected": "obj_a", "terminal_hold_observed": None},
        ),
        "goal_not_reached": ExecutionResult(
            state=ExecutionState.FAILED, request_accepted=True, motion_completed=True,
            target_reached=False, reason=ReasonCode.EXEC_GOAL_NOT_REACHED,
        ),
        "stopped": ExecutionResult(
            state=ExecutionState.STOPPED, request_accepted=True, evidence={"motion": True}
        ),
    }


class RepoCase(unittest.TestCase):
    """요청·계획까지 준비된 저장소."""

    def setUp(self):
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)
        self.repo.save_request(
            RequestRecord("req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0)
        )
        self.plan = make_plan()
        self.repo.save_plan("req_1", self.plan, 1_000.5)

    def start(self, execution_id: str) -> str:
        rec = self.repo.begin_execution(
            execution_id=execution_id, request_id="req_1", plan_id="plan_1",
            adapter_id="adapter_fake", policy_id="policy_a", policy_version="1.0",
            started_at=1_001.0,
        )
        return rec.execution_id

    def finish(self, execution_id: str, result: ExecutionResult) -> None:
        self.repo.append_result(execution_id, result, 1_002.0)


class TestSchemaAndMigration(unittest.TestCase):
    def test_schema_version_is_applied(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        self.assertEqual(repo.schema_version(), LATEST_SCHEMA_VERSION)

    def test_migration_is_idempotent_across_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "store.sqlite3")
            first = SqliteRepository(path, now=0.0)
            first.close()
            second = SqliteRepository(path, now=1.0)
            self.addCleanup(second.close)
            self.assertEqual(second.schema_version(), LATEST_SCHEMA_VERSION)

    def test_storage_schema_version_is_separate_from_contract_version(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        self.assertNotEqual(str(repo.schema_version()), TASK_PLAN_SCHEMA_VERSION)


class TestPlanRoundTrip(RepoCase):
    def test_plan_survives_and_hash_is_preserved(self):
        rec = self.repo.get_plan("plan_1")
        self.assertEqual(rec.plan, self.plan)
        self.assertEqual(rec.plan_hash, self.plan.plan_hash())
        self.assertEqual(rec.plan.plan_hash(), self.plan.plan_hash())

    def test_versions_are_searchable_and_match_the_original(self):
        rec = self.repo.get_plan("plan_1")
        self.assertEqual(rec.plan.schema_version, TASK_PLAN_SCHEMA_VERSION)
        self.assertEqual(rec.plan.profile_id, "profile_a")
        self.assertEqual(rec.plan.profile_version, "1.0")

    def test_terminal_hold_survives_both_values(self):
        self.assertEqual(self.repo.get_plan("plan_1").plan.terminal_hold, "obj_a")
        other = make_plan(plan_id="plan_2", terminal_hold=None, utterance="이송")
        self.repo.save_plan("req_1", other, 1_000.6)
        self.assertIsNone(self.repo.get_plan("plan_2").plan.terminal_hold)

    def test_persists_across_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "store.sqlite3")
            first = SqliteRepository(path, now=0.0)
            first.save_request(RequestRecord("req_1", "u", TASK_PLAN_SCHEMA_VERSION, 1.0))
            first.save_plan("req_1", make_plan(), 2.0)
            first.close()
            second = SqliteRepository(path, now=0.0)
            self.addCleanup(second.close)
            self.assertEqual(second.get_plan("plan_1").plan, make_plan())

    def test_missing_plan_is_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self.repo.get_plan("nope")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)


class TestIdempotencyAndUniqueness(RepoCase):
    def test_same_request_saved_twice_is_idempotent(self):
        self.repo.save_request(
            RequestRecord("req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0)
        )   # 예외 없음

    def test_same_request_id_with_different_content_is_refused(self):
        with self.assertRaises(IntegrityViolation):
            self.repo.save_request(RequestRecord("req_1", "다른 발화", "2.0", 999.0))

    def test_same_plan_saved_twice_is_idempotent(self):
        again = self.repo.save_plan("req_1", self.plan, 1_000.9)
        self.assertEqual(again.plan_id, "plan_1")

    def test_same_content_with_a_different_plan_id_is_refused(self):
        with self.assertRaises(IntegrityViolation):
            self.repo.save_plan("req_1", make_plan(plan_id="plan_dup"), 1_001.0)

    def test_open_attempt_blocks_a_new_attempt(self):
        self.start("exec_1")
        with self.assertRaises(IntegrityViolation) as ctx:
            self.start("exec_2")
        self.assertIs(ctx.exception.reason, ReasonCode.EXEC_GOAL_REJECTED)

    def test_plan_from_another_request_is_refused(self):
        self.repo.save_request(RequestRecord("req_2", "다른 요청", "2.0", 999.0))
        with self.assertRaises(IntegrityViolation):
            self.repo.begin_execution(
                execution_id="exec_x", request_id="req_2", plan_id="plan_1",
                adapter_id="a", policy_id="p", policy_version="1.0", started_at=1.0,
            )


class TestRetryHistory(RepoCase):
    def test_retry_gets_a_new_execution_id_and_attempt_no(self):
        self.start("exec_1")
        self.finish("exec_1", all_result_shapes()["task_failed"])
        self.start("exec_2")
        self.finish("exec_2", success({"lift_m": 0.01}))
        attempts = self.repo.executions_for_request("req_1")
        self.assertEqual([a.attempt_no for a in attempts], [1, 2])
        self.assertEqual([a.execution_id for a in attempts], ["exec_1", "exec_2"])

    def test_earlier_attempt_history_is_not_overwritten(self):
        self.start("exec_1")
        self.repo.append_observation("exec_1", kind="probe", observed_at=1.0, payload={"n": 1})
        self.finish("exec_1", all_result_shapes()["task_failed"])
        self.start("exec_2")
        self.finish("exec_2", success({}))
        first = self.repo.trace("exec_1")
        self.assertEqual(len(first.observations), 1)
        self.assertFalse(first.final_result.result.task_succeeded)
        self.assertTrue(self.repo.trace("exec_2").final_result.result.task_succeeded)

    def test_every_attempt_records_adapter_profile_and_policy(self):
        self.start("exec_1")
        rec = self.repo.get_execution("exec_1")
        self.assertEqual(rec.adapter_id, "adapter_fake")
        self.assertEqual((rec.profile_id, rec.profile_version), ("profile_a", "1.0"))
        self.assertEqual((rec.policy_id, rec.policy_version), ("policy_a", "1.0"))
        self.assertEqual(rec.schema_version, TASK_PLAN_SCHEMA_VERSION)
        self.assertEqual(rec.plan_hash, self.plan.plan_hash())


class TestStateTransitionHistory(RepoCase):
    def test_transitions_are_appended_in_order_with_from_state(self):
        self.start("exec_1")
        for state in (ExecutionState.ACCEPTED, ExecutionState.EXECUTING, ExecutionState.COMPLETED):
            self.repo.append_state_transition("exec_1", to_state=state, occurred_at=1.0)
        rows = self.repo.state_transitions("exec_1")
        self.assertEqual([r.seq for r in rows], [1, 2, 3])
        self.assertIsNone(rows[0].from_state)
        self.assertEqual(
            [(r.from_state, r.to_state) for r in rows[1:]],
            [
                (ExecutionState.ACCEPTED, ExecutionState.EXECUTING),
                (ExecutionState.EXECUTING, ExecutionState.COMPLETED),
            ],
        )

    def test_disallowed_transition_is_refused_at_write_time(self):
        self.start("exec_1")
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.UNKNOWN, occurred_at=1.0
        )
        with self.assertRaises(IntegrityViolation):
            self.repo.append_state_transition(
                "exec_1", to_state=ExecutionState.COMPLETED, occurred_at=2.0
            )

    def test_transition_reason_is_preserved(self):
        self.start("exec_1")
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.ACCEPTED, occurred_at=1.0
        )
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.STOPPING, occurred_at=2.0,
            reason=ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        )
        self.assertIs(
            self.repo.state_transitions("exec_1")[-1].reason,
            ReasonCode.EXEC_ENVIRONMENT_CHANGED,
        )


class TestObservationsAndResults(RepoCase):
    def test_observations_are_appended_not_overwritten(self):
        self.start("exec_1")
        for i in range(3):
            self.repo.append_observation(
                "exec_1", kind="joint_states", observed_at=float(i), payload={"j1": i}
            )
        rows = self.repo.observations("exec_1")
        self.assertEqual([r.seq for r in rows], [1, 2, 3])
        self.assertEqual([r.payload["j1"] for r in rows], [0, 1, 2])

    def test_results_are_appended_and_latest_is_final(self):
        self.start("exec_1")
        self.repo.append_result("exec_1", unverifiable(ReasonCode.EXEC_UNVERIFIABLE, {}), 1.0)
        self.repo.append_result("exec_1", all_result_shapes()["task_failed"], 2.0)
        rows = self.repo.results("exec_1")
        self.assertEqual([r.seq for r in rows], [1, 2])
        self.assertIs(self.repo.final_result("exec_1").result.reason, ReasonCode.EXEC_TASK_FAILED)

    def test_every_result_shape_round_trips(self):
        for name, result in all_result_shapes().items():
            with self.subTest(shape=name):
                self.start(f"e_{name}")
                self.finish(f"e_{name}", result)
                back = self.repo.final_result(f"e_{name}").result
                self.assertEqual(back.to_dict(), result.to_dict())
                self.assertIs(back.state, result.state)
                self.assertIs(back.reason, result.reason)

    def test_five_axes_are_stored_separately(self):
        self.start("exec_1")
        result = all_result_shapes()["goal_not_reached"]
        self.finish("exec_1", result)
        back = self.repo.final_result("exec_1").result
        self.assertTrue(back.request_accepted)
        self.assertTrue(back.motion_completed)
        self.assertFalse(back.target_reached)
        self.assertFalse(back.task_succeeded)
        self.assertTrue(back.verified)

    def test_unverifiable_does_not_become_success(self):
        self.start("exec_1")
        self.finish("exec_1", unverifiable(ReasonCode.EXEC_UNVERIFIABLE, {}))
        back = self.repo.final_result("exec_1").result
        self.assertFalse(back.task_succeeded)
        self.assertFalse(back.verified)
        self.assertIs(back.state, ExecutionState.UNKNOWN)

    def test_every_reason_code_round_trips(self):
        for code in ReasonCode:
            with self.subTest(code=code):
                eid = f"e_{code.value.replace('.', '_')}"
                self.start(eid)
                self.finish(eid, rejected(code, {}))
                self.assertIs(self.repo.final_result(eid).result.reason, code)


class TestValidationAndPermitHistory(RepoCase):
    def test_validation_results_are_appended_with_policy_version(self):
        for i, decision in enumerate(("block", "allow"), start=1):
            self.repo.save_validation(
                ValidationRecord(
                    validation_id=f"val_{i}", plan_id="plan_1",
                    plan_hash=self.plan.plan_hash(), decision=decision,
                    policy_id="policy_a", policy_version="1.0", evaluated_at=float(i),
                    rule_results=(
                        RuleResultRecord("E-HOLD-003", "block", ReasonCode.SAFETY_HOLD_INVALID, "m"),
                    ),
                )
            )
        rows = self.repo.validations_for_plan("plan_1")
        self.assertEqual([r.decision for r in rows], ["block", "allow"])
        self.assertIs(rows[0].rule_results[0].reason, ReasonCode.SAFETY_HOLD_INVALID)
        self.assertEqual(rows[0].policy_version, "1.0")

    def test_permit_reasons_are_all_preserved(self):
        self.repo.save_permit(
            PermitRecord(
                permit_id="permit_1", plan_id="plan_1", plan_hash=self.plan.plan_hash(),
                granted=False, policy_id="policy_a", policy_version="1.0", decided_at=1.0,
                reasons=(
                    PermitReasonRecord(ReasonCode.ROBOT_STATE_STALE, "오래된 관측"),
                    PermitReasonRecord(ReasonCode.EXEC_PERMIT_DENIED, "안전 판정 ASK"),
                ),
            )
        )
        rec = self.repo.permits_for_plan("plan_1")[0]
        self.assertFalse(rec.granted)
        self.assertEqual(
            [r.reason for r in rec.reasons],
            [ReasonCode.ROBOT_STATE_STALE, ReasonCode.EXEC_PERMIT_DENIED],
        )


class TestCorruptionDetection(RepoCase):
    """저장된 행이 변조되거나 계약에 어긋나면 조회에서 거부한다 (요구 14)."""

    def _raw(self, sql: str, params: tuple) -> None:
        self.repo._conn.execute(sql, params)

    def test_tampered_plan_json_is_detected_by_hash(self):
        self._raw(
            "UPDATE plans SET plan_json = ? WHERE plan_id = ?",
            ('{"schema_version":"2.0","plan_id":"plan_1","robot_id":"robot_a",'
             '"profile_id":"profile_a","profile_version":"1.0","created_at":1000.0,'
             '"ttl_sec":60.0,"utterance":"","terminal_hold":null,'
             '"steps":[{"skill":"home","args":{}}]}', "plan_1"),
        )
        with self.assertRaises(CorruptedRecord) as ctx:
            self.repo.get_plan("plan_1")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_HASH_MISMATCH)

    def test_searchable_column_out_of_sync_is_detected(self):
        self._raw("UPDATE plans SET profile_version = '9.9' WHERE plan_id = ?", ("plan_1",))
        with self.assertRaises(CorruptedRecord):
            self.repo.get_plan("plan_1")

    def test_unknown_state_is_rejected(self):
        self.start("exec_1")
        self.finish("exec_1", success({}))
        self._raw("UPDATE execution_results SET state = 'nope' WHERE execution_id = ?", ("exec_1",))
        with self.assertRaises(CorruptedRecord):
            self.repo.results("exec_1")

    def test_unknown_reason_is_rejected(self):
        self.start("exec_1")
        self.finish("exec_1", unverifiable(ReasonCode.EXEC_UNVERIFIABLE, {}))
        self._raw("UPDATE execution_results SET reason = 'x.y' WHERE execution_id = ?", ("exec_1",))
        with self.assertRaises(CorruptedRecord):
            self.repo.results("exec_1")

    def test_contract_violating_result_row_is_rejected(self):
        self.start("exec_1")
        self.finish("exec_1", unverifiable(ReasonCode.EXEC_UNVERIFIABLE, {}))
        self._raw(
            "UPDATE execution_results SET task_succeeded = 1 WHERE execution_id = ?", ("exec_1",)
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.results("exec_1")

    def test_broken_transition_order_is_rejected(self):
        self.start("exec_1")
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.ACCEPTED, occurred_at=1.0
        )
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.EXECUTING, occurred_at=2.0
        )
        self._raw(
            "UPDATE state_transitions SET to_state = 'idle' WHERE execution_id = ? AND seq = 2",
            ("exec_1",),
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.state_transitions("exec_1")

    def test_gap_in_transition_seq_is_rejected(self):
        self.start("exec_1")
        self.repo.append_state_transition(
            "exec_1", to_state=ExecutionState.ACCEPTED, occurred_at=1.0
        )
        self._raw(
            "UPDATE state_transitions SET seq = 5 WHERE execution_id = ?", ("exec_1",)
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.state_transitions("exec_1")

    def test_gap_in_attempt_no_is_rejected(self):
        self.start("exec_1")
        self._raw("UPDATE executions SET attempt_no = 7 WHERE execution_id = ?", ("exec_1",))
        with self.assertRaises(CorruptedRecord):
            self.repo.executions_for_request("req_1")


class TestApiRoundTrip(RepoCase):
    """API 경계 모델을 거친 왕복 (요구 15)."""

    def test_plan_json_to_storage_to_json(self):
        # plan_hash는 plan_id를 포함하지 않으므로, setUp의 계획과 내용이 같으면
        # `(request_id, plan_hash)` 유일성 제약에 걸린다. 별도 요청으로 저장한다.
        self.repo.save_request(
            RequestRecord("req_api", "API 왕복", TASK_PLAN_SCHEMA_VERSION, 998.0)
        )
        original = TaskPlanModel.from_core(make_plan(plan_id="plan_api")).model_dump(
            exclude={"plan_hash"}
        )
        plan = parse_task_plan(original).to_core()
        self.repo.save_plan("req_api", plan, 1_001.0)
        restored = TaskPlanModel.from_core(self.repo.get_plan("plan_api").plan)
        self.assertEqual(restored.model_dump(exclude={"plan_hash"}), original)
        self.assertEqual(restored.plan_hash, plan.plan_hash())

    def test_plan_hash_ignores_plan_id_so_uniqueness_catches_duplicates(self):
        """같은 요청에 내용이 같은 계획을 다른 id로 넣는 것을 막는다."""
        with self.assertRaises(IntegrityViolation):
            self.repo.save_plan("req_1", make_plan(plan_id="plan_other"), 1_001.0)

    def test_result_json_survives_storage(self):
        for name, result in all_result_shapes().items():
            with self.subTest(shape=name):
                before = ExecutionResultModel.from_core(result).model_dump()
                self.start(f"api_{name}")
                self.finish(f"api_{name}", result)
                after = ExecutionResultModel.from_core(
                    self.repo.final_result(f"api_{name}").result
                ).model_dump()
                self.assertEqual(after, before)

    def test_every_execution_state_round_trips_through_api_model(self):
        for state in ExecutionState:
            with self.subTest(state=state):
                needs_reason = state in (ExecutionState.FAILED, ExecutionState.UNKNOWN)
                result = ExecutionResult(
                    state=state, request_accepted=True,
                    verified=state is not ExecutionState.UNKNOWN,
                    reason=ReasonCode.EXEC_ABORTED if needs_reason else None,
                )
                eid = f"st_{state.value}"
                self.start(eid)
                self.finish(eid, result)
                back = self.repo.final_result(eid).result
                self.assertIs(back.state, state)
                self.assertEqual(ExecutionResultModel.from_core(back).state, state.value)


class TestLayeringHasNoSqlLeak(unittest.TestCase):
    """core·validation·API가 sqlite3를 직접 쓰지 않는다 (요구 1, 2, 3)."""

    ROOT = Path(__file__).resolve().parents[2]
    CLEAN_DIRS = ("core", "validation", "robots/base", "robots/fake", "server", "config", "stt")

    def test_no_sqlite3_import_outside_storage_backend(self):
        for d in self.CLEAN_DIRS:
            for path in sorted((self.ROOT / d).glob("*.py")):
                with self.subTest(file=f"{d}/{path.name}"):
                    text = path.read_text(encoding="utf-8")
                    self.assertNotIn("import sqlite3", text)
                    self.assertNotIn("sqlite3.", text)

    def test_no_sql_in_the_repository_interface(self):
        text = (self.ROOT / "storage" / "repository.py").read_text(encoding="utf-8").upper()
        for keyword in ("SELECT ", "INSERT ", "UPDATE ", "CREATE TABLE", "BEGIN IMMEDIATE"):
            with self.subTest(keyword=keyword):
                self.assertNotIn(keyword, text)

    def test_sql_lives_only_in_the_sqlite_package(self):
        for path in sorted((self.ROOT / "storage").glob("*.py")):
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8").upper()
                self.assertNotIn("CREATE TABLE", text)
                self.assertNotIn("SELECT ", text)


def inference(**over) -> SttInferenceRecord:
    kw = dict(
        stt_inference_id="stt_1", session_id="sess_1", attempt_no=1,
        schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=900.0,
        model_name="small", model_version="ct2-rev1",
        profile_id="lowspec-cpu-int8", profile_version="lowspec-1.0",
        verification="verified", device="cpu", compute_type="int8", language="ko",
        audio_duration_ms=2000, processing_duration_ms=5500,
        model_load_duration_ms=5380,
        transcript="obj_a를 집어서 들고 있어", confidence=0.91,
        confidence_metric="exp(avg_logprob) 길이가중평균",
        final_adopted=False, vad_speech_detected=True, vad_max_probability=0.97,
    )
    kw.update(over)
    return SttInferenceRecord(**kw)


class TestSttInferenceStorage(unittest.TestCase):
    """STT 실행 기록은 append-only다 (마이그레이션 0003)."""

    def setUp(self):
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)

    def test_requests_no_longer_carries_stt_measurement_columns(self):
        columns = {
            r[1] for r in self.repo._conn.execute("PRAGMA table_info(requests)")
        }
        for gone in ("transcript_confidence", "stt_model", "stt_language",
                     "stt_config_version", "stt_input_at", "stt_confirmed_at",
                     "stt_latency_sec", "needs_user_confirmation"):
            with self.subTest(column=gone):
                self.assertNotIn(gone, columns)
        self.assertIn("selected_stt_inference_id", columns)

    def test_legacy_0002_values_are_kept_not_fabricated(self):
        """0002 컬럼의 옛 값은 격리 테이블에 그대로 보존한다.

        옛 스키마에는 전사 본문·장치·음성 길이·처리 시간이 없어서 새 테이블로
        옮기면 측정값을 지어내야 한다. 그래서 옮기지 않고 보존한다.
        """
        tables = {
            r[0] for r in self.repo._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertIn("stt_metadata_legacy_0002", tables)
        columns = {
            r[1] for r in self.repo._conn.execute(
                "PRAGMA table_info(stt_metadata_legacy_0002)")
        }
        self.assertIn("stt_latency_sec", columns)
        self.assertIn("stt_config_version", columns)

    def test_partial_transcript_and_pcm_have_no_columns(self):
        columns = {
            r[1] for r in self.repo._conn.execute("PRAGMA table_info(stt_inferences)")
        }
        for banned in ("partial", "pcm", "audio_blob", "waveform", "samples"):
            with self.subTest(banned=banned):
                self.assertFalse([c for c in columns if banned in c])
        # 음성 길이는 숫자로만 남는다 — 원본은 설정된 별도 경로에 둔다.
        self.assertIn("audio_duration_ms", columns)

    def test_append_and_read_back_keeps_every_measured_field(self):
        self.repo.append_stt_inference(inference())
        got = self.repo.get_stt_inference("stt_1")
        self.assertEqual(got, inference())
        self.assertAlmostEqual(got.rtf, 2.75)
        self.assertAlmostEqual(got.throughput, 0.363636, places=6)
        self.assertEqual(got.model_load_duration_ms, 5380)

    def test_same_content_reappend_is_idempotent(self):
        self.repo.append_stt_inference(inference())
        self.repo.append_stt_inference(inference())
        self.assertEqual(len(self.repo.stt_inferences_for_session("sess_1")), 1)

    def test_different_content_under_the_same_id_is_refused(self):
        self.repo.append_stt_inference(inference())
        with self.assertRaises(IntegrityViolation):
            self.repo.append_stt_inference(inference(transcript="다른 전사"))
        self.assertEqual(self.repo.get_stt_inference("stt_1").transcript,
                         "obj_a를 집어서 들고 있어")

    def test_attempt_numbers_do_not_collide_within_a_session(self):
        self.repo.append_stt_inference(inference())
        with self.assertRaises(IntegrityViolation):
            self.repo.append_stt_inference(inference(stt_inference_id="stt_2"))

    def test_next_attempt_no_counts_up_per_session(self):
        self.assertEqual(self.repo.next_stt_attempt_no("sess_1"), 1)
        self.repo.append_stt_inference(inference())
        self.assertEqual(self.repo.next_stt_attempt_no("sess_1"), 2)
        self.assertEqual(self.repo.next_stt_attempt_no("sess_other"), 1)

    def test_model_comparison_keeps_every_profile_result(self):
        """같은 음성을 두 구성으로 돌린 결과가 모두 남는다."""
        self.repo.append_stt_inference(inference())
        self.repo.append_stt_inference(
            inference(
                stt_inference_id="stt_2", attempt_no=2,
                profile_id="turbo-auto-int8", profile_version="turbo-1.0",
                verification="candidate", model_name="large-v3-turbo", device="auto",
                processing_duration_ms=1800, model_load_duration_ms=None,
                confidence=0.95,
            )
        )
        rows = self.repo.stt_inferences_for_session("sess_1")
        self.assertEqual([r.profile_id for r in rows],
                         ["lowspec-cpu-int8", "turbo-auto-int8"])
        self.assertEqual([r.verification for r in rows], ["verified", "candidate"])
        self.assertAlmostEqual(rows[0].rtf, 2.75)
        self.assertAlmostEqual(rows[1].rtf, 0.9)

    def test_adopted_inference_links_the_request_atomically(self):
        request = RequestRecord(
            "req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0,
            selected_stt_inference_id="stt_1",
        )
        self.repo.save_request_with_stt_inference(
            request, inference(final_adopted=True, request_id="req_1")
        )
        self.assertEqual(self.repo.get_request("req_1").selected_stt_inference_id, "stt_1")
        selected = self.repo.selected_stt_inference("req_1")
        self.assertTrue(selected.final_adopted)
        self.assertEqual(selected.transcript, "obj_a를 집어서 들고 있어")

    def test_a_request_cannot_adopt_two_inferences(self):
        request = RequestRecord(
            "req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0,
            selected_stt_inference_id="stt_1",
        )
        self.repo.save_request_with_stt_inference(
            request, inference(final_adopted=True, request_id="req_1")
        )
        with self.assertRaises(IntegrityViolation):
            self.repo.append_stt_inference(
                inference(stt_inference_id="stt_2", attempt_no=2,
                          final_adopted=True, request_id="req_1")
            )

    def test_unadopted_attempts_stay_visible_for_the_request(self):
        request = RequestRecord(
            "req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0,
            selected_stt_inference_id="stt_2",
        )
        self.repo.save_request_with_stt_inference(
            request,
            inference(stt_inference_id="stt_2", attempt_no=2,
                      final_adopted=True, request_id="req_1"),
        )
        self.repo.append_stt_inference(
            inference(stt_inference_id="stt_1", attempt_no=1, request_id="req_1",
                      reason_code=ReasonCode.STT_LOW_CONFIDENCE, confidence=0.2)
        )
        rows = self.repo.stt_inferences_for_request("req_1")
        self.assertEqual(len(rows), 2)
        # 시도 순서대로 읽힌다 — 되묻기(1번)가 먼저, 채택된 시도(2번)가 뒤다.
        self.assertEqual([r.attempt_no for r in rows], [1, 2])
        self.assertEqual([r.final_adopted for r in rows], [False, True])

    def test_request_cannot_point_at_a_missing_inference(self):
        with self.assertRaises(StorageError):
            self.repo.save_request(
                RequestRecord("req_1", "x", TASK_PLAN_SCHEMA_VERSION, 1.0,
                              selected_stt_inference_id="ghost")
            )

    def test_text_request_has_no_selected_inference(self):
        self.repo.save_request(
            RequestRecord("req_text", "obj_a를 집어", TASK_PLAN_SCHEMA_VERSION, 1.0)
        )
        self.assertIsNone(self.repo.selected_stt_inference("req_text"))

    def test_tampered_rtf_copy_is_reported_as_corruption(self):
        """rtf는 원자료에서 파생한 사본이다. 어긋나면 조용히 고치지 않는다."""
        self.repo.append_stt_inference(inference())
        self.repo._conn.execute(
            "UPDATE stt_inferences SET rtf = 0.1 WHERE stt_inference_id = 'stt_1'"
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.get_stt_inference("stt_1")

    def test_unknown_reason_code_is_reported_as_corruption(self):
        self.repo.append_stt_inference(
            inference(reason_code=ReasonCode.STT_LOW_CONFIDENCE)
        )
        self.repo._conn.execute(
            "UPDATE stt_inferences SET reason_code = 'stt.made_up'"
            " WHERE stt_inference_id = 'stt_1'"
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.get_stt_inference("stt_1")

    def test_unknown_verification_value_is_refused_by_the_database(self):
        import sqlite3
        with self.assertRaises((IntegrityViolation, sqlite3.IntegrityError)):
            self.repo._conn.execute(
                "INSERT INTO stt_inferences"
                " (stt_inference_id, session_id, attempt_no, schema_version, created_at,"
                "  model_name, model_version, profile_id, profile_version, verification,"
                "  device, compute_type, language, audio_duration_ms,"
                "  processing_duration_ms, transcript, confidence, confidence_metric,"
                "  final_adopted)"
                " VALUES ('x','s',1,'2.0',1.0,'m','v','p','pv','probably-fine',"
                "  'cpu','int8','ko',1,1,'t',0.5,'m',0)"
            )

    def test_repository_contract_has_no_update_or_delete_for_inferences(self):
        from storage.repository import Repository
        names = [n for n in dir(Repository) if "stt_inference" in n]
        self.assertTrue(names)
        for name in names:
            with self.subTest(method=name):
                self.assertFalse(
                    name.startswith(("update_", "delete_", "replace_", "set_")),
                    f"append-only 계약에 갱신 메서드가 있다: {name}",
                )


def attempt(**over) -> PlanningAttemptRecord:
    kw = dict(
        planning_attempt_id="pa_1", request_id="req_1", attempt_no=1,
        schema_version=TASK_PLAN_SCHEMA_VERSION,
        resource_catalog_version="cell-demo-1.0", skill_catalog_version="atomic-5-1.0",
        started_at=1_000.0, ended_at=1_000.8, duration_ms=800,
        status=PlanningAttemptStatus.SUCCEEDED,
        provider_id="mock", model_id="mock-rule-based",
        prompt_template_version="plan-ko-1.0", output_schema_version="plan-out-1.0",
        plan_id="plan_1", plan_hash="hash_1", is_mock=True,
    )
    kw.update(over)
    return PlanningAttemptRecord(**kw)


class TestPlanningAttemptStorage(unittest.TestCase):
    """계획 생성 시도는 append-only다 (마이그레이션 0005)."""

    def setUp(self):
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)
        self.repo.save_request(
            RequestRecord("req_1", "obj_a를 집어서 들고 있어", TASK_PLAN_SCHEMA_VERSION, 999.0)
        )

    def test_round_trip_keeps_every_link_field(self):
        self.repo.append_planning_attempt(attempt())
        got = self.repo.get_planning_attempt("pa_1")
        self.assertEqual(got, attempt())
        for name in ("planning_attempt_id", "request_id", "attempt_no", "provider_id",
                     "model_id", "prompt_template_version", "output_schema_version",
                     "resource_catalog_version", "skill_catalog_version",
                     "started_at", "ended_at", "duration_ms", "status",
                     "plan_id", "plan_hash", "is_mock", "is_retry",
                     "previous_attempt_id", "stt_inference_id"):
            with self.subTest(field=name):
                self.assertTrue(hasattr(got, name))

    def test_same_content_reappend_is_idempotent(self):
        self.repo.append_planning_attempt(attempt())
        self.repo.append_planning_attempt(attempt())
        self.assertEqual(len(self.repo.planning_attempts_for_request("req_1")), 1)

    def test_retry_does_not_overwrite_the_previous_attempt(self):
        self.repo.append_planning_attempt(
            attempt(status=PlanningAttemptStatus.FAILED,
                    reason_code=ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
                    plan_id=None, plan_hash=None, is_mock=False,
                    provider_id="mock", model_id="mock-rule-based")
        )
        self.repo.append_planning_attempt(
            attempt(planning_attempt_id="pa_2", attempt_no=2, is_retry=True,
                    previous_attempt_id="pa_1", plan_id="plan_2", plan_hash="hash_2")
        )
        rows = self.repo.planning_attempts_for_request("req_1")
        self.assertEqual([r.planning_attempt_id for r in rows], ["pa_1", "pa_2"])
        self.assertIs(rows[0].status, PlanningAttemptStatus.FAILED)
        self.assertEqual(rows[1].previous_attempt_id, "pa_1")

    def test_different_content_under_the_same_id_is_refused(self):
        self.repo.append_planning_attempt(attempt())
        with self.assertRaises(IntegrityViolation):
            self.repo.append_planning_attempt(attempt(duration_ms=999))

    def test_attempt_numbers_do_not_collide_within_a_request(self):
        self.repo.append_planning_attempt(attempt())
        with self.assertRaises(IntegrityViolation):
            self.repo.append_planning_attempt(attempt(planning_attempt_id="pa_2"))

    def test_next_attempt_no_counts_up_per_request(self):
        self.assertEqual(self.repo.next_planning_attempt_no("req_1"), 1)
        self.repo.append_planning_attempt(attempt())
        self.assertEqual(self.repo.next_planning_attempt_no("req_1"), 2)
        self.assertEqual(self.repo.next_planning_attempt_no("req_other"), 1)

    def test_plan_can_be_traced_back_to_its_attempt(self):
        self.repo.append_planning_attempt(attempt())
        found = self.repo.planning_attempt_for_plan("plan_1")
        self.assertIsNotNone(found)
        self.assertEqual(found.planning_attempt_id, "pa_1")
        self.assertIsNone(self.repo.planning_attempt_for_plan("plan_nope"))

    def test_two_attempts_cannot_claim_the_same_plan(self):
        self.repo.append_planning_attempt(attempt())
        with self.assertRaises(IntegrityViolation):
            self.repo.append_planning_attempt(
                attempt(planning_attempt_id="pa_2", attempt_no=2, is_retry=True,
                        previous_attempt_id="pa_1")
            )

    def test_mock_flag_survives_the_round_trip(self):
        self.repo.append_planning_attempt(attempt(is_mock=True))
        self.assertTrue(self.repo.get_planning_attempt("pa_1").is_mock)

    def test_mock_and_real_attempts_can_be_counted_separately(self):
        """Mock 결과를 실제 LLM 성공으로 집계하지 않는다."""
        self.repo.append_planning_attempt(attempt(is_mock=True))
        self.repo.append_planning_attempt(
            attempt(planning_attempt_id="pa_2", attempt_no=2, is_mock=False,
                    provider_id="vllm-openai", model_id="qwen3-candidate",
                    plan_id="plan_2", plan_hash="hash_2")
        )
        rows = self.repo.planning_attempts_for_request("req_1")
        real = [r for r in rows if not r.is_mock]
        self.assertEqual(len(real), 1)
        self.assertEqual(real[0].provider_id, "vllm-openai")

    def test_stop_bypass_has_no_provider(self):
        self.repo.append_planning_attempt(
            attempt(status=PlanningAttemptStatus.STOP_BYPASS, provider_id=None,
                    model_id=None, prompt_template_version=None,
                    output_schema_version=None, is_mock=False)
        )
        got = self.repo.get_planning_attempt("pa_1")
        self.assertIsNone(got.provider_id)
        self.assertEqual(got.plan_id, "plan_1")

    def test_payload_is_stored_with_its_version(self):
        payload = PlanningPayload(
            payload_version="planning-payload-1.0",
            body_json='{"utterance": "obj_a를 집어"}', truncated=False,
            retention_days=30,
        )
        self.repo.append_planning_attempt(attempt(payload=payload))
        got = self.repo.get_planning_attempt("pa_1").payload
        self.assertEqual(got, payload)

    def test_attempt_without_payload_round_trips_as_none(self):
        self.repo.append_planning_attempt(attempt(payload=None))
        self.assertIsNone(self.repo.get_planning_attempt("pa_1").payload)

    def test_stt_inference_link_is_enforced(self):
        import sqlite3
        with self.assertRaises((IntegrityViolation, sqlite3.IntegrityError)):
            self.repo.append_planning_attempt(attempt(stt_inference_id="stt_ghost"))

    def test_unknown_status_is_refused_by_the_database(self):
        """상태 값은 테이블 CHECK가 먼저 막는다."""
        import sqlite3
        self.repo.append_planning_attempt(attempt())
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo._conn.execute(
                "UPDATE planning_attempts SET status = 'maybe'"
                " WHERE planning_attempt_id = 'pa_1'"
            )

    def test_unknown_reason_code_in_the_row_is_corruption(self):
        """CHECK를 통과하지만 계약에 없는 값은 읽을 때 잡는다."""
        self.repo.append_planning_attempt(
            attempt(status=PlanningAttemptStatus.FAILED,
                    reason_code=ReasonCode.PLAN_LLM_TIMEOUT,
                    plan_id=None, plan_hash=None, is_mock=False)
        )
        self.repo._conn.execute(
            "UPDATE planning_attempts SET reason_code = 'plan.made_up'"
            " WHERE planning_attempt_id = 'pa_1'"
        )
        with self.assertRaises(CorruptedRecord):
            self.repo.get_planning_attempt("pa_1")

    def test_contract_has_no_update_or_delete_for_attempts(self):
        from storage.repository import Repository
        names = [n for n in dir(Repository) if "planning_attempt" in n]
        self.assertTrue(names)
        for name in names:
            with self.subTest(method=name):
                self.assertFalse(name.startswith(("update_", "delete_", "replace_", "set_")))


class TestLegacySttMetadataStaysSeparate(unittest.TestCase):
    """0002 자료는 legacy다. canonical stt_inferences와 섞지 않는다."""

    def setUp(self):
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)
        self.repo.save_request(
            RequestRecord("req_1", "obj_a를 집어", TASK_PLAN_SCHEMA_VERSION, 999.0)
        )

    def _insert_legacy(self):
        self.repo._conn.execute(
            "INSERT INTO stt_metadata_legacy_0002"
            " (request_id, transcript_confidence, stt_model, stt_language,"
            "  stt_config_version, stt_input_at, stt_confirmed_at, stt_latency_sec)"
            " VALUES ('req_1', 0.8, 'large-v3-turbo', 'ko', 'old-1.0', 1.0, 1.4, 0.4)"
        )

    def test_legacy_rows_are_reachable_only_through_the_legacy_accessor(self):
        self._insert_legacy()
        legacy = self.repo.legacy_stt_metadata("req_1")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy["source"], "migration_0002_legacy")
        self.assertEqual(legacy["stt_config_version"], "old-1.0")

    def test_legacy_rows_do_not_appear_in_canonical_queries(self):
        self._insert_legacy()
        self.assertEqual(self.repo.stt_inferences_for_request("req_1"), ())
        self.assertEqual(self.repo.stt_inferences_for_session("req_1"), ())
        self.assertIsNone(self.repo.selected_stt_inference("req_1"))
        from storage.records import SttExecutionPath
        self.assertEqual(
            self.repo.stt_inferences_by_path(SttExecutionPath.OPERATIONAL), ()
        )

    def test_legacy_values_are_not_converted_into_inference_records(self):
        """없는 측정값을 만들어 이관하지 않는다."""
        self._insert_legacy()
        legacy = self.repo.legacy_stt_metadata("req_1")
        for absent in ("transcript", "audio_duration_ms", "processing_duration_ms",
                       "device", "compute_type", "rtf"):
            with self.subTest(field=absent):
                self.assertNotIn(absent, legacy)

    def test_canonical_queries_do_not_read_the_legacy_table(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "storage" / "sqlite" / "repository.py"
        ).read_text(encoding="utf-8")
        canonical = [
            line for line in source.splitlines()
            if "stt_metadata_legacy_0002" in line
        ]
        # 이 테이블을 읽는 SQL은 legacy 접근자 안에만 있다.
        self.assertEqual(len(canonical), 1, canonical)
        self.assertIn("SELECT * FROM stt_metadata_legacy_0002", canonical[0])

    def test_missing_legacy_row_is_none_not_an_error(self):
        self.assertIsNone(self.repo.legacy_stt_metadata("req_1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
