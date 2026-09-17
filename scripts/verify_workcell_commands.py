#!/usr/bin/env python3
"""텍스트 명령 → 계획 → 안전 판단 → 실행 검증 (8-08 우선순위 6).

웹 API에 **실제로 요청을 보내** 요구된 명령들의 판단과 실행을 기록한다.
화면 코드를 흉내내지 않는다 — 브라우저가 쓰는 것과 같은 경로
(`/v1/plan` → `/v1/decision` → `/v1/execute`)를 쓴다.

기록하는 것: 판단(PASS/ASK/BLOCK), 이유 코드, 계획 스텝, 자원 대조, 실행 관측.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/workcell/command_verification.json"

#: 요구된 명령과 기대 결과. **기대와 다르면 실패로 센다.**
CASES = [
    ("안전 위치로 이동해줘", "PASS", None),
    ("1번 팔레트 위치로 이동해줘", "PASS", None),
    ("2번 팔레트 위치로 이동해줘", "PASS", None),
    ("3번 팔레트 위치로 이동해줘", "PASS", None),
    ("컨베이어 위치로 이동해줘", "PASS", None),
    ("그거 저기로 옮겨줘", "ASK", "plan.clarification_required"),
    ("1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘", "BLOCK",
     "capability.profile_incomplete"),
    ("2번 팔레트에서 B자재를 집어서 컨베이어에 올려줘", "BLOCK",
     "capability.profile_incomplete"),
    ("3번 팔레트에서 C자재를 집어서 컨베이어에 올려줘", "BLOCK",
     "capability.profile_incomplete"),
]
#: 정지 발화. **계획 생성을 거치지 않는다** — 화면이 즉시 전체 정지로 보낸다.
STOP_UTTERANCES = ["멈춰", "정지", "스톱"]
VERDICT = {"allow": "PASS", "ask": "ASK", "block": "BLOCK"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8093")
    parser.add_argument("--execute", default="2번 팔레트 위치로 이동해줘",
                        help="PASS 계획 하나를 실제로 실행한다")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    def call(method: str, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            args.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")
        except urllib.error.URLError as exc:
            print(f"서버에 연결할 수 없다: {args.base} — {exc}", file=sys.stderr)
            raise SystemExit(2)

    status, config = call("GET", "/v1/config")
    robot = config.get("robot") or {}
    workcell = robot.get("workcell") or {}
    if not workcell.get("registered"):
        print("작업 셀이 연결되지 않았다 — ./scripts/run_web_workcell.sh로 띄운다",
              file=sys.stderr)
        return 3
    stop_keywords = list(
        ((config.get("policies") or {}).get("stt") or {}).get("stop_keywords") or [])

    _, session = call("POST", "/v1/sessions", {})
    session_id = session["session_id"]

    checks: list[dict] = []
    for utterance, expected, expected_reason in CASES:
        _, payload = call("POST", "/v1/plan",
                          {"session_id": session_id, "utterance": utterance})
        if payload.get("ok"):
            validation = payload["validation"]
            verdict = VERDICT.get(validation["decision"], validation["decision"])
            reason = validation.get("reason_code")
            steps = [{"skill": s["skill"], "args": s.get("args")}
                     for s in payload["plan"]["steps"]]
            geometry = validation.get("geometry") or {}
            entry = {
                "utterance": utterance, "verdict": verdict, "reason_code": reason,
                "executable": payload.get("executable"),
                "steps": steps,
                "validator_id": geometry.get("validator_id"),
                "geometry_input_complete": geometry.get("input_complete"),
            }
        else:
            reason = payload.get("reason_code")
            verdict = "ASK" if reason == "plan.clarification_required" else "BLOCK"
            entry = {
                "utterance": utterance, "verdict": verdict, "reason_code": reason,
                "executable": False,
                "detail": payload.get("detail") or payload.get("clarification"),
                "draft_steps": payload.get("draft_steps"),
                "slots": payload.get("slots"),
            }
        entry["expected_verdict"] = expected
        entry["expected_reason_code"] = expected_reason
        entry["passed"] = (entry["verdict"] == expected
                           and (expected_reason is None
                                or entry["reason_code"] == expected_reason))
        checks.append(entry)
        print(f"  [{'통과' if entry['passed'] else '실패'}] {utterance}"
              f" -> {entry['verdict']} {entry['reason_code'] or ''}")

    # 정지 발화: 화면이 계획 생성을 거치지 않는지 **정책에서** 확인한다.
    stop_entry = {
        "utterances": STOP_UTTERANCES,
        "policy_stop_keywords": stop_keywords,
        "matched": [u for u in STOP_UTTERANCES
                    if any(k.replace(" ", "") in u.replace(" ", "")
                           for k in stop_keywords)],
        "route": "계획 생성을 거치지 않고 전체 정지로 보낸다"
                 " (html/static/js/main.js isStopUtterance)",
        "why": "정지만 담은 계획은 안전 정책의 종료 스킬 요구(E-SEQ-002)와"
               " 충돌해 차단된다 — 실측으로 확인했다",
    }
    stop_entry["passed"] = len(stop_entry["matched"]) >= 2
    checks.append({"utterance": "정지 발화 경로", **stop_entry})
    print(f"  [{'통과' if stop_entry['passed'] else '실패'}] 정지 발화 경로 —"
          f" 정책 키워드 {stop_keywords}, 일치 {stop_entry['matched']}")

    # PASS 계획 하나를 실제로 실행한다(화면의 "실행 시작"과 같은 경로).
    execution: dict = {"utterance": args.execute}
    _, payload = call("POST", "/v1/plan",
                      {"session_id": session_id, "utterance": args.execute})
    if not payload.get("ok") or not payload.get("executable"):
        execution.update({"passed": False,
                          "detail": "실행 가능한 계획을 얻지 못했다"})
    else:
        plan = payload["plan"]
        _, decision = call("POST", "/v1/decision", {
            "session_id": session_id, "request_id": payload["request_id"],
            "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
            "decision": "approve", "note": "화면의 실행 시작"})
        _, result = call("POST", "/v1/execute", {
            "session_id": session_id, "request_id": payload["request_id"],
            "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
            "robot_id": plan["robot_id"], "profile_id": plan["profile_id"],
            "profile_version": plan["profile_version"],
            "approval_id": decision.get("approval_id")})
        steps = []
        for row in result.get("steps", []):
            evidence = row.get("evidence") or {}
            steps.append({
                "index": row["index"], "skill": row["skill"],
                "request_accepted": row["request_accepted"],
                "target_reached": row["target_reached"],
                "task_succeeded": row["task_succeeded"],
                "pose": evidence.get("pose"),
                "max_error_rad": evidence.get("max_error_rad"),
                "gripper_aperture_m": (evidence.get("gripper") or {}).get("aperture_m"),
                "planning_scene_valid": (evidence.get("planning_scene") or {}).get("valid"),
                "controllers": evidence.get("controllers"),
            })
        final = result.get("final") or {}
        execution.update({
            "execution_id": result.get("execution_id"),
            "adapter_kind": result.get("adapter_kind"),
            "is_simulated": result.get("is_simulated"),
            "steps": steps,
            "all_steps_reached": bool(steps) and all(s["target_reached"] for s in steps),
            "final_state": final.get("state"),
            "final_reason_code": final.get("reason_code"),
            "final_evidence": final.get("evidence"),
            "final_note": "파지 상태를 관측할 수 없어 최종 판정은"
                          " exec.unverifiable다. **성공으로 바꾸지 않는다.**"
                          " 각 스텝의 도달은 관측으로 확인됐다",
            # 스텝 도달이 관측으로 확인되면 통과로 센다. 최종 파지 판정은
            # 관측 수단이 없어 별도 항목으로 남긴다(pick/place 관문 조건).
            "passed": bool(steps) and all(s["target_reached"] for s in steps),
        })
    checks.append({"utterance": "PASS 계획 실행", **execution})
    print(f"  [{'통과' if execution.get('passed') else '실패'}] PASS 계획 실행 —"
          f" 스텝 도달 {execution.get('all_steps_reached')},"
          f" 최종 {execution.get('final_reason_code')}")

    passed = sum(1 for c in checks if c.get("passed"))
    report = {
        "schema": "forstick2.workcell_command_verification/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": args.base,
        "is_simulated": True,
        "real_hardware_verified": False,
        "robot": {k: robot.get(k) for k in
                  ("robot_id", "kind", "is_simulated", "supported_skills")},
        "workcell": {k: workcell.get(k) for k in
                     ("workcell_id", "workcell_version", "world", "gz_partition",
                      "ros_domain_id", "adapter")},
        "prompt_note": "계획 생성은 LLM이 한다. 같은 발화가 항상 같은 계획을"
                       " 주지 않는다 — 판단(PASS/ASK/BLOCK)이 가드다",
        "checks": checks,
        "passed_count": passed,
        "total_count": len(checks),
        "pick_place": {
            "enabled": False,
            "reason_code": "capability.profile_incomplete",
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"  {out.relative_to(ROOT)} 기록 — {passed}/{len(checks)} 통과")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
