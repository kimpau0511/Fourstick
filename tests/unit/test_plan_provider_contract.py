"""LLM PlanProvider 경계 검증 (md/개발플랜.md 5-01).

**실제 LLM을 호출하지 않는다.** 여기서 확인하는 것은 계약이다.
- Provider 인터페이스가 공급자 SDK와 분리돼 있다.
- 모델 출력을 믿지 않고 구조 검증을 거친다.
- 파싱 실패·스키마 위반·버전 불일치·미지원 스킬·미등록 리소스·타임아웃·
  공급자 오류가 **서로 다른 이유 코드**로 구분된다.
- 프롬프트 템플릿과 출력 스키마에 버전이 있다.
- 재시도가 정책 안에서만 일어난다.
- 비밀정보가 기록에 남지 않는다.
"""

from __future__ import annotations

import ast
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_capability_profile,
    load_resource_catalog,
    load_skill_catalog,
)
from core.constants import ATOMIC_SKILLS, TASK_PLAN_SCHEMA_VERSION
from core.policy import PlanningPolicy, PolicyError
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceKind
from core.skill_catalog import SkillCatalog, SkillCatalogError, SkillEntry
from planning.attempt_runner import PAYLOAD_VERSION, run_planning
from planning.mock_provider import MockPlanProvider
from planning.output_parser import draft_from_output, parse_output
from planning.plan_provider import (
    DraftStep,
    PlanningContext,
    PlanningMode,
    PlanProviderError,
)
from planning.prompt import (
    OUTPUT_SCHEMA_VERSION,
    PROMPT_TEMPLATE_VERSION,
    output_json_schema,
    render_prompt,
)
from planning.redaction import REDACTED, looks_secret, redact, truncate
from planning.slot_extractor import extract_slots
from storage.records import PlanningAttemptStatus

CONFIG = ROOT / "examples" / "config"
STOP_KEYWORDS = ("정지", "멈춰")



def imported_modules(path: Path) -> set[str]:
    """모듈이 실제로 import하는 이름. 주석·docstring은 보지 않는다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def code_strings(path: Path) -> str:
    """코드 안의 문자열 리터럴만 이어붙인다. docstring은 뺀다."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                out.append(node.value)
    return "\n".join(out)


def load(name):
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


def catalogs():
    return (
        load_resource_catalog(load("valid_resource_catalog.json")),
        load_skill_catalog(load("valid_skill_catalog.json")),
        load_capability_profile(load("valid_capability_profile.json")),
    )


def policy(**over) -> PlanningPolicy:
    kw = dict(
        policy_version="fixture-1.0", max_attempts=2, request_timeout_sec=10.0,
        retryable_reasons=(
            ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
            ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID,
            ReasonCode.PLAN_LLM_UNAVAILABLE,
        ),
        store_payloads=True, payload_max_chars=4000, payload_retention_days=30,
        provenance={
            k: "fixture" for k in
            ("max_attempts", "request_timeout_sec", "retryable_reasons")
        },
    )
    kw.update(over)
    return PlanningPolicy(**kw)


def safety_policy():
    from core.policy import SafetyPolicy

    return SafetyPolicy(
        "fixture", 12,
        {"max_steps": "fixture", "required_final_skill": "fixture",
         "approach_skill": "fixture"},
        required_final_skill="home", approach_skill="move",
    )


def termination(profile=None):
    from core.termination import termination_requirement

    return termination_requirement(
        profile=profile or catalogs()[2], safety_policy=safety_policy()
    )


def approach(profile=None):
    from core.termination import approach_requirement

    return approach_requirement(
        skill_catalog=catalogs()[1], safety_policy=safety_policy(),
        profile=profile or catalogs()[2],
    )


