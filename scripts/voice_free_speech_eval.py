#!/usr/bin/env python3
"""자유 발화 해석 시험(2026-10-08) — 일반 경로 `Api.create_plan`을 그대로 부르고 **계획·검증까지만** 본다.

    source ~/voiceeval-run/env.sh && .venv/bin/python scripts/voice_free_speech_eval.py dev
    ... final   # 최종 평가(개선에 쓰지 않은 자료)

- 승인·실행(`decide`·`execute`)을 부르지 않는다. 사례마다 새 세션(앞 사례의 맥락이 섞이지 않게).
- **격리 확인**: 작업 셀 매니페스트가 음성 평가 전용(active_voiceeval.json)이고 ROS 도메인·파티션이 라이브(44·
  forstick2_fr3_workcell)가 아니며 DB가 운영 DB가 아니어야 시작한다. 아니면 거절.
- 결과: `~/voiceeval-run/reports/free_speech/<split>-<시각>.json` — 사례별 원문·기대·판정·Qwen 해석(작업들)·
  되묻기 문구·계획 단계. 해석 실패(모델 응답 없음·형식 오류)와 서버 판정(되묻기·차단)을 나눠 남긴다.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUN = Path.home() / "voiceeval-run"


def require_isolation() -> None:
    bad = []
    if Path(os.environ.get("FORSTICK2_WORKCELL_MANIFEST", "")).name != "active_voiceeval.json":
        bad.append("FORSTICK2_WORKCELL_MANIFEST가 active_voiceeval.json이 아니다")
    if os.environ.get("ROS_DOMAIN_ID") in (None, "", "44"):
        bad.append("ROS_DOMAIN_ID가 없거나 라이브(44)")
    if os.environ.get("GZ_PARTITION") in (None, "", "forstick2_fr3_workcell"):
        bad.append("GZ_PARTITION이 없거나 라이브")
    if not str(os.environ.get("FORSTICK2_WORKCELL_LOG_DIR", "")).startswith(str(RUN)):
        bad.append("FORSTICK2_WORKCELL_LOG_DIR가 격리 폴더가 아니다")
    if bad:
        sys.exit("격리를 확인할 수 없어 시작하지 않는다: " + "; ".join(bad) + " — source ~/voiceeval-run/env.sh")


def classify(r: dict) -> tuple[str, dict]:
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
    return f"other:{r.get('reason_code')}", {}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("split", nargs="?", default="dev")
    ap.add_argument("--from-stt", default=None)
    ap.add_argument("--text-run", default=None)
    args = ap.parse_args()
    split = args.split
    require_isolation()
    stt_text = text_ok = None
    if args.from_stt:
        stt = json.loads(Path(args.from_stt).read_text(encoding="utf-8"))
        stt_text = {c["id"]: c["hypothesis_raw"] for c in stt["cases"]}
        stt_source = {"dataset": stt["dataset"]["id"], "model_key": stt["model_key"], "file": args.from_stt}
    if args.text_run:
        text_ok = {c["id"]: c["ok"] for c in json.loads(Path(args.text_run).read_text(encoding="utf-8"))["cases"]}
    from server.config import ServerConfig
    from server.runtime import build_runtime
    from server.api import Api, ApiError

    run_id = time.strftime("%Y%m%d-%H%M%S")
    db = RUN / "db" / f"free_speech-{split}-{run_id}.sqlite3"
    db.parent.mkdir(parents=True, exist_ok=True)
    cfg = dataclasses.replace(ServerConfig.from_env(), db_path=db, enable_stt=False, enable_workcell_robot=True,
                              enable_fake_robot=False, session_idle_timeout_sec=86400.0)
    rt = build_runtime(cfg)
    if not rt.robot_configured or not rt.planning.available:
        sys.exit(f"작업 셀·모델을 쓸 수 없다: robot={rt.robot_configured} planning={rt.planning.detail}")
    api = Api(rt)
    rows = [json.loads(l) for l in (ROOT / "fixtures/voice_free_speech" / f"{split}.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    out = []
    for case in rows:
        if stt_text is not None:
            case = {**case, "script": case["utterance"], "utterance": stt_text[case["id"]]}
        sid = api.create_session(origin="voiceeval")["session_id"]
        t0 = time.time()
        try:
            r = api.create_plan(session_id=sid, utterance=case["utterance"])
        except ApiError as exc:
            r = {"ok": False, "reason_code": str(getattr(exc, "reason", "")), "detail": str(exc)}
        got, slots = classify(r)
        exp = case["expect"]
        ok = got == exp["decision"] and all(slots.get(k) == exp[k] for k in ("material", "to") if k in exp)
        interp = r.get("intent_interpretation") or {}
        row = {**case, "got": got, "slots": slots, "ok": ok, "sec": round(time.time() - t0, 2),
               "reason_code": r.get("reason_code"), "clarification": r.get("clarification") or r.get("detail"),
               "interpretation": {k: interp.get(k) for k in ("ok", "action", "confidence", "failure", "reason", "model_id")} if interp else None,
               "interpretation_failed": bool(interp) and interp.get("ok") is False,
               "tasks": r.get("intent_tasks"), "draft_steps": r.get("draft_steps")}
        wrong_plan = got == "plan" and not ok
        row["wrong_plan"] = wrong_plan
        if stt_text is not None:
            row["cause"] = ("ok" if ok else "stt_cause" if text_ok and text_ok.get(case["id"]) else "interpretation_cause")
        out.append(row)
        print(("OK  " if ok else "FAIL"), case["id"], case["category"], case["utterance"], "→", got, slots or "",
              "|", (row["clarification"] or "")[:80], flush=True)
    summary = {"split": split, "cases": len(out), "ok": sum(r["ok"] for r in out),
               "by_category": {}, "interpretation_failures": sum(r["interpretation_failed"] for r in out),
               "model": rt.planning.detail, "run_id": run_id, "db": str(db),
               "wrong_plans": sum(r["wrong_plan"] for r in out),
               "input": "stt" if stt_text is not None else "text"}
    if stt_text is not None:
        summary["stt_source"] = stt_source
        summary["causes"] = {k: sum(r.get("cause") == k for r in out) for k in ("ok", "stt_cause", "interpretation_cause")}
    for r in out:
        c = summary["by_category"].setdefault(r["category"], [0, 0]); c[0] += r["ok"]; c[1] += 1
    tag = f"{split}-stt-{stt_source['dataset']}" if stt_text is not None else split
    path = RUN / "reports" / "free_speech" / f"{tag}-{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"summary": summary, "cases": out}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print("결과:", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
