"""목표 배치 E2E 반복 검증 (Gazebo 시뮬레이션, 실행 중인 웹 서버 경유).

    FORSTICK2_SIM_PICK_PLACE_DEMO=1 python3 scripts/verify_arrangement_e2e.py \
        [--base http://127.0.0.1:8094] [--rounds 2] [--no-stop-test]

한 라운드:
  1. 초기화 — 모든 자재를 원래 자리로(목표 API), 환경 비움
  2. 환경 정보 문장 — "3번 칸 고장났어", "C자재는 없어" → ENVIRONMENT
  3. 목표 문장 — "모든 자재를 컨베이어에 올려줘" → 금지 칸·쓰지 않는 자재를 피한 계획
  4. 환경 해제 — "3번 칸 고쳤어. C자재 다시 있어"
  5. Qwen 해석 — "A자재랑 B자재 자리 좀 서로 바꿔줘"
  6. 순서 지정 — "C자재를 먼저 컨베이어 3번에 올리고 B자재는 원래 자리로 돌려놔"
  7. (첫 라운드만) STOP — 목표 실행 중 STOP → 다음 단계 미시작 확인 → 복구 → 재계획 완료
  8. 초기화

목표마다 판정은 셋 다 통과해야 한다.
  - 목표 상태 completed + final_result.ok (단계마다 상태 기록 대조)
  - reconcile(읽기 전용 관측): 기록된 자재가 모두 on_conveyor
  - 기록의 칸 = 계획의 기대 배치

실제 로봇에 연결하지 않는다. 모든 결과는 is_simulated=true다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports/workcell"
FINAL = ("completed", "failed", "stopped", "cancelled")


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: float = 120.0) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}

    def command(self, text: str, session: str | None = None) -> dict:
        body = {"mode": "simulation_demo", "source": "text", "utterance": text}
        if session:
            body["session_id"] = session
        status, payload = self.call("POST", "/v1/sim-demo/command", body)
        payload["_http"] = status
        return payload

    def status(self) -> dict:
        return self.call("GET", "/v1/sim-demo")[1]

    def wait_job(self, job_id: str, limit: float = 600.0) -> dict:
        deadline = time.time() + limit
        while time.time() < deadline:
            job = self.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1]
            if job.get("status") != "running":
                return job
            time.sleep(3)
        return {"status": "timeout", "job_id": job_id}

    def wait_goal(self, goal_id: str, limit: float = 1800.0, on_poll=None) -> dict:
        deadline = time.time() + limit
        while time.time() < deadline:
            goal = self.call("GET", f"/v1/sim-demo/goals/{goal_id}")[1]
            if on_poll is not None:
                on_poll(goal)
            if goal.get("status") in FINAL:
                return goal
            time.sleep(3)
        return {"status": "timeout", "goal_id": goal_id}

    def wait_idle(self, limit: float = 600.0) -> None:
        deadline = time.time() + limit
        while time.time() < deadline:
            st = self.status()
            goal = st.get("goal") or {}
            if not st.get("running_job") and goal.get("status") not in (
                    "running", "stopping"):
                return
            time.sleep(3)
        raise RuntimeError("작업 셀이 비지 않았다")


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def observe(client: Client, expected: dict | None) -> dict:
    """reconcile(읽기 전용)을 돌리고 기록·관측을 기대 배치와 대조한다."""
    status, started = client.call("POST", "/v1/sim-demo/reconcile", {})
    job_id = (started.get("job") or started).get("job_id")
    if not job_id:
        return {"passed": False, "detail": f"reconcile을 시작하지 못했다: {status} {started}"}
    job = client.wait_job(job_id, 180)
    st = client.status()
    demo = st.get("state") or {}
    records = {m: r for m, r in (demo.get("objects") or {}).items()}
    findings = (demo.get("last_reconcile") or {}).get("findings") or []
    verdicts = {f["model"]: f.get("verdict") for f in findings}
    problems = []
    if job.get("status") != "finished":
        problems.append(f"reconcile job {job.get('status')}")
    for model, record in records.items():
        want_verdict = "on_pallet" if record.get("state") == "on_pallet" else "on_conveyor"
        if verdicts.get(model) != want_verdict:
            problems.append(f"{model} 관측 판정 {verdicts.get(model)} (기대 {want_verdict})")
    centers = {row["slot"]: row.get("center_m")
               for row in (st.get("conveyor") or {}).get("slots") or []}
    observed = {f["model"]: f.get("observed_pose_m") for f in findings}
    if expected is not None:
        for model, where in expected.items():
            record = records.get(model)
            slot = (None if record is None else record.get("pallet")
                    if record.get("state") == "on_pallet" else (record.get("slot") or "slot_1"))
            want = None if where == "origin" else where
            if slot != want:
                problems.append(f"{model} 기록 {slot} ≠ 기대 {want}")
            # 기록만 믿지 않는다 — 관측 pose가 기대 칸 중심 0.02 m 안인가.
            if want is not None and centers.get(want) and observed.get(model):
                import math
                gap = math.dist(observed[model], centers[want])
                if gap > 0.02:
                    problems.append(f"{model} 관측이 {want} 중심에서 {gap:.4f} m")
            # 다른 팔레트: 관측 pose가 그 팔레트 자리 0.02 m 안인가(reconcile 계산).
            if want is not None and want.startswith("loc_pallet"):
                gap = next((f.get("pallet_gap_m") for f in findings
                            if f["model"] == model), None)
                if gap is None or gap > 0.02:
                    problems.append(f"{model} 관측이 {want} 자리에서 {gap} m")
    return {"passed": not problems, "problems": problems,
            "records": {m: (r.get("state"), r.get("slot") or r.get("pallet"))
                        for m, r in records.items()},
            "observed": {f["model"]: {"verdict": f.get("verdict"),
                                      "conveyor_gap_m": f.get("conveyor_gap_m"),
                                      "pallet_gap_m": f.get("pallet_gap_m"),
                                      "observed_pose_m": f.get("observed_pose_m")}
                         for f in findings}}


def run_goal(client: Client, goal: dict, label: str, results: list,
             stop_after_sec: float | None = None) -> dict:
    goal_id = goal["goal_id"]
    plan = [(s["action"], s["material"], s["to"]) for s in goal.get("plan") or []]
    log(f"  계획 {goal_id}: {plan}")
    status, confirmed = client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm",
                                    {"action": "confirm"})
    if status != 202:
        row = {"label": label, "passed": False, "detail": f"confirm {status} {confirmed}"}
        results.append(row)
        return row
    stop_sent = {"at": None}
    started = time.time()

    def maybe_stop(g):
        if stop_after_sec is None or stop_sent["at"] is not None:
            return
        if g.get("status") == "running" and time.time() - started >= stop_after_sec:
            stop_sent["at"] = time.time()
            client.call("POST", f"/v1/sim-demo/goals/{goal_id}/stop", {})
            log(f"  STOP 요청 ({time.time() - started:.0f}s)")

    final = client.wait_goal(goal_id, on_poll=maybe_stop)
    steps = [s.get("status") for s in final.get("plan") or []]
    log(f"  결과 {final.get('status')} · 단계 {steps} · "
        f"{(final.get('final_result') or {}).get('detail')}")
    return {"goal_id": goal_id, "final": final, "steps": steps,
            "expected": (final.get("reasoning") or {}).get("expected"),
            "elapsed_sec": round(time.time() - started, 1)}


def text_goal(client: Client, text: str, label: str, results: list,
              expect_interpreter: str | None = None) -> None:
    log(f"[{label}] {text}")
    payload = client.command(text)
    if payload.get("decision") != "CONFIRM_GOAL":
        results.append({"label": label, "text": text, "passed": False,
                        "detail": f"{payload.get('_http')} {payload.get('decision')}"
                                  f" {payload.get('reason')}"})
        log(f"  실패: {payload.get('decision')} {payload.get('reason')}")
        return
    interp = (payload.get("arrangement_interpretation") or {}).get("interpreted_by")
    evidence = (payload.get("interpretation") or {}).get("evidence") or []
    if evidence:
        log(f"  해석 근거: {evidence}")
    outcome = run_goal(client, payload["goal"], label, results)
    seen = observe(client, outcome.get("expected"))
    ok = (outcome["final"].get("status") == "completed"
          and (outcome["final"].get("final_result") or {}).get("ok") is True
          and seen["passed"]
          and (expect_interpreter is None or interp == expect_interpreter))
    row = {"label": label, "text": text, "passed": ok,
           "interpreted_by": interp, "goal_id": outcome["goal_id"],
           "steps": outcome["steps"], "elapsed_sec": outcome["elapsed_sec"],
           "notes": (outcome["final"].get("reasoning") or {}).get("notes"),
           "plan": [{k: st.get(k) for k in ("action", "material", "from", "to", "reason",
                                             "job_id")}
                    for st in outcome["final"].get("plan") or []],
           "observation": seen}
    results.append(row)
    log(f"  {'PASS' if ok else 'FAIL'} 관측 {seen.get('observed')} {seen.get('problems')}")
    return row


def env_statement(client: Client, text: str, label: str, results: list,
                  expect: dict) -> None:
    log(f"[{label}] {text}")
    payload = client.command(text)
    env = payload.get("environment") or {}
    ok = payload.get("decision") == "ENVIRONMENT" and all(
        sorted(env.get(k) or []) == sorted(v) for k, v in expect.items())
    results.append({"label": label, "text": text, "passed": ok, "environment": env,
                    "decision": payload.get("decision")})
    log(f"  {'PASS' if ok else 'FAIL'} {payload.get('decision')} {env}")


def reset(client: Client, materials: list[str], results: list, label: str) -> None:
    client.call("POST", "/v1/sim-demo/environment",
                {"blocked_slots": [], "unavailable_materials": []})
    status, goal = client.call("POST", "/v1/sim-demo/goals", {
        "goal": "arrange", "spec": {"targets": {m: "origin" for m in materials}}})
    log(f"[{label}] 초기화 — 모든 자재 원래 자리")
    if status != 201:
        results.append({"label": label, "passed": False, "detail": f"{status} {goal}"})
        return
    if not goal.get("plan"):
        client.call("POST", f"/v1/sim-demo/goals/{goal['goal_id']}/confirm",
                    {"action": "confirm"})
        seen = observe(client, {m: "origin" for m in materials})
        results.append({"label": label, "passed": seen["passed"], "steps": [],
                        "observation": seen})
        log(f"  이미 원래 자리 · 관측 {'PASS' if seen['passed'] else 'FAIL'}")
        return
    outcome = run_goal(client, goal, label, results)
    seen = observe(client, {m: "origin" for m in materials})
    ok = outcome["final"].get("status") == "completed" and seen["passed"]
    results.append({"label": label, "passed": ok, "steps": outcome["steps"],
                    "observation": seen})
    log(f"  {'PASS' if ok else 'FAIL'}")


def stop_test(client: Client, results: list) -> None:
    """목표 실행 중 STOP → 다음 단계 미시작 → 복구 → 같은 문장 재계획 완료."""
    text = "B자재는 컨베이어 1번에 놓고 A자재는 컨베이어 2번에 놓아줘"
    log(f"[STOP] {text}")
    payload = client.command(text)
    if payload.get("decision") != "CONFIRM_GOAL":
        results.append({"label": "STOP", "passed": False,
                        "detail": f"{payload.get('decision')} {payload.get('reason')}"})
        return
    outcome = run_goal(client, payload["goal"], "STOP", results, stop_after_sec=25)
    final = outcome["final"]
    stopped_ok = (final.get("status") == "stopped"
                  and all(s in ("pending",) for s in outcome["steps"][1:]))
    log(f"  STOP 판정: status={final.get('status')} 이후 단계={outcome['steps'][1:]}")
    client.wait_idle()
    # 복구: 체크포인트가 있으면 사전검사 → 이어서, 없으면 원위치 복원.
    recovery = []
    for _ in range(3):
        st = client.status()
        rows = [m for m in st.get("materials") or []
                if any((m.get("actions") or {}).get(a) for a in
                       ("resume_preflight", "resume", "restore"))
                and (m.get("record") or {}).get("state") not in (None, "held_on_target")]
        checkpoint = (st.get("state") or {}).get("checkpoint") or {}
        if not rows and not checkpoint:
            break
        row = rows[0] if rows else next(m for m in st["materials"]
                                        if m["model"] == checkpoint.get("model"))
        actions = row.get("actions") or {}
        for action in ("resume_preflight", "resume") if actions.get("resume_preflight") \
                or actions.get("resume") else ("restore",):
            body = {"action": action, "material": row["model"]}
            if action.startswith("resume"):
                body["checkpoint_id"] = checkpoint.get("checkpoint_id")
            status, job = client.call("POST", "/v1/sim-demo/jobs", body)
            done = client.wait_job(job.get("job_id", "")) if status == 202 else {
                "status": "rejected", "detail": job}
            recovery.append({"action": action, "material": row["model"],
                             "http": status, "job_status": done.get("status"),
                             "exit_code": done.get("exit_code"),
                             "report": (done.get("report") or {}).get("status")})
            log(f"  복구 {action} {row['model']} → {done.get('status')}"
                f" {done.get('exit_code')} {(done.get('report') or {}).get('status')}")
            if done.get("exit_code") not in (0, None):
                break
    seen_after_recovery = observe(client, None)
    results.append({"label": "STOP", "passed": stopped_ok and seen_after_recovery["passed"],
                    "steps": outcome["steps"], "recovery": recovery,
                    "observation": seen_after_recovery})
    log(f"  STOP {'PASS' if stopped_ok else 'FAIL'} · 복구 관측"
        f" {'PASS' if seen_after_recovery['passed'] else 'FAIL'}")
    text_goal(client, text, "STOP 후 재계획", results)


def held_stop_test(client: Client, results: list) -> None:
    """자재를 **든 뒤** STOP → 체크포인트 사전검사·재개 → 같은 문장 재계획.

    lift(7단계)가 끝난 뒤, 든 채로 컨베이어에 접근하는 동안 멈춘다. 복구는 최대
    한 번의 사전검사 + 한 번의 재개이고, 실패하면 원위치 복원으로 안전하게 끝낸다
    (무한 재시도 없음).
    """
    text = "B자재는 컨베이어 2번, A자재는 컨베이어 1번에 놓아줘"
    log(f"[파지 후 STOP] {text}")
    payload = client.command(text)
    if payload.get("decision") != "CONFIRM_GOAL":
        results.append({"label": "파지 후 STOP", "passed": False,
                        "detail": f"{payload.get('decision')} {payload.get('reason')}"})
        return
    goal = payload["goal"]
    goal_id = goal["goal_id"]
    client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})
    stop_stage = None
    deadline = time.time() + 600
    while time.time() < deadline:
        g = client.call("GET", f"/v1/sim-demo/goals/{goal_id}")[1]
        job_id = (g.get("plan") or [{}])[0].get("job_id")
        if job_id:
            job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1]
            done = max((row.get("no", 0) for row in job.get("progress") or []), default=0)
            if done >= 7:
                client.call("POST", f"/v1/sim-demo/goals/{goal_id}/stop", {})
                stop_stage = done
                log(f"  STOP 요청 — 완료 단계 {done}/12 (그리퍼 닫기·lift 뒤, 든 채 이동 중)")
                break
            if job.get("status") != "running":
                break
        time.sleep(0.5)
    final = client.wait_goal(goal_id)
    steps = [s.get("status") for s in final.get("plan") or []]
    job_id = (final.get("plan") or [{}])[0].get("job_id")
    job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1] if job_id else {}
    report = job.get("report") or {}
    st = client.status()
    demo = st.get("state") or {}
    checkpoint = demo.get("checkpoint") or {}
    # 경로 순서에 따라 먼저 움직이는 자재가 바뀐다 — 체크포인트가 가리키는 자재를 따른다.
    held_model = checkpoint.get("model") or "material_b"
    planned = {st.get("material"): st.get("to") for st in final.get("plan") or []}
    record = (demo.get("objects") or {}).get(held_model) or {}
    log(f"  결과 {final.get('status')} · 단계 {steps} · 보고 {report.get('status')}"
        f" · 기록 {record.get('state')} · 체크포인트 {checkpoint.get('checkpoint_id')}"
        f" object_state={checkpoint.get('object_state')}")
    stopped_ok = (stop_stage is not None and final.get("status") == "stopped"
                  and steps[1:] == ["pending"] * (len(steps) - 1))
    client.wait_idle()
    recovery = []
    resumed = False
    if checkpoint.get("model") and checkpoint.get("checkpoint_id"):
        for action in ("resume_preflight", "resume"):
            status, started = client.call("POST", "/v1/sim-demo/jobs", {
                "action": action, "material": held_model,
                "checkpoint_id": checkpoint["checkpoint_id"]})
            done = client.wait_job(started.get("job_id", "")) if status == 202 else {
                "status": "rejected", "detail": started}
            row = {"action": action, "http": status, "job_status": done.get("status"),
                   "exit_code": done.get("exit_code"),
                   "report": (done.get("report") or {}).get("status")}
            recovery.append(row)
            log(f"  복구 {action} → {row}")
            if done.get("exit_code") != 0:
                break
        else:
            resumed = True
    if not resumed:
        # 안전한 끝: 원위치 복원(순간 이동). 재시도하지 않는다.
        status, started = client.call("POST", "/v1/sim-demo/jobs",
                                      {"action": "restore", "material": held_model})
        done = client.wait_job(started.get("job_id", "")) if status == 202 else {}
        recovery.append({"action": "restore", "http": status,
                         "exit_code": done.get("exit_code")})
        log(f"  재개 실패 → 원위치 복원 {done.get('exit_code')}")
    seen = observe(client, {held_model: planned.get(held_model)} if resumed else None)
    ok = stopped_ok and resumed and seen["passed"]
    results.append({"label": "파지 후 STOP", "passed": ok, "stop_after_stage": stop_stage,
                    "steps": steps, "job_report": report.get("status"),
                    "stop_confirmed": (report.get("simulation_demo") or {}).get("stop_confirmed"),
                    "checkpoint": {k: checkpoint.get(k) for k in
                                   ("checkpoint_id", "object_state", "model")},
                    "recovery": recovery, "observation": seen})
    log(f"  파지 후 STOP {'PASS' if ok else 'FAIL'} · 관측 {seen.get('observed')}"
        f" {seen.get('problems')}")
    text_goal(client, text, "파지 후 STOP 재계획", results)


def expect_decision(client: Client, text: str, allowed: tuple, label: str,
                    results: list) -> None:
    payload = client.command(text)
    ok = payload.get("decision") in allowed and not payload.get("job")
    confirmation = payload.get("confirmation") or {}
    if confirmation.get("kind") == "goal":
        client.call("POST", f"/v1/sim-demo/goals/{confirmation['goal_id']}/confirm",
                    {"action": "cancel"})
    results.append({"label": label, "text": text, "passed": ok,
                    "decision": payload.get("decision"), "reason": payload.get("reason")})
    log(f"[{label}] {text} → {payload.get('decision')} {payload.get('reason') or ''}")


def held_stop_move_test(client: Client, results: list, text: str = "B자재를 컨베이어 3번으로 옮겨줘",
                        material: str = "material_b", to_slot: str = "slot_3") -> None:
    """칸 → 칸 이동 중 **든 뒤** STOP → 체크포인트 → 재개가 **계획한 목적지 칸**에 놓는가.

    기본 문장은 경로가 비어 있는 B 칸 2 → 칸 3이다(A 칸 1 → 칸 3은 칸 2를 스쳐
    2026-09-25부터 우회 계획이 된다 — 측정한 경로 여유)."""
    log(f"[칸 이동 파지 후 STOP] {text}")
    payload = client.command(text)
    if payload.get("decision") != "CONFIRM_GOAL":
        results.append({"label": "칸 이동 파지 후 STOP", "passed": False,
                        "detail": f"{payload.get('decision')} {payload.get('reason')}"})
        return
    goal_id = payload["goal"]["goal_id"]
    plan = [(s["action"], s["from"], s["to"]) for s in payload["goal"]["plan"]]
    client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})
    stop_stage = None
    deadline = time.time() + 600
    while time.time() < deadline:
        g = client.call("GET", f"/v1/sim-demo/goals/{goal_id}")[1]
        job_id = (g.get("plan") or [{}])[0].get("job_id")
        if job_id:
            job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1]
            done = max((row.get("no", 0) for row in job.get("progress") or []), default=0)
            if done >= 7:
                client.call("POST", f"/v1/sim-demo/goals/{goal_id}/stop", {})
                stop_stage = done
                log(f"  STOP 요청 — 완료 단계 {done}/12 (든 채 도착 칸으로 이동 중)")
                break
            if job.get("status") != "running":
                break
        time.sleep(0.5)
    final = client.wait_goal(goal_id)
    client.wait_idle()
    demo = client.status().get("state") or {}
    checkpoint = demo.get("checkpoint") or {}
    record = (demo.get("objects") or {}).get(material) or {}
    log(f"  결과 {final.get('status')} · 기록 {record.get('state')}/{record.get('slot')}"
        f" · 체크포인트 {checkpoint.get('checkpoint_id')} held={checkpoint.get('object_state')}"
        f" · 묶인 목적지 {checkpoint.get('destination_slot')}")
    ok_stop = (stop_stage is not None and final.get("status") == "stopped"
               and checkpoint.get("destination_slot") == to_slot
               and checkpoint.get("mode") == "slot_move")
    recovery, resumed = [], False
    if checkpoint.get("model") == material and checkpoint.get("object_state") == "held":
        for action in ("resume_preflight", "resume"):
            status, started = client.call("POST", "/v1/sim-demo/jobs", {
                "action": action, "material": material,
                "checkpoint_id": checkpoint["checkpoint_id"]})
            done = client.wait_job(started.get("job_id", "")) if status == 202 else {
                "status": "rejected", "detail": started}
            recovery.append({"action": action, "http": status,
                             "exit_code": done.get("exit_code"),
                             "report": (done.get("report") or {}).get("status")})
            log(f"  복구 {action} → {recovery[-1]}")
            if done.get("exit_code") != 0:
                break
        else:
            resumed = True
    if not resumed:
        status, started = client.call("POST", "/v1/sim-demo/jobs",
                                      {"action": "restore", "material": material})
        client.wait_job(started.get("job_id", "")) if status == 202 else None
        log("  재개 실패 → 원위치 복원(재시도 없음)")
    seen = observe(client, {material: to_slot} if resumed else None)
    ok = ok_stop and resumed and seen["passed"]
    results.append({"label": "칸 이동 파지 후 STOP", "passed": ok, "plan": plan,
                    "stop_after_stage": stop_stage, "checkpoint": {
                        k: checkpoint.get(k) for k in ("checkpoint_id", "mode", "object_state",
                                                       "source_slot", "destination_slot")},
                    "recovery": recovery, "observation": seen})
    log(f"  칸 이동 파지 후 STOP {'PASS' if ok else 'FAIL'} · {seen.get('problems')}")


def held_stop_route_test(client: Client, results: list, text: str, material: str,
                         want_destination: str, expected: str) -> None:
    """팔레트 경로 이송 중 **든 뒤** STOP → 체크포인트(mode=route) → 재개가 묶인 목적지에 놓는가."""
    label = "팔레트 경로 파지 후 STOP"
    log(f"[{label}] {text}")
    payload = client.command(text)
    if payload.get("decision") != "CONFIRM_GOAL":
        results.append({"label": label, "passed": False,
                        "detail": f"{payload.get('decision')} {payload.get('reason')}"})
        log(f"  실패: {payload.get('decision')} {payload.get('reason')}")
        return
    goal_id = payload["goal"]["goal_id"]
    plan = [(s["action"], s["material"], s["from"], s["to"]) for s in payload["goal"]["plan"]]
    log(f"  계획 {plan}")
    client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})
    stop_stage = None
    deadline = time.time() + 600
    while time.time() < deadline:
        g = client.call("GET", f"/v1/sim-demo/goals/{goal_id}")[1]
        job_id = (g.get("plan") or [{}])[0].get("job_id")
        if job_id:
            job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1]
            done = max((row.get("no", 0) for row in job.get("progress") or []), default=0)
            if done >= 7:
                client.call("POST", f"/v1/sim-demo/goals/{goal_id}/stop", {})
                stop_stage = done
                log(f"  STOP 요청 — 완료 단계 {done}/12 (든 채 목적지로 이동 중)")
                break
            if job.get("status") != "running":
                break
        time.sleep(0.5)
    final = client.wait_goal(goal_id)
    client.wait_idle()
    demo = client.status().get("state") or {}
    checkpoint = demo.get("checkpoint") or {}
    record = (demo.get("objects") or {}).get(material) or {}
    log(f"  결과 {final.get('status')} · 기록 {record.get('state')}"
        f" · 체크포인트 {checkpoint.get('checkpoint_id')} held={checkpoint.get('object_state')}"
        f" mode={checkpoint.get('mode')} · 묶인 목적지 {checkpoint.get('destination_slot')}")
    ok_stop = (stop_stage is not None and final.get("status") == "stopped"
               and checkpoint.get("destination_slot") == want_destination
               and checkpoint.get("mode") == "route")
    recovery, resumed = [], False
    if checkpoint.get("model") == material and checkpoint.get("object_state") == "held":
        for action in ("resume_preflight", "resume"):
            status, started = client.call("POST", "/v1/sim-demo/jobs", {
                "action": action, "material": material,
                "checkpoint_id": checkpoint["checkpoint_id"]})
            done = client.wait_job(started.get("job_id", "")) if status == 202 else {
                "status": "rejected", "detail": started}
            recovery.append({"action": action, "http": status,
                             "exit_code": done.get("exit_code"),
                             "report": (done.get("report") or {}).get("status")})
            log(f"  복구 {action} → {recovery[-1]}")
            if done.get("exit_code") != 0:
                break
        else:
            resumed = True
    if not resumed:
        status, started = client.call("POST", "/v1/sim-demo/jobs",
                                      {"action": "restore", "material": material})
        client.wait_job(started.get("job_id", "")) if status == 202 else None
        log("  재개 실패 → 원위치 복원(재시도 없음)")
    seen = observe(client, {material: expected} if resumed else None)
    ok = ok_stop and resumed and seen["passed"]
    results.append({"label": label, "text": text, "passed": ok, "plan": plan,
                    "stop_after_stage": stop_stage, "checkpoint": {
                        k: checkpoint.get(k) for k in ("checkpoint_id", "mode", "object_state",
                                                       "source_slot", "destination_slot")},
                    "recovery": recovery, "observation": seen})
    log(f"  {label} {'PASS' if ok else 'FAIL'} · {seen.get('problems')}")


def pallet_suite_first(client: Client, materials: list[str], results: list) -> None:
    """다른 팔레트로 옮기기 — 점유 목적지를 먼저 비우는 계획 → 실행 → 관측."""
    reset(client, materials, results, "팔레트 초기화")
    # C가 3번(초록) 팔레트를 차지 → C를 빈 칸으로 먼저 빼고 B를 3번 팔레트로.
    text_goal(client, "파란 자재를 초록 팔레트로 옮겨줘", "팔레트 이송(점유 목적지 비우기)",
              results)


def pallet_suite_second(client: Client, materials: list[str], results: list) -> None:
    """(서버 재시작 뒤) 비울 곳 없음 ASK → 다른 팔레트에서 다시 집어 복귀 + 파지 후 STOP."""
    client.call("POST", "/v1/sim-demo/environment",
                {"blocked_slots": ["slot_2", "slot_3"], "unavailable_materials": []})
    expect_decision(client, "C자재는 2번 팔레트로, A자재는 3번 팔레트로 옮겨줘", ("ASK",),
                    "점유 목적지 비울 곳 없음 ASK", results)
    client.call("POST", "/v1/sim-demo/environment",
                {"blocked_slots": [], "unavailable_materials": []})
    held_stop_route_test(client, results, "B자재를 원래 자리로 돌려놔", "material_b",
                         "loc_pallet_2", "origin")
    text_goal(client, "C자재를 컨베이어 1번에서 2번 팔레트로 옮겨줘",
              "칸 → 다른 팔레트", results)
    text_goal(client, "C자재를 원래 자리로 돌려놔", "다른 팔레트 → 원래 자리", results)
    reset(client, materials, results, "팔레트 마무리 초기화")


def observe_only(client: Client, results: list) -> None:
    seen = observe(client, None)
    results.append({"label": "관측 대조(읽기 전용)", "passed": seen["passed"],
                    "observation": seen})
    log(f"  관측 대조 {'PASS' if seen['passed'] else 'FAIL'} {seen.get('records')}"
        f" {seen.get('problems')}")


GZ_WORLD = "forstick2_fr3_2f85_workcell"
GZ_PARTITION = os.environ.get("GZ_PARTITION", "forstick2_fr3_workcell")


def preflight_evidence(client: Client, row: dict | None) -> list[dict]:
    """목표의 단계마다 실행기 보고서의 scene 동기화·경로 검사 증거."""
    out = []
    for step in (row or {}).get("plan") or []:
        job = client.call("GET", f"/v1/sim-demo/jobs/{step.get('job_id')}")[1] \
            if step.get("job_id") else {}
        report = job.get("report") or {}
        sync, path = report.get("scene_sync") or {}, report.get("path_check") or {}
        out.append({"material": step.get("material"), "from": step.get("from"),
                    "to": step.get("to"), "sync_verified": sync.get("verified"),
                    "scene_moves": sorted(((sync.get("plan") or {}).get("moves") or {})),
                    "path_samples": path.get("samples"),
                    "path_failed": path.get("failed_samples")})
    return out


def evidence_goal(client: Client, text: str, label: str, results: list,
                  expect_plan: list | None = None) -> None:
    """목표 실행 + 단계마다 scene 동기화 되읽기 확인·경로 표본 검사 통과를 요구한다."""
    row = text_goal(client, text, label, results)
    if row is None:
        return
    evidence = preflight_evidence(client, row)
    row["preflight"] = evidence
    good = bool(evidence) and all(e["sync_verified"] is True and (e["path_samples"] or 0) > 0
                                  and e["path_failed"] == 0 for e in evidence)
    plan = [(p["material"], p["from"], p["to"]) for p in row.get("plan") or []]
    row["plan_matches"] = expect_plan is None or plan == [tuple(x) for x in expect_plan]
    row["passed"] = bool(row["passed"] and good and row["plan_matches"])
    log(f"  증거 {evidence}")
    log(f"  계획 {plan} (기대 {expect_plan}) · 동기화·경로 {'OK' if good else 'BAD'}"
        f" → {'PASS' if row['passed'] else 'FAIL'}")


def gz_model_position(model: str):
    """Gazebo가 지금 보고하는 자재 위치(`gz model -p`). 못 읽으면 None."""
    import re
    import subprocess
    out = subprocess.run(["gz", "model", "-m", model, "-p"], capture_output=True, text=True,
                         timeout=15, env=dict(os.environ, GZ_PARTITION=GZ_PARTITION))
    match = re.search(r"XYZ.*?\n\s*\[([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\]", out.stdout)
    return None if match is None else tuple(float(v) for v in match.groups())


def gz_set_pose(model: str, xyz) -> bool:
    """검증 전용 결함 주입: Gazebo에서 자재를 옮긴다(기록은 그대로).

    성공은 서비스 응답이 아니라 **되읽은 위치**로 판정한다(응답이 늦어도 적용될 수 있다).
    """
    import math
    import subprocess
    req = (f'name: "{model}", position: {{x: {xyz[0]}, y: {xyz[1]}, z: {xyz[2]}}},'
           ' orientation: {w: 1}')
    for _ in range(2):
        subprocess.run(["gz", "service", "-s", f"/world/{GZ_WORLD}/set_pose",
                        "--reqtype", "gz.msgs.Pose", "--reptype", "gz.msgs.Boolean",
                        "--timeout", "3000", "--req", req],
                       capture_output=True, text=True, timeout=10,
                       env=dict(os.environ, GZ_PARTITION=GZ_PARTITION))
        time.sleep(1.0)
        seen = gz_model_position(model)
        if seen is not None and math.dist(seen[:2], xyz[:2]) <= 0.005:
            return True
    return False


def run_script(args: list[str], label: str) -> dict:
    """실행기를 직접 부른다(서버 계획기를 거치지 않는 경로의 실행기 검사 확인용)."""
    import subprocess
    out = ROOT / f"reports/workcell/clearance_direct_{label}.json"
    out.unlink(missing_ok=True)       # 이전 실행의 보고서를 읽지 않는다
    proc = subprocess.run(["./scripts/demo_workcell_pick_place.sh", *args, "--out", str(out)],
                          cwd=ROOT, capture_output=True, text=True, timeout=600,
                          env=dict(os.environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1"))
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    return {"exit_code": proc.returncode, "report": report,
            "stderr_tail": proc.stderr[-600:], "stdout_tail": proc.stdout[-600:]}


def mismatch_block_test(client: Client, results: list) -> None:
    """기록 ↔ 관측 불일치 → 로봇 명령 없이 차단. 전제: C가 칸 2에 있다."""
    records = (client.status().get("state") or {}).get("objects") or {}
    if (records.get("material_c") or {}).get("slot") != "slot_2":
        status, goal = client.call("POST", "/v1/sim-demo/goals", {
            "goal": "arrange", "spec": {"targets": {"material_c": "slot_2"}}})
        log(f"[불일치 준비] C → 칸 2 ({status})")
        if status == 201:
            outcome = run_goal(client, goal, "불일치 준비", results)
            seen = observe(client, {"material_c": "slot_2"})
            results.append({"label": "불일치 준비(C→칸 2)", "steps": outcome["steps"],
                            "passed": outcome["final"].get("status") == "completed"
                            and seen["passed"], "observation": seen})
    # 기록 ↔ 관측 불일치: 기록상 원래 자리인 B를 Gazebo에서 3 cm 밀어 둔다
    #    → 다음 이송은 scene을 맞추지 못하므로 로봇 명령 없이 막혀야 한다.
    moved = gz_set_pose("material_b", (0.5, 0.03, 0.84))
    time.sleep(1.0)
    log(f"[불일치 주입] material_b 3 cm 이동 (성공={moved})")
    payload = client.command("C자재를 컨베이어 3번으로 옮겨줘")
    blocked_row = {"label": "기록↔관측 불일치 → 차단", "passed": False,
                   "injected": moved, "decision": payload.get("decision")}
    if payload.get("decision") == "CONFIRM_GOAL":
        outcome = run_goal(client, payload["goal"], "불일치 차단", results)
        job_id = (outcome["final"].get("plan") or [{}])[0].get("job_id")
        job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1] if job_id else {}
        report = job.get("report") or {}
        codes = report.get("reason_codes") or []
        blocked_row.update(
            goal_status=outcome["final"].get("status"), reason_codes=codes,
            findings=report.get("findings"),
            passed=bool(moved and outcome["final"].get("status") == "failed"
                        and any("environment_unavailable" in str(c) for c in codes)
                        and not (report.get("stage_records") or [])))
        log(f"  목표 {outcome['final'].get('status')} · 사유 {codes}"
            f" · {report.get('findings')}")
    results.append(blocked_row)
    log(f"  불일치 차단 {'PASS' if blocked_row['passed'] else 'FAIL'}")
    # 정리: B를 선언 자리로 되돌리고(시뮬레이터 복원, 되읽기 확인) 전체 초기화.
    restored = run_script(["pallet_2", "material_b", "--restore-only"], "restore_b")
    log(f"  B 복원 exit={restored['exit_code']}")
    time.sleep(1.0)


def prepare(client: Client, targets: dict, label: str, results: list) -> bool:
    """검증 준비 배치(목표 API). 관측까지 맞아야 참."""
    status, goal = client.call("POST", "/v1/sim-demo/goals",
                               {"goal": "arrange", "spec": {"targets": targets}})
    log(f"[{label}] 준비 {targets} ({status})")
    if status != 201:
        results.append({"label": label, "passed": False, "detail": f"{status} {goal}"})
        return False
    outcome = run_goal(client, goal, label, results)
    seen = observe(client, targets)
    ok = outcome["final"].get("status") == "completed" and seen["passed"]
    results.append({"label": label, "passed": ok, "observation": seen})
    return ok


def idle_after(client: Client, seconds: float = 4.0) -> bool:
    """명령 뒤 승인 전: 실행 중인 작업이 생기지 않는가."""
    end = time.time() + seconds
    while time.time() < end:
        if client.status().get("running_job"):
            return False
        time.sleep(0.5)
    return True


def single_confirm(client: Client, payload: dict, label: str, results: list,
                   expected: dict) -> None:
    """단일 명령 확인 카드 → 승인 → 작업 완료 → 관측 대조."""
    token = (payload.get("confirmation") or {}).get("token")
    no_motion = idle_after(client)
    status, started = client.call("POST", "/v1/sim-demo/confirm",
                                  {"token": token, "action": "confirm"})
    job_id = ((started.get("job") or {}).get("job_id") or started.get("job_id"))
    job = client.wait_job(job_id) if job_id else {"status": f"no job ({status})"}
    seen = observe(client, expected)
    ok = no_motion and job.get("status") == "finished" and job.get("exit_code") == 0 \
        and seen["passed"]
    results.append({"label": label, "passed": ok, "decision": payload.get("decision"),
                    "no_motion_before_confirm": no_motion, "job": job.get("status"),
                    "exit_code": job.get("exit_code"), "observation": seen})
    log(f"  {'PASS' if ok else 'FAIL'} 승인 전 정지={no_motion} · 작업 {job.get('status')}"
        f"/{job.get('exit_code')} · {seen.get('problems')}")


def restore_suite(client: Client, materials: list[str], results: list) -> None:
    """“초록자재다시팔레트로가져다놔” · “원상복귀” — 해석·계획·확인·실행을 따라간다."""
    reset(client, materials, results, "복귀 문장 초기화")
    text1 = "초록자재다시팔레트로가져다놔"
    # 1) C가 컨베이어 칸에 있다 → 확인 카드(단일 복귀) → 승인 → 원래 자리.
    if prepare(client, {"material_c": "slot_2"}, "준비: C 칸 2", results):
        payload = client.command(text1)
        log(f"[C 칸 → 원래] {text1} → {payload.get('decision')} {payload.get('intent')}"
            f" {payload.get('material')} · {(payload.get('interpretation') or {}).get('evidence')}")
        if payload.get("decision") == "CONFIRM" and payload.get("intent") == "return":
            single_confirm(client, payload, "C 칸 → 원래(띄어쓰기 없음)", results,
                           {"material_c": "origin"})
        else:
            results.append({"label": "C 칸 → 원래(띄어쓰기 없음)", "passed": False,
                            "detail": f"{payload.get('decision')} {payload.get('reason')}"})
    # 2) C가 다른 팔레트에 있다 → 목표 카드 → 승인 → 원래 자리.
    if prepare(client, {"material_c": "loc_pallet_1", "material_a": "slot_1"},
               "준비: A 칸 1 · C 1번 팔레트", results):
        text_goal(client, "초록 자재 다시 팔레트로 가져다 놔", "C 다른 팔레트 → 원래", results)
    # 3) C가 이미 원래 자리 → 움직이지 않는다.
    payload = client.command(text1)
    no_motion = idle_after(client, 3.0)
    ok = (payload.get("decision") not in ("RUN", "CONFIRM", "CONFIRM_GOAL") and no_motion
          and "이미 원래 자리" in str(payload.get("reason")))
    results.append({"label": "C 이미 원래 자리 → 움직이지 않음", "passed": ok,
                    "decision": payload.get("decision"), "reason": payload.get("reason")})
    log(f"[C 이미 원래] {payload.get('decision')} {payload.get('reason')} → {'PASS' if ok else 'FAIL'}")
    # 4) 원상복귀: 같은 세션의 직전 작업 맥락 → 되묻기 → '전부' → 목표 카드(대상·순서)
    #    → 승인 전 정지 확인 → 실행 중 STOP → 복구 → 다시 원상복귀로 완료.
    status, session = client.call("POST", "/v1/sessions", {"origin": "e2e"})
    sid = session.get("session_id")
    prep = client.command("B자재는 컨베이어 2번, C자재는 2번 팔레트로 옮겨줘", session=sid)
    if prep.get("decision") == "CONFIRM_GOAL":
        outcome = run_goal(client, prep["goal"], "준비: B 칸 2 · C 2번 팔레트", results)
        seen = observe(client, None)
        results.append({"label": "준비(세션 문장): B 칸 2 · C 2번 팔레트",
                        "passed": outcome["final"].get("status") == "completed"
                        and seen["passed"], "observation": seen})
    asked = client.command("원상복귀", session=sid)
    ok_ask = asked.get("decision") == "ASK" and "전부" in str(asked.get("reason"))
    results.append({"label": "원상복귀 — 직전 작업 맥락이면 되묻기", "passed": ok_ask,
                    "reason": asked.get("reason")})
    log(f"[원상복귀 되묻기] {asked.get('decision')} {asked.get('reason')}")
    answer = client.command("전부", session=sid)
    plan = [(s["material"], s["from"], s["to"], s.get("reason"))
            for s in (answer.get("goal") or {}).get("plan") or []]
    log(f"[원상복귀 '전부'] {answer.get('decision')} · 계획 {plan}")
    no_motion = idle_after(client)
    card_ok = (answer.get("decision") == "CONFIRM_GOAL" and no_motion
               and sorted(p[0] for p in plan) == ["material_a", "material_b", "material_c"]
               and all(p[2] == "origin" for p in plan) and all(p[3] for p in plan))
    results.append({"label": "원상복귀 확인 카드(대상·순서·이유) · 승인 전 정지",
                    "passed": card_ok, "plan": plan, "no_motion_before_confirm": no_motion})
    if answer.get("decision") == "CONFIRM_GOAL":
        outcome = run_goal(client, answer["goal"], "원상복귀 STOP", results,
                           stop_after_sec=25)
        stopped = outcome["final"].get("status") == "stopped"
        log(f"  STOP → {outcome['final'].get('status')} · 단계 {outcome['steps']}")
        client.wait_idle()
        # 복구: 체크포인트가 있으면 사전검사·재개, 없으면 원위치 복원(기존 절차 그대로).
        st = client.status()
        checkpoint = (st.get("state") or {}).get("checkpoint") or {}
        recovery = []
        if checkpoint.get("model"):
            for action in ("resume_preflight", "resume"):
                code, job = client.call("POST", "/v1/sim-demo/jobs", {
                    "action": action, "material": checkpoint["model"],
                    "checkpoint_id": checkpoint.get("checkpoint_id")})
                done = client.wait_job(job.get("job_id", "")) if code == 202 else {}
                recovery.append((action, done.get("status"), done.get("exit_code")))
                if done.get("exit_code") != 0:
                    break
        # 체크포인트가 없는 정지(집기 전)는 기존 절차대로 원위치 복원이다.
        for row in client.status().get("materials") or []:
            record = row.get("record") or {}
            if record.get("state") in ("stopped_unrestored", "fault_unrestored") \
                    and (row.get("actions") or {}).get("restore"):
                code, job = client.call("POST", "/v1/sim-demo/jobs",
                                        {"action": "restore", "material": row["model"]})
                done = client.wait_job(job.get("job_id", "")) if code == 202 else {}
                recovery.append(("restore", row["model"], done.get("status"),
                                 done.get("exit_code")))
        log(f"  복구 {recovery}")
        results.append({"label": "원상복귀 중 STOP → 복구", "passed": stopped and bool(recovery),
                        "steps": outcome["steps"], "recovery": recovery})
        text_goal(client, "원상복귀", "원상복귀 재계획 → 완료", results)
    # 5) 모두 원래 자리 → 움직이지 않는다.
    payload = client.command("원상복귀")
    no_motion = idle_after(client, 3.0)
    ok = payload.get("decision") == "NOOP" and no_motion and not payload.get("goal")
    results.append({"label": "원상복귀 — 이미 모두 원래 자리", "passed": ok,
                    "decision": payload.get("decision"), "reason": payload.get("reason")})
    log(f"[원상복귀 NOOP] {payload.get('decision')} {payload.get('reason')} → {'PASS' if ok else 'FAIL'}")
    seen = observe(client, {m: "origin" for m in materials})
    results.append({"label": "마무리 관측(모두 원래 자리)", "passed": seen["passed"],
                    "observation": seen})


def held_stop_restore_test(client: Client, results: list) -> None:
    """든 뒤 STOP → 재개 대신 **원위치 복원**(실패 복구 경로). 관절이 풀리고 래치가 풀리는가."""
    import json as _json
    label = "파지 후 STOP → 복원(관절 해제)"
    status, goal = client.call("POST", "/v1/sim-demo/goals", {
        "goal": "arrange", "spec": {"targets": {"material_c": "slot_3"}}})
    if status != 201:
        results.append({"label": label, "passed": False, "detail": f"{status} {goal}"})
        return
    goal_id = goal["goal_id"]
    client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})
    stop_stage = None
    deadline = time.time() + 600
    while time.time() < deadline:
        g = client.call("GET", f"/v1/sim-demo/goals/{goal_id}")[1]
        job_id = (g.get("plan") or [{}])[0].get("job_id")
        if job_id:
            job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")[1]
            done = max((row.get("no", 0) for row in job.get("progress") or []), default=0)
            if done >= 7:
                client.call("POST", f"/v1/sim-demo/goals/{goal_id}/stop", {})
                stop_stage = done
                break
            if job.get("status") != "running":
                break
        time.sleep(0.5)
    client.wait_goal(goal_id)
    client.wait_idle()
    joints = Path("/tmp/forstick2_workcell/fixture_joints.json")
    before = _json.loads(joints.read_text()).get("material_c") if joints.exists() else None
    status, job = client.call("POST", "/v1/sim-demo/jobs",
                              {"action": "restore", "material": "material_c"})
    done = client.wait_job(job.get("job_id", "")) if status == 202 else {"status": status}
    after = _json.loads(joints.read_text()).get("material_c") if joints.exists() else None
    seen = observe(client, None)
    st = client.status()
    record = ((st.get("state") or {}).get("objects") or {}).get("material_c")
    ok = (stop_stage is not None and (before or {}).get("state") == "attached"
          and done.get("exit_code") == 0 and (after or {}).get("state") == "detached"
          and record is None and seen["passed"])
    results.append({"label": label, "passed": ok, "stop_after_stage": stop_stage,
                    "joint_before": before, "joint_after": after,
                    "restore_exit": done.get("exit_code"), "record_after": record,
                    "observation": seen})
    log(f"[{label}] 정지 단계 {stop_stage} · 관절 {(before or {}).get('state')} →"
        f" {(after or {}).get('state')} · 복원 {done.get('exit_code')} · 기록 {record}"
        f" → {'PASS' if ok else 'FAIL'}")


def fixture_suite(client: Client, materials: list[str], results: list) -> None:
    """붙이기 방식(관절) — 파지 후 STOP→재개(이송·경로), STOP→복원, 새 이송."""
    reset(client, materials, results, "고정 장치 초기화")
    held_stop_test(client, results)                       # 팔레트→칸 이송 중 든 뒤 STOP→재개
    reset(client, materials, results, "고정 장치 중간 초기화")
    held_stop_restore_test(client, results)
    text_goal(client, "C자재는 컨베이어 3번, B자재는 원래 자리에 놓아줘",
              "복원 뒤 새 이송(래치 해제 확인)", results)
    reset(client, materials, results, "고정 장치 마무리 초기화")


CYCLE_STEPS = (("material_a", "slot_1"), ("material_c", "loc_pallet_1"),
               ("material_a", "loc_pallet_3"), ("material_c", "slot_2"),
               ("material_a", "origin"), ("material_c", "origin"))


def cycles_suite(client: Client, materials: list[str], results: list, rounds: int = 3) -> None:
    """초기화 없이 원래 팔레트·다른 팔레트·컨베이어를 오가며 반복 — 위치 오차가 쌓여도
    다시 집고 놓는가. 막히면 그 이유(검사 거절)를 그대로 남긴다(복원하지 않는다)."""
    reset(client, materials, results, "반복 전 초기화(한 번만)")
    for round_no in range(1, rounds + 1):
        for model, where in CYCLE_STEPS:
            label = f"반복 R{round_no} {model} → {where}"
            status, goal = client.call("POST", "/v1/sim-demo/goals", {
                "goal": "arrange", "spec": {"targets": {model: where}}})
            if status != 201:
                results.append({"label": label, "passed": False, "detail": f"{status} {goal}"})
                log(f"[{label}] 계획 거절 {status} {goal}")
                return
            outcome = run_goal(client, goal, label, results)
            final = outcome["final"]
            reports = []
            for st in final.get("plan") or []:
                job = client.call("GET", f"/v1/sim-demo/jobs/{st.get('job_id')}")[1] \
                    if st.get("job_id") else {}
                rep = job.get("report") or {}
                sync = {m["model"]: round((m.get("record_gap_m") or 0) * 1000, 2)
                        for m in ((rep.get("scene_sync") or {}).get("plan") or {})
                        .get("materials", [])}
                reports.append({"material": st.get("material"), "to": st.get("to"),
                                "status": rep.get("status"), "reason_codes":
                                rep.get("reason_codes"), "findings": rep.get("findings"),
                                "gap_mm_at_start": sync})
            seen = observe(client, None)
            ok = final.get("status") == "completed" and seen["passed"]
            results.append({"label": label, "passed": ok, "steps": outcome["steps"],
                            "jobs": reports, "observation": seen})
            log(f"  {'PASS' if ok else 'FAIL'} · 시작 시 기록↔관측 차(mm) "
                f"{[r['gap_mm_at_start'] for r in reports]} · {[r['status'] for r in reports]}")
            if not ok:
                log(f"  막힌 이유: {[r['findings'] for r in reports]}")
                return


def clearance_suite(client: Client, materials: list[str], results: list) -> None:
    """scene 동기화 + 경로 검사 — 대표 경로와 경로를 막는 자재."""
    reset(client, materials, results, "경로 초기화")
    # 1) 목적지를 먼저 비우는 경로: C를 칸 1로 뺀 뒤(scene에 C를 칸 1로 옮김) B를 3번 팔레트로.
    evidence_goal(client, "파란 자재를 초록 팔레트로 옮겨줘", "비우고 팔레트 이송", results,
                  [("material_c", "origin", "slot_1"), ("material_b", "origin", "loc_pallet_3")])
    # 2) 칸 → 칸 직접(경로가 비어 있음): C 칸 1 → 칸 2.
    evidence_goal(client, "C자재를 컨베이어 2번으로 옮겨줘", "칸 → 칸 직접", results,
                  [("material_c", "slot_1", "slot_2")])
    # 3) 경로에 다른 자재(C, 칸 2) — A 1번 팔레트 → 칸 1 직접 경로가 칸 2를 스친다(측정)
    #    → 계획기가 빈 팔레트(C의 3번은 B가 차지… 비어 있는 곳)를 거쳐 우회한다.
    evidence_goal(client, "A자재를 컨베이어 1번으로 옮겨줘", "경로 막힘 → 우회 계획", results)
    # 4) 실행기 자체 검사: 계획기를 거치지 않고 A 칸 1 → 칸 3(칸 2에 C) 직접 이동을 요청한다.
    #    (a) 측정 표로 계약이 막는다. (b) 표 검사를 빼도(검증 전용) scene 동기화 뒤 live 경로
    #    표본 검사가 막는다. 둘 다 로봇 명령 0, 기록 그대로여야 한다.
    for tag, extra, want in (("table", [], "contract"),
                             ("live", ["--verify-live-path-only"], "live")):
        before = client.status().get("state", {}).get("objects", {}).get("material_a")
        direct = run_script(["pallet_1", "material_a", "--move-from-slot", "slot_1",
                             "--move-to-slot", "slot_3", *extra], f"move_a_1_3_{tag}")
        report = direct["report"]
        after = client.status().get("state", {}).get("objects", {}).get("material_a")
        codes = [str(c) for c in report.get("reason_codes") or []]
        texts = " ".join(str(f.get("detail")) for f in report.get("findings") or [])
        if want == "contract":
            caught = "path_obstructed" in texts
        else:
            caught = ((report.get("path_check") or {}).get("failed_samples", 0) > 0
                      and (report.get("scene_sync") or {}).get("verified") is True)
        ok = (direct["exit_code"] != 0 and report.get("status") == "slot_move_not_started"
              and "geometry.collision" in codes and caught and before == after
              and not report.get("stage_records"))
        results.append({"label": f"실행기 차단 — {tag}(칸 1→3, 칸 2에 C)", "passed": ok,
                         "exit_code": direct["exit_code"], "status": report.get("status"),
                         "reason_codes": codes, "findings": report.get("findings"),
                         "path_check": report.get("path_check"),
                         "scene_sync_verified": (report.get("scene_sync") or {}).get("verified"),
                         "injected": report.get("injected"), "record_unchanged": before == after,
                         "stdout_tail": direct["stdout_tail"], "stderr_tail": direct["stderr_tail"]})
        log(f"[실행기 차단 {tag}] exit={direct['exit_code']} status={report.get('status')}"
            f" codes={codes} path={report.get('path_check')} · {texts[:160]}"
            f" → {'PASS' if ok else 'FAIL'}")
    seen = observe(client, None)
    results.append({"label": "차단 뒤 관측(움직이지 않음)", "passed": seen["passed"],
                    "observation": seen})
    # 5) 다른 팔레트에서 다시 집기 + 파지 후 STOP·재개(재개는 scene을 바꾸지 않고 확인만).
    held_stop_route_test(client, results, "B자재를 원래 자리로 돌려놔", "material_b",
                         "loc_pallet_2", "origin")
    mismatch_block_test(client, results)
    reset(client, materials, results, "경로 마무리 초기화")


def transfer_suite(client: Client, materials: list[str], results: list) -> None:
    """공통 transfer — 칸 → 칸 직접 이동, 연쇄, 맞바꾸기, 차단, 파지 후 STOP·재개."""
    reset(client, materials, results, "transfer 초기화")
    text_goal(client, "A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘",
              "transfer 준비(팔레트→칸)", results)
    text_goal(client, "A자재를 컨베이어 3번으로 옮겨줘",
              "칸 1→3(칸 2에 B — 측정한 경로가 막혀 빈 팔레트로 우회)", results)
    text_goal(client, "A자재는 컨베이어 2번, B자재는 컨베이어 1번에 놓아줘",
              "칸→칸 연쇄(B 2→1, A 3→2)", results)
    text_goal(client, "A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘",
              "칸 맞바꾸기(순환 → 한 자재만 우회)", results)
    expect_decision(client, "A자재를 컨베이어 3번에서 2번으로 옮겨줘", ("ASK",),
                    "출발지 불일치 ASK", results)
    expect_decision(client, "B자재를 컨베이어 2번에서 7번 팔레트로 옮겨줘", ("BLOCK",),
                    "없는 팔레트 BLOCK", results)
    held_stop_move_test(client, results)
    reset(client, materials, results, "transfer 마무리 초기화")


def context_suite(client: Client, materials: list[str], results: list) -> None:
    """생략·지시·맥락 표현 — 해석 근거가 붙은 확인 카드 → 실행 → 관측."""
    reset(client, materials, results, "맥락 초기화")
    text_goal(client, "파란 거 2번으로", "맥락: 색+맨 번호(B→칸2)", results)
    text_goal(client, "그거 3번 칸으로", "맥락: 그거=B(칸2→칸3)", results)
    text_goal(client, "그다음 그거 제자리로", "맥락: 그거=B(복귀)", results)
    expect_decision(client, "주황 거 빈 칸에", ("ASK",), "맥락: 빈 칸 여럿 → 되묻기", results)
    text_goal(client, "1번", "맥락: 답 '1번'으로 완성(A→칸1)", results)
    reset(client, materials, results, "맥락 마무리 초기화")


def color_suite(client: Client, materials: list[str], results: list) -> None:
    reset(client, materials, results, "색 초기화")
    text_goal(client, "파란색 자재는 컨베이어 2번에 놓고 초록 자재는 컨베이어 1번에 올려줘",
              "색 지정 배치", results)
    text_goal(client, "파란색 자재는 빼고 나머지를 제자리로 돌려놔", "색 제외·나머지", results)
    for text in ("빨간 자재를 1번 칸으로 옮겨줘", "노란 자재 먼저 옮겨줘"):
        payload = client.command(text)
        ok = payload.get("decision") == "ASK" and not payload.get("job")
        results.append({"label": f"색 미등록 ASK: {text}", "passed": ok,
                        "decision": payload.get("decision"), "reason": payload.get("reason")})
        log(f"[색 미등록] {text} → {payload.get('decision')} {payload.get('reason')}")
    reset(client, materials, results, "색 마무리 초기화")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--no-stop-test", action="store_true")
    parser.add_argument("--suite", choices=("default", "color", "held-stop", "final",
                                            "transfer", "context", "pallet-1", "pallet-2",
                                            "observe", "clearance", "mismatch", "restore",
                                            "fixture", "cycles", "all"),
                        default="default",
                        help="final = 색 + 파지 후 STOP + 기본 1라운드")
    args = parser.parse_args()
    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.", file=sys.stderr)
        return 2
    client = Client(args.base)
    st = client.status()
    if not st.get("enabled"):
        print(f"시연을 쓸 수 없다: {st}", file=sys.stderr)
        return 2
    materials = [m["model"] for m in st.get("materials") or []]
    client.wait_idle()
    results: list[dict] = []
    started = time.time()
    if args.suite == "pallet-1":
        log("===== 다른 팔레트 1 =====")
        pallet_suite_first(client, materials, results)
    if args.suite == "observe":
        observe_only(client, results)
    if args.suite == "cycles":
        log("===== 초기화 없는 반복 =====")
        cycles_suite(client, materials, results)
    if args.suite == "fixture":
        log("===== 붙이기 방식(관절) =====")
        fixture_suite(client, materials, results)
    if args.suite == "restore":
        log("===== 복귀 문장 · 원상복귀 =====")
        restore_suite(client, materials, results)
    if args.suite == "mismatch":
        log("===== 기록↔관측 불일치 차단 =====")
        mismatch_block_test(client, results)
        reset(client, materials, results, "불일치 마무리 초기화")
    if args.suite == "clearance":
        log("===== scene 동기화 · 경로 검사 =====")
        clearance_suite(client, materials, results)
    if args.suite == "pallet-2":
        log("===== 다른 팔레트 2 =====")
        pallet_suite_second(client, materials, results)
    if args.suite in ("context", "all"):
        log("===== 맥락 해석 =====")
        context_suite(client, materials, results)
    if args.suite in ("transfer", "all"):
        log("===== 공통 transfer =====")
        transfer_suite(client, materials, results)
    if args.suite in ("color", "final", "all"):
        log("===== 색 지정 =====")
        color_suite(client, materials, results)
    if args.suite in ("held-stop", "final", "all"):
        log("===== 파지 후 STOP =====")
        reset(client, materials, results, "파지 STOP 전 초기화")
        held_stop_test(client, results)
        reset(client, materials, results, "파지 STOP 마무리 초기화")
    rounds = args.rounds if args.suite in ("default", "final", "all") else 0
    for round_no in range(1, rounds + 1):
        log(f"===== 라운드 {round_no} =====")
        tag = f"R{round_no}"
        reset(client, materials, results, f"{tag} 초기화")
        env_statement(client, "3번 칸 고장났어", f"{tag} 환경-금지칸", results,
                      {"blocked_slots": ["slot_3"]})
        env_statement(client, "C자재는 없어", f"{tag} 환경-자재없음", results,
                      {"unavailable_materials": ["material_c"]})
        text_goal(client, "모든 자재를 컨베이어에 올려줘", f"{tag} 환경 반영 배치", results)
        env_statement(client, "3번 칸 고쳤어. C자재 다시 있어", f"{tag} 환경-해제", results,
                      {"blocked_slots": [], "unavailable_materials": []})
        text_goal(client, "A자재랑 B자재 자리 좀 서로 바꿔줘", f"{tag} Qwen 자리바꿈",
                  results, expect_interpreter="qwen")
        text_goal(client, "C자재를 먼저 컨베이어 3번에 올리고 B자재는 원래 자리로 돌려놔",
                  f"{tag} 순서 지정", results)
        if round_no == 1 and not args.no_stop_test:
            reset(client, materials, results, f"{tag} STOP 전 초기화")
            stop_test(client, results)
        reset(client, materials, results, f"{tag} 마무리 초기화")
    passed = sum(1 for r in results if r.get("passed"))
    report = {"schema": "forstick2.arrangement_e2e/1", "is_simulated": True,
              "real_hardware_verified": False, "base": args.base,
              "rounds": args.rounds, "elapsed_sec": round(time.time() - started, 1),
              "passed": passed, "total": len(results), "results": results}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"arrangement_e2e_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"===== {passed}/{len(results)} 통과 · {report['elapsed_sec']}s · {out}")
    for row in results:
        if not row.get("passed"):
            log(f"  FAIL {row['label']}: {row.get('detail') or row.get('observation', {}).get('problems')}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