def context(utterance="1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘"):
    resources, skills, profile = catalogs()
    return PlanningContext(
        utterance=utterance, slots=extract_slots(utterance, resources),
        allowed_skills=profile.supported_skills,
        allowed_locations=resources.ids_of_kind(ResourceKind.LOCATION),
        allowed_objects=resources.ids_of_kind(ResourceKind.OBJECT),
        robot_id="robot_fake", profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        catalog_version=resources.catalog_version,
        schema_version=TASK_PLAN_SCHEMA_VERSION,
    )


def good_output(**over) -> dict:
    payload = {
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "result": "plan",
        "steps": [
            {"skill": "move", "args": {"to": "loc_pallet_1"}},
            {"skill": "pick", "args": {"object": "obj_a", "from": "loc_pallet_1"}},
            {"skill": "home", "args": {}},
        ],
        "terminal_hold": "obj_a",
    }
    payload.update(over)
    return payload


def draft_of(payload, **over):
    kw = dict(
        provider_id="scripted", model_id="scripted-model",
        prompt_template_version=PROMPT_TEMPLATE_VERSION,
        output_schema_version=OUTPUT_SCHEMA_VERSION,
        mode=PlanningMode.JSON_SCHEMA, latency_sec=0.1,
    )
    kw.update(over)
    return draft_from_output(payload, **kw)


class TestSkillCatalogGuardsTheContract(unittest.TestCase):
    def test_catalog_covers_every_atomic_skill(self):
        _, skills, _ = catalogs()
        self.assertEqual(set(skills.names()), set(ATOMIC_SKILLS))

    def test_catalog_cannot_add_a_skill_outside_the_contract(self):
        with self.assertRaises(SkillCatalogError) as ctx:
            SkillEntry(skill="weld", description="용접", arg_kinds={})
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_UNSUPPORTED_SKILL)

    def test_catalog_cannot_add_an_arg_outside_the_contract(self):
        with self.assertRaises(SkillCatalogError) as ctx:
            SkillEntry(
                skill="move", description="이동",
                arg_kinds={"to": ResourceKind.LOCATION, "speed": ResourceKind.LOCATION},
            )
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_ARG_UNKNOWN)

    def test_required_args_come_from_the_contract_not_the_catalog(self):
        _, skills, _ = catalogs()
        self.assertEqual(skills.get("pick").required_args, ("object", "from"))

    def test_unknown_skill_lookup_is_refused(self):
        _, skills, _ = catalogs()
        with self.assertRaises(SkillCatalogError):
            skills.get("weld")

    def test_arg_kinds_are_declared_for_grounding(self):
        _, skills, _ = catalogs()
        self.assertIs(skills.arg_kind("move", "to"), ResourceKind.LOCATION)
        self.assertIs(skills.arg_kind("pick", "object"), ResourceKind.OBJECT)


