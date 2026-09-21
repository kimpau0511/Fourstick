#!/usr/bin/env python3
"""작업 셀 명령 평가 결과 요약 (8-13).

```
python3 scripts/workcell_eval_summary.py reports/workcell_eval/<run>.json [...]
```

실행기 보고서(JSON)를 읽어 **다시 계산하지 않고** 다음을 덧붙인다.

1. 무결성 — 세트별 사례 수·중복·누락, 전송 실패·timeout·API 오류를 성공 결과와
   분리해 센다. 품질 지표는 그런 사례를 뺀 부분집합으로도 낸다.
2. 시간 — 계획 생성·관문 시간의 평균·중앙값·P95, 실패·timeout 비율.
3. 비결정성 — 같은 세트를 두 번 돌린 결과의 판정·이유·계획 차이.
4. 실패 유형 — 자원 치환 / 자원 누락 / 팔레트·자재 조합 오류 / 모호 명령 처리 오류 /
   STOP 처리 오류 / 단계 누락·순서 오류 / PASS·BLOCK·ASK 오판 / timeout·모델 응답 오류.

실제 모델과 double은 표를 따로 낸다. 합격선은 만들지 않는다.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planning import workcell_command_eval as ev  # noqa: E402
from planning.workcell_command_eval import DECISIONS, quantiles  # noqa: E402

FIXTURES = ROOT / "fixtures/workcell_eval"
TRANSPORT = {
    "plan.llm_timeout", "plan.llm_unavailable", "plan.llm_output_unparseable",
    "plan.llm_output_schema_invalid", "plan.llm_retry_exhausted",
    "plan.llm_model_mismatch", "plan.llm_reasoning_leaked", "plan.prompt_version_mismatch",
}
TYPES = ("timeout·모델 응답 오류", "STOP 처리 오류", "자원 치환", "팔레트·자재 조합 오류",
         "자원 누락", "모호 명령 처리 오류", "Task Plan 단계 누락·순서 오류",
         "PASS/BLOCK/ASK 오판", "기타")
METRIC_KEYS = ("intent_accuracy", "resource_accuracy", "plan_structure_accuracy",
               "decision_accuracy", "reason_code_accuracy", "executable_accuracy",
               "mismatch_block_rate", "stop_bypass_accuracy", "plan_validation_accuracy",
               "execution_block_accuracy", "all_checks_pass_rate")


def rescore(run: dict) -> int:
    """저장된 관측으로 **현재 채점 규칙**을 다시 적용한다. 관측은 바꾸지 않는다.

    실행기는 실행 당시 규칙으로 checks를 남긴다. 규칙이 뒤에 고쳐지면(예: 이동만
    만든 계획에 pick/place 계획 검증 채점을 적용하지 않기) 여기서 다시 센다.
    바뀐 사례 수를 돌려준다.
    """
    scenarios = ev.load_scenarios(FIXTURES / "scenarios.json")
    rows = {r.id: r for r in ev.load_set(FIXTURES / f"{run['split']}_set.jsonl", scenarios, run["split"])}
    changed = 0
    for c in run["cases"]:
        case = rows.get(c["id"])
        if case is None:
            continue
        o = c["observed"]
        obs = ev.Observation(
            ok=o["ok"], bypassed_model=o["bypassed_model"], decision=o["decision"],
            reason_code=o["reason_code"], executable=o["executable"], slots=tuple(o["slots"]),
            steps=tuple(ev.ExpectedStep(skill=s["skill"], args=dict(s["args"])) for s in o["steps"]),
            terminal_hold=o.get("terminal_hold"), clarification=o.get("clarification"),
            consistency={"status": o.get("consistency_status"), "only_in_plan": o.get("only_in_plan") or []},
            plan_validation=o.get("plan_validation"), geometry_decision=o.get("geometry_decision"),
            planning_ms=o.get("planning_ms"), gate_ms=o.get("gate_ms"), total_ms=o.get("total_ms") or 0.0,
            provider_id=o.get("provider_id"), model_id=o.get("model_id"),
            served_model_id=o.get("served_model_id"), is_mock=o.get("is_mock"),
            plan_hash=o.get("plan_hash"), detail=o.get("detail") or "",
        )
        result = ev.CaseResult(case, obs)
        fresh = result.to_dict()
        if fresh["checks"] != c["checks"] or fresh["all_ok"] != c["all_ok"]:
            changed += 1
        c["checks"] = fresh["checks"]
        c["all_ok"] = fresh["all_ok"]
        c["problems"] = fresh["problems"]
        c["derived_intent"] = fresh["derived_intent"]
    summary = ev.RunSummary(provider_label=run["provider_label"], is_mock=run["is_mock"], split=run["split"])
    for c in run["cases"]:
        case = rows.get(c["id"])
        if case is None:
            continue
        o = c["observed"]
        summary.add(ev.CaseResult(case, ev.Observation(
            ok=o["ok"], bypassed_model=o["bypassed_model"], decision=o["decision"],
            reason_code=o["reason_code"], executable=o["executable"], slots=tuple(o["slots"]),
            steps=tuple(ev.ExpectedStep(skill=s["skill"], args=dict(s["args"])) for s in o["steps"]),
            terminal_hold=o.get("terminal_hold"), clarification=o.get("clarification"),
            consistency={"status": o.get("consistency_status"), "only_in_plan": o.get("only_in_plan") or []},
            plan_validation=o.get("plan_validation"), geometry_decision=o.get("geometry_decision"),
            planning_ms=o.get("planning_ms"), gate_ms=o.get("gate_ms"), total_ms=o.get("total_ms") or 0.0,
            provider_id=o.get("provider_id"), model_id=o.get("model_id"),
            served_model_id=o.get("served_model_id"), is_mock=o.get("is_mock"),
            plan_hash=o.get("plan_hash"), detail=o.get("detail") or "")))
    run["metrics"] = summary.metrics()
    run["confusion"] = summary.confusion()
    run["by_category"] = summary.by_category()
    repro = {f["id"]: f["repro"] for f in run.get("failures", [])}
    run["failures"] = [dict(f, repro=repro.get(f["id"], f["repro"]))
                       for f in summary.failures(repro_prefix="./scripts/run_workcell_command_eval.sh")]
    return changed


def fixture_ids(split: str) -> list[str]:
    return [json.loads(l)["id"] for l in (FIXTURES / f"{split}_set.jsonl")
            .read_text(encoding="utf-8").splitlines() if l.strip()]


def is_execution_error(case: dict) -> bool:
    o = case["observed"]
    if o.get("reason_code") in TRANSPORT:
        return True
    detail = o.get("detail") or ""
    return detail.startswith(("ApiError", "Exception", "RuntimeError", "TimeoutError"))


def used_resources(case: dict) -> set[str]:
    out: set[str] = set()
    for s in case["observed"]["steps"]:
        for k in ("to", "from", "object"):
            if s["args"].get(k):
                out.add(s["args"][k])
    if case["observed"].get("terminal_hold"):
        out.add(case["observed"]["terminal_hold"])
    return out


def failure_types(case: dict) -> list[str]:
    c = case["checks"]
    o = case["observed"]
    tags: list[str] = []
    if is_execution_error(case):
        tags.append("timeout·모델 응답 오류")
    if c.get("stop_bypass") is False:
        tags.append("STOP 처리 오류")
    if o.get("mismatch_observed"):
        tags.append("자원 치환")
    if c.get("plan_validation") is False:
        tags.append("팔레트·자재 조합 오류")
    expected_res = set(case["expected"]["resources"])
    if o["steps"] and expected_res and (expected_res - used_resources(case)) \
            and not o.get("bypassed_model"):
        tags.append("자원 누락")
    if c.get("resources") is False:
        tags.append("자원 누락")
    if case["category"] == "모호한 발화" and (c.get("decision") is False or c.get("intent") is False):
        tags.append("모호 명령 처리 오류")
    if c.get("structure") is False:
        tags.append("Task Plan 단계 누락·순서 오류")
    if c.get("decision") is False or c.get("reason") is False or c.get("executable") is False \
            or c.get("execution_block") is False:
        tags.append("PASS/BLOCK/ASK 오판")
    if not tags:
        tags.append("기타")
    seen: list[str] = []
    for t in tags:
        if t not in seen:
            seen.append(t)
    return seen


def rate(values):
    rows = [v for v in values if v is not None]
    return (sum(1 for v in rows if v) / len(rows)) if rows else None


def metrics_of(cases: list[dict]) -> dict:
    """실행기와 같은 정의로, 주어진 부분집합에서 다시 센다(원자료 checks 사용)."""
    def col(name):
        return [c["checks"].get(name) for c in cases]
    return {
        "intent_accuracy": rate(col("intent")),
        "resource_accuracy": rate(col("resources")),
        "plan_structure_accuracy": rate(col("structure")),
        "decision_accuracy": rate(col("decision")),
        "reason_code_accuracy": rate(col("reason")),
        "executable_accuracy": rate(col("executable")),
        "mismatch_block_rate": rate(col("mismatch_blocked")),
        "stop_bypass_accuracy": rate(col("stop_bypass")),
        "plan_validation_accuracy": rate(col("plan_validation")),
        "execution_block_accuracy": rate(col("execution_block")),
        "all_checks_pass_rate": rate([c["all_ok"] for c in cases]),
        "case_count": len(cases),
    }


def timing_of(cases: list[dict]) -> dict:
    planning = [c["observed"]["planning_ms"] for c in cases
                if c["observed"]["planning_ms"] is not None and not c["observed"]["bypassed_model"]]
    gate = [c["observed"]["gate_ms"] for c in cases if c["observed"]["gate_ms"] is not None]
    total = [c["observed"]["total_ms"] for c in cases]

    def q(values):
        base = quantiles(values)
        if base is None:
            return None
        ordered = sorted(values)
        pos = 0.95 * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        base["p95"] = ordered[low] + (ordered[high] - ordered[low]) * (pos - low)
        return base
    timeouts = sum(1 for c in cases if c["observed"]["reason_code"] == "plan.llm_timeout")
    errors = sum(1 for c in cases if is_execution_error(c))
    return {"planning_ms": q(planning), "gate_ms": q(gate), "total_ms": q(total),
            "timeout_count": timeouts, "execution_error_count": errors,
            "timeout_rate": (timeouts / len(cases)) if cases else None,
            "execution_error_rate": (errors / len(cases)) if cases else None}


def integrity(run: dict) -> dict:
    ids = [c["id"] for c in run["cases"]]
    expected = fixture_ids(run["split"])
    dup = [k for k, v in Counter(ids).items() if v > 1]
    missing = [i for i in expected if i not in set(ids)]
    extra = [i for i in ids if i not in set(expected)]
    errs = [c["id"] for c in run["cases"] if is_execution_error(c)]
    return {"case_count": len(ids), "expected_count": len(expected),
            "exact": len(ids) == len(expected) == 100 and not dup and not missing and not extra,
            "duplicates": dup, "missing": missing, "extra": extra,
            "execution_errors": errs}


def nondeterminism(a: dict, b: dict) -> dict:
    by = {c["id"]: c for c in b["cases"]}
    diffs = []
    same_dec = same_plan = same_reason = 0
    n = 0
    for c in a["cases"]:
        d = by.get(c["id"])
        if d is None:
            continue
        n += 1
        oa, ob = c["observed"], d["observed"]
        dec = oa["decision"] == ob["decision"]
        rea = oa["reason_code"] == ob["reason_code"]
        pln = oa["steps"] == ob["steps"] and oa["terminal_hold"] == ob["terminal_hold"]
        same_dec += dec
        same_reason += rea
        same_plan += pln
        if not (dec and rea and pln):
            diffs.append({"id": c["id"], "scenario": c["scenario"], "utterance": c["utterance"],
                          "pass1": {"decision": oa["decision"], "reason_code": oa["reason_code"],
                                    "steps": oa["steps"], "all_ok": c["all_ok"]},
                          "pass2": {"decision": ob["decision"], "reason_code": ob["reason_code"],
                                    "steps": ob["steps"], "all_ok": d["all_ok"]}})
    return {"compared": n, "decision_agreement": (same_dec / n) if n else None,
            "reason_agreement": (same_reason / n) if n else None,
            "plan_agreement": (same_plan / n) if n else None,
            "differing_count": len(diffs), "differing": diffs}


def fmt(v, spec="{:.3f}"):
    return "없음" if v is None else spec.format(v)


def main(argv: list[str]) -> int:
    paths = [Path(p).resolve() for p in argv]
    if not paths:
        print(__doc__)
        return 2
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    stamp = time.strftime("%Y%m%dT%H%M%S")
    out_json = ROOT / "reports/workcell_eval" / f"summary_{stamp}.json"
    out_md = ROOT / "reports/workcell_eval" / f"summary_{stamp}.md"

    runs: list[dict] = []
    rescored: dict[str, int] = {}
    for rep in reports:
        for i, run in enumerate(rep["runs"]):
            rescored[f"{rep['run_id']}:{run['provider_label']}/{run['split']}#{i + 1}"] = rescore(run)
            runs.append({"run_id": rep["run_id"], "label": rep.get("label"),
                         "generated_at": rep.get("generated_at"), "versions": rep["versions"],
                         "provider_label": run["provider_label"], "is_mock": run["is_mock"],
                         "split": run["split"], "pass_no": i + 1, "run": run,
                         "geometry_validator_available": rep.get("geometry_validator_available")})

    summary = {"schema": "forstick2.workcell_command_eval.summary/1",
               "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "sources": [str(p.relative_to(ROOT)) for p in paths],
               "rescored_changed_cases": rescored,
               "runs": [], "nondeterminism": {}}
    lines: list[str] = []
    add = lines.append
    add(f"# 작업 셀 명령 평가 요약 — {stamp}")
    add("")
    add("> **시뮬레이터·계획 단계 평가.** 실제 로봇에 연결하지 않았고 명령을 보내지 않았다."
        " 실제 모델과 test double은 표를 따로 둔다. 합격선은 없다.")
    add("")
    add("## 원본")
    add("")
    add("채점은 저장된 관측에 **현재 채점 규칙**을 다시 적용했다(관측값은 바꾸지 않는다)."
        " 실행 당시 checks와 달라진 사례 수: "
        + ", ".join(f"{k.split(':', 1)[1]} {v}건" for k, v in rescored.items()) + ".")
    add("")
    for rep in reports:
        v = rep["versions"]
        add(f"- `{rep['run_id']}` ({rep.get('generated_at')}) — 모델 `{v.get('model_id')}` ·"
            f" 서버 `{v.get('model_server')}` · LLM 설정 `{v.get('llm_config_version')}` ·"
            f" 프롬프트 `{v.get('prompt_template_version')}`/`{v.get('output_schema_version')}` ·"
            f" 카탈로그 `{v.get('resource_catalog_version')}` · 셀 `{v.get('workcell_id')}`"
            f" `{v.get('workcell_version')}` · Profile `{v.get('profile_id')}`"
            f" `{v.get('profile_version')}` · git `{v.get('git_commit')}`"
            f"{'(dirty)' if v.get('git_dirty') else ''} · 기하 검사기"
            f" {'있음' if rep.get('geometry_validator_available') else '없음'}")
    add("")

    # 세트별 표 — 실제 모델과 double을 나눈다.
    for mock_flag, title in ((False, "실제 모델"), (True, "test double")):
        group = [r for r in runs if r["is_mock"] == mock_flag]
        if not group:
            continue
        add(f"## {title}")
        add("")
        if mock_flag:
            add("> double 결과다. **실제 모델 성능으로 읽지 않는다.** 하니스·관문 동작 확인용이다.")
            add("")
        header = " | ".join(f"{r['provider_label']}/{r['split']}#{r['pass_no']}" for r in group)
        add("### 무결성")
        add("")
        add("| 실행 | 사례 수 | 정확히 100 | 중복 | 누락 | 실행 오류(전송·timeout·API) |")
        add("|---|---|---|---|---|---|")
        for r in group:
            it = integrity(r["run"])
            r["integrity"] = it
            add(f"| {r['provider_label']}/{r['split']}#{r['pass_no']} | {it['case_count']} |"
                f" {'예' if it['exact'] else '**아니오**'} | {len(it['duplicates'])} |"
                f" {len(it['missing'])} | {len(it['execution_errors'])}"
                + (f" ({', '.join(it['execution_errors'][:8])})" if it["execution_errors"] else "") + " |")
        add("")
        add("### 지표 (전체 사례)")
        add("")
        add("| 지표 | " + header + " |")
        add("|---|" + "---|" * len(group))
        for key in METRIC_KEYS:
            add(f"| {key} | " + " | ".join(fmt(r["run"]["metrics"].get(key)) for r in group) + " |")
        add("| mismatch_observed_count | " + " | ".join(
            str(r["run"]["metrics"].get("mismatch_observed_count")) for r in group) + " |")
        add("")
        any_err = any(r["integrity"]["execution_errors"] for r in group)
        if any_err:
            add("### 지표 (실행 오류 제외)")
            add("")
            add("| 지표 | " + header + " |")
            add("|---|" + "---|" * len(group))
            clean = {id(r): metrics_of([c for c in r["run"]["cases"] if not is_execution_error(c)])
                     for r in group}
            for key in METRIC_KEYS:
                add(f"| {key} | " + " | ".join(fmt(clean[id(r)].get(key)) for r in group) + " |")
            add("| case_count | " + " | ".join(str(clean[id(r)]["case_count"]) for r in group) + " |")
            add("")
        add("### 시간 (ms)")
        add("")
        add("| 실행 | 계획 생성 mean/median/p95/max (n) | 관문 mean/median/p95 (n) | timeout | 실행 오류 |")
        add("|---|---|---|---|---|")
        for r in group:
            t = timing_of(r["run"]["cases"])
            r["timing"] = t
            p, g = t["planning_ms"], t["gate_ms"]
            add(f"| {r['provider_label']}/{r['split']}#{r['pass_no']} | "
                + (f"{p['mean']:.0f}/{p['median']:.0f}/{p['p95']:.0f}/{p['max']:.0f} ({p['n']})" if p else "—")
                + " | " + (f"{g['mean']:.0f}/{g['median']:.0f}/{g['p95']:.0f} ({g['n']})" if g else "—")
                + f" | {t['timeout_count']} ({fmt(t['timeout_rate'])}) | {t['execution_error_count']}"
                  f" ({fmt(t['execution_error_rate'])}) |")
        add("")
        for r in group:
            add(f"### 혼동 행렬 — {r['provider_label']}/{r['split']}#{r['pass_no']} (행 기대, 열 관측)")
            add("")
            add("| 기대 \\ 관측 | " + " | ".join(DECISIONS) + " |")
            add("|---|" + "---|" * len(DECISIONS))
            for row in DECISIONS:
                add(f"| {row} | " + " | ".join(str(r["run"]["confusion"][row][c]) for c in DECISIONS) + " |")
            add("")
        add("### 실패 유형")
        add("")
        add("| 유형 | " + header + " |")
        add("|---|" + "---|" * len(group))
        typed: dict[int, Counter] = {}
        for r in group:
            counter: Counter = Counter()
            records = []
            for c in r["run"]["cases"]:
                if c["all_ok"]:
                    continue
                tags = failure_types(c)
                counter[tags[0]] += 1
                records.append({"id": c["id"], "scenario": c["scenario"], "category": c["category"],
                                "utterance": c["utterance"], "types": tags,
                                "expected": c["expected"],
                                "observed": {k: c["observed"][k] for k in
                                             ("decision", "reason_code", "executable", "steps",
                                              "slots", "clarification", "bypassed_model")},
                                "problems": c["problems"],
                                "repro": next((f["repro"] for f in r["run"]["failures"]
                                               if f["id"] == c["id"]), None)})
            typed[id(r)] = counter
            r["failure_records"] = records
        for name in TYPES:
            add(f"| {name} | " + " | ".join(str(typed[id(r)].get(name, 0)) for r in group) + " |")
        add("| 실패 합계 | " + " | ".join(str(sum(typed[id(r)].values())) for r in group) + " |")
        add("")
        for r in group:
            if not r["failure_records"]:
                continue
            add(f"#### 실패 사례 — {r['provider_label']}/{r['split']}#{r['pass_no']}")
            add("")
            for rec in r["failure_records"]:
                o = rec["observed"]
                add(f"- **{rec['id']}** {rec['scenario']} · {rec['category']} · "
                    f"[{' / '.join(rec['types'])}]")
                add(f"  - 발화: “{rec['utterance']}”")
                add(f"  - 기대: {'/'.join(rec['expected']['decision'])} · 이유 "
                    f"{rec['expected']['reason_codes'] or '없음'} · 의도 {rec['expected']['intent']}")
                add(f"  - 실제: {o['decision']} · 이유 {o['reason_code']} · 의도 관측 "
                    f"{'stop' if o['bypassed_model'] else ('없음' if not o['steps'] else ','.join(s['skill'] for s in o['steps']))}"
                    + (f" · 질문 “{o['clarification']}”" if o.get("clarification") else ""))
                for p in rec["problems"]:
                    add(f"  - {p}")
                add(f"  - 재현: `{rec['repro']}`")
            add("")

    # 비결정성 — 같은 provider·split의 pass1 vs pass2
    pairs = defaultdict(list)
    for r in runs:
        pairs[(r["provider_label"], r["split"])].append(r)
    add("## 비결정성 (같은 세트 반복)")
    add("")
    found = False
    for key, group in pairs.items():
        if len(group) < 2:
            continue
        found = True
        nd = nondeterminism(group[0]["run"], group[1]["run"])
        summary["nondeterminism"][f"{key[0]}/{key[1]}"] = nd
        add(f"- {key[0]}/{key[1]}: 비교 {nd['compared']}건 · 판정 일치 {fmt(nd['decision_agreement'])}"
            f" · 이유 일치 {fmt(nd['reason_agreement'])} · 계획 일치 {fmt(nd['plan_agreement'])}"
            f" · 다른 사례 {nd['differing_count']}건")
        for d in nd["differing"]:
            add(f"  - {d['id']} {d['scenario']} “{d['utterance']}” — 1회: {d['pass1']['decision']}/"
                f"{d['pass1']['reason_code']} ({'OK' if d['pass1']['all_ok'] else 'FAIL'}) · 2회: "
                f"{d['pass2']['decision']}/{d['pass2']['reason_code']} ({'OK' if d['pass2']['all_ok'] else 'FAIL'})")
    if not found:
        add("- 반복 실행이 없다")
    add("")
    add("## 남는 것")
    add("")
    add("- `real_hardware_ready=false` · `real_hardware_verified=false`. 이 요약은 바꾸지 않는다.")
    add("- Gazebo simulation_e2e는 모델 평가와 별개 축이다. 승격하지 않는다.")
    add("- 봉인셋 실패는 분석만 한다. 봉인셋에 맞춘 프롬프트 수정은 하지 않는다.")

    for r in runs:
        summary["runs"].append({
            "run_id": r["run_id"], "label": r["label"], "provider_label": r["provider_label"],
            "is_mock": r["is_mock"], "split": r["split"], "pass_no": r["pass_no"],
            "versions": r["versions"], "integrity": r.get("integrity"),
            "metrics_all": r["run"]["metrics"],
            "metrics_without_execution_errors": metrics_of(
                [c for c in r["run"]["cases"] if not is_execution_error(c)]),
            "timing": r.get("timing"), "confusion": r["run"]["confusion"],
            "failure_type_counts": dict(Counter(
                failure_types(c)[0] for c in r["run"]["cases"] if not c["all_ok"])),
            "failures": r.get("failure_records", []),
        })
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"요약: {out_md.relative_to(ROOT)} · {out_json.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
