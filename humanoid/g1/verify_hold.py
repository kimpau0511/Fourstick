"""G1 자리 유지 검증 — 무목표 90 s · 걷다가 STOP 뒤 90 s (시뮬레이션 시각).

    humanoid/g1/run_nav.sh verify_hold.py [--seconds 90]

- 위치·방향은 검증기가 직접 구독한 Gazebo `pose/info`로 잰다(최대 이탈, 컨베이어 앞면 최소 거리).
- 보정 동작 횟수는 제어기 상태(`/g1/status`)의 `hold.correcting`이 꺼짐→켜짐으로 바뀐 횟수(상태는
  시뮬레이션 0.1 s마다). 보정 중 명령은 `balance: correcting_drift`.
- 세계 초기화·순간 이동을 하지 않는다. 시각이 되돌아가면 실패.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from nav_controller import SITE  # noqa: E402
from verify_roundtrip import PoseLog, Runner, safety  # noqa: E402

OUT = ROOT / "reports/humanoid/g1_hold.json"
FRONT_X = SITE["conveyor"]["front_face_x_m"]
MIN_CLEARANCE = SITE["keep_out"]["min_pelvis_to_front_face_m"]
HOLD_ON_M = 0.15


class StatusLog:
    def __init__(self, client):
        self.rows: list[dict] = []
        self.lock = threading.Lock()
        self.client = client
        self.stop_ev = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        last = None
        while not self.stop_ev.is_set():
            st = self.client.latest(0.5)
            if st and st.get("sim_time_s") != last:
                last = st["sim_time_s"]
                with self.lock:
                    self.rows.append({"sim": st["sim_time_s"], "mode": st["mode"],
                                      "balance": st.get("balance"), "hold": st.get("hold"),
                                      "cmd": st.get("cmd")})
            time.sleep(0.02)

    def since(self, t0, t1=math.inf):
        with self.lock:
            return [r for r in self.rows if t0 <= r["sim"] <= t1]


def analyse(runner: Runner, status: StatusLog, t0: float, t1: float, name: str, extra: dict) -> dict:
    rows = runner.poses.since(t0, t1)
    st = status.since(t0, t1)
    anchors = {json.dumps(r["hold"]["anchor"]["xy"]) for r in st
               if r["hold"] and r["hold"].get("anchor")}
    anchor = next((r["hold"]["anchor"] for r in st if r["hold"] and r["hold"].get("anchor")), None)
    episodes, prev = 0, False
    correcting_ticks = 0
    for r in st:
        on = bool(r["hold"] and r["hold"].get("correcting"))
        correcting_ticks += on
        if on and not prev:
            episodes += 1
        prev = on
    dev = [math.hypot(r[2] - anchor["xy"][0], r[3] - anchor["xy"][1]) for r in rows
           if anchor and r[0] >= anchor["sim"]]
    start = rows[0]
    travel = [math.hypot(r[2] - start[2], r[3] - start[3]) for r in rows]
    yaw_dev = [abs((r[5] - anchor["yaw"] + math.pi) % (2 * math.pi) - math.pi) for r in rows
               if anchor and r[0] >= anchor["sim"]]
    s = safety(rows)
    near = [FRONT_X - r[2] for r in rows]
    checks = {
        "duration": rows[-1][0] - rows[0][0] >= (t1 - t0) - 0.5,
        "single_anchor": len(anchors) == 1,
        "bounded_from_anchor": bool(dev) and max(dev) <= 0.25,
        "corrections_happened": episodes >= 2,
        "no_goal_resumed": all(r["mode"] == "stand" for r in st),
        "no_fall": s["min_z_m"] >= 0.65 and s["min_upright_cos"] >= 0.94,
        "no_teleport": s["max_obs_speed_mps"] <= 2.0,
        "keep_out": min(near) >= MIN_CLEARANCE,
        "no_time_rollback": runner.poses.time_went_back == 0,
    }
    out = {"name": name, "passed": all(checks.values()), "checks": checks,
           "window_sim_s": round(rows[-1][0] - rows[0][0], 3), **extra,
           "anchor": anchor, "anchors_seen": len(anchors),
           "max_dev_from_anchor_m": round(max(dev), 3) if dev else None,
           "mean_dev_from_anchor_m": round(sum(dev) / len(dev), 3) if dev else None,
           "max_yaw_dev_rad": round(max(yaw_dev), 3) if yaw_dev else None,
           "max_travel_from_window_start_m": round(max(travel), 3),
           "correction_episodes": episodes,
           "correcting_share": round(correcting_ticks / max(1, len(st)), 3),
           "status_samples": len(st),
           "min_front_clearance_m": round(min(near), 3),
           "safety": s}
    print(f"{name}: {'PASS' if out['passed'] else 'FAIL'} · 기준 이탈 최대 {out['max_dev_from_anchor_m']} m · "
          f"보정 {episodes}회(보정 중 {out['correcting_share'] * 100:.0f}%) · 컨베이어 앞면 최소 "
          f"{out['min_front_clearance_m']} m · {out['window_sim_s']} s(sim) "
          f"{[k for k, v in checks.items() if not v]}", flush=True)
    return out


def main() -> int:
    import warnings
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=90.0)
    parser.add_argument("--stop-before-goal-m", type=float, default=0.9)
    args = parser.parse_args()
    runner = Runner()
    status = StatusLog(runner.client)
    results = []
    wall0, sim0 = time.time(), runner.poses.last()[0]
    try:
        # 준비: 컨베이어에서 떨어진 곳(속도 명령). 그 뒤는 명령 없이 둔다.
        setup = runner.move_away(2.0)
        print(f"준비: {setup}", flush=True)
        # A. 무목표 90 s — 목표를 모두 취소하고 아무 명령도 보내지 않는다.
        st = runner.client.send("stop")
        runner.reaction(st["id"])
        t0, w0 = runner.poses.last()[0], time.time()
        runner.wait_sim(args.seconds)
        t1 = runner.poses.last()[0]
        results.append(analyse(runner, status, t0, t1, "무목표 90 s",
                               {"wall_s": round(time.time() - w0, 1)}))
        # B. 컨베이어로 걷다가 목표 0.9 m 앞에서 STOP → 90 s.
        gx, gy = SITE["safe_points"]["conveyor_front"]["xy_m"]
        go = runner.client.send("goto", target="conveyor_front")
        runner.reaction(go["id"])
        while True:
            r = runner.poses.last()
            if math.hypot(r[2] - gx, r[3] - gy) <= args.stop_before_goal_m:
                break
            time.sleep(0.005)
        rows = runner.poses.since(r[0] - 0.5)
        speed = math.hypot(rows[-1][2] - rows[0][2], rows[-1][3] - rows[0][3]) / max(
            1e-6, rows[-1][0] - rows[0][0])
        stop = runner.client.send("stop")
        react = runner.reaction(stop["id"])
        t0, w0 = react["applied_sim_s"], time.time()
        runner.wait_sim(args.seconds)
        t1 = runner.poses.last()[0]
        results.append(analyse(runner, status, t0, t1, "STOP 뒤 90 s", {
            "wall_s": round(time.time() - w0, 1), "speed_at_stop_mps": round(speed, 3),
            "dist_to_goal_at_stop_m": round(math.hypot(r[2] - gx, r[3] - gy), 3),
            "stop_reaction": react}))
    finally:
        status.stop_ev.set()
        report = {"schema": "forstick2.g1_hold/1", "is_simulated": True,
                  "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "hold_rule": {"on_m": HOLD_ON_M, "off_m": 0.06, "speed_mps": 0.25,
                                "guard_before_keep_out_m": 0.25},
                  "min_front_clearance_required_m": MIN_CLEARANCE,
                  "timing": {"sim_s": round(runner.poses.last()[0] - sim0, 1),
                             "wall_s": round(time.time() - wall0, 1)},
                  "passed": len(results) == 2 and all(r["passed"] for r in results),
                  "results": results}
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"결과: {'PASS' if report['passed'] else 'FAIL'} → {OUT}", flush=True)
        sys.stdout.flush()
        os._exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
