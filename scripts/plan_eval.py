"""계획 생성 평가 실행기 (md/개발플랜.md 5-02). `scripts/run_plan_eval.sh`가 호출한다.

한 번의 실행에서 두 공급자를 **따로** 돌린다.
- 실제 LLM (vLLM OpenAI 호환). `is_mock=false`
- Mock Provider (규칙 기반). `is_mock=true`

집계도 따로 낸다. Mock 통과를 실제 모델 성능으로 읽으면 안 된다.

각 사례는 계획 생성 → SafetyValidator → ExecutionPermit까지 실제로 통과시키고,
시도 기록을 SQLite에 append-only로 남긴다.

요구정의서에 합격 기준이 없으므로 **합격 판정을 하지 않는다.**

기록 방침:
- 시도 기록은 `planning_attempts`에 append-only로 쌓인다. 기존 실행 결과를
  덮어쓰거나 다시 계산하지 않는다.
- 평가 실행은 `execution_path=evaluation`과 `evaluation_run_id`로 운영 요청
  통계와 구분된다.
- `evaluation_label`(baseline / candidate)과 `prompt_template_version`으로
  프롬프트 버전별 비교가 된다. **모델 간 비교가 아니다** — 같은 model_id와
  같은 서버 설정에서 프롬프트만 바꿔 비교한다.
- 보고서 파일명에 프롬프트 버전과 라벨이 들어간다. 기준선 파일을 덮지 않는다.

  ./scripts/run_plan_eval.sh                          기본(실제 + Mock)
  PLAN_EVAL_LABEL=candidate ./scripts/run_plan_eval.sh   라벨 지정
  PLAN_EVAL_BASELINE=reports/plan_eval_baseline_plan-ko-1.0.json ... 비교 대상
  PLAN_EVAL_MOCK_ONLY=1 ./scripts/run_plan_eval.sh       Mock만
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_capability_profile,
    load_planning_policy,
    load_resource_catalog,
    load_skill_catalog,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.policy import FreshnessPolicy, LlmProviderConfig, SafetyPolicy
from core.termination import approach_requirement, termination_requirement
from core.reason_codes import ReasonCode
from planning.attempt_runner import persist_attempts, run_planning
from planning.evaluation import CaseOutcome, EvalSummary, ExpectedStep, load_cases
from planning.mock_provider import MockPlanProvider
from planning.openai_compat import OpenAiCompatClient
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION
from planning.vllm_provider import VllmPlanProvider
from storage.records import (
    PlanningExecutionPath,
    RequestRecord,
    ValidationStage,
    ValidationStageResult,
)
from storage.sqlite.repository import SqliteRepository
from validation.execution_permit import PermitContext, check_execution_permit
from validation.safety_validator import SafetyDecision, aggregate, evaluate

CONFIG = ROOT / "examples" / "config"
FIXTURE = ROOT / "fixtures" / "planning" / "eval_ko_commands.jsonl"
STOP_KEYWORDS = ("정지", "멈춰", "스톱")
#: 반복 안정성 측정 횟수.
REPEATS = int(os.environ.get("PLAN_EVAL_REPEATS", "3"))
#: 이번 실행의 라벨. 프롬프트 버전별 비교의 키다.
LABEL = os.environ.get("PLAN_EVAL_LABEL", "candidate")
#: 구조 검증에서 막힌 것으로 보는 이유 코드.
STRUCTURE_REASONS = {
    ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
    ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID,
    ReasonCode.PLAN_PROMPT_VERSION_MISMATCH,
    ReasonCode.PLAN_LLM_REASONING_LEAKED,
}
TRANSPORT_REASONS = {
    ReasonCode.PLAN_LLM_TIMEOUT,
    ReasonCode.PLAN_LLM_UNAVAILABLE,
    ReasonCode.PLAN_LLM_MODEL_MISMATCH,
    ReasonCode.PLAN_LLM_RETRY_EXHAUSTED,
}


def load(name):
    return json.loads((CONFIG / name).read_text(encoding="utf-8"))


def fmt(value, spec="{:.3f}"):
    return "없음" if value is None else spec.format(value)


def main() -> int:
    resources = load_resource_catalog(load("valid_resource_catalog.json"))
    skills = load_skill_catalog(load("valid_skill_catalog.json"))
    profile = load_capability_profile(load("valid_capability_profile.json"))
    policy = load_planning_policy(load("valid_planning_policy.json"))
    safety_policy = SafetyPolicy(
        "eval-1.0", 12,
        {"max_steps": "평가 fixture",
         "required_final_skill": "E-SEQ-002가 요구하는 복귀 종료. 프롬프트도 같은 값을 읽는다",
         "approach_skill": "E-SEQ-003/004가 요구하는 접근 이동. 프롬프트도 같은 값을 읽는다"},
        required_final_skill="home", approach_skill="move",
    )
    freshness = FreshnessPolicy(
        "eval-1.0", 5.0, 5.0,
        {"robot_state_max_age_sec": "평가 fixture",
         "environment_max_age_sec": "평가 fixture"},
    )
    cases = load_cases(FIXTURE)
    termination = termination_requirement(
        profile=profile, safety_policy=safety_policy
    )
    approach = approach_requirement(
        skill_catalog=skills, safety_policy=safety_policy, profile=profile
    )
    run_id = f"eval-{time.strftime('%Y%m%dT%H%M%S')}-{LABEL}"
    print(f"[실행] run_id={run_id} label={LABEL} "
          f"prompt={PROMPT_TEMPLATE_VERSION} output_schema={OUTPUT_SCHEMA_VERSION}")
    print(f"[종료 조건] final_skill={termination.final_skill} "
          f"max_steps={termination.max_steps} "
          f"hold_possible={termination.hold_possible} "
          f"출처={termination.sources}")
    print(f"[접근 요구] approach={approach.approach_skill} "
          f"위치 인자={approach.location_args}")

    db_path = ROOT / "reports" / "plan_eval.sqlite3"
    db_path.parent.mkdir(exist_ok=True)
    repo = SqliteRepository(str(db_path), now=time.time())

    providers: list[tuple[str, object, LlmProviderConfig | None]] = []
    if not os.environ.get("PLAN_EVAL_MOCK_ONLY"):
        llm_config = load_llm_config()
        client = OpenAiCompatClient(config=llm_config)
        provider = VllmPlanProvider(
            config=llm_config, client=client,
            resource_catalog=resources, skill_catalog=skills,
            termination=termination, approach=approach,
        )
        info = provider.verify_server()
        print(f"[서버] {info.model_ids} / vLLM {info.server_version}"
              f" / max_model_len {info.max_model_len}")
        print(f"[구성] model_id={llm_config.model_id}"
              f" quantization={llm_config.quantization}"
              f" thinking={llm_config.thinking_mode.value}"
              f" structured={llm_config.structured_output.value}")
        providers.append(("real", provider, llm_config))
    providers.append((
        "mock",
        MockPlanProvider(hold_keywords=("들고 있어",), termination=termination),
        None,
    ))

    report: dict = {
        "schema_version": TASK_PLAN_SCHEMA_VERSION,
        "evaluation_run_id": run_id,
        "evaluation_label": LABEL,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "approach": {
            "approach_skill": approach.approach_skill,
            "location_args": [list(pair) for pair in approach.location_args],
        },
        "termination": {
            "final_skill": termination.final_skill,
            "max_steps": termination.max_steps,
            "hold_possible": termination.hold_possible,
            "sources": list(termination.sources),
            "conflicts": list(termination.conflicts),
        },
        "case_count": len(cases),
        "repeats": REPEATS,
        "summaries": {},
    }

    for label, provider, llm_config in providers:
        summary = EvalSummary(
            provider_id=provider.provider_id, model_id=provider.model_name,
            is_mock=(label == "mock"),
        )
        print(f"\n=== {label}: {provider.provider_id} / {provider.model_name} ===")
        print(f"{'id':<22}{'분류':<14}{'결과':<10}{'지연(s)':>9}  이유/스텝")
        for case in cases:
            outcome = run_case(
                case, provider=provider, label=label, repo=repo,
                resources=resources, skills=skills, profile=profile,
                policy=policy, safety_policy=safety_policy, freshness=freshness,
                attempt_index=0, run_id=run_id,
            )
            summary.add(case, outcome)
            verdict = (
                "우회" if outcome.bypassed_model
                else ("계획" if outcome.planned else "거부")
            )
            tail = (
                ",".join(s.skill for s in outcome.actual_steps) if outcome.planned
                else (outcome.reason_code.value if outcome.reason_code else "")
            )
            print(f"{case.case_id:<22}{case.category:<14}{verdict:<10}"
                  f"{outcome.latency_sec:>9.2f}  {tail}")

            if case.repeat:
                hashes = [outcome.plan_hash]
                for i in range(1, REPEATS):
                    again = run_case(
                        case, provider=provider, label=label, repo=repo,
                        resources=resources, skills=skills, profile=profile,
                        policy=policy, safety_policy=safety_policy,
                        freshness=freshness, attempt_index=i, run_id=run_id,
                    )
                    hashes.append(again.plan_hash)
                summary.repeats[case.case_id] = hashes
                print(f"{'':<22}{'  반복':<14}{'':<10}{'':>9}  "
                      f"{'동일' if len(set(hashes)) == 1 else '불일치'} {hashes}")

        d = summary.to_dict()
        if llm_config is not None:
            d["server"] = {
                "base_url": llm_config.base_url,
                "model_id": llm_config.model_id,
                "quantization": llm_config.quantization,
                "max_model_len": llm_config.max_model_len,
                "max_tokens": llm_config.max_tokens,
                "temperature": llm_config.temperature,
                "top_p": llm_config.top_p,
                "seed": llm_config.seed,
                "thinking_mode": llm_config.thinking_mode.value,
                "structured_output": llm_config.structured_output.value,
                "config_version": llm_config.config_version,
            }
        report["summaries"][label] = d
        print_summary(label, d)

    compare_with_baseline(report)

    # 실행마다 새 파일. 이전 측정(기준선 포함)을 덮지 않는다.
    out = ROOT / "reports" / f"plan_eval_{PROMPT_TEMPLATE_VERSION}_{run_id}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\n결과 저장: {out.relative_to(ROOT)} / DB {db_path.relative_to(ROOT)}")
    print("요구정의서에 합격 기준이 없다. 합격 판정을 하지 않고 측정값만 기록한다.")
    repo.close()
    return 0


#: 비교할 지표와 방향(True면 높을수록 개선).
COMPARED = (
    ("json_structure_rate", True),
    ("core_semantics_rate", True),
    ("skill_match_rate", True),
    ("arg_match_rate", True),
    ("step_order_rate", True),
    ("blocked_unknown_rate", True),
    ("reject_rate", True),
    ("stop_bypass_rate", True),
    ("unsafe_plan_permit_block_rate", True),
    ("misintent_executable_rate", False),
    ("misintent_executable_of_all_rejects", False),
    ("missing_trailing_home_rate", False),
    ("timeout_rate", False),
    ("stability_rate", True),
    ("clarification_rate", True),
)


def derive_missing_metrics(summary: dict) -> dict:
    """기준선에 없는 지표를 **보존된 원자료(cases)에서** 계산한다.

    기준선 보고서 파일은 고치지 않는다. 지표 정의가 나중에 추가·수정돼도 같은
    정의로 비교할 수 있게, 저장된 사례 결과에서 파생값만 계산한다.
    """
    cases = summary.get("cases") or []
    rejects = [c for c in cases if c.get("kind") == "reject"]
    out: dict = {}
    if rejects:
        granted = [c for c in rejects if c.get("permit_granted")]
        out["misintent_executable_of_all_rejects"] = len(granted) / len(rejects)
    return out


def compare_with_baseline(report: dict) -> None:
    """기준선 보고서와 비교한다. 기준선 파일은 읽기만 한다."""
    path = ROOT / os.environ.get(
        "PLAN_EVAL_BASELINE", "reports/plan_eval_baseline_plan-ko-1.0.json"
    )
    if not path.exists():
        print(f"\n[비교] 기준선 파일이 없다: {path}")
        return
    baseline = json.loads(path.read_text(encoding="utf-8"))
    base_prompt = baseline.get("prompt_template_version", "plan-ko-1.0")
    print(f"\n[프롬프트 버전 비교] {base_prompt} -> {PROMPT_TEMPLATE_VERSION}"
          "  (같은 model_id·서버 설정. 모델 간 비교가 아니다)")
    for label in ("real", "mock"):
        base = baseline.get("summaries", {}).get(label)
        now = report["summaries"].get(label)
        if not base or not now:
            continue
        base_model = base.get("model_id")
        now_model = now.get("model_id")
        note = "" if base_model == now_model else f"  ** model_id 불일치: {base_model} vs {now_model}"
        print(f"\n  [{label}] model_id={now_model}{note}")
        print("  (기준선에 없는 지표는 기준선 원자료에서 같은 정의로 파생 계산했다."
              " 기준선 파일은 고치지 않는다)")
        print(f"  {'지표':<26}{'기준선':>10}{'개선안':>10}{'변화':>10}")
        derived = derive_missing_metrics(base)
        for key, higher_is_better in COMPARED:
            b = base["metrics"].get(key)
            if b is None and key in derived:
                b = derived[key]
            n = now["metrics"].get(key)
            if b is None and n is None:
                continue
            delta = None if (b is None or n is None) else n - b
            mark = ""
            if delta is not None and abs(delta) >= 0.0005:
                improved = delta > 0 if higher_is_better else delta < 0
                mark = " 개선" if improved else " 악화"
            print(f"  {key:<26}{fmt(b):>10}{fmt(n):>10}"
                  f"{'-' if delta is None else format(delta, '+.3f'):>10}{mark}")
        bl, nl = base.get("latency_sec"), now.get("latency_sec")
        if bl and nl:
            print(f"  {'지연 median/p90/max':<26}"
                  f"{bl['median']:.2f}/{bl['p90']:.2f}/{bl['max']:.2f}"
                  f"  ->  {nl['median']:.2f}/{nl['p90']:.2f}/{nl['max']:.2f}")
        bt, nt = base.get("tokens"), now.get("tokens")
        if bt and nt:
            print(f"  {'토큰 prompt/completion':<26}"
                  f"{bt['prompt_mean']:.0f}/{bt['completion_mean']:.0f}"
                  f"  ->  {nt['prompt_mean']:.0f}/{nt['completion_mean']:.0f}")


def load_llm_config() -> LlmProviderConfig:
    from config.loader import load_llm_provider_config

    name = os.environ.get("PLAN_EVAL_LLM_CONFIG", "valid_llm_provider_qwen3.json")
    return load_llm_provider_config(load(name))


def run_case(
    case, *, provider, label, repo, resources, skills, profile, policy,
    safety_policy, freshness, attempt_index: int, run_id: str,
) -> CaseOutcome:
    """사례 1건: 계획 생성 → 안전 검증 → 실행 허가 → 기록."""
    # 실행 묶음 식별자를 넣는다. 같은 사례를 다시 돌려도 이전 실행 기록과
    # 충돌하지 않고 새 행으로 쌓인다(append-only).
    request_id = f"{run_id}:{label}:{case.case_id}:{attempt_index}"
    repo.save_request(
        RequestRecord(request_id, case.utterance, TASK_PLAN_SCHEMA_VERSION, time.time())
    )
    state: dict = {}

    def post_checks(plan):
        results = evaluate(plan, safety_policy, resources)
        decision = aggregate(results)
        state["safety_allowed"] = decision is SafetyDecision.ALLOW
        stages = [
            ValidationStageResult(
                stage=ValidationStage.SAFETY, passed=True,
            ) if state["safety_allowed"] else ValidationStageResult(
                stage=ValidationStage.SAFETY, passed=False,
                reason_code=ReasonCode.SAFETY_SEQUENCE_INVALID,
                detail=f"판정 {decision.value}",
            )
        ]
        permit = check_execution_permit(
            plan, results,
            PermitContext(
                now=time.time(), robot_ready=True,
                robot_state_observed_at=time.time(),
                robot_state_valid=True, environment_observed_at=time.time(),
                recorded_environment_version=1, current_environment_version=1,
                recorded_environment_session="eval", current_environment_session="eval",
                recorded_plan_hash=plan.plan_hash(), profile_id=profile.profile_id,
                profile_version=profile.profile_version,
                supported_skills=profile.supported_skills,
            ),
            freshness,
        )
        state["permit_granted"] = permit.granted
        stages.append(
            ValidationStageResult(stage=ValidationStage.PERMIT, passed=True)
            if permit.granted else
            ValidationStageResult(
                stage=ValidationStage.PERMIT, passed=False,
                reason_code=permit.reason_codes()[0],
                detail=",".join(r.value for r in permit.reason_codes())[:200],
            )
        )
        return stages

    started = time.monotonic()
    run = run_planning(
        case.utterance, request_id=request_id, catalog=resources,
        skill_catalog=skills, profile=profile, provider=provider, policy=policy,
        robot_id="robot_eval",
        plan_id_factory=lambda: f"plan_{request_id}",
        attempt_id_factory=lambda n: f"pa_{request_id}_{n}",
        now_utc=time.time, clock=time.monotonic, ttl_sec=300.0,
        stop_keywords=STOP_KEYWORDS, schema_version=TASK_PLAN_SCHEMA_VERSION,
        post_checks=post_checks,
        execution_path=PlanningExecutionPath.EVALUATION,
        evaluation_run_id=run_id,
        evaluation_label=f"{LABEL}:{label}",
        evaluation_case_id=case.case_id,
    )
    latency = time.monotonic() - started
    persist_attempts(repo, run.attempts)

    reason = None if run.outcome.failure is None else run.outcome.failure.reason
    last = run.attempts[-1] if run.attempts else None
    return CaseOutcome(
        case_id=case.case_id, category=case.category, kind=case.kind,
        planned=run.ok,
        json_ok=(reason not in STRUCTURE_REASONS and reason not in TRANSPORT_REASONS),
        semantics_ok=run.ok,
        latency_sec=latency, reason_code=reason,
        timed_out=(reason is ReasonCode.PLAN_LLM_TIMEOUT),
        bypassed_model=run.outcome.bypassed_model,
        plan_hash=None if run.outcome.plan is None else run.outcome.plan.plan_hash(),
        actual_steps=tuple(
            ExpectedStep(skill=s.skill, args=dict(s.args))
            for s in (run.outcome.plan.steps if run.outcome.plan else ())
        ),
        actual_terminal_hold=(
            run.outcome.plan.terminal_hold if run.outcome.plan else None
        ),
        clarification=run.outcome.clarification,
        prompt_tokens=None if last is None else last.prompt_tokens,
        completion_tokens=None if last is None else last.completion_tokens,
        safety_allowed=state.get("safety_allowed"),
        permit_granted=state.get("permit_granted"),
        draft_steps=tuple(
            ExpectedStep(skill=s.skill, args=dict(s.args))
            for s in (run.outcome.draft.steps if run.outcome.draft else ())
        ),
        draft_terminal_hold=(
            run.outcome.draft.terminal_hold if run.outcome.draft else None
        ),
    )


def print_summary(label: str, d: dict) -> None:
    m = d["metrics"]
    print(f"\n[{label} 요약]  is_mock={d['is_mock']}  사례 {d['case_count']}건")
    rows = (
        ("JSON 구조 성공률", "json_structure_rate"),
        ("core 의미 검증 통과율", "core_semantics_rate"),
        ("예상 스킬 일치율", "skill_match_rate"),
        ("예상 인자 일치율", "arg_match_rate"),
        ("단계 순서 일치율", "step_order_rate"),
        ("미등록·미지원 차단율", "blocked_unknown_rate"),
        ("전체 거부 정확도", "reject_rate"),
        ("STOP 우회율", "stop_bypass_rate"),
        ("안전차단→허가차단율", "unsafe_plan_permit_block_rate"),
        ("의도불일치(계획난 것 중)", "misintent_executable_rate"),
        ("의도불일치(거부사례 전체)", "misintent_executable_of_all_rejects"),
        ("마지막 home 누락율", "missing_trailing_home_rate"),
        ("timeout 비율", "timeout_rate"),
        ("반복 안정성", "stability_rate"),
        ("확인 요청율(거부 사례)", "clarification_rate"),
    )
    for title, key in rows:
        print(f"  {title:<22}{fmt(m[key])}")
    lat = d["latency_sec"]
    if lat:
        print(f"  {'지연(s)':<22}n={lat['n']} min {lat['min']:.2f} / "
              f"median {lat['median']:.2f} / p90 {lat['p90']:.2f} / "
              f"max {lat['max']:.2f} / mean {lat['mean']:.2f}")
    tok = d["tokens"]
    if tok:
        print(f"  {'토큰':<22}prompt mean {tok['prompt_mean']:.0f} / max "
              f"{tok['prompt_max']} / completion mean "
              f"{fmt(tok['completion_mean'], '{:.0f}')}")
    if d.get("misintent_executable_cases"):
        print(f"  ** 의도와 다른데 실행 허가까지 통과: "
              f"{d['misintent_executable_cases']}")
    if d["failures"]:
        print(f"  실패 사례 {len(d['failures'])}건:")
        for f in d["failures"]:
            print(f"    - {f['id']} ({f['category']}): {f['problem']}"
                  f" / reason={f['reason_code']}")


if __name__ == "__main__":
    raise SystemExit(main())
