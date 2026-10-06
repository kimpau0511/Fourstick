#!/usr/bin/env python3
"""일반 경로 pick/place E2E — `텍스트 → /v1/plan → /v1/decision → /v1/execute → 관측`.

```
python3 scripts/verify_general_pick_place.py --base http://127.0.0.1:8096 --rounds 3
```

**실제 Gazebo 작업 셀에서 돈다(시뮬레이션).** 시연 경로(`/v1/sim-demo/*`)를 쓰지 않는다.
각 이송마다 서버 판정과 별도로 `/v1/sim-view/state`(Gazebo pose)와 `/v1/sim-demo`(상태
기록)를 다시 읽어 자재가 도착지 중심 허용치 안에 있는지 본다. 차단 사례는 실행 요청이
거부되고 자재가 움직이지 않았는지 본다. 결과는 `reports/workcell/general_pick_place_e2e.json`.
"""

from __future__ import annotations

import argparse
import json
import math
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/workcell/general_pick_place_e2e.json"
GRASP = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json").read_text(encoding="utf-8"))
WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
#: 같은 자리 판정 허용치 — 서버(`validation/simulation_demo_state.ORIGIN_TOLERANCE_M`)와 같다.
TOLERANCE_M = 0.02
MODEL = {"mat_a": "material_a", "mat_b": "material_b", "mat_c": "material_c"}


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.ctx = ssl._create_unverified_context()

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 600.0):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ctx) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}


def centers() -> dict:
    out = {name: tuple(row["object_world_center_m"])
           for name, row in GRASP["conveyor_slots"]["slots"].items()}
    frames = WORKCELL["frames"]

    def resolve(name):
        frame = frames[name]
        parent = frame.get("parent")
        xyz = frame.get("xyz_m") or [0, 0, 0]
        if not parent or parent not in frames:
            return [float(v) for v in xyz]
        base = resolve(parent)
        return [b + float(v) for b, v in zip(base, xyz)]

    for model, row in WORKCELL["models"].items():
        if row.get("kind") == "material":
            parent = frames[row["frame"]]["parent"]
            pallet = next(r["resource_id"] for m, r in WORKCELL["models"].items()
                          if r.get("kind") == "pallet" and r.get("frame") == parent)
            out[pallet] = tuple(resolve(row["frame"]))
    return out


def observe(client: Client) -> dict:
    _, view = client.call("GET", "/v1/sim-view/state")
    _, demo = client.call("GET", "/v1/sim-demo")
    objects = ((demo.get("state") or {}).get("objects") or {}) if isinstance(demo.get("state"), dict) else {}
    return {"poses": {m: p[:3] for m, p in (view.get("materials") or {}).items()},
            "stale": view.get("stale"), "records": {
                m: (r.get("slot") if r.get("state") == "held_on_target" else r.get("pallet") or r.get("state"))
                for m, r in objects.items()},
            "running_job": demo.get("running_job")}


def where(observed: dict, model: str, table: dict) -> tuple[str | None, float | None]:
    pose = observed["poses"].get(model)
    if pose is None:
        return None, None
    best = min(table.items(), key=lambda kv: math.dist(pose, kv[1]))
    gap = math.dist(pose, best[1])
    return (best[0] if gap <= TOLERANCE_M else None), round(gap, 4)