class TestVersioning(unittest.TestCase):
    def test_prompt_and_output_schema_carry_versions(self):
        rendering = render_prompt(
            context(), resource_catalog=catalogs()[0], skill_catalog=catalogs()[1],
            termination=termination(), approach=approach(),
        )
        self.assertEqual(rendering.template_version, PROMPT_TEMPLATE_VERSION)
        self.assertEqual(rendering.output_schema_version, OUTPUT_SCHEMA_VERSION)
        self.assertTrue(PROMPT_TEMPLATE_VERSION and OUTPUT_SCHEMA_VERSION)

    def test_prompt_lists_only_catalog_values(self):
        resources, skills, _ = catalogs()
        rendering = render_prompt(
            context(), resource_catalog=resources, skill_catalog=skills,
            termination=termination(), approach=approach(),
        )
        for rid in resources.ids_of_kind(ResourceKind.LOCATION):
            self.assertIn(rid, rendering.system)
        for skill in skills.names():
            self.assertIn(skill, rendering.system)
        # 카탈로그에 없는 셀 이름·로봇 수치가 프롬프트에 박혀 있지 않다.
        for banned in ("UR5e", "FR3", "joint", "0.85", "radian"):
            self.assertNotIn(banned, rendering.system)

    def test_prompt_has_no_endpoint_or_credentials(self):
        """프롬프트 모듈은 전송을 모른다. 자격정보도 없다.

        주석·docstring이 아니라 실제 import와 코드 문자열만 본다 — 문서에
        "5-02의 클라이언트가 전송한다"고 쓰는 것은 문제가 아니다.
        """
        path = ROOT / "planning" / "prompt.py"
        for banned in ("requests", "httpx", "openai", "urllib", "socket", "os"):
            with self.subTest(module=banned):
                self.assertNotIn(banned, imported_modules(path))
        literals = code_strings(path).lower()
        for banned in ("api_key", "bearer", "authorization", "localhost", ":8000"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, literals)

    def test_output_schema_restricts_skills_to_the_catalog(self):
        schema = output_json_schema(catalogs()[1], catalogs()[0])
        self.assertEqual(
            set(schema["properties"]["steps"]["items"]["properties"]["skill"]["enum"]),
            set(ATOMIC_SKILLS),
        )
        self.assertFalse(schema["additionalProperties"])

    def test_version_mismatch_in_output_is_its_own_reason_code(self):
        with self.assertRaises(PlanProviderError) as ctx:
            parse_output(
                good_output(output_schema_version="plan-out-0.9"),
                expected_schema_version=OUTPUT_SCHEMA_VERSION,
            )
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_PROMPT_VERSION_MISMATCH)


class TestOutputIsNotTrusted(unittest.TestCase):
    """LLM 출력은 Pydantic 구조 검증을 통과해야 초안이 된다."""

    def parse(self, raw):
        return parse_output(raw, expected_schema_version=OUTPUT_SCHEMA_VERSION)

    def test_valid_json_text_is_accepted(self):
        parsed = self.parse(json.dumps(good_output(), ensure_ascii=False))
        self.assertEqual([s.skill for s in parsed["steps"]], ["move", "pick", "home"])
        self.assertEqual(parsed["terminal_hold"], "obj_a")

    def test_code_fence_is_stripped_but_nothing_else_is_fixed(self):
        raw = "```json\n" + json.dumps(good_output()) + "\n```"
        self.assertTrue(self.parse(raw)["steps"])

    def test_broken_json_is_unparseable(self):
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse("계획: move 하세요")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE)

    def test_json_array_is_unparseable_as_a_plan(self):
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse("[1, 2, 3]")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE)

    def test_missing_steps_is_a_schema_violation(self):
        payload = good_output()
        del payload["steps"]
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse(payload)
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID)

    def test_empty_steps_is_a_schema_violation(self):
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse(good_output(steps=[]))
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID)

    def test_extra_field_is_a_schema_violation(self):
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse(good_output(confidence=0.9))
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID)

    def test_numeric_arg_is_refused_not_coerced(self):
        """좌표·숫자를 인자로 넣을 수 없다. strict 모드라 문자열로 바꿔주지 않는다."""
        with self.assertRaises(PlanProviderError) as ctx:
            self.parse(good_output(steps=[{"skill": "move", "args": {"to": 3}}]))
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID)

    def test_draft_records_the_versions_and_identity(self):
        draft = draft_of(good_output())
        self.assertEqual(draft.provider_id, "scripted")
        self.assertEqual(draft.model_name, "scripted-model")
        self.assertEqual(draft.prompt_template_version, PROMPT_TEMPLATE_VERSION)
        self.assertEqual(draft.output_schema_version, OUTPUT_SCHEMA_VERSION)
        self.assertFalse(draft.is_mock)


