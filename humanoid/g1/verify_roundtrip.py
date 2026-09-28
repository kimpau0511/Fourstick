"""G1 연속 제어 검증 — 명령 전환, 컨베이어 앞 왕복, 이동 중 STOP (Gazebo 관측으로 판정).

    humanoid/g1/run_gazebo.sh --nav
    humanoid/g1/run_nav.sh nav_controller.py --rtf 1.0 --reset-world &     # 연속 제어기
    humanoid/g1/run_nav.sh verify_roundtrip.py --trips 3 --stops 3

- 이 검증기는 제어기 상태(`/g1/status`)와 **따로** Gazebo `pose/info`를 구독해 골반 pose를
  (시뮬레이션 시각, 받은 벽시계 시각)과 함께 기록한다. 도착·복귀·정지 판정은 이 기록으로 한다.
- 검증 도중 세계 초기화·순간 이동을 하지 않는다. 시각이 되돌아가거나 연속 관측 사이 속도가
  비정상이면 실패로 잡는다.
- 시간은 두 가지를 섞지 않는다: `*_sim_s`(Gazebo 시각), `*_wall_s`(벽시계).
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

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from g1cmd import Client  # noqa: E402
from nav_controller import MODEL, SITE, WORLD, wrap, yaw_of  # noqa: E402

OUT = ROOT / "reports/humanoid/g1_roundtrip.json"
ARR = SITE["arrival"]
CRITERIA = {
    "arrive_pos_tol_m": ARR["position_tol_m"],
    "arrive_yaw_tol_rad": ARR["yaw_tol_rad"],
    "min_base_z_m": 0.65,
    "min_upright_cos": 0.94,
    "max_obs_speed_mps": 2.0,                 # 연속 관측 사이(순간 이동 감지)
    "min_front_clearance_m": SITE["keep_out"]["min_pelvis_to_front_face_m"],
    "leg_timeout_sim_s": 60.0,
    "stop_settle_speed_mps": 0.10,            # 0.5 s(시뮬레이션) 창 평균 속도
    "stop_hold_sim_s": 10.0,                  # STOP 뒤 균형·자리 유지 관찰(3 s에선 표류를 못 봤다)
    "stop_max_travel_after_m": 0.40,          # STOP 적용 뒤 관찰 구간 전체의 최대 이동
    "idle_hold_sim_s": 20.0,                  # 목표 없이 대기
    "idle_max_drift_m": 0.25,                 # 대기 중 최대 이동(자리 유지 0.15 m + 여유)
    "stop_max_settle_sim_s": 2.0,
    "turn_response_rate": 0.2,                # rad/s — 회전 명령 반응 감지
}


class PoseLog:
    """Gazebo pose/info 기록: (sim, wall, x, y, z, yaw, upright)."""

    def __init__(self, node):
        from gz.msgs.pose_v_pb2 import Pose_V
        self.lock = threading.Lock()
        self.rows: list[tuple] = []
        self.time_went_back = 0
        node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._on)

    def _on(self, msg):
        t = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
        for p in msg.pose:
            if p.name == MODEL:
                o = p.orientation
                row = (t, time.time(), p.position.x, p.position.y, p.position.z,
                       yaw_of(o.x, o.y, o.z, o.w), 1 - 2 * (o.x * o.x + o.y * o.y))
                with self.lock:
                    if self.rows and t < self.rows[-1][0] - 1e-9:
                        self.time_went_back += 1
                    if not self.rows or t > self.rows[-1][0]:
                        self.rows.append(row)
                return

    def last(self):
        with self.lock:
            return self.rows[-1] if self.rows else None

    def since(self, sim_t0: float, sim_t1: float = math.inf):
        with self.lock:
            return [r for r in self.rows if sim_t0 <= r[0] <= sim_t1]


def window_speed(rows, t_end, window=0.5):
    w = [r for r in rows if t_end - window <= r[0] <= t_end]
    if len(w) < 2 or w[-1][0] - w[0][0] < window * 0.6:
        return None
    return math.hypot(w[-1][2] - w[0][2], w[-1][3] - w[0][3]) / (w[-1][0] - w[0][0])


def safety(rows) -> dict:
    a = np.array([r[:7] for r in rows])
    dt = np.diff(a[:, 0])
    sp = np.linalg.norm(np.diff(a[:, 2:4], axis=0), axis=1) / np.where(dt > 0, dt, np.nan)
    conv = SITE["conveyor"]
    near = np.abs(a[:, 3] - conv["center_m"][1]) < conv["size_m"][1] / 2 + 0.3
    clearance = (conv["front_face_x_m"] - a[near, 2]).min() if near.any() else None
    return {"min_z_m": round(float(a[:, 4].min()), 4),
            "min_upright_cos": round(float(a[:, 6].min()), 4),
            "max_obs_speed_mps": round(float(np.nanmax(sp)), 3) if sp.size else 0.0,
            "min_front_clearance_m": None if clearance is None else round(float(clearance), 3),
            "samples": len(rows)}


def safety_ok(s: dict) -> dict:
    c = CRITERIA
    return {"no_fall": s["min_z_m"] >= c["min_base_z_m"] and
            s["min_upright_cos"] >= c["min_upright_cos"],
            "no_teleport": s["max_obs_speed_mps"] <= c["max_obs_speed_mps"],
            "keep_out": s["min_front_clearance_m"] is None or
            s["min_front_clearance_m"] >= c["min_front_clearance_m"]}


class Runner:
    def __init__(self):
        self.client = Client()
        self.poses = PoseLog(self.client.node)
        deadline = time.time() + 10
        while time.time() < deadline and (self.poses.last() is None or
                                          self.client.latest(0.1) is None):
            time.sleep(0.1)
        if self.poses.last() is None or self.client.latest(0.1) is None:
            raise RuntimeError("제어기 상태 또는 Gazebo pose가 오지 않는다 — 제어기가 도는지 확인")

    def status(self):
        return self.client.latest(1.0)

    def wait_status(self, pred, timeout_wall: float, sim_limit: float | None = None):
        """상태가 조건을 만족할 때까지. sim_limit(시뮬레이션 시각)을 넘으면 None."""
        deadline = time.time() + timeout_wall
        while time.time() < deadline:
            st = self.status()
            if st and pred(st):
                return st
            if sim_limit is not None and st and st["sim_time_s"] > sim_limit:
                return None
            time.sleep(0.05)
        return None

    def reaction(self, cmd_id: str, timeout=5.0):
        st = self.wait_status(lambda s: any(r.get("id") == cmd_id for r in s["reactions_last"]),
                              timeout)
        if st is None:
            return None
        r = next(r for r in st["reactions_last"] if r.get("id") == cmd_id)
        return {"ok": r["ok"], "reason": r.get("reason"),
                "sent_to_accepted_wall_ms": round((r["accepted_wall"] - r["sent_wall"]) * 1000, 2),
                "accepted_to_applied_wall_ms": round((r["applied_wall"] - r["accepted_wall"]) * 1000, 2),
                "sent_to_applied_wall_ms": round((r["applied_wall"] - r["sent_wall"]) * 1000, 2),
                "applied_sim_s": r["applied_sim"], "cancelled_goal": r.get("cancelled_goal"),
                "start": r.get("start")}

    def wait_sim(self, sim_seconds: float):
        t0 = self.poses.last()[0]
        while self.poses.last()[0] < t0 + sim_seconds:
            time.sleep(0.02)

    def leg(self, name: str, send: dict, goal_xy, goal_yaw) -> dict:
        """목표 명령을 보내고 도착(제어기 판정)을 기다린 뒤, 자체 관측으로 다시 판정한다."""
        t_sim0 = self.poses.last()[0]
        w0 = time.time()
        cmd = self.client.send(**send)
        react = self.reaction(cmd["id"])
        st = self.wait_status(
            lambda s: s["nav"]["arrived_sim"] is not None and s["nav"]["goal"] and
            s["nav"]["goal"]["name"] == name, timeout_wall=CRITERIA["leg_timeout_sim_s"] * 3,
            sim_limit=t_sim0 + CRITERIA["leg_timeout_sim_s"])
        claimed = st is not None
        arrived_sim = st["nav"]["arrived_sim"] if claimed else None
        # 도착 판정 뒤 1 s(시뮬레이션) 관측으로 다시 본다(제어기 판정을 그대로 믿지 않는다).
        self.wait_sim(1.0)
        now = self.poses.last()[0]
        tail = self.poses.since(now - 1.0, now)
        dists = [math.hypot(r[2] - goal_xy[0], r[3] - goal_xy[1]) for r in tail]
        yaws = [abs(wrap(r[5] - goal_yaw)) for r in tail]
        rows = self.poses.since(t_sim0, now)
        s = safety(rows)
        observed_ok = bool(tail) and max(dists) < CRITERIA["arrive_pos_tol_m"] and \
            max(yaws) < CRITERIA["arrive_yaw_tol_rad"]
        checks = {"controller_claims_arrival": claimed, "observed_at_goal": observed_ok,
                  **safety_ok(s)}
        return {"leg": name, "passed": all(checks.values()), "checks": checks,
                "command_reaction": react,
                "duration_sim_s": round(arrived_sim - t_sim0, 3) if claimed else None,
                "duration_wall_s": round(time.time() - w0, 3),
                "observed_final": {"max_dist_m": round(max(dists), 4) if dists else None,
                                   "max_yaw_err_rad": round(max(yaws), 4) if yaws else None,
                                   "xy": [round(tail[-1][2], 4), round(tail[-1][3], 4)] if tail else None,
                                   "yaw": round(tail[-1][5], 4) if tail else None},
                "safety": s}

    def save_start(self) -> dict:
        self.wait_sim(1.0)
        obs = self.poses.last()
        cmd = self.client.send("save_start")
        react = self.reaction(cmd["id"])
        return {"verifier_obs": {"sim": obs[0], "xy": [obs[2], obs[3]], "yaw": obs[5]},
                "controller_saved": react and react["start"], "reaction": react}

    def resets(self) -> int:
        st = self.status()
        return len(st["memory_resets"]) if st else -1

    # ── 시나리오 ────────────────────────────────────────────────────────
    def roundtrip(self, i: int) -> dict:
        t0 = self.poses.last()[0]
        back0 = self.poses.time_went_back
        mem0 = self.status()["memory_resets"]
        start = self.save_start()
        sx, sy = start["verifier_obs"]["xy"]
        syaw = start["verifier_obs"]["yaw"]
        p = SITE["safe_points"]["conveyor_front"]
        out = self.leg("conveyor_front", {"kind": "goto", "target": "conveyor_front"},
                       p["xy_m"], p["yaw_rad"])
        self.wait_sim(2.0)                                   # 도착 지점 대기
        back = self.leg("start", {"kind": "return"}, (sx, sy), syaw)
        rows = self.poses.since(t0)
        checks = {"out": out["passed"], "back": back["passed"],
                  "no_world_reset": self.poses.time_went_back == back0,
                  "memory_kept": self.status()["memory_resets"] == mem0,
                  **safety_ok(safety(rows))}
        print(f"왕복 {i}: {'PASS' if all(checks.values()) else 'FAIL'} "
              f"가기 {out['duration_sim_s']} s(sim)/{out['duration_wall_s']} s(wall) "
              f"오차 {out['observed_final']['max_dist_m']} m · "
              f"오기 {back['duration_sim_s']} s(sim)/{back['duration_wall_s']} s(wall) "
              f"오차 {back['observed_final']['max_dist_m']} m "
              f"{[k for k, v in checks.items() if not v]}", flush=True)
        return {"kind": "roundtrip", "index": i, "passed": all(checks.values()),
                "checks": checks, "start": start, "out": out, "back": back,
                "safety": safety(rows)}

    def stop_midway(self, i: int, stop_after_m: float) -> dict:
        t0 = self.poses.last()[0]
        back0 = self.poses.time_went_back
        mem0 = self.status()["memory_resets"]
        start = self.save_start()
        sx, sy = start["verifier_obs"]["xy"]
        syaw = start["verifier_obs"]["yaw"]
        go = self.client.send("goto", target="conveyor_front")
        go_react = self.reaction(go["id"])
        # 관측으로 trigger 만큼 간 순간(걷는 중) STOP. 왕복 거리가 짧으면 그 절반에서.
        gx, gy = SITE["safe_points"]["conveyor_front"]["xy_m"]
        trigger = min(stop_after_m, 0.5 * math.hypot(gx - sx, gy - sy))
        t_start = self.poses.last()[0]
        while True:
            r = self.poses.last()
            if math.hypot(r[2] - sx, r[3] - sy) >= trigger:
                break
            if r[0] - t_start > CRITERIA["leg_timeout_sim_s"]:
                raise RuntimeError(f"STOP 지점({trigger:.2f} m)까지 가지 않았다")
            time.sleep(0.005)
        moving_speed = window_speed(self.poses.since(r[0] - 1.0), r[0])   # 직전 0.5 s 평균
        stop = self.client.send("stop")
        stop_react = self.reaction(stop["id"])
        applied = stop_react["applied_sim_s"]
        st = self.status()
        cancelled = st["nav"]["goal"] is None and st["mode"] == "stand"
        # 정지 뒤 균형 유지 관찰.
        self.wait_sim(CRITERIA["stop_hold_sim_s"] + 0.5)
        rows_after = self.poses.since(applied)
        at_apply = rows_after[0]
        settle_sim = None
        for row in rows_after:
            v = window_speed(rows_after, row[0])
            if v is not None and v < CRITERIA["stop_settle_speed_mps"] and row[0] - applied >= 0.5:
                # 적용 뒤 0.5 s 창 평균 속도가 기준 아래로 내려온 **창 끝** 시각(보수적 — 창 길이 포함)
                settle_sim = round(row[0] - applied, 3)
                break
        travel_after = max(math.hypot(r[2] - at_apply[2], r[3] - at_apply[3]) for r in rows_after)
        hold = [r for r in rows_after if r[0] >= applied + 1.0]
        s_hold = safety(hold)
        not_resumed = self.status()["nav"]["goal"] is None
        # 그다음 출발점으로 복귀(정상 이동 재개 확인).
        back = self.leg("start", {"kind": "return"}, (sx, sy), syaw)
        rows = self.poses.since(t0)
        checks = {"stopped_while_moving": moving_speed is not None and moving_speed > 0.2,
                  "goal_cancelled": cancelled and not_resumed,
                  "settled": settle_sim is not None and settle_sim <= CRITERIA["stop_max_settle_sim_s"],
                  "travel_after_stop": travel_after <= CRITERIA["stop_max_travel_after_m"],
                  "balanced_after_stop": all(safety_ok(s_hold).values()),
                  "returned": back["passed"],
                  "no_world_reset": self.poses.time_went_back == back0,
                  "memory_kept": self.status()["memory_resets"] == mem0,
                  **safety_ok(safety(rows))}
        print(f"STOP {i}: {'PASS' if all(checks.values()) else 'FAIL'} "
              f"속도 {moving_speed and round(moving_speed, 3)} m/s에서 정지 · 반응 "
              f"{stop_react['sent_to_applied_wall_ms']} ms(wall) · 정착 {settle_sim} s(sim) · "
              f"추가 이동 {round(travel_after, 3)} m · 복귀 오차 {back['observed_final']['max_dist_m']} m "
              f"{[k for k, v in checks.items() if not v]}", flush=True)
        return {"kind": "stop_midway", "index": i, "passed": all(checks.values()),
                "checks": checks, "start": start, "goto_reaction": go_react,
                "stop_reaction": stop_react, "speed_when_stopped_mps": moving_speed,
                "settle_after_apply_sim_s": settle_sim, "travel_after_stop_m": round(travel_after, 3),
                "hold_safety": s_hold, "back": back, "safety": safety(rows)}

    def move_away(self, min_dist: float = 2.0) -> dict:
        """시험 준비(웹 아님): 컨베이어 앞 지점에서 min_dist 이상 떨어질 때까지 속도 명령으로 걷는다.
        왕복·STOP이 짧은 거리로 쉽게 통과하지 않게 한다."""
        gx, gy = SITE["safe_points"]["conveyor_front"]["xy_m"]
        cx, cy = SITE["conveyor"]["center_m"][:2]
        r = self.poses.last()
        if math.hypot(r[2] - gx, r[3] - gy) >= min_dist:
            return {"moved": False}
        away = math.atan2(r[3] - cy, r[2] - cx)                      # 컨베이어 반대 방향
        while abs(wrap(away - self.poses.last()[5])) > 0.2:
            err = wrap(away - self.poses.last()[5])
            self.client.send("velocity", vx=0.0, vy=0.0, wz=math.copysign(0.5, err))
            self.wait_sim(0.3)
        self.client.send("velocity", vx=0.4, vy=0.0, wz=0.0)
        t0 = self.poses.last()[0]
        while True:
            r = self.poses.last()
            if math.hypot(r[2] - gx, r[3] - gy) >= min_dist or r[0] - t0 > 30:
                break
            time.sleep(0.05)
        st = self.client.send("stop")
        self.reaction(st["id"])
        self.wait_sim(3.0)
        r = self.poses.last()
        return {"moved": True, "dist_to_point_m": round(math.hypot(r[2] - gx, r[3] - gy), 3)}

    def idle_hold(self) -> dict:
        """목표 없이 대기할 때 제자리 걸음 표류가 자리 유지로 묶이는지."""
        st = self.client.send("stop")
        self.reaction(st["id"])
        self.wait_sim(2.0)                                   # 기준 위치를 잡을 시간
        t0 = self.poses.last()[0]
        self.wait_sim(CRITERIA["idle_hold_sim_s"])
        rows = self.poses.since(t0)
        drift = max(math.hypot(r[2] - rows[0][2], r[3] - rows[0][3]) for r in rows)
        checks = {"bounded": drift <= CRITERIA["idle_max_drift_m"], **safety_ok(safety(rows))}
        print(f"대기 {CRITERIA['idle_hold_sim_s']} s(sim): 최대 이동 {drift:.3f} m "
              f"{'PASS' if all(checks.values()) else 'FAIL'}", flush=True)
        return {"kind": "idle_hold", "passed": all(checks.values()), "checks": checks,
                "max_drift_m": round(drift, 3), "hold": self.status().get("hold"),
                "safety": safety(rows)}

    def command_switch(self) -> dict:
        """실행 중 명령 전환(걷기 → 회전 → 걷기 → 정지): 반응 시간·기억 유지."""
        mem0 = self.status()["memory_resets"]
        t0 = self.poses.last()[0]
        seq = [("velocity", {"vx": 0.3, "vy": 0.0, "wz": 0.0}, 2.0),
               ("velocity", {"vx": 0.0, "vy": 0.0, "wz": 0.5}, 2.5),
               ("velocity", {"vx": 0.3, "vy": 0.0, "wz": 0.0}, 2.0),
               ("stop", {}, 3.0)]
        steps = []
        for kind, fields, hold in seq:
            c = self.client.send(kind, **fields)
            react = self.reaction(c["id"])
            self.wait_sim(hold)
            rows = self.poses.since(react["applied_sim_s"])
            # 반응 감지: 회전 명령은 yaw 속도, 걷기는 속도, 정지는 속도 감소.
            resp = None
            for k in range(1, len(rows)):
                t = rows[k][0]
                w = [r for r in rows if t - 0.3 <= r[0] <= t]
                if len(w) < 2 or w[-1][0] - w[0][0] < 0.2:
                    continue
                span = w[-1][0] - w[0][0]
                rate = abs(wrap(w[-1][5] - w[0][5])) / span
                v = math.hypot(w[-1][2] - w[0][2], w[-1][3] - w[0][3]) / span
                if kind == "velocity" and fields["wz"] and rate > CRITERIA["turn_response_rate"]:
                    resp = t - react["applied_sim_s"]; break
                if kind == "velocity" and fields["vx"] and v > 0.15 and rate < 0.3:
                    resp = t - react["applied_sim_s"]; break
                if kind == "stop" and v < CRITERIA["stop_settle_speed_mps"]:
                    resp = t - react["applied_sim_s"]; break
            steps.append({"command": kind, **fields, "reaction": react,
                          "observed_response_sim_s": None if resp is None else round(resp, 3),
                          "note": "관측 창(0.3 s) 끝 시각 기준 — 창 길이만큼 늦게 잡힌다"})
            print(f"  전환 {kind} {fields}: 적용 {react['sent_to_applied_wall_ms']} ms(wall), "
                  f"관측 반응 {steps[-1]['observed_response_sim_s']} s(sim)", flush=True)
        rows = self.poses.since(t0)
        checks = {"all_applied": all(s["reaction"] and s["reaction"]["ok"] for s in steps),
                  "all_observed": all(s["observed_response_sim_s"] is not None for s in steps),
                  "memory_kept": self.status()["memory_resets"] == mem0,
                  **safety_ok(safety(rows))}
        return {"kind": "command_switch", "passed": all(checks.values()), "checks": checks,
                "steps": steps, "safety": safety(rows)}


def main() -> int:
    import warnings
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--trips", type=int, default=3)
    parser.add_argument("--stops", type=int, default=3)
    parser.add_argument("--stop-after-m", type=float, default=1.0)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    runner = Runner()
    results = []
    status0 = runner.status()
    wall0, sim0 = time.time(), runner.poses.last()[0]
    try:
        # 시험 준비를 먼저: 컨베이어를 등지고 떨어진 곳에서 시작한다(명령 전환 시험이 앞으로 걷는다).
        setup = runner.move_away()
        print(f"시험 준비(속도 명령): {setup}", flush=True)
        results.append(runner.command_switch())
        results.append(runner.idle_hold())
        setup2 = runner.move_away()
        print(f"시험 준비(속도 명령): {setup2}", flush=True)
        # 명령 전환 뒤 자리를 옮겼다 — 왕복은 매번 그 자리(관측)를 출발점으로 저장한다.
        for i in range(1, args.trips + 1):
            results.append(runner.roundtrip(i))
        for i in range(1, args.stops + 1):
            results.append(runner.stop_midway(i, args.stop_after_m))
    finally:
        status1 = runner.status()
        sim1 = runner.poses.last()[0]
        report = {
            "schema": "forstick2.g1_roundtrip/1", "is_simulated": True,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "site": SITE, "criteria": CRITERIA,
            "observation_source": f"/world/{WORLD}/pose/info (검증기가 직접 구독)",
            "timing": {"sim_elapsed_s": round(sim1 - sim0, 3),
                       "wall_elapsed_s": round(time.time() - wall0, 3),
                       "rtf_measured": round((sim1 - sim0) / (time.time() - wall0), 3),
                       "controller_timing_ms": status1 and status1["timing_ms_last500"],
                       "controller_rtf_target": status1 and status1["rtf_target"]},
            "memory_resets": status1 and status1["memory_resets"],
            "passed": bool(results) and all(r["passed"] for r in results) and
            len(results) == 2 + args.trips + args.stops,
            "results": results,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        print(f"결과: {'PASS' if report['passed'] else 'FAIL'} "
              f"({sum(r['passed'] for r in results)}/{2 + args.trips + args.stops}) · "
              f"RTF {report['timing']['rtf_measured']} → {args.out}", flush=True)
        _ = status0
        sys.stdout.flush()
        os._exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