def run_case(client: Client, sid: str, utterance: str, *, expect: str, table: dict) -> dict:
    """expect: 'execute'(통과해야 한다) | 'blocked'(관문이 막아야 한다)."""
    row = {"utterance": utterance, "expect": expect, "at": time.time()}
    before = observe(client)
    row["before"] = {m: where(before, m, table)[0] for m in before["poses"]}
    status, plan = client.call("POST", "/v1/plan", {"session_id": sid, "utterance": utterance})
    row["plan_http"] = status
    row["plan_ok"] = plan.get("ok")
    row["steps"] = [(s["skill"], s["args"]) for s in (plan.get("plan") or {}).get("steps", [])] \
        or plan.get("draft_steps")
    validation = plan.get("validation") or {}
    row["gate"] = {"decision": validation.get("decision"), "reason_code": validation.get("reason_code"),
                   "detail": validation.get("detail")} if validation else {
        "decision": "PLAN_FAILED", "reason_code": plan.get("reason_code"), "detail": plan.get("detail")}
    rules = (validation.get("capability") or {}).get("rules") or []
    row["transfer_rule"] = next((r for r in rules if r.get("code") == "C-TRANSFER"), None)
    if plan.get("ok"):
        p = plan["plan"]
        status, decision = client.call("POST", "/v1/decision", {
            "session_id": sid, "request_id": plan["request_id"], "plan_id": p["plan_id"],
            "plan_hash": p["plan_hash"], "decision": "approve",
            "robot_id": p["robot_id"], "profile_id": p["profile_id"],
            "profile_version": p["profile_version"]})
        row["decision_http"] = status
        started = time.time()
        status, result = client.call("POST", "/v1/execute", {
            "session_id": sid, "request_id": plan["request_id"], "plan_id": p["plan_id"],
            "approval_id": decision.get("approval_id")})
        row["execute_http"] = status
        row["execute_sec"] = round(time.time() - started, 1)
        row["execute_ok"] = result.get("ok")
        row["final"] = {k: (result.get("final") or {}).get(k)
                        for k in ("state", "reason_code", "task_succeeded")} if result.get("final") else {
            "reason_code": result.get("reason_code"), "detail": result.get("detail") or result.get("error")}
        transfer_steps = [s for s in result.get("steps") or []
                          if (s.get("evidence") or {}).get("executed_by") == "transfer"]
        if transfer_steps:
            ev = transfer_steps[0]["evidence"]
            row["transfer"] = {k: ev.get(k) for k in ("job_id", "job_status", "recorded_location",
                                                     "final_position", "contract", "detail")}
    after = observe(client)
    row["after"] = {m: where(after, m, table) for m in after["poses"]}
    moved = {m for m in row["before"] if row["before"][m] != row["after"][m][0]}
    row["moved"] = sorted(moved)
    if expect == "execute":
        dest = (row.get("transfer") or {}).get("contract", {}).get("destination")
        model = (row.get("transfer") or {}).get("contract") and next(
            (MODEL[s[1].get("object")] for s in row["steps"] or [] if s[0] == "pick"), None)
        independent = None if not model else row["after"].get(model)
        row["independent_final"] = {"model": model, "expected": dest, "observed": independent}
        row["passed"] = bool(row.get("execute_ok") and independent and independent[0] == dest
                             and after["records"].get(model, None) in (dest, None)
                             and moved <= {model})
    else:
        row["passed"] = (row["gate"]["decision"] != "ALLOW" and not row.get("execute_ok")
                         and not moved)
    print(f"[{'통과' if row['passed'] else '실패'}] {utterance} → 관문 {row['gate']['decision']}"
          f" {row['gate']['reason_code'] or ''} · 실행 {row.get('execute_http', '-')}"
          f" {row.get('final', {}).get('state', '')} · 이동 {row['moved']}"
          + (f" · 독립 관측 {row['independent_final']['observed']}" if expect == "execute" else ""),
          flush=True)
    return row


def wait_job(client: Client, job_id: str, *, timeout_sec: float = 600.0) -> dict:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        _, job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")
        if job.get("status") != "running":
            return job
        time.sleep(1.0)
    return {"status": "timeout"}


