"""Mock 기반 계획 생성 → 저장 → 안전 검증 → 실행 허가 (md/개발플랜.md 5-01).

**실제 LLM을 쓰지 않는다.** Mock Provider로 경로 전체가 이어지는지 본다.
확인 대상:
- 계획 생성 성공이 실행 승인이 아니다. SafetyValidator와 ExecutionPermit을
  지나야 실행 대상이 된다.
- 시도 기록이 DB에 남고, 계획에서 시도로 거꾸로 추적된다.
- Mock 결과가 실제 LLM 성공이나 운영 검증 완료로 보이지 않는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_capability_profile,
    load_resource_catalog,
    load_skill_catalog,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.policy import FreshnessPolicy, PlanningPolicy, SafetyPolicy
from core.reason_codes import ReasonCode
from core.termination import termination_requirement
from planning.attempt_runner import persist_attempts, run_planning
from planning.mock_provider import MockPlanProvider
from storage.records import PlanningAttemptStatus, RequestRecord
from storage.sqlite.repository import SqliteRepository
from validation.execution_permit import PermitContext, check_execution_permit
from validation.safety_validator import RuleStatus, SafetyDecision, aggregate, evaluate

CONFIG = ROOT / "examples" / "config"
STOP_KEYWORDS = ("정지", "멈춰")
TRANSFER = "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"


def load(name):
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


class MockPlanningFlow(unittest.TestCase):
    def setUp(self):
        self.resources = load_resource_catalog(load("valid_resource_catalog.json"))
        self.skills = load_skill_catalog(load("valid_skill_catalog.json"))
        self.profile = load_capability_profile(load("valid_capability_profile.json"))
        self.safety_policy = SafetyPolicy(
            "fixture", 12,
            {"max_steps": "fixture", "required_final_skill": "fixture"},
            required_final_skill="home",
        )
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)
        self.repo.save_request(
            RequestRecord("req_1", TRANSFER, TASK_PLAN_SCHEMA_VERSION, 999.0)
        )
        self.plan_ids = iter(f"plan_{i}" for i in range(1, 20))
        self.policy = PlanningPolicy(
            policy_version="fixture-1.0", max_attempts=2, request_timeout_sec=10.0,
            retryable_reasons=(ReasonCode.PLAN_LLM_UNAVAILABLE,),
            store_payloads=True, payload_max_chars=4000, payload_retention_days=14,
            provenance={
                k: "fixture" for k in
                ("max_attempts", "request_timeout_sec", "retryable_reasons")
            },
        )

    def plan_it(self, utterance=TRANSFER, *, stt_inference_id=None):
        run = run_planning(
            utterance, request_id="req_1", catalog=self.resources,
            skill_catalog=self.skills, profile=self.profile,
            provider=MockPlanProvider(
                hold_keywords=("들고 있어",),
                termination=termination_requirement(
                    profile=self.profile, safety_policy=self.safety_policy
                ),
            ),
            policy=self.policy, robot_id="robot_fake",
            plan_id_factory=lambda: next(self.plan_ids),
            attempt_id_factory=lambda n: f"pa_{n}",
            now_utc=lambda: 1_000.0, clock=lambda: 0.0, ttl_sec=60.0,
            stop_keywords=STOP_KEYWORDS, schema_version=TASK_PLAN_SCHEMA_VERSION,
            stt_inference_id=stt_inference_id,
        )
        persist_attempts(self.repo, run.attempts)
        return run

    def safety(self, plan):
        results = evaluate(plan, self.safety_policy, self.resources)
        return aggregate(results), results

    def permit(self, plan, results, **over):
        kw = dict(
            now=1_000.5, robot_ready=True, robot_state_observed_at=1_000.4,
            robot_state_valid=True, environment_observed_at=1_000.4,
            recorded_environment_version=3, current_environment_version=3,
            recorded_environment_session="sess_a", current_environment_session="sess_a",
            recorded_plan_hash=plan.plan_hash(), profile_id=self.profile.profile_id,
            profile_version=self.profile.profile_version,
            supported_skills=self.profile.supported_skills,
        )
        kw.update(over)
        return check_execution_permit(
            plan, results, PermitContext(**kw),
            FreshnessPolicy(
                "fixture", 5.0, 5.0,
                {"robot_state_max_age_sec": "fixture",
                 "environment_max_age_sec": "fixture"},
            ),
        )


class TestMockPathReachesAPermit(MockPlanningFlow):
    def test_plan_then_safety_then_permit(self):
        run = self.plan_it()
        self.assertTrue(run.ok)
        decision, results = self.safety(run.outcome.plan)
        self.assertIs(decision, SafetyDecision.ALLOW,
                      [r.message for r in results if r.status is not RuleStatus.PASS])
        permit = self.permit(run.outcome.plan, results)
        self.assertTrue(permit.granted, permit.reason_codes())

    def test_planning_success_alone_is_not_a_permit(self):
        """계획이 만들어졌다는 것만으로 실행 대상이 되지 않는다."""
        run = self.plan_it()
        _, results = self.safety(run.outcome.plan)
        denied = self.permit(run.outcome.plan, results, robot_ready=False)
        self.assertFalse(denied.granted)
        self.assertIn(ReasonCode.ROBOT_NOT_CONNECTED, denied.reason_codes())

    def test_stale_state_denies_the_permit_even_with_a_valid_plan(self):
        run = self.plan_it()
        _, results = self.safety(run.outcome.plan)
        denied = self.permit(
            run.outcome.plan, results, robot_state_observed_at=1.0, now=1_000.5
        )
        self.assertFalse(denied.granted)
        self.assertIn(ReasonCode.ROBOT_STATE_STALE, denied.reason_codes())

    def test_stop_plan_also_goes_through_validation(self):
        run = self.plan_it("지금 멈춰")
        self.assertTrue(run.ok)
        self.assertIs(run.attempts[0].status, PlanningAttemptStatus.STOP_BYPASS)
        decision, results = self.safety(run.outcome.plan)
        # stop 단독 계획은 home으로 끝나지 않으므로 안전 검증에서 걸린다 —
        # 정지는 계획 실행 경로가 아니라 STOP 계약으로 처리한다는 뜻이다.
        self.assertIs(decision, SafetyDecision.BLOCK)
        self.assertIn(
            ReasonCode.SAFETY_SEQUENCE_INVALID,
            {r.reason for r in results if r.reason is not None},
        )


class TestAttemptsArePersisted(MockPlanningFlow):
    def test_attempt_is_stored_and_linked_to_the_plan(self):
        run = self.plan_it()
        stored = self.repo.planning_attempts_for_request("req_1")
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].plan_id, run.outcome.plan.plan_id)
        back = self.repo.planning_attempt_for_plan(run.outcome.plan.plan_id)
        self.assertEqual(back.planning_attempt_id, "pa_1")

    def test_plan_hash_in_the_attempt_matches_the_plan(self):
        run = self.plan_it()
        stored = self.repo.get_planning_attempt("pa_1")
        self.assertEqual(stored.plan_hash, run.outcome.plan.plan_hash())

    def test_stored_attempt_keeps_the_mock_flag(self):
        self.plan_it()
        self.assertTrue(self.repo.get_planning_attempt("pa_1").is_mock)

    def test_saving_the_plan_does_not_change_the_attempt(self):
        run = self.plan_it()
        self.repo.save_plan("req_1", run.outcome.plan, 1_000.9)
        self.assertEqual(self.repo.get_planning_attempt("pa_1").plan_id,
                         run.outcome.plan.plan_id)

    def test_payload_has_no_secrets(self):
        self.plan_it()
        payload = self.repo.get_planning_attempt("pa_1").payload
        self.assertIsNotNone(payload)
        for banned in ("api_key", "authorization", "bearer", "sk-"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, payload.body_json.lower())

    def test_failed_attempt_is_stored_with_its_reason(self):
        run = run_planning(
            "그거 저기로", request_id="req_1", catalog=self.resources,
            skill_catalog=self.skills, profile=self.profile,
            provider=MockPlanProvider(
                termination=termination_requirement(
                    profile=self.profile, safety_policy=self.safety_policy
                ),
            ),
            policy=self.policy, robot_id="robot_fake",
            plan_id_factory=lambda: next(self.plan_ids),
            attempt_id_factory=lambda n: f"pa_f{n}",
            now_utc=lambda: 1_000.0, clock=lambda: 0.0, ttl_sec=60.0,
            stop_keywords=STOP_KEYWORDS, schema_version=TASK_PLAN_SCHEMA_VERSION,
        )
        persist_attempts(self.repo, run.attempts)
        self.assertFalse(run.ok)
        stored = self.repo.get_planning_attempt("pa_f1")
        self.assertIs(stored.status, PlanningAttemptStatus.FAILED)
        self.assertIs(stored.reason_code, ReasonCode.PLAN_SLOT_INCOMPLETE)
        self.assertIsNone(stored.plan_id)

    def test_stt_inference_link_is_kept_when_present(self):
        from storage.records import SttInferenceRecord

        self.repo.append_stt_inference(
            SttInferenceRecord(
                stt_inference_id="stt_1", session_id="sess_1", attempt_no=1,
                schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=998.0,
                model_name="small", model_version="rev", profile_id="lowspec-cpu-int8",
                profile_version="lowspec-1.0", verification="verified", device="cpu",
                compute_type="int8", language="ko", audio_duration_ms=1500,
                processing_duration_ms=4100, transcript=TRANSFER, confidence=0.88,
                confidence_metric="exp(avg_logprob) 길이가중평균", final_adopted=False,
                request_id="req_1",
            )
        )
        self.plan_it(stt_inference_id="stt_1")
        self.assertEqual(
            self.repo.get_planning_attempt("pa_1").stt_inference_id, "stt_1"
        )


class TestMockIsNotOperationalVerification(MockPlanningFlow):
    def test_no_attempt_claims_a_real_provider(self):
        self.plan_it()
        rows = self.repo.planning_attempts_for_request("req_1")
        self.assertTrue(all(r.is_mock for r in rows))
        self.assertTrue(all(r.provider_id == "mock" for r in rows))

    def test_real_provider_attempts_are_absent(self):
        """실제 LLM 검증 기록이 없다. Mock 통과를 실제 검증으로 읽지 않는다."""
        self.plan_it()
        rows = self.repo.planning_attempts_for_request("req_1")
        self.assertEqual([r for r in rows if not r.is_mock], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
