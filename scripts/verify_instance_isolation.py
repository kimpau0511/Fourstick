"""독립 실행 인스턴스 회귀 검증: 이송·복귀 반복 + 기존(다른 파티션) Gazebo를 건드리지 않았는지.

    python3 scripts/verify_instance_isolation.py --base http://127.0.0.1:8096 \
        --partition forstick2_clean_check --other-partition forstick2_fr3_workcell --cycles 3

- 대상 인스턴스에서 "주황 자재 컨베이어로 옮겨줘" → 확인 → 이송, "주황 자재 원래 자리로 돌려놔" → 확인 → 복귀를 반복한다.
  각 작업의 결과(`report.status`)와 Gazebo 직접 관측(대상 파티션 pose/info)으로 자재 위치를 판정한다.
- 다른 파티션(기존 개발 셀)의 자재 pose와 로봇 관절(model joint_state)을 시작·끝에 직접 읽어, 움직였으면 실패다.
- gz CLI는 `-n 1`로 스스로 끝나는 형태만 쓴다(ROS CLI는 쓰지 않는다).
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORLD = "forstick2_fr3_2f85_workcell"
CONVEYOR_SLOT_1 = (0.25, -0.50, 0.75)
TOL_M = 0.02


def call(base, method, path, body=None, timeout=180):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def gz_once(partition: str, topic: str) -> dict:
    cmd = f"source /opt/ros/lyrical/setup.bash && gz topic -e -n 1 -t {topic} --json-output"
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=60,
                         env={"GZ_PARTITION": partition, "PATH": "/usr/bin:/bin", "HOME": "/tmp"}).stdout
    return json.loads(out.splitlines()[0])


def materials(partition: str) -> dict:
    return {p["name"]: [round(p["position"].get(k, 0.0), 4) for k in ("x", "y", "z")]
            for p in gz_once(partition, f"/world/{WORLD}/pose/info")["pose"]
            if p.get("name", "").startswith("material_")}


ROBOT_LINKS = ("shoulder_Link", "upperarm_Link", "forearm_Link", "wrist1_Link", "wrist2_Link", "wrist3_Link",
               "robotiq_85_left_knuckle_link", "robotiq_85_right_knuckle_link")


def joints(partition: str) -> dict:
    """로봇 링크 pose(부모 기준 — 관절이 움직이면 바뀐다). FR3 관절 상태는 gz 토픽으로 나오지 않는다(ROS로만)."""
    out = {}
    for p in gz_once(partition, f"/world/{WORLD}/pose/info")["pose"]:
        if p.get("name") in ROBOT_LINKS:
            o = p.get("orientation", {})
            for k, v in list(p["position"].items()) + [("q" + k, o.get(k, 0.0)) for k in ("x", "y", "z", "w")]:
                out[f"{p['name']}.{k}"] = round(v, 4)
    return out


def run_job(base, sid, utterance, want):
    s, d = call(base, "POST", "/v1/sim-demo/command", {"session_id": sid, "mode": "simulation_demo",
                                                         "source": "text", "utterance": utterance})
    if d.get("decision") != "CONFIRM":
        return {"ok": False, "why": f"확인 카드 아님: {d.get('decision')} {d.get('reason')}"}
    s, c = call(base, "POST", "/v1/sim-demo/confirm",
                {"session_id": sid, "token": d["confirmation"]["token"], "action": "confirm"})
    job = (c.get("job") or {}).get("job_id")
    if not job:
        return {"ok": False, "why": f"작업이 만들어지지 않음: {c.get('reason')}"}
    t0 = time.time()
    while True:
        s, j = call(base, "GET", f"/v1/sim-demo/jobs/{job}")
        if j.get("status") != "running":
            break
        if time.time() - t0 > 900:
            return {"ok": False, "why": "제한시간 초과", "job": job}
        time.sleep(3)
    rep = j.get("report") or {}
    diag = [line for line in (j.get("console_tail") or []) if "fixture-diag" in line or "[BAD]" in line]
    return {"ok": rep.get("status") == want, "job": job, "status": rep.get("status"),
            "sec": round(time.time() - t0), "reason_codes": rep.get("reason_codes"), "diag": diag[-6:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8096")
    ap.add_argument("--partition", required=True, help="검증 대상 인스턴스의 GZ_PARTITION")
    ap.add_argument("--other-partition", default="", help="건드리면 안 되는 다른 셀의 GZ_PARTITION")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--out", default=str(ROOT / "reports/workcell/instance_isolation.json"))
    args = ap.parse_args()
    other0 = (materials(args.other_partition), joints(args.other_partition)) if args.other_partition else None
    s, sess = call(args.base, "POST", "/v1/sessions", {})
    sid = sess["session_id"]
    rows, ok = [], True
    for k in range(args.cycles):
        for utterance, want, where in (("주황 자재 컨베이어로 옮겨줘", "simulation_transfer_completed", CONVEYOR_SLOT_1),
                                       ("주황 자재 원래 자리로 돌려놔", "returned_to_origin", (0.5, 0.2, 0.84))):
            r = run_job(args.base, sid, utterance, want)
            pos = materials(args.partition).get("material_a")
            r["material_a"] = pos
            r["at_expected"] = pos is not None and math.dist(pos, where) <= TOL_M
            r["ok"] = bool(r["ok"] and r["at_expected"])
            r.update(cycle=k + 1, utterance=utterance)
            rows.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
            if not r["ok"]:
                ok = False
                break
        if not ok:
            break
    isolation = None
    if other0:
        m1, j1 = materials(args.other_partition), joints(args.other_partition)
        moved = {n: (other0[0][n], m1.get(n)) for n in other0[0]
                 if m1.get(n) is None or math.dist(other0[0][n], m1[n]) > 1e-3}
        jmoved = {n: (other0[1][n], j1.get(n)) for n in other0[1]
                  if j1.get(n) is None or abs(other0[1][n] - j1[n]) > 1e-3}
        isolation = {"other_partition": args.other_partition, "materials_moved": moved,
                     "joints_moved": jmoved, "untouched": not moved and not jmoved}
        print("다른 셀:", json.dumps(isolation, ensure_ascii=False), flush=True)
        ok = ok and isolation["untouched"]
    report = {"schema": "forstick2.instance_isolation/1", "is_simulated": True,
              "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "base": args.base,
              "partition": args.partition, "cycles_requested": args.cycles, "passed": ok,
              "jobs": rows, "isolation": isolation}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"결과: {'PASS' if ok else 'FAIL'} · 작업 {sum(r['ok'] for r in rows)}/{len(rows)} → {args.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