class TestPolicyBoundsRetries(unittest.TestCase):
    def test_max_attempts_is_required_to_be_at_least_one(self):
        with self.assertRaises(PolicyError):
            policy(max_attempts=0)

    def test_policy_needs_provenance(self):
        with self.assertRaises(PolicyError) as ctx:
            policy(provenance={"max_attempts": "fixture"})
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)

    def test_retryable_reason_is_retried_within_the_limit(self):
        p = policy(max_attempts=3)
        self.assertTrue(p.should_retry(ReasonCode.PLAN_LLM_UNAVAILABLE, 1))
        self.assertTrue(p.should_retry(ReasonCode.PLAN_LLM_UNAVAILABLE, 2))
        self.assertFalse(p.should_retry(ReasonCode.PLAN_LLM_UNAVAILABLE, 3))

    def test_non_retryable_reason_is_not_retried(self):
        self.assertFalse(
            policy().should_retry(ReasonCode.PLAN_UNKNOWN_RESOURCE, 1),
            "미등록 리소스는 같은 입력에서 같은 결과가 나온다",
        )

    def test_success_is_not_retried(self):
        self.assertFalse(policy().should_retry(None, 1))

    def test_payload_settings_are_required_when_storing(self):
        with self.assertRaises(PolicyError):
            policy(store_payloads=True, payload_max_chars=0)
        with self.assertRaises(PolicyError):
            policy(store_payloads=True, payload_retention_days=0)


class TestRedaction(unittest.TestCase):
    def test_secret_keys_are_replaced_entirely(self):
        body = redact({"api_key": "sk-abcdef123456", "model": "qwen"})
        self.assertEqual(body["api_key"], REDACTED)
        self.assertEqual(body["model"], "qwen")

    def test_authorization_header_is_removed(self):
        body = redact({"headers": {"Authorization": "Bearer abc.def"}})
        self.assertEqual(body["headers"]["Authorization"], REDACTED)

    def test_bearer_inside_a_value_is_removed(self):
        self.assertNotIn("abc.def", redact("헤더: Bearer abc.def"))

    def test_environment_dumps_are_treated_as_secret(self):
        self.assertTrue(looks_secret("environ"))
        self.assertTrue(looks_secret("ENV_VARS"))
        self.assertEqual(redact({"environ": {"PATH": "/usr/bin"}})["environ"], REDACTED)

    def test_nested_structures_are_walked(self):
        body = redact({"calls": [{"token": "t"}, {"prompt": "안녕"}]})
        self.assertEqual(body["calls"][0]["token"], REDACTED)
        self.assertEqual(body["calls"][1]["prompt"], "안녕")

    def test_truncation_is_reported(self):
        text, truncated = truncate("0123456789", 4)
        self.assertEqual(text, "0123")
        self.assertTrue(truncated)
        self.assertEqual(truncate("짧다", 100), ("짧다", False))


