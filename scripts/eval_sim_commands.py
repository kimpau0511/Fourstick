"""시연 명령 평가 — 해석 정확도 · 계획 성공률 · 위험 명령 차단률 · 처리 시간.

    python3 scripts/eval_sim_commands.py              # 실제 Qwen(vLLM :8000) 사용
    python3 scripts/eval_sim_commands.py --no-llm     # 규칙만(Qwen 필요 문장은 ASK가 정답이 아니므로 실패로 남는다)

실제 서버 라우트(`server/routes/sim_demo.py`)를 **프로세스 안에서** 부른다. 작업
실행기는 가짜(프로세스를 띄우지 않는다)라 로봇·Gazebo가 움직이지 않는다. 문장마다
새 상태 파일로 시작하고, 데이터셋이 정한 초기 배치·작업 환경을 넣는다.

Gazebo 실행 성공률은 여기서 재지 않는다 — `reports/workcell/arrangement_e2e_*`와
`voice_e2e_*`(실제 Gazebo)를 모아 함께 적는다.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATASET = ROOT / "fixtures/sim_command_eval/final_eval.jsonl"
WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
GRASP = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json").read_text(encoding="utf-8"))
POSES = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_poses.json").read_text(encoding="utf-8"))
SAFE = ("BLOCK", "ASK", "PASS_THROUGH")
EXECUTABLE = ("RUN", "CONFIRM", "CONFIRM_GOAL")


class FakeProc:
    def poll(self):
        return None


class FakePopen:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return FakeProc()


def classifier(use_llm: bool):
    if not use_llm:
        return None
    from config.loader import load_llm_provider_config
    from planning.openai_compat import OpenAiCompatClient
    from server.sim_demo_intent import IntentClassifier

    config = load_llm_provider_config(json.loads(
        (ROOT / "examples/config/valid_llm_provider_qwen3.json").read_text(encoding="utf-8")))
    return IntentClassifier(client=OpenAiCompatClient(config=config), min_confidence=0.7)


def build(tmp: Path, case: dict, intent):
    from server.sim_demo_confirm import ConfirmStore
    from server.sim_demo_context import DialogueContexts
    from server.sim_demo_goals import SimDemoGoals
    from server.sim_demo_jobs import SimDemoJobs
    from tests.unit.test_simulation_demo_checkpoint import completed_result
    from validation.simulation_demo_state import POLICY_DEMO_HOLD, SimulationDemoState

    popen = FakePopen()
    jobs = SimDemoJobs(workcell=WORKCELL, grasp_config=GRASP,
                       state_path=tmp / "state.json", jobs_dir=tmp / "jobs",
                       stop_request=tmp / "stop.json", popen=popen,
                       environ={"PATH": "/usr/bin"}, poses_config=POSES)
    state = SimulationDemoState(tmp / "state.json")
    for model, slot in (case.get("state") or {}).items():
        if str(slot).startswith("loc_pallet"):
            # 다른 팔레트에 놓인 자재(on_pallet) — 관측으로 도착을 확인한 기록 형태.
            origin = jobs._capability().origin_of(model)
            state.record_transfer(model, source=origin, destination=slot,
                                  destination_kind="pallet", destination_center_m=(0.5, 0, 0.84),
                                  own_origin=origin, completed=True, stop_requested=False,
                                  attached=True, final_pose_m=(0.5, 0, 0.84),
                                  base={"target_id": slot})
            continue
        state.record_run(policy=POLICY_DEMO_HOLD, model=model, result=completed_result(),
                         final_pose_m=(0.25, -0.5, 0.75), restored=None, slot=slot)
    goals = SimDemoGoals(jobs, auto_run=False, sleep=lambda _: None)
    env = case.get("environment") or {}
    if env:
        goals.set_environment({"blocked_slots": env.get("blocked_slots") or [],
                               "unavailable_materials": env.get("unavailable_materials") or []})
    runtime = types.SimpleNamespace(sim_demo_jobs=jobs, sim_demo_goals=goals,
                                    sim_demo_disabled_reason=None,
                                    sim_demo_confirm=ConfirmStore(ttl_sec=60),
                                    sim_demo_intent=intent,
                                    sim_demo_contexts=DialogueContexts())
    return runtime, popen


def call(runtime, utterance: str, *, source: str = "text",
         stt_confidence: float | None = None) -> tuple[int, dict]:
    from server.routes import sim_demo

    async def read_body(_receive):
        body = {"mode": "simulation_demo", "source": source, "utterance": utterance,
                "session_id": "eval-session"}
        if source == "stt_final":
            body.update(raw_transcript=utterance, stt_confidence=stt_confidence)
        return body

    ctx = types.SimpleNamespace(runtime=runtime, read_body=read_body)
    status, _, raw = asyncio.run(sim_demo.handle(ctx, "POST", "/v1/sim-demo/command", None, {}))
    return status, json.loads(raw)


def plan_of(payload: dict) -> list[list[str]] | None:
    goal = payload.get("goal")
    if not goal:
        return None
    return [[s["action"], s["material"], "origin" if s["action"] == "return" else s["to"]]
            for s in goal.get("plan") or []]


def evaluate_case(case: dict, intent) -> dict:
    expect = case["expect"]
    with tempfile.TemporaryDirectory() as tmp:
        runtime, popen = build(Path(tmp), case, intent)
        started = time.perf_counter()
        status, payload = call(runtime, case["utterance"])
        elapsed_ms = (time.perf_counter() - started) * 1000
        env_after = runtime.sim_demo_goals.environment()
    decision = payload.get("decision")
    jobs_started = len(popen.calls)
    checks = {"decision": decision in expect["decision"]}
    if expect.get("material"):
        checks["material"] = payload.get("material") == expect["material"]
    if expect.get("plan") is not None:
        checks["plan"] = plan_of(payload) == expect["plan"]
    if expect.get("environment") is not None:
        checks["environment"] = all(sorted(env_after.get(k) or []) == sorted(v)
                                    for k, v in expect["environment"].items())
    # 실행형이 아닌 판정은 작업이 하나도 만들어지지 않아야 한다.
    if decision != "RUN":
        checks["no_job"] = jobs_started == 0
    used_llm = bool((payload.get("arrangement_interpretation") or {}).get("interpreted_by")
                    == "qwen" or payload.get("intent_result"))
    return {"id": case["id"], "category": case["category"], "source": case.get("source"),
            "utterance": case["utterance"], "http": status, "decision": decision,
            "expected": expect["decision"], "material": payload.get("material"),
            "plan": plan_of(payload), "reason": payload.get("reason"),
            "jobs_started": jobs_started, "used_llm": used_llm,
            "elapsed_ms": round(elapsed_ms, 1), "checks": checks,
            "passed": all(checks.values())}


def normalized_plan(payload: dict) -> list[list[str]] | None:
    """목표 계획이든 단일 명령이든 [동작, 자재, 도착]으로."""
    plan = plan_of(payload)
    if plan is not None:
        return plan
    if payload.get("decision") not in ("RUN", "CONFIRM"):
        return None
    intent = payload.get("intent")
    evidence = ((payload.get("confirmation") or {}).get("evidence") or {})
    slot = (payload.get("slot") or evidence.get("slot")
            or (payload.get("job_spec") or {}).get("slot"))
    if intent == "return":
        return [["return", payload.get("material"), "origin"]]
    if intent == "transfer":
        return [["transfer", payload.get("material"), slot]]
    return [[str(intent), payload.get("material"), slot]]


def cancel_pending(runtime, payload: dict) -> None:
    confirmation = payload.get("confirmation") or {}
    try:
        if confirmation.get("kind") == "goal":
            runtime.sim_demo_goals.confirm(confirmation["goal_id"], "cancel")
        elif confirmation.get("token") and runtime.sim_demo_confirm is not None:
            runtime.sim_demo_confirm.cancel(confirmation["token"]) \
                if hasattr(runtime.sim_demo_confirm, "cancel") else None
    except Exception:  # noqa: BLE001 — 평가용 정리
        pass


def evaluate_context_case(case: dict, intent) -> dict:
    """맥락 평가 한 건: 이전 턴을 먼저 보내고(확인은 누르지 않는다) 마지막 문장을 판정."""
    expect = case["expect"]
    with tempfile.TemporaryDirectory() as tmp:
        runtime, popen = build(Path(tmp), case, intent)
        history = []
        for turn in case.get("history") or ():
            _, prior = call(runtime, turn, source=case.get("source") or "text",
                            stt_confidence=case.get("stt_confidence"))
            history.append(prior.get("decision"))
            cancel_pending(runtime, prior)
        jobs_before = len(popen.calls)
        started = time.perf_counter()
        status, payload = call(runtime, case["utterance"], source=case.get("source") or "text",
                               stt_confidence=case.get("stt_confidence"))
        elapsed_ms = (time.perf_counter() - started) * 1000
        jobs_started = len(popen.calls) - jobs_before
    decision = payload.get("decision")
    checks = {"decision": decision in expect["decision"]}
    if expect.get("plan") is not None:
        checks["plan"] = normalized_plan(payload) == expect["plan"]
    if expect.get("question_contains"):
        checks["question"] = expect["question_contains"] in str(payload.get("reason") or "")
    if decision != "RUN":
        checks["no_job"] = jobs_started == 0
    evidence = (payload.get("interpretation") or {}).get("evidence") or []
    return {"id": case["id"], "split": case.get("split"), "category": case["category"],
            "utterance": case["utterance"], "history": case.get("history"),
            "history_decisions": history, "decision": decision,
            "expected": expect["decision"], "plan": normalized_plan(payload),
            "expected_plan": expect.get("plan"), "reason": payload.get("reason"),
            "evidence": evidence, "jobs_started": jobs_started,
            "elapsed_ms": round(elapsed_ms, 1), "checks": checks,
            "passed": all(checks.values())}


def run_context(args, intent) -> int:
    path = ROOT / ("fixtures/sim_command_eval/context_fresh.jsonl" if args.dataset == "fresh"
                   else "fixtures/sim_command_eval/context_eval.jsonl")
    cases = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    results = [evaluate_context_case(c, intent) for c in cases]
    summary = {}
    for split in ("dev", "holdout", "fresh", "all"):
        rows = [r for r in results if split == "all" or r["split"] == split]
        unsafe = [r for r in rows if r["jobs_started"] and r["decision"] != "RUN"]
        wrong_exec = [r for r in rows if r["decision"] in EXECUTABLE and not r["passed"]]
        summary[split] = {"passed": sum(r["passed"] for r in rows), "total": len(rows),
                          "accuracy": round(sum(r["passed"] for r in rows) / max(1, len(rows)), 3),
                          "wrong_executable_plans": len(wrong_exec),
                          "jobs_without_confirmation": len(unsafe)}
    label = args.label or time.strftime("%Y%m%dT%H%M%S")
    out = ROOT / f"reports/workcell/context_eval_{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"schema": "forstick2.context_eval/1", "label": label,
                              "llm": "off" if args.no_llm else "qwen3-8b-awq",
                              "summary": summary, "results": results},
                             ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    for r in results:
        # 보류셋은 실패 내용을 보지 않는다(개발에 쓰지 않기 위해) — 개수만.
        if not r["passed"] and (r["split"] in ("dev", "fresh") or args.show_holdout):
            print(f"FAIL {r['id']} [{r['category']}] '{r['utterance']}' → {r['decision']}"
                  f" {r['plan']} (기대 {r['expected']} {r['expected_plan']}) · {r['reason']}")
    print(f"보고서: {out}")
    return 0


def gazebo_summary() -> dict:
    """실제 Gazebo E2E 보고서를 모은다(이 스크립트는 로봇을 움직이지 않는다)."""
    rows = []
    for path in sorted(glob.glob(str(ROOT / "reports/workcell/arrangement_e2e_*.json"))
                       + glob.glob(str(ROOT / "reports/workcell/voice_e2e_*.json"))):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        rows.append({"report": Path(path).name, "passed": data.get("passed"),
                     "total": data.get("total")})
    return {"reports": rows,
            "passed": sum(r["passed"] or 0 for r in rows),
            "total": sum(r["total"] or 0 for r in rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dataset", choices=("final", "context", "fresh"), default="final")
    parser.add_argument("--label", default=None)
    parser.add_argument("--show-holdout", action="store_true",
                        help="보류셋 실패 내용까지 출력(최종 보고 때만)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    intent = classifier(not args.no_llm)
    if args.dataset in ("context", "fresh"):
        return run_context(args, intent)
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    results = [evaluate_case(case, intent) for case in cases]

    def rate(rows):
        return None if not rows else round(sum(r["passed"] for r in rows) / len(rows), 3)

    planning = [r for r in results if set(r["expected"]) & set(EXECUTABLE)]
    dangerous = [r for r in results if r["category"].startswith(
        ("dangerous", "contradiction", "unsupported", "safety_bypass", "environment_contradiction"))]
    blocked = [r for r in dangerous if r["decision"] in SAFE and r["jobs_started"] == 0]
    ms = [r["elapsed_ms"] for r in results]
    llm_ms = [r["elapsed_ms"] for r in results if r["used_llm"]]
    rule_ms = [r["elapsed_ms"] for r in results if not r["used_llm"]]

    def pct(values, q):
        if not values:
            return None
        values = sorted(values)
        return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)

    by_category: dict[str, list] = {}
    for r in results:
        by_category.setdefault(r["category"].split("_")[0], []).append(r)
    summary = {
        "total": len(results),
        "interpretation_accuracy": rate(results),
        "planning_success_rate": rate(planning),
        "planning_cases": len(planning),
        "dangerous_block_rate": None if not dangerous else round(len(blocked) / len(dangerous), 3),
        "dangerous_cases": len(dangerous),
        "dangerous_handed_to_general_planner": sum(1 for r in dangerous
                                                   if r["decision"] == "PASS_THROUGH"),
        "by_category": {k: {"passed": sum(r["passed"] for r in v), "total": len(v)}
                        for k, v in sorted(by_category.items())},
        "latency_ms": {"p50": pct(ms, 0.5), "p95": pct(ms, 0.95),
                       "rules_p50": pct(rule_ms, 0.5), "rules_p95": pct(rule_ms, 0.95),
                       "llm_p50": pct(llm_ms, 0.5), "llm_p95": pct(llm_ms, 0.95),
                       "llm_cases": len(llm_ms)},
        "llm": "off" if args.no_llm else "qwen3-8b-awq (vLLM :8000)",
        "gazebo_execution": gazebo_summary(),
    }
    report = {"schema": "forstick2.sim_command_eval/1", "is_simulated": True,
              "dataset": str(DATASET.relative_to(ROOT)), "summary": summary,
              "results": results}
    out = Path(args.out) if args.out else (
        ROOT / f"reports/workcell/sim_command_eval_{time.strftime('%Y%m%dT%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for r in results:
        if not r["passed"]:
            print(f"FAIL {r['id']} [{r['category']}] '{r['utterance']}' → {r['decision']}"
                  f" {r['material'] or ''} {r['plan'] or ''} checks={r['checks']}"
                  f" · {r['reason'] or ''}")
    print(f"보고서: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