def run_stop_case(client: Client, sid: str, utterance: str, *, table: dict,
                  stop_after_stage: int) -> dict:
    """실행 중 전체 정지 → 정지 기록·래치 → 기존 체크포인트 재개로 복구."""
    import threading

    row = {"utterance": utterance, "expect": "stop", "at": time.time()}
    status, plan = client.call("POST", "/v1/plan", {"session_id": sid, "utterance": utterance})
    p = plan["plan"]
    _, decision = client.call("POST", "/v1/decision", {
        "session_id": sid, "request_id": plan["request_id"], "plan_id": p["plan_id"],
        "plan_hash": p["plan_hash"], "decision": "approve"})
    body = {"session_id": sid, "request_id": plan["request_id"], "plan_id": p["plan_id"],
            "approval_id": decision.get("approval_id")}
    box: dict = {}
    worker = threading.Thread(target=lambda: box.update(
        zip(("http", "result"), client.call("POST", "/v1/execute", body))))
    worker.start()
    job_id, stopped_at = None, None
    while worker.is_alive() and stopped_at is None:
        _, demo = client.call("GET", "/v1/sim-demo")
        running = demo.get("running_job") or {}
        job_id = running.get("job_id") or job_id
        if job_id:
            _, job = client.call("GET", f"/v1/sim-demo/jobs/{job_id}")
            done = max([r["no"] for r in job.get("progress") or []] or [0])
            if done >= stop_after_stage:
                _, stop = client.call("POST", "/v1/stop", {"session_id": sid})
                stopped_at = {"after_stage": done, "stop_response": {
                    k: stop.get(k) for k in ("ok", "requested", "detail") if k in stop}}
        time.sleep(0.3)
    worker.join()
    result = box.get("result") or {}
    row["job_id"] = job_id
    row["stop"] = stopped_at
    row["final"] = {k: (result.get("final") or {}).get(k) for k in ("state", "reason_code")}
    row["interrupted"] = result.get("interrupted")
    # 래치: 정지가 걸려 있고, 같은 승인으로 다시 실행하면 거부돼야 한다. 환경(자재 위치)이
    # 바뀌었으면 승인 대조가 먼저 거부한다 — 어느 쪽이든 실행되지 않아야 한다.
    _, robots = client.call("GET", "/v1/robots")
    row["latch_active"] = (robots.get("stop_diagnostics") or {}).get("stop_latch_active")
    again, again_body = client.call("POST", "/v1/execute", body)
    row["re_execute"] = {"http": again, "reason_code": again_body.get("reason_code")}
    _, demo = client.call("GET", "/v1/sim-demo")
    state = demo.get("state") or {}
    checkpoint = state.get("checkpoint") or {}
    row["checkpoint"] = {k: checkpoint.get(k) for k in ("checkpoint_id", "model", "object_state",
                                                        "stopped_stage")}
    # 복구: 시연 경로의 검증된 체크포인트 재개(새 실행기를 만들지 않는다).
    recovery = None
    if checkpoint.get("checkpoint_id"):
        status, job = client.call("POST", "/v1/sim-demo/jobs", {
            "action": "resume", "material": checkpoint.get("model"),
            "checkpoint_id": checkpoint["checkpoint_id"]})
        recovery = {"http": status, "job_id": job.get("job_id")}
        if job.get("job_id"):
            done = wait_job(client, job["job_id"])
            recovery["status"] = (done.get("report") or {}).get("status")
    row["recovery"] = recovery
    after = observe(client)
    row["after"] = {m: where(after, m, table) for m in after["poses"]}
    row["passed"] = bool(row["final"].get("state") == "stopped"
                         and row["interrupted"] == "exec.stopped"
                         and row["latch_active"] is True
                         and row["re_execute"]["http"] == 409
                         and row["checkpoint"].get("checkpoint_id"))
    print(f"[{'통과' if row['passed'] else '실패'}] 정지: {utterance} → 단계 {stopped_at} ·"
          f" 최종 {row['final']} · 래치 {row['latch_active']} · 재실행 {row['re_execute']}"
          f" · 체크포인트 {row['checkpoint']}"
          f" · 복구 {recovery}", flush=True)
    return row


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8096")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cases", choices=("all", "transfer", "blocked", "route", "stop"),
                        default="all")
    parser.add_argument("--stop-after-stage", type=int, default=7,
                        help="이 단계까지 끝난 뒤 전체 정지(7=lift 뒤, 자재를 든 채 이동 중)")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    client = Client(args.base)
    table = centers()
    _, session = client.call("POST", "/v1/sessions", {"origin": "verify_general_pick_place"})
    sid = session["session_id"]
    report = {"schema": "forstick2.general_pick_place_e2e/1", "is_simulated": True,
              "base": args.base, "started_at": time.time(), "cases": []}
    _, config = client.call("GET", "/v1/config")
    report["profile_version"] = (config.get("robot") or {}).get("profile_version")
    blocked = [
        "A자재를 2번 팔레트에서 컨베이어로 옮겨줘",          # 관측 출발지와 다르다
        "C자재를 3번 팔레트에서 1번 팔레트로 옮겨줘",        # 도착지에 A자재가 있다
        "A자재를 컨베이어로 옮겨줘",                       # 출발지를 말하지 않았다
        "D자재를 1번 팔레트에서 컨베이어로 옮겨줘",          # 셀에 없는 자재
        "A자재를 1번 팔레트에서 4번 팔레트로 옮겨줘",        # 셀에 없는 위치
    ]
    transfers = [
        "A자재를 1번 팔레트에서 컨베이어로 옮겨줘",
        "A자재를 컨베이어에서 1번 팔레트로 옮겨줘",
        "C자재를 3번 팔레트에서 컨베이어로 옮겨줘",
        "C자재를 컨베이어에서 3번 팔레트로 옮겨줘",
    ]
    routes = [
        "A자재를 1번 팔레트에서 2번 팔레트로 옮겨줘",
        "A자재를 2번 팔레트에서 1번 팔레트로 옮겨줘",
    ]
    try:
        if args.cases == "route":
            for text in routes:
                row = run_case(client, sid, text, expect="execute", table=table)
                report["cases"].append(row)
                if not row["passed"]:
                    raise SystemExit("이송 실패 — 다음 이송을 멈춘다")
        if args.cases == "stop":
            report["cases"].append(run_stop_case(
                client, sid, "A자재를 1번 팔레트에서 컨베이어로 옮겨줘", table=table,
                stop_after_stage=args.stop_after_stage))
            report["cases"].append(run_case(
                client, sid, "A자재를 컨베이어에서 1번 팔레트로 옮겨줘", expect="execute",
                table=table))
        if args.cases in ("all", "blocked"):
            for text in blocked:
                report["cases"].append(run_case(client, sid, text, expect="blocked", table=table))
        if args.cases in ("all", "transfer"):
            for n in range(args.rounds):
                for text in transfers:
                    row = run_case(client, sid, text, expect="execute", table=table)
                    row["round"] = n + 1
                    report["cases"].append(row)
                    if not row["passed"]:
                        raise SystemExit("이송 실패 — 다음 이송을 멈춘다")
    finally:
        report["finished_at"] = time.time()
        rows = report["cases"]
        report["summary"] = {
            "transfer_passed": sum(1 for r in rows if r["expect"] == "execute" and r["passed"]),
            "transfer_total": sum(1 for r in rows if r["expect"] == "execute"),
            "blocked_passed": sum(1 for r in rows if r["expect"] == "blocked" and r["passed"]),
            "blocked_total": sum(1 for r in rows if r["expect"] == "blocked"),
            "stop_passed": sum(1 for r in rows if r["expect"] == "stop" and r["passed"]),
            "stop_total": sum(1 for r in rows if r["expect"] == "stop"),
        }
        out = Path(args.out)
        previous = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
        runs = previous.get("runs", []) if previous.get("schema") == report["schema"] else []
        runs.append(report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"schema": report["schema"], "runs": runs},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[요약] {report['summary']} → {out}")
    return 0 if all(r["passed"] for r in report["cases"]) else 1


if __name__ == "__main__":
    sys.exit(main())
