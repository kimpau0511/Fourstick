#!/usr/bin/env python3
"""명령 해석 평가(2026-10-08) — 일반 경로 `Api.create_plan`을 그대로 부르고 **계획·검증까지만** 본다(승인·실행·정지 없음).

    source ~/interp-run/env.sh && .venv/bin/python scripts/command_interp_eval.py dev|final [--set fixtures/... ]

- 사례마다 새 세션. 사례에 `state`가 있으면 그 사례 동안만 작업 상태(자재 위치)를 **시험용 대역**으로 바꾼다
  (프로세스 안에서만 — 상태 파일·셀을 바꾸지 않는다).
- 결과 분류: 정상 해석 / 적절한 되묻기 / 불필요한 되묻기 / 잘못된 자재·목적지 계획 / 기대값 불일치.
  '잘못된 자재·목적지 계획' = 계획이 만들어졌는데 기대(자재·목적지)와 다르거나, 계획이 없어야 하는데 만들어짐.
- 격리 확인: 매니페스트가 시험 전용, ROS 도메인·파티션이 라이브가 아님, 상태 폴더가 실행 폴더 안. 아니면 시작 거부.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN = Path(os.environ.get("FORSTICK2_EVAL_RUN_DIR") or Path.home() / "interp-run")
KINDS = ("정상 해석", "적절한 되묻기", "불필요한 되묻기", "잘못된 자재·목적지 계획", "기대값 불일치")


def require_isolation() -> None:
    bad = []
    if Path(os.environ.get("FORSTICK2_WORKCELL_MANIFEST", "")).name != "active_voiceeval.json":
        bad.append("FORSTICK2_WORKCELL_MANIFEST가 시험 전용(active_voiceeval.json)이 아니다")
    if os.environ.get("ROS_DOMAIN_ID") in (None, "", "44"):
        bad.append("ROS_DOMAIN_ID가 없거나 라이브(44)")
    if os.environ.get("GZ_PARTITION") in (None, "", "forstick2_fr3_workcell"):
        bad.append("GZ_PARTITION이 없거나 라이브")
    if not str(os.environ.get("FORSTICK2_WORKCELL_LOG_DIR", "")).startswith(str(RUN)):
        bad.append("FORSTICK2_WORKCELL_LOG_DIR가 실행 폴더 안이 아니다")
    if bad:
        sys.exit("격리를 확인할 수 없어 시작하지 않는다: " + "; ".join(bad))


def outcome(r: dict) -> tuple[str, dict]:
    steps = r.get("draft_steps") or []
    place = next((s for s in steps if s.get("skill") == "place"), None)
    dec = (r.get("decision") or "").upper()
    if dec == "NOOP":
        return "noop", {}
    if place:
        a = place.get("args") or {}
        return "plan", {"material": a.get("object"), "to": a.get("to")}
    if dec == "ASK":
        return "ask", {}
    if dec == "BLOCK" or r.get("blocked"):
        return "block", {}
    if r.get("reason_code") == "plan.clarification_required" and not steps:
        return "ask", {}
    return f"other:{r.get('reason_code')}", {}


def classify(expect: dict, got: str, slots: dict) -> str:
    want = expect["decision"]
    if got == "plan":
        if want == "plan" and slots.get("material") == expect.get("material") and slots.get("to") == expect.get("to"):
            return "정상 해석"
        return "잘못된 자재·목적지 계획"
    if want == "plan":
        return "불필요한 되묻기" if got == "ask" else "기대값 불일치"
    if got == want:
        return "적절한 되묻기" if want == "ask" else "정상 해석"
    return "기대값 불일치"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("split")
    ap.add_argument("--set", default="fixtures/command_interp")
    # 음성 경로(합성 음성 보조 시험): STT 결과 파일(scripts/stt_recognition_eval.py)의 **원본** STT 문장을 발화로 쓴다.
    # 원본 문장·신뢰도는 바꾸지 않고 그대로 기록한다. --text-run(같은 split의 텍스트 실행)이 있으면 실패 원인을 나눈다:
    # 텍스트로는 맞았는데 STT 입력으로 틀렸으면 STT 원인, 텍스트로도 틀렸으면 해석 원인.
    ap.add_argument("--from-stt", default=None)
    ap.add_argument("--text-run", default=None)
    args = ap.parse_args()
    require_isolation()
    stt = text_ok = None
    if args.from_stt:
        d = json.loads(Path(args.from_stt).read_text(encoding="utf-8"))
        stt = {c["id"]: c for c in d["cases"]}
        stt_source = {"dataset": d["dataset"]["id"], "model_key": d["model_key"], "file": args.from_stt,
                      "synth_engine": d["dataset"].get("synth_engine"), "source_type": d["dataset"].get("source_type")}
    if args.text_run:
        text_ok = {c["id"]: c["kind"] in ("정상 해석", "적절한 되묻기")
                   for c in json.loads(Path(args.text_run).read_text(encoding="utf-8"))["cases"]}
    from server.config import ServerConfig
    from server.runtime import build_runtime
    from server.api import Api, ApiError

    run_id = time.strftime("%Y%m%d-%H%M%S")
    db = RUN / "db" / f"interp-{args.split}-{run_id}.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = dataclasses.replace(ServerConfig.from_env(), db_path=db, enable_stt=False, enable_workcell_robot=True,
                              enable_fake_robot=False, session_idle_timeout_sec=86400.0)
    rt = build_runtime(cfg)
    if not rt.robot_configured or not rt.planning.available:
        sys.exit(f"작업 셀·모델을 쓸 수 없다: robot={rt.robot_configured} planning={rt.planning.detail}")
    api = Api(rt)
    jobs = rt.sim_demo_jobs
    real_world = jobs.transfer_world
    model_of = {row["resource_id"]: row["gazebo_model"] for row in jobs.workcell.get("resource_map", ()) if row.get("gazebo_model")}
    path = ROOT / args.set / f"{args.split}.jsonl"
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for case in rows:
        if stt is not None:
            c = stt[case["id"]]
            case = {**case, "script": case["utterance"], "utterance": c["hypothesis_raw"],
                    "stt_confidence": c.get("stt_confidence")}
        if case.get("state"):
            base = real_world()
            loc = dict(base.location_of)
            loc.update({model_of[k]: v for k, v in case["state"].items()})
            jobs.transfer_world = (lambda w=dataclasses.replace(base, location_of=loc): w)   # 시험용 상태 대역
        else:
            jobs.transfer_world = real_world
        sid = api.create_session(origin="interp-eval")["session_id"]
        t0 = time.time()
        try:
            r = api.create_plan(session_id=sid, utterance=case["utterance"])
        except ApiError as exc:
            r = {"ok": False, "reason_code": str(getattr(exc, "reason", "")), "detail": str(exc)}
        got, slots = outcome(r)
        kind = classify(case["expect"], got, slots)
        interp = r.get("intent_interpretation") or {}
        row = {**case, "got": got, "slots": slots, "kind": kind, "sec": round(time.time() - t0, 2),
               "reason_code": r.get("reason_code"), "message": r.get("clarification") or r.get("detail"),
               "interpretation": {k: interp.get(k) for k in ("ok", "action", "confidence", "failure", "spelling", "corrections")} if interp else None,
               "tasks": r.get("intent_tasks"), "draft_steps": r.get("draft_steps")}
        if stt is not None:
            good = kind in ("정상 해석", "적절한 되묻기")
            row["cause"] = ("ok" if good else "stt_cause" if text_ok and text_ok.get(case["id"])
                            else "interpretation_cause" if text_ok else "unknown")
        out.append(row)
        mark = {"정상 해석": "OK  ", "적절한 되묻기": "OK  ", "불필요한 되묻기": "ASK!", "잘못된 자재·목적지 계획": "WRONG", "기대값 불일치": "DIFF"}[kind]
        print(mark, case["id"], case["category"], case["utterance"], "→", got, slots or "", "|", (row["message"] or "")[:70], flush=True)
    jobs.transfer_world = real_world
    summary = {"split": args.split, "set": args.set, "cases": len(out), "kinds": {k: sum(r["kind"] == k for r in out) for k in KINDS},
               "by_category": {}, "model": rt.planning.detail, "run_id": run_id, "db": str(db),
               "input": "stt" if stt is not None else "text"}
    if stt is not None:
        summary["stt_source"] = stt_source
        summary["causes"] = {k: sum(r.get("cause") == k for r in out)
                             for k in ("ok", "stt_cause", "interpretation_cause", "unknown")}
    for r in out:
        c = summary["by_category"].setdefault(r["category"], {"ok": 0, "n": 0})
        c["n"] += 1; c["ok"] += r["kind"] in ("정상 해석", "적절한 되묻기")
    tag = f"{args.split}-stt-{stt_source['dataset']}" if stt is not None else args.split
    p = RUN / "reports" / f"interp-{tag}-{run_id}.json"
    p.write_text(json.dumps({"summary": summary, "cases": out}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary["kinds"], ensure_ascii=False), json.dumps(summary.get("causes") or {}, ensure_ascii=False))
    print("결과:", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
