#!/usr/bin/env python3
"""실행 결과 판정 계약 검증 (8-09 우선 작업 1).

웹 API에 **실제로 요청을 보내** 판정 계약이 요구대로 동작하는지 확인한다.

확인 항목:
  01 home 성공
  02 빈손 1번 팔레트 접근 move 성공
  03 빈손 컨베이어 접근 move 성공
  04 전역 STOP 관측 확인 성공
  05 관측 불가 상태의 전역 STOP → **정지 미확인 실패**
     (joint_state_broadcaster를 잠시 비활성화해 관측 경로를 끊는다. 되돌린다.)
  06 pick 계획 → 차단 또는 `exec.unverifiable` (성공으로 바뀌지 않는다)
  07 순수 모션 계획에서 **파지 관측을 하지 않는다**
     (응답의 `hold_observation_performed=false`로 확인한다)

대기를 늘려 실패를 통과시키지 않는다. 기대와 다르면 실패로 센다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/workcell/outcome_contract.json"
BROADCASTER = "joint_state_broadcaster"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8093")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--skip-unobservable-stop", action="store_true",
                        help="05번(컨트롤러 비활성화)을 건너뛴다")
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
    workcell = (config.get("robot") or {}).get("workcell") or {}
    if not workcell.get("registered"):
        print("작업 셀이 연결되지 않았다 — ./scripts/run_web_workcell.sh로 띄운다",
              file=sys.stderr)
        return 3

    _, session = call("POST", "/v1/sessions", {})
    session_id = session["session_id"]
    checks: dict[str, dict] = {}

    def record(name: str, entry: dict) -> None:
        checks[name] = entry
        print(f"  [{'통과' if entry['passed'] else '실패'}] {name}: "
              f"{entry.get('summary', '')}")

    def run_plan(utterance: str) -> dict:
        """계획 → 결정 → 실행. 화면의 '실행 시작'과 같은 경로다."""
        _, payload = call("POST", "/v1/plan",
                          {"session_id": session_id, "utterance": utterance})
        if not payload.get("ok"):
            return {"blocked": True, "reason_code": payload.get("reason_code"),
                    "detail": payload.get("detail") or payload.get("clarification"),
                    "draft_steps": payload.get("draft_steps")}
        plan = payload["plan"]
        validation = payload["validation"]
        if not payload.get("executable"):
            return {"blocked": True, "verdict": validation["decision"],
                    "reason_code": validation.get("reason_code"),
                    "steps": [s["skill"] for s in plan["steps"]]}
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
        return {"blocked": False,
                "steps": [s["skill"] for s in plan["steps"]],
                "step_args": [s.get("args") for s in plan["steps"]],
                "result": result}

    def motion_entry(name: str, utterance: str, expect_steps=None) -> dict:
        run = run_plan(utterance)
        if run["blocked"]:
            return {"passed": False, "utterance": utterance, **run,
                    "summary": f"차단됨 {run.get('reason_code')}"}
        result = run["result"]
        final = result.get("final") or {}
        evidence = final.get("evidence") or {}
        requirement = evidence.get("hold_requirement") or {}
        steps = [
            {"index": row["index"], "skill": row["skill"],
             "target_reached": row["target_reached"],
             "pose": (row.get("evidence") or {}).get("pose"),
             "max_error_rad": (row.get("evidence") or {}).get("max_error_rad"),
             "planning_scene_valid": ((row.get("evidence") or {})
                                      .get("planning_scene") or {}).get("valid")}
            for row in result.get("steps", [])
        ]
        passed = bool(
            final.get("task_succeeded") is True
            and final.get("state") == "completed"
            and final.get("reason_code") is None
            and requirement.get("hold_required") is False
            # 파지 관측을 **하지 않았다**는 사실도 함께 확인한다.
            and evidence.get("hold_observation_performed") is False
            and evidence.get("planning_scene_revalidated") is True
            and all(s["target_reached"] for s in steps))
        if expect_steps is not None:
            passed = passed and run["steps"] == expect_steps
        return {
            "passed": passed, "utterance": utterance,
            "plan_steps": run["steps"], "step_args": run["step_args"],
            "final_state": final.get("state"),
            "task_succeeded": final.get("task_succeeded"),
            "final_reason_code": final.get("reason_code"),
            "hold_required": requirement.get("hold_required"),
            "hold_basis": requirement.get("basis"),
            "hold_observation_performed": evidence.get("hold_observation_performed"),
            "planning_scene_revalidated": evidence.get("planning_scene_revalidated"),
            "steps": steps,
            "summary": f"{final.get('state')} 성공={final.get('task_succeeded')}"
                       f" 파지요구={requirement.get('hold_required')}"
                       f" 파지관측={evidence.get('hold_observation_performed')}",
        }

    record("01_home_success", motion_entry(
        "01_home_success", "안전 위치로 이동해줘", expect_steps=["home"]))
    record("02_move_pallet_1_success",
           motion_entry("02", "1번 팔레트 위치로 이동해줘"))
    record("03_move_conveyor_success",
           motion_entry("03", "컨베이어 위치로 이동해줘"))

    # 04 전역 STOP — 관측 확인
    _, stop = call("POST", "/v1/stop", {"session_id": session_id})
    confirm = stop.get("confirm_result") or {}
    record("04_global_stop_confirmed", {
        "passed": bool(stop.get("ok") and stop.get("confirmed")
                       and confirm.get("verified") is True
                       and confirm.get("state") == "stopped"),
        "requested": stop.get("requested"), "confirmed": stop.get("confirmed"),
        "state": stop.get("state"), "reason_code": stop.get("reason_code"),
        "confirm_result": confirm,
        "summary": f"{stop.get('state')} confirmed={stop.get('confirmed')}",
    })

    # 05 관측 경로를 끊고 전역 STOP — **정지 미확인이어야 한다**
    if args.skip_unobservable_stop:
        record("05_global_stop_unconfirmed", {
            "passed": False, "skipped": True,
            "summary": "--skip-unobservable-stop로 건너뜀"})
    else:
        def controller(action: str) -> str:
            command = ["ros2", "control", "switch_controllers",
                       f"--{action}", BROADCASTER]
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=120)
            return (done.stdout + done.stderr).strip()[:200]

        off = controller("deactivate")
        time.sleep(3)
        _, stop2 = call("POST", "/v1/stop", {"session_id": session_id})
        confirm2 = stop2.get("confirm_result") or {}
        on = controller("activate")
        time.sleep(3)
        record("05_global_stop_unconfirmed", {
            "passed": bool(stop2.get("ok") is False
                           and stop2.get("confirmed") is False
                           and stop2.get("reason_code") == "exec.stop_unconfirmed"),
            "method": f"{BROADCASTER}를 잠시 비활성화해 /joint_states를 끊었다",
            "deactivate_output": off, "activate_output": on,
            "ok": stop2.get("ok"), "confirmed": stop2.get("confirmed"),
            "state": stop2.get("state"), "reason_code": stop2.get("reason_code"),
            "confirm_result": confirm2,
            "note": "대기를 늘려 통과시키지 않는다. 관측이 없으면 미확인이다",
            "summary": f"{stop2.get('state')} reason={stop2.get('reason_code')}",
        })

    # 06 pick 계획 — 차단 또는 확인 불가. **성공으로 바뀌지 않는다.**
    run = run_plan("1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘")
    if run["blocked"]:
        entry = {
            "passed": run.get("reason_code") == "capability.profile_incomplete",
            "blocked": True, "reason_code": run.get("reason_code"),
            "detail": run.get("detail"), "draft_steps": run.get("draft_steps"),
            "summary": f"차단 {run.get('reason_code')}",
        }
    else:
        final = (run["result"].get("final") or {})
        entry = {
            "passed": final.get("task_succeeded") is not True,
            "blocked": False, "final_state": final.get("state"),
            "final_reason_code": final.get("reason_code"),
            "summary": f"{final.get('state')} {final.get('reason_code')}",
        }
    record("06_pick_not_succeeded", entry)

    # 07 순수 모션 계획에서 파지 관측을 하지 않았다 (01~03의 근거를 모아 본다)
    motion_names = ["01_home_success", "02_move_pallet_1_success",
                    "03_move_conveyor_success"]
    performed = [checks[n].get("hold_observation_performed") for n in motion_names]
    record("07_no_grasp_observation_on_motion_plans", {
        "passed": all(value is False for value in performed),
        "checked": motion_names, "hold_observation_performed": performed,
        "note": "요구되지 않는 계획에서는 adapter.state()를 호출하지 않는다."
                " 단위 테스트가 호출 횟수 0을 확인한다"
                " (tests/unit/test_outcome_hold_requirement.py)",
        "summary": f"파지 관측 수행 여부 {performed}",
    })

    passed = sum(1 for entry in checks.values() if entry["passed"])
    report = {
        "schema": "forstick2.workcell_outcome_contract/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": args.base,
        "is_simulated": True,
        "real_hardware_verified": False,
        "workcell": {k: workcell.get(k) for k in
                     ("workcell_id", "world", "gz_partition", "ros_domain_id")},
        "contract": {
            "hold_required_basis": "TaskPlan.terminal_hold 또는 스킬 카탈로그가"
                                   " object로 선언한 인자를 쓰는 스텝",
            "not_skill_name_based": True,
            "stop_verdict": "요청 접수가 아니라 confirm_stopped()의 관측 결과",
            "module": "validation/outcome_verifier.py",
        },
        "checks": checks,
        "passed_count": passed,
        "total_count": len(checks),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"  {out.relative_to(ROOT)} 기록 — {passed}/{len(checks)} 통과")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
