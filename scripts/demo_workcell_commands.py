#!/usr/bin/env python3
"""텍스트 명령 시연 (8-09 우선 작업 3).

요구된 6개 명령을 **웹 API로** 순서대로 실행한다. 브라우저가 쓰는 경로와 같다
(`/v1/plan` → `/v1/decision` → `/v1/execute`, 정지는 `/v1/stop`).

  1. 안전 위치로 이동해줘
  2. 1번 팔레트 위치로 이동해줘
  3. 2번 팔레트 위치로 이동해줘
  4. 3번 팔레트 위치로 이동해줘
  5. 컨베이어 위치로 이동해줘
  6. 멈춰                     ← **계획 생성을 거치지 않고 즉시 전체 정지**

반복 실행할 수 있다. 시작과 끝에 안전 home으로 돌아간다.
각 단계마다 장면 프레임을 저장해 사용자가 나중에 확인할 수 있게 한다
(서버가 렌더링한다 — GUI 창 부하와 무관하다).

**대기를 늘려 실패를 통과시키지 않는다.** 관측 도달·정지 확인이 기준이다.
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
OUT = ROOT / "reports/workcell/demo_commands.json"
DEFAULT_FRAMES = Path("/tmp/forstick2_workcell/demo_frames")

MOVE_COMMANDS = [
    "안전 위치로 이동해줘",
    "1번 팔레트 위치로 이동해줘",
    "2번 팔레트 위치로 이동해줘",
    "3번 팔레트 위치로 이동해줘",
    "컨베이어 위치로 이동해줘",
]
STOP_COMMAND = "멈춰"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8093")
    parser.add_argument("--frames", default=str(DEFAULT_FRAMES),
                        help="장면 프레임 저장 디렉터리 (없으면 저장 생략)")
    parser.add_argument("--no-frames", action="store_true")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    frames_dir = None if args.no_frames else Path(args.frames)
    if frames_dir is not None:
        frames_dir.mkdir(parents=True, exist_ok=True)

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

    def frame(label: str) -> str | None:
        if frames_dir is None:
            return None
        target = frames_dir / f"{label}.png"
        done = subprocess.run(
            [sys.executable, str(ROOT / "scripts/capture_workcell_scene.py"),
             "--out", str(target)],
            capture_output=True, text=True, timeout=180)
        return str(target) if done.returncode == 0 else None

    status, config = call("GET", "/v1/config")
    workcell = (config.get("robot") or {}).get("workcell") or {}
    if not workcell.get("registered"):
        print("작업 셀이 연결되지 않았다 — ./scripts/run_web_workcell.sh로 띄운다",
              file=sys.stderr)
        return 3
    stop_keywords = list(
        ((config.get("policies") or {}).get("stt") or {}).get("stop_keywords") or [])

    _, session = call("POST", "/v1/sessions", {})
    session_id = session["session_id"]
    steps: list[dict] = []

    print(f"시연 시작 — world={workcell.get('world')}"
          f" partition={workcell.get('gz_partition')}")
    if frames_dir is not None:
        print(f"  장면 프레임: {frames_dir}")

    for index, utterance in enumerate(MOVE_COMMANDS, start=1):
        print(f"\n[{index}/6] {utterance}")
        _, payload = call("POST", "/v1/plan",
                          {"session_id": session_id, "utterance": utterance})
        if not payload.get("ok"):
            entry = {"no": index, "utterance": utterance, "passed": False,
                     "blocked": True, "reason_code": payload.get("reason_code"),
                     "detail": payload.get("detail")}
            steps.append(entry)
            print(f"      차단 {entry['reason_code']}")
            continue
        plan = payload["plan"]
        validation = payload["validation"]
        verdict = {"allow": "PASS", "ask": "ASK",
                   "block": "BLOCK"}.get(validation["decision"])
        print(f"      판단 {verdict} · 스텝"
              f" {[s['skill'] for s in plan['steps']]}")
        if not payload.get("executable"):
            entry = {"no": index, "utterance": utterance, "passed": False,
                     "verdict": verdict,
                     "reason_code": validation.get("reason_code")}
            steps.append(entry)
            continue
        # 화면의 "실행 시작"이 하는 일과 같다: 사용자 결정을 기록하고 실행한다.
        _, decision = call("POST", "/v1/decision", {
            "session_id": session_id, "request_id": payload["request_id"],
            "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
            "decision": "approve", "note": "시연 스크립트"})
        # 실행은 동기 응답이다. 계획이 `home`으로 끝나므로 응답을 기다린 뒤
        # 찍으면 항상 home만 보인다. **실행 중에도 프레임을 남긴다** —
        # 사용자가 팔이 그 위치에 간 것을 볼 수 있어야 한다.
        import threading

        holder: dict = {}

        def execute() -> None:
            holder["value"] = call("POST", "/v1/execute", {
                "session_id": session_id, "request_id": payload["request_id"],
                "plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"],
                "robot_id": plan["robot_id"], "profile_id": plan["profile_id"],
                "profile_version": plan["profile_version"],
                "approval_id": decision.get("approval_id")})

        worker = threading.Thread(target=execute, daemon=True)
        worker.start()
        mid_frames: list[str] = []
        # move 스텝이 끝나고 home으로 돌아가기 전 구간을 노린다. 못 찍으면
        # 그 사실을 그대로 남긴다(빈 그림을 만들지 않는다).
        while worker.is_alive() and len(mid_frames) < 2:
            time.sleep(4.0)
            if not worker.is_alive():
                break
            saved_mid = frame(f"{index:02d}_mid{len(mid_frames) + 1}")
            if saved_mid:
                mid_frames.append(saved_mid)
        worker.join(timeout=300)
        if "value" not in holder:
            entry = {"no": index, "utterance": utterance, "passed": False,
                     "detail": "실행 응답을 받지 못했다"}
            steps.append(entry)
            print("      실행 응답을 받지 못했다")
            continue
        _, result = holder["value"]
        final = result.get("final") or {}
        evidence = final.get("evidence") or {}
        rows = [{"index": row["index"], "skill": row["skill"],
                 "pose": (row.get("evidence") or {}).get("pose"),
                 "target_reached": row["target_reached"],
                 "max_error_rad": (row.get("evidence") or {}).get("max_error_rad")}
                for row in result.get("steps", [])]
        saved = frame(f"{index:02d}_{'_'.join(s['skill'] for s in plan['steps'])}")
        entry = {
            "no": index, "utterance": utterance, "verdict": verdict,
            "plan_steps": [s["skill"] for s in plan["steps"]],
            "step_args": [s.get("args") for s in plan["steps"]],
            "final_state": final.get("state"),
            "task_succeeded": final.get("task_succeeded"),
            "final_reason_code": final.get("reason_code"),
            "hold_required": (evidence.get("hold_requirement") or {}).get(
                "hold_required"),
            "hold_observation_performed": evidence.get(
                "hold_observation_performed"),
            "planning_scene_revalidated": evidence.get(
                "planning_scene_revalidated"),
            "steps": rows,
            "frame": saved,
            "frames_during_execution": mid_frames,
            # 스텝 기록이 없으면 통과로 세지 않는다.
            "passed": bool(rows and final.get("task_succeeded") is True
                           and all(r["target_reached"] for r in rows)),
        }
        steps.append(entry)
        print(f"      실행 {entry['final_state']}"
              f" 성공={entry['task_succeeded']}"
              f" 최대오차={max((r['max_error_rad'] or 0) for r in rows) if rows else 0:.5f} rad"
              f" 스텝 {len(rows)}개")
        if mid_frames:
            print(f"      실행 중 프레임 {len(mid_frames)}장: {mid_frames[0]}")
        if saved:
            print(f"      완료 프레임 {saved}")

    # 6번: 정지 발화. **계획 생성을 거치지 않는다.**
    print(f"\n[6/6] {STOP_COMMAND}  (계획 생성을 거치지 않는다)")
    matched = any(k.replace(" ", "") in STOP_COMMAND.replace(" ", "")
                  for k in stop_keywords)
    _, stop = call("POST", "/v1/stop", {"session_id": session_id})
    confirm = stop.get("confirm_result") or {}
    saved = frame("06_stop")
    stop_entry = {
        "no": 6, "utterance": STOP_COMMAND,
        "route": "정지 키워드 일치 → /v1/stop (LLM 우회)",
        "policy_stop_keywords": stop_keywords,
        "keyword_matched": matched,
        "requested": stop.get("requested"), "confirmed": stop.get("confirmed"),
        "state": stop.get("state"), "reason_code": stop.get("reason_code"),
        "confirm_evidence": confirm.get("evidence"),
        "frame": saved,
        "passed": bool(matched and stop.get("confirmed")
                       and confirm.get("verified") is True),
    }
    steps.append(stop_entry)
    print(f"      {stop_entry['state']} confirmed={stop_entry['confirmed']}"
          f" 키워드일치={matched}")

    # 끝에 안전 home으로 되돌린다(반복 실행 가능하게).
    restore = subprocess.run(
        [sys.executable, str(ROOT / "scripts/goto_workcell_pose.py"),
         "--pose", "safe_home", "--seconds", "6"],
        capture_output=True, text=True, timeout=300)
    print(f"\n복귀: {restore.stdout.strip() or restore.stderr.strip()[:120]}")

    passed = sum(1 for entry in steps if entry["passed"])
    report = {
        "schema": "forstick2.workcell_demo_commands/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base": args.base,
        "is_simulated": True,
        "real_hardware_verified": False,
        "workcell": {k: workcell.get(k) for k in
                     ("workcell_id", "world", "gz_partition", "ros_domain_id")},
        "frames_dir": None if frames_dir is None else str(frames_dir),
        "repeatable": True,
        "restored_to": "safe_home",
        "restore_output": restore.stdout.strip()[:200],
        "steps": steps,
        "passed_count": passed,
        "total_count": len(steps),
        "note": "정지 발화는 계획 생성을 거치지 않는다. 정지만 담은 계획은"
                " 안전 정책의 종료 스킬 요구(E-SEQ-002)와 충돌해 차단된다",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\n시연 {passed}/{len(steps)} 통과 — {out.relative_to(ROOT)}")
    return 0 if passed == len(steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