class TestRunnerRecordsEveryAttempt(unittest.TestCase):
    def setUp(self):
        self.resources, self.skills, self.profile = catalogs()
        self.plan_ids = iter(f"plan_{i}" for i in range(1, 50))
        self.utc = iter(1_000.0 + i for i in range(50))
        self.mono = iter(float(i) / 10 for i in range(200))

    def run_it(self, provider, *, utterance="1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
               pol=None, stt_inference_id=None):
        return run_planning(
            utterance, request_id="req_1", catalog=self.resources,
            skill_catalog=self.skills, profile=self.profile, provider=provider,
            policy=pol or policy(), robot_id="robot_fake",
            plan_id_factory=lambda: next(self.plan_ids),
            attempt_id_factory=lambda n: f"attempt_{n}",
            now_utc=lambda: next(self.utc), clock=lambda: next(self.mono),
            ttl_sec=60.0, stop_keywords=STOP_KEYWORDS,
            schema_version=TASK_PLAN_SCHEMA_VERSION,
            stt_inference_id=stt_inference_id,
        )

    class Scripted:
        """정해진 결과를 순서대로 돌려준다."""

        def __init__(self, outcomes):
            self._outcomes = list(outcomes)
            self.calls = 0

        provider_id = "scripted"
        model_name = "scripted-model"
        is_mock = False
        prompt_template_version = PROMPT_TEMPLATE_VERSION
        output_schema_version = OUTPUT_SCHEMA_VERSION

        def generate(self, context):
            value = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
            self.calls += 1
            if isinstance(value, Exception):
                raise value
            return draft_of(value)

    def test_success_records_one_attempt_with_the_plan(self):
        run = self.run_it(self.Scripted([good_output()]))
        self.assertTrue(run.ok)
        self.assertEqual(run.attempt_count, 1)
        a = run.attempts[0]
        self.assertIs(a.status, PlanningAttemptStatus.SUCCEEDED)
        self.assertEqual(a.plan_id, run.outcome.plan.plan_id)
        self.assertEqual(a.plan_hash, run.outcome.plan.plan_hash())
        self.assertEqual(a.provider_id, "scripted")
        self.assertEqual(a.model_id, "scripted-model")
        self.assertEqual(a.prompt_template_version, PROMPT_TEMPLATE_VERSION)
        self.assertEqual(a.output_schema_version, OUTPUT_SCHEMA_VERSION)
        self.assertEqual(a.resource_catalog_version, self.resources.catalog_version)
        self.assertEqual(a.skill_catalog_version, self.skills.catalog_version)
        self.assertFalse(a.is_retry)
        self.assertIsNone(a.previous_attempt_id)
        self.assertIsNone(a.reason_code)

    def test_retry_gets_a_new_attempt_id_and_links_the_previous_one(self):
        provider = self.Scripted([
            PlanProviderError(ReasonCode.PLAN_LLM_UNAVAILABLE, "모델 없음"),
            good_output(),
        ])
        run = self.run_it(provider)
        self.assertTrue(run.ok)
        self.assertEqual(run.attempt_count, 2)
        first, second = run.attempts
        self.assertIs(first.status, PlanningAttemptStatus.FAILED)
        self.assertIs(first.reason_code, ReasonCode.PLAN_LLM_UNAVAILABLE)
        self.assertNotEqual(first.planning_attempt_id, second.planning_attempt_id)
        self.assertTrue(second.is_retry)
        self.assertEqual(second.previous_attempt_id, first.planning_attempt_id)
        self.assertEqual([a.attempt_no for a in run.attempts], [1, 2])

    def test_non_retryable_failure_stops_after_one_call(self):
        provider = self.Scripted([
            good_output(steps=[{"skill": "move", "args": {"to": "loc_pallet_9"}}]),
        ])
        run = self.run_it(provider)
        self.assertFalse(run.ok)
        self.assertEqual(provider.calls, 1, "같은 호출을 반복했다")
        self.assertIs(run.outcome.failure.reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)

    def test_retry_exhaustion_has_its_own_reason_code(self):
        provider = self.Scripted([
            PlanProviderError(ReasonCode.PLAN_LLM_UNAVAILABLE, "계속 실패"),
        ])
        run = self.run_it(provider, pol=policy(max_attempts=2))
        self.assertFalse(run.ok)
        self.assertEqual(provider.calls, 2)
        self.assertIs(run.outcome.failure.reason, ReasonCode.PLAN_LLM_RETRY_EXHAUSTED)
        # 마지막 실제 사유는 시도 기록에 남아 있다.
        self.assertIs(run.attempts[-1].reason_code, ReasonCode.PLAN_LLM_UNAVAILABLE)

    def test_timeout_is_its_own_reason_code_and_frees_the_caller(self):
        class Slow:
            provider_id = "slow"
            model_name = "slow-model"

            def generate(self, context):
                time.sleep(2.0)
                raise AssertionError("여기 오면 안 된다")

        started = time.monotonic()
        run = run_planning(
            "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘", request_id="req_1",
            catalog=self.resources, skill_catalog=self.skills, profile=self.profile,
            provider=Slow(), policy=policy(max_attempts=1, request_timeout_sec=0.2),
            robot_id="robot_fake", plan_id_factory=lambda: next(self.plan_ids),
            attempt_id_factory=lambda n: f"attempt_{n}",
            now_utc=lambda: 1_000.0, clock=time.monotonic, ttl_sec=60.0,
            stop_keywords=STOP_KEYWORDS, schema_version=TASK_PLAN_SCHEMA_VERSION,
        )
        waited = time.monotonic() - started
        self.assertFalse(run.ok)
        self.assertIs(run.outcome.failure.reason, ReasonCode.PLAN_LLM_TIMEOUT)
        self.assertIs(run.attempts[0].status, PlanningAttemptStatus.FAILED)
        self.assertLess(waited, 1.5, f"제한시간이 호출자를 놓아주지 않았다({waited:.2f}s)")

    def test_stop_bypass_records_no_provider(self):
        provider = self.Scripted([good_output()])
        run = self.run_it(provider, utterance="지금 멈춰")
        self.assertTrue(run.ok)
        self.assertEqual(provider.calls, 0, "정지에 모델을 호출했다")
        a = run.attempts[0]
        self.assertIs(a.status, PlanningAttemptStatus.STOP_BYPASS)
        self.assertIsNone(a.provider_id)
        self.assertIsNone(a.model_id)
        self.assertFalse(a.is_mock)
        self.assertEqual(a.plan_id, run.outcome.plan.plan_id)

    def test_mock_attempts_are_flagged(self):
        run = self.run_it(MockPlanProvider(
            hold_keywords=("들고 있어",), termination=termination(self.profile)))
        self.assertTrue(run.ok)
        self.assertTrue(run.used_mock)
        self.assertTrue(run.attempts[0].is_mock)
        self.assertEqual(run.attempts[0].provider_id, "mock")

    def test_failed_attempt_still_records_the_prompt_version(self):
        """초안이 없는 실패에도 프롬프트 버전이 남는다.

        없으면 프롬프트 버전별 비교에서 실패·확인 요청 행이 빠진다.
        """
        provider = self.Scripted([
            PlanProviderError(ReasonCode.PLAN_LLM_UNAVAILABLE, "모델 없음"),
        ])
        run = self.run_it(provider, pol=policy(max_attempts=1))
        self.assertFalse(run.ok)
        self.assertEqual(run.attempts[0].prompt_template_version,
                         PROMPT_TEMPLATE_VERSION)
        self.assertEqual(run.attempts[0].output_schema_version,
                         OUTPUT_SCHEMA_VERSION)
        self.assertIsNone(run.attempts[0].served_model_id)

    def test_clarification_attempt_records_the_prompt_version(self):
        from planning.output_parser import ClarificationNeeded

        provider = self.Scripted([ClarificationNeeded("어느 자재인지 알 수 없다")])
        run = self.run_it(provider, pol=policy(max_attempts=1))
        self.assertFalse(run.ok)
        self.assertIs(run.outcome.failure.reason,
                      ReasonCode.PLAN_CLARIFICATION_REQUIRED)
        self.assertEqual(run.outcome.clarification, "어느 자재인지 알 수 없다")
        self.assertEqual(run.attempts[0].prompt_template_version,
                         PROMPT_TEMPLATE_VERSION)

    def test_mock_failure_is_still_recorded_as_mock(self):
        """초안이 없는 실패에서도 Mock 분류가 유지된다.

        이전에는 `PlanDraft.is_mock`에서 역추정해서, Mock이 실패하면 초안이
        없어 실제 호출(is_mock=False)로 기록됐다. 실측에서 Mock 실패 23건이
        실제 호출 통계에 섞였다.
        """
        run = self.run_it(
            MockPlanProvider(termination=termination(self.profile)),
            utterance="그거 저기로",
        )
        self.assertFalse(run.ok)
        self.assertTrue(run.attempts[0].is_mock, "Mock 실패가 실제 호출로 기록됐다")
        self.assertIsNone(run.attempts[0].served_model_id)

    def test_stop_bypass_is_not_flagged_as_mock(self):
        """정지 우회는 공급자를 호출하지 않는다 — Mock 여부와 무관하다."""
        run = self.run_it(
            MockPlanProvider(termination=termination(self.profile)),
            utterance="지금 멈춰",
        )
        self.assertTrue(run.ok)
        self.assertFalse(run.attempts[0].is_mock)

    def test_stt_inference_id_is_carried_into_the_attempt(self):
        run = self.run_it(self.Scripted([good_output()]), stt_inference_id="stt_7")
        self.assertEqual(run.attempts[0].stt_inference_id, "stt_7")

    def test_payload_is_versioned_and_scrubbed(self):
        run = self.run_it(self.Scripted([good_output()]))
        payload = run.attempts[0].payload
        self.assertIsNotNone(payload)
        self.assertEqual(payload.payload_version, PAYLOAD_VERSION)
        self.assertEqual(payload.retention_days, 30)
        self.assertNotIn("api_key", payload.body_json.lower())

    def test_payload_is_omitted_when_the_policy_says_so(self):
        run = self.run_it(self.Scripted([good_output()]),
                          pol=policy(store_payloads=False))
        self.assertIsNone(run.attempts[0].payload)

    def test_duration_is_recorded_per_attempt(self):
        run = self.run_it(self.Scripted([good_output()]))
        a = run.attempts[0]
        self.assertGreaterEqual(a.duration_ms, 0)
        self.assertGreaterEqual(a.ended_at, a.started_at)


