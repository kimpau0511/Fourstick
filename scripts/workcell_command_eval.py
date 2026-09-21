#!/usr/bin/env python3
"""작업 셀 명령 평가 실행기 (8-13). `scripts/run_workcell_command_eval.sh`가 호출한다.

```
./scripts/run_workcell_command_eval.sh                      # 실제 모델 + double, dev+sealed
./scripts/run_workcell_command_eval.sh --provider double    # double만 (모델 없이)
./scripts/run_workcell_command_eval.sh --set dev --repeat 2 # 재현성
./scripts/run_workcell_command_eval.sh --only dev_003       # 사례 하나 재현
```

**웹 UI와 같은 경로를 지난다.** `server.api.Api.create_plan`을 그대로 호출하고
그 결과(판정·이유 코드·자원 대조·계획 검증)를 읽는다. 판정을 다시 계산하지
않는다. 실행(`execute`)은 호출하지 않는다.

- 실제 모델(vLLM)과 test double은 **따로** 돈다. 집계도 따로 낸다.
- 모델 서버가 없으면 실제 모델 결과를 만들지 않는다 — 추정하지 않는다.
- 기록은 평가 전용 SQLite(`reports/workcell_eval/<run>.sqlite3`)에 남는다.
  운영 DB(`reports/web.sqlite3`)를 건드리지 않는다.
- 결과마다 모델·프롬프트·Profile·카탈로그·작업 셀 설정 버전을 적는다.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from planning import workcell_command_eval as ev  # noqa: E402
from planning.workcell_command_eval import (  # noqa: E402
    CaseResult, RuleDoubleProvider, RunSummary, SubstitutingDoubleProvider,
)
from server.config import ServerConfig  # noqa: E402
from server.runtime import FeatureState, build_runtime  # noqa: E402
from storage.records import PlanningExecutionPath  # noqa: E402

FIXTURES = ROOT / "fixtures/workcell_eval"
OUT_DIR = ROOT / "reports/workcell_eval"
PROVIDERS = ("real", "double", "substituting")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def git_state() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                  text=True, timeout=10).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    return {"commit": run("rev-parse", "--short", "HEAD") or None,
            "dirty": bool(run("status", "--porcelain"))}


def versions(runtime, config: ServerConfig) -> dict:
    from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION

    manifest = json.loads(config.workcell_manifest.read_text(encoding="utf-8"))
    cell = json.loads((config.workcell_manifest.parent / manifest["workcell_config"])
                      .read_text(encoding="utf-8"))
    llm = runtime.llm_config
    git = git_state()
    return {
        "git_commit": git["commit"], "git_dirty": git["dirty"],
        "model_id": None if llm is None else llm.model_id,
        "model_server": runtime.planning.detail if runtime.planning.available else None,
        "llm_config_version": None if llm is None else llm.config_version,
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "profile_id": None if runtime.profile is None else runtime.profile.profile_id,
        "profile_version": None if runtime.profile is None else runtime.profile.profile_version,
        "resource_catalog_version": runtime.resource_catalog.catalog_version,
        "skill_catalog_version": runtime.skill_catalog.catalog_version,
        "safety_policy_version": getattr(runtime.safety_policy, "policy_version", None),
        "stop_keywords": list(runtime.stt_policy.stop_keywords),
        "workcell_id": cell.get("workcell_id"),
        "workcell_version": cell.get("workcell_version"),
        "adapter_module": manifest.get("adapter_module"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "scenarios_sha": sha256(FIXTURES / "scenarios.json"),
        "dev_set_sha": sha256(FIXTURES / "dev_set.jsonl"),
        "sealed_set_sha": sha256(FIXTURES / "sealed_set.jsonl"),
        "python": sys.version.split()[0],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--set", default="all", choices=("dev", "sealed", "sealed2", "sealed3", "all"))
    parser.add_argument("--provider", default="all", choices=(*PROVIDERS, "all"))
    parser.add_argument("--repeat", type=int, default=1, help="같은 세트 반복 횟수(재현성)")
    parser.add_argument("--only", action="append", default=[], help="발화 id (반복 지정)")
    parser.add_argument("--label", default="run")
    parser.add_argument("--out-dir", default=str(OUT_DIR))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"wce-{time.strftime('%Y%m%dT%H%M%S')}-{args.label}"

    scenarios = ev.load_scenarios(FIXTURES / "scenarios.json")
    sets: dict[str, tuple] = {}
    for split in ("dev", "sealed", "sealed2", "sealed3"):
        # sealed2는 다음 평가 턴 전용 holdout이다. --set all에 포함하지 않는다 —
        # 명시적으로 --set sealed2로만 실행한다.
        if args.set == split or (args.set == "all" and split not in ("sealed2", "sealed3")):
            path = FIXTURES / f"{split}_set.jsonl"
            if not path.is_file():
                continue
            rows = ev.load_set(path, scenarios, split)
            if args.only:
                rows = tuple(r for r in rows if r.id in set(args.only))
            if rows:
                sets[split] = rows
    if not sets:
        print("평가할 발화가 없다", file=sys.stderr)
        return 2

    config = dataclasses.replace(
        ServerConfig.from_env(),
        db_path=out_dir / f"{run_id}.sqlite3",
        enable_stt=False, enable_workcell_robot=True, enable_fake_robot=False,
        session_idle_timeout_sec=24 * 3600.0,
    )
    runtime = build_runtime(config)
    workcell = runtime.workcell or {}
    print(f"[실행] run_id={run_id}")
    print(f"[작업 셀] registered={workcell.get('registered')} "
          f"adapter={workcell.get('adapter_module')} is_simulated={workcell.get('is_simulated')}")
    if not runtime.robot_configured:
        print(f"[중단] 작업 셀 로봇이 등록되지 않았다: {workcell.get('detail')}", file=sys.stderr)
        return 3
    real_provider = runtime.provider
    print(f"[모델] available={runtime.planning.available} · {runtime.planning.detail}")
    validator = runtime.validator()
    print(f"[기하 검사기] {'있음' if validator is not None else '없음 — 기하 검사는 ASK로 남는다'}")

    import server.api as api_module
    from server.api import Api

    api = Api(runtime=runtime)
    captured: dict = {}
    original_run_planning = api_module.run_planning

    def capturing_run_planning(*a, **kw):
        # 운영 경로가 아니라 **평가 경로**로 기록한다. 같은 발화를 다시 돌려도
        # 운영 통계와 섞이지 않는다.
        kw["execution_path"] = PlanningExecutionPath.EVALUATION
        kw["evaluation_run_id"] = run_id
        kw["evaluation_label"] = captured.get("label")
        kw["evaluation_case_id"] = captured.get("case_id")
        run = original_run_planning(*a, **kw)
        captured["run"] = run
        return run

    api_module.run_planning = capturing_run_planning
    original_gate = api.gate_for

    def timed_gate(plan, slots):
        started = time.perf_counter()
        try:
            return original_gate(plan, slots)
        finally:
            captured["gate_ms"] = (time.perf_counter() - started) * 1000.0

    api.gate_for = timed_gate
    session_id = api.create_session(origin="evaluation")["session_id"]

    wanted = PROVIDERS if args.provider == "all" else (args.provider,)
    providers: list[tuple[str, object, bool]] = []
    for name in wanted:
        if name == "real":
            if real_provider is None:
                print("[모델 없음] 실제 모델 결과를 만들지 않는다 — 품질 점수를 추정하지 않는다")
                continue
            providers.append(("real", real_provider, False))
        elif name == "double":
            providers.append(("double-rule", RuleDoubleProvider(termination=runtime.termination), True))
        elif name == "substituting":
            providers.append(("double-substituting",
                              SubstitutingDoubleProvider(termination=runtime.termination), True))

    repro_prefix = "./scripts/run_workcell_command_eval.sh"
    report: dict = {
        "schema": "forstick2.workcell_command_eval/1",
        "run_id": run_id, "label": args.label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": ("시뮬레이터·계획 단계 평가. 실제 로봇에 연결하지 않았고 명령을 보내지"
                 " 않았다. pick/place 계획 검증 통과는 실행 가능이 아니다."),
        "versions": versions(runtime, config),
        "dataset": {"scenario_count": len(scenarios),
                    **{f"{k}_count": len(v) for k, v in sets.items()}},
        "geometry_validator_available": validator is not None,
        "runs": [], "repeat": {},
        "voice": {"status": ("실제 사람 음성 자료가 없다(fixtures/stt/audio 0건)."
                             " 텍스트 평가와 분리하며 transcript 정확도·계획 정확도를"
                             " 이 보고서에서 계산하지 않는다"),
                  "transcript_accuracy": None, "plan_accuracy_from_transcript": None},
        "db_path": str(config.db_path.relative_to(ROOT)),
    }

    for label, provider, is_mock in providers:
        runtime.provider = provider
        runtime.planning = FeatureState(True, label)
        for split, rows in sets.items():
            passes: list[RunSummary] = []
            for rep in range(max(1, args.repeat)):
                summary = RunSummary(provider_label=label, is_mock=is_mock, split=split)
                print(f"\n=== {label} · {split} · {rep + 1}/{args.repeat} "
                      f"({'double' if is_mock else '실제 모델'}) ===")
                print(f"{'id':<12}{'시나리오':<6}{'판정':<7}{'이유':<34}{'ms':>7}  {'검사'}")
                for case in rows:
                    captured.clear()
                    captured["label"] = f"{args.label}:{label}:{split}:{rep}"
                    captured["case_id"] = case.id
                    started = time.perf_counter()
                    try:
                        payload = api.create_plan(session_id=session_id, utterance=case.utterance)
                    except Exception as exc:  # noqa: BLE001 — API 오류도 관측이다
                        payload = {"ok": False, "reason_code": getattr(
                            getattr(exc, "reason", None), "value", None),
                            "detail": f"{type(exc).__name__}: {exc}"[:200]}
                    total_ms = (time.perf_counter() - started) * 1000.0
                    run = captured.get("run")
                    attempts = list(getattr(run, "attempts", ()) or ())
                    last = attempts[-1] if attempts else None
                    attempt = None if last is None else {
                        "provider_id": last.provider_id, "model_id": last.model_id,
                        "served_model_id": last.served_model_id, "is_mock": last.is_mock,
                    }
                    planning_ms = (sum(a.duration_ms for a in attempts) if attempts else None)
                    bypassed = bool(run is not None and run.outcome.bypassed_model)
                    obs = ev.observation_from_payload(
                        payload, bypassed=bypassed, planning_ms=planning_ms,
                        gate_ms=captured.get("gate_ms"), total_ms=total_ms, attempt=attempt,
                    )
                    result = CaseResult(case, obs)
                    summary.add(result)
                    mark = "OK " if result.all_ok else "FAIL"
                    print(f"{case.id:<12}{case.scenario:<6}{obs.decision:<7}"
                          f"{(obs.reason_code or '-')[:33]:<34}{total_ms:>7.0f}  {mark}"
                          + ("" if result.all_ok else " · " + "; ".join(result.problems())[:90]))
                passes.append(summary)
                report["runs"].append(summary.to_dict(repro_prefix=f"{repro_prefix} --provider "
                                                      f"{'real' if not is_mock else label.split('-')[1]}"
                                                      f" --set {split}"))
                m = summary.metrics()
                print(f"--- {label}/{split}: 전체 통과 {ev._fmt(m['all_checks_pass_rate'])} · "
                      f"판정 {ev._fmt(m['decision_accuracy'])} · 이유 {ev._fmt(m['reason_code_accuracy'])}"
                      f" · 자원 {ev._fmt(m['resource_accuracy'])} · 정지 {ev._fmt(m['stop_bypass_accuracy'])}"
                      f" · 대조차단 {ev._fmt(m['mismatch_block_rate'])}({m['mismatch_observed_count']})")
            if len(passes) >= 2:
                report["repeat"][f"{label}/{split}"] = ev.repeat_agreement(
                    passes[0].results, passes[1].results)

    json_path = out_dir / f"{run_id}.json"
    md_path = out_dir / f"{run_id}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(ev.render_markdown(report), encoding="utf-8")
    print(f"\n결과: {json_path.relative_to(ROOT)} · {md_path.relative_to(ROOT)}")
    print("합격선은 없다. 측정값만 기록한다. 실제 로봇에는 아무것도 보내지 않았다.")
    api_module.run_planning = original_run_planning
    try:
        runtime.repository.close()
    except Exception:  # noqa: BLE001
        pass
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    code = main()
    # rclpy 구독 콜백이 인터프리터 종료 중 SIGSEGV를 내는 것을 피한다(8-11 실측).
    os._exit(code)