class TestProviderIsSeparateFromSdk(unittest.TestCase):
    def test_provider_port_imports_no_client_library(self):
        """포트가 공급자 SDK를 import하지 않는다.

        docstring이 vLLM·OpenAI 호환 클라이언트를 **언급**하는 것은 설계 설명이다.
        확인 대상은 실제 import다.
        """
        for module in ("planning/plan_provider.py", "planning/pipeline.py",
                       "planning/attempt_runner.py", "planning/output_parser.py"):
            imported = imported_modules(ROOT / module)
            for banned in ("requests", "httpx", "openai", "urllib", "socket", "aiohttp"):
                with self.subTest(module=module, banned=banned):
                    self.assertNotIn(banned, imported)

    def test_runner_holds_no_prompt_text(self):
        literals = code_strings(ROOT / "planning" / "attempt_runner.py")
        for banned in ("너는", "temperature", "max_tokens", "system"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, literals)

    def test_real_provider_adds_no_sdk_dependency(self):
        """공급자 구현체가 생겼지만 SDK를 넣지 않았다 (5-02).

        vLLM의 OpenAI 호환 API는 평범한 HTTP+JSON이라 표준 라이브러리로
        호출한다. 쓰는 엔드포인트가 두 개뿐이어서 SDK의 버전 고정·업그레이드
        부담을 지지 않는다.
        """
        imported = imported_modules(ROOT / "planning" / "openai_compat.py")
        self.assertIn("urllib", imported)
        for banned in ("openai", "httpx", "requests", "aiohttp"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, imported)
        # 공급자 구현체도 SDK를 모른다.
        provider_imports = imported_modules(ROOT / "planning" / "vllm_provider.py")
        for banned in ("openai", "httpx", "requests", "urllib", "os"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, provider_imports)
        # 주석이 아니라 실제 의존성 줄만 본다.
        pinned = [
            line.strip()
            for line in (ROOT / "requirements-stt.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertEqual(
            pinned, ["faster-whisper==1.2.1", "ctranslate2==4.8.1", "onnxruntime==1.29.0"],
            "LLM 공급자 의존성이 추가됐다 — 공급자·모델은 아직 정해지지 않았다",
        )
        self.assertFalse(
            list(ROOT.glob("requirements-llm*.txt")),
            "LLM 의존성 파일이 생겼다 — 실제 연동 전에 사용자 확인이 필요하다",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
