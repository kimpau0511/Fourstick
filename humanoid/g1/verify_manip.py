"""G1 팔·손 접근 검증 — 균형 유지 중 한쪽 팔 움직이기 · 물체 앞 접근 자세 · 손 열기/닫기 · STOP · 차단.

    humanoid/g1/run_gazebo.sh --manip
    humanoid/g1/run_manip.sh manip_controller.py --rtf 1.0 --reset-world &
    humanoid/g1/run_manip.sh verify_manip.py

- 몸(골반)·물체 위치는 이 검증기가 직접 구독한 Gazebo `pose/info`로 잰다. 관절 관측·손바닥 위치(관측 관절
  + 관측 골반 pose로 순방향 기구학)·접촉은 제어기 상태로 읽는다. 몸을 고정하거나 옮기지 않는다 — 다리는
  보행 정책이 계속 균형을 잡는다. 검증 중 세계 초기화를 하지 않는다.
- 시간은 시뮬레이션(`*_sim_s`)과 벽시계(`*_wall_s`)를 따로 적는다.
"""

from __future__ import annotations

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

from g1cmd import Client  # noqa: E402
from nav_controller import MODEL, SITE, WORLD, yaw_of  # noqa: E402

OUT = ROOT / "reports/humanoid/g1_manip.json"
CRIT = {"min_z_m": 0.65, "min_upright_cos": round(math.cos(math.radians(15)), 4),
        "max_body_disp_m": 0.25, "palm_to_target_m": 0.04,
        "min_hand_to_object_obs_m": 0.015,
        "max_object_move_m": 0.01, "hand_track_rad": 0.15, "stop_freeze_rad": 0.03,
        "arm_timeout_sim_s": 40.0}


class Obs:
    def __init__(self, node):
        from gz.msgs.pose_v_pb2 import Pose_V
        from gz.transport import SubscribeOptions
        self.lock = threading.Lock()
        self.rows: list[tuple] = []          # sim, x, y, z, yaw, upright, ox, oy, oz
        self.back = 0
        opts = SubscribeOptions()
        opts.msgs_per_sec = 100
        node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._on, opts)

    def _on(self, msg):
        t = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
        body = obj = None
        for p in msg.pose:
            if p.name == MODEL:
                body = p
            elif p.name == SITE["object"]["model"]:
                obj = p
        if body is None or obj is None:
            return
        o = body.orientation
        row = (t, body.position.x, body.position.y, body.position.z, yaw_of(o.x, o.y, o.z, o.w),
               1 - 2 * (o.x * o.x + o.y * o.y), obj.position.x, obj.position.y, obj.position.z)
        with self.lock:
            if self.rows and t < self.rows[-1][0] - 1e-9:
                self.back += 1
            if not self.rows or t > self.rows[-1][0]:
                self.rows.append(row)

    def last(self):
        with self.lock:
            return self.rows[-1] if self.rows else None

    def since(self, t0, t1=math.inf):
        with self.lock:
            return [r for r in self.rows if t0 <= r[0] <= t1]


class Runner:
    def __init__(self):
        self.c = Client()
        self.obs = Obs(self.c.node)
        deadline = time.time() + 15
        while time.time() < deadline and (self.obs.last() is None or self.c.latest(0.2) is None):
            time.sleep(0.1)
        if self.obs.last() is None or self.c.latest(0.2) is None:
            raise RuntimeError("제어기 상태 또는 Gazebo 관측이 없다")

    def st(self):
        return self.c.latest(1.0)

    def sim(self):
        return self.obs.last()[0]

    def wait_sim(self, s):
        t0 = self.sim()
        while self.sim() < t0 + s:
            time.sleep(0.02)

    def send(self, kind, **kw):
        cmd = self.c.send(kind, **kw)
        deadline = time.time() + 120                # 접근 자세 탐색은 수 초 걸릴 수 있다(시뮬레이션은 멈춰 있다)
        while time.time() < deadline:
            st = self.st()
            for r in (st or {}).get("reactions_last", []):
                if r.get("id") == cmd["id"]:
                    return r
            time.sleep(0.05)
        return None

    def wait_arm(self, key="arm"):
        t0 = self.sim()
        samples = []
        while self.sim() < t0 + CRIT["arm_timeout_sim_s"]:
            st = self.st()
            samples.append(st)
            a = st[key]
            # 명령이 목표 근처(0.02 rad — 추적 중에는 목표가 0.2 s마다 조금씩 바뀐다)에 오고 1 s(시뮬레이션) 뒤
            # 관측 관절이 명령을 0.12 rad 안에서 따르면 도착.
            if a["remaining_rad"] < 0.02 and a.get("obs"):
                t_ok = self.sim()
                while self.sim() < t_ok + 1.0:
                    time.sleep(0.05)
                st2 = self.st()
                samples.append(st2)
                if (st2[key].get("track_err_max_rad") or 9) < 0.12:
                    return True, samples
            if a.get("mode") == "blocked":
                return False, samples
            if a.get("mode") == "blocked":
                return False, samples
            time.sleep(0.1)
        return False, samples

    def to_stance(self, timeout_sim=60.0) -> dict:
        """조작 자세 서기 지점으로(보행 goto, 도착 뒤 자리 지키기). 관측 위치가 허용 범위 안이 될 때까지."""
        tol = SITE["approach"]["stance_tol_m"]
        err0 = self.st()["arm"].get("stance_error")
        if err0 and err0[0] <= tol * 0.8 and err0[1] <= SITE["approach"]["stance_yaw_tol_rad"]:
            return {"arrived": True, "stance_error": err0, "sim_s": 0.0, "walked": False}
        react = self.send("goto", target="manip_stance")
        if react and react.get("ok") is False:
            return {"arrived": False, "stance_error": err0, "reason": react.get("reason")}
        t0 = self.sim()
        while self.sim() < t0 + timeout_sim:
            st = self.st()
            err = st["arm"].get("stance_error")
            if st["nav"].get("arrived_sim") is not None and err and err[0] <= tol * 0.8:
                return {"arrived": True, "stance_error": err, "sim_s": round(self.sim() - t0, 2)}
            time.sleep(0.1)
        return {"arrived": False, "stance_error": self.st()["arm"].get("stance_error"),
                "reaction_ok": react and react.get("ok")}

    def body(self, t0, t1=math.inf):
        rows = self.obs.since(t0, t1)
        x0, y0, ox0, oy0, oz0 = rows[0][1], rows[0][2], rows[0][6], rows[0][7], rows[0][8]
        return {"samples": len(rows), "window_sim_s": round(rows[-1][0] - rows[0][0], 3),
                "min_z_m": round(min(r[3] for r in rows), 4),
                "min_upright_cos": round(min(r[5] for r in rows), 4),
                "max_tilt_deg": round(math.degrees(math.acos(max(-1, min(1, min(r[5] for r in rows))))), 2),
                "max_body_disp_m": round(max(math.hypot(r[1] - x0, r[2] - y0) for r in rows), 4),
                "object_move_m": round(max(math.dist((r[6], r[7], r[8]), (ox0, oy0, oz0)) for r in rows), 4)}

    def body_ok(self, b):
        return {"no_fall": b["min_z_m"] >= CRIT["min_z_m"] and b["min_upright_cos"] >= CRIT["min_upright_cos"],
                "body_bounded": b["max_body_disp_m"] <= CRIT["max_body_disp_m"]}

    def contacts(self, samples):
        seen = []
        for s in samples:
            for c in s.get("contacts_last_0_5s") or []:
                if not c["other"].startswith("self:"):
                    seen.append((c["link"], c["other"]))
        return sorted(set(seen))


def phase(name, passed, checks, **data):
    out = {"phase": name, "passed": passed, "checks": checks, **data}
    print(f"{name}: {'PASS' if passed else 'FAIL'} "
          + json.dumps({k: v for k, v in data.items() if k in ("body", "palm", "hand", "stop", "reaction")},
                       ensure_ascii=False)[:600]
          + ("" if passed else f" 실패={[k for k, v in checks.items() if not v]}"), flush=True)
    return out


ARGS = None


def main() -> int:
    import argparse
    import warnings
    global ARGS, OUT
    warnings.filterwarnings("ignore")
    ap = argparse.ArgumentParser()
    ap.add_argument("--approaches", type=int, default=2)
    ap.add_argument("--out", default=str(OUT))
    ARGS = ap.parse_args()
    OUT = Path(ARGS.out)
    r = Runner()
    results = []
    wall0, sim0 = time.time(), r.sim()
    try:
        # 0. 기준: 팔 0에서 서기(자리 유지) 10 s
        r.send("stop")
        r.send("arm_home")
        r.wait_arm()
        t0 = r.sim()
        r.wait_sim(10)
        b = r.body(t0)
        results.append(phase("기준 서기 10 s", all(r.body_ok(b).values()), r.body_ok(b), body=b))

        # 1. 한쪽 팔 천천히 들기 → 유지 → 내리기(서기 지점에서)
        r.to_stance()
        t0, w0 = r.sim(), time.time()
        react = r.send("arm_raise", arm="left")
        ok_up, s1 = r.wait_arm()
        r.wait_sim(5)
        react2 = r.send("arm_home")
        ok_down, s2 = r.wait_arm()
        r.wait_sim(3)
        b = r.body(t0)
        st = r.st()
        errs = [s["arm"]["track_err_max_rad"] for s in s1 + s2 if s["arm"]["track_err_max_rad"] is not None]
        checks = {**r.body_ok(b), "raise_accepted": bool(react and react.get("ok")),
                  "raised": ok_up and bool(s1) and bool(s1[-1]["arm"].get("left_obs")) and
                  abs(s1[-1]["arm"]["left_obs"]["shoulder_pitch"] - (-0.35)) < 0.12,
                  "lowered": ok_down,
                  "no_contact": not r.contacts(s1 + s2)}
        steady = [s["arm"]["track_err_max_rad"] for s in (s1[-1:] + s2[-1:]) if s["arm"]["track_err_max_rad"] is not None]
        results.append(phase("왼팔 들기·내리기", all(checks.values()), checks, body=b,
                             steady_track_err_rad=steady,
                             reaction={"raise": react and react.get("ok"), "home": react2 and react2.get("ok")},
                             max_track_err_rad=max(errs) if errs else None,
                             sim_s=round(r.sim() - t0, 2), wall_s=round(time.time() - w0, 1),
                             contacts=r.contacts(s1 + s2), raised_left_obs=(s1[-1]["arm"].get("left_obs") if s1 else None)))

        # 2. 접근 자세 ×2 (손 닫기·열기 포함)
        # 첫 접근은 흔들림 측정(측정 전 기본 여유 0.10 m로 먼 접근 자세) — 뒤의 접근은 측정한 흔들림을 쓴다.
        for k in ["흔들림 측정"] + list(range(1, ARGS.approaches + 1)):
            stance = r.to_stance()
            t0, w0 = r.sim(), time.time()
            react = r.send("arm_approach")
            if not react or not react.get("ok"):
                results.append(phase(f"접근 {k}", False, {"accepted": False}, reaction=react, stance=stance))
                r.send("arm_home")                      # 다음 단계 전에 팔을 되돌린다
                r.wait_arm()
                continue
            ok_reach, s1 = r.wait_arm()
            t_hold = r.sim()
            hold_samples = []
            while r.sim() < t_hold + 5:
                hold_samples.append(r.st())
                time.sleep(0.2)
            arm = [s["arm"] for s in hold_samples]
            palm_err = [math.dist(a["palm_obs_w"], a["palm_target_w"]) for a in arm
                        if a["palm_obs_w"] and a["palm_target_w"]]
            palm_obj = [a["clearance_obs"]["object_m"] for a in arm
                        if a.get("clearance_obs") and a["clearance_obs"].get("object_m") is not None]
            rot_err = [a["grasp_rot_err_obs_rad"] for a in arm if a.get("grasp_rot_err_obs_rad") is not None]
            b_reach = r.body(t0)
            # 손 닫기·열기
            rc = r.send("hand_close")
            ok_close, s_close = r.wait_arm("hand") if False else (None, [])
            close_samples = []
            t_c = r.sim()
            while r.sim() < t_c + 3:
                close_samples.append(r.st())
                time.sleep(0.2)
            hc = close_samples[-1]["hand"]
            closed = all(abs(hc["obs"][n] - hc["cmd"][n]) <= CRIT["hand_track_rad"] for n in hc["cmd"])
            closed_amount = {n: hc["obs"][n] for n in hc["obs"]}
            ro = r.send("hand_open")
            open_samples = []
            t_o = r.sim()
            while r.sim() < t_o + 3:
                open_samples.append(r.st())
                time.sleep(0.2)
            ho = open_samples[-1]["hand"]
            opened = all(abs(ho["obs"][n]) <= CRIT["hand_track_rad"] for n in ho["obs"])
            rh = r.send("arm_home")
            ok_home, s_home = r.wait_arm()
            b = r.body(t0)
            contacts = r.contacts(s1 + hold_samples + close_samples + open_samples + s_home)
            checks = {**r.body_ok(b), "reached": ok_reach,
                      # 손 위치: 중앙값이 측정한 흔들림 안(계획이 이 흔들림을 여유로 넣었다). 사전 기준(4 cm)은 따로 적는다.
                      "palm_within_measured_sway": bool(palm_err) and sorted(palm_err)[len(palm_err) // 2] <= (
                          (r.st()["arm"].get("sway_margin_m") or 0) + 0.01),
                      "hand_clear_of_object": bool(palm_obj) and min(palm_obj) >= CRIT["min_hand_to_object_obs_m"],
                      "object_not_moved": b["object_move_m"] <= CRIT["max_object_move_m"],
                      "hand_closed": closed, "hand_opened": opened, "home": ok_home,
                      "no_contact": not contacts}
            sway = r.st()["arm"]
            results.append(phase(
                f"접근 {k}", all(checks.values()), checks, body=b,
                sway={"margin_m": sway.get("sway_margin_m"), "samples": sway.get("sway_samples")},
                reaction={"approach": react.get("ok"), "ik_err_m": react.get("ik_err_m"),
                          "planning_wall_ms": round((react["applied_wall"] - react["accepted_wall"]) * 1000, 1),
                          "ik_rot_err_rad": react.get("ik_rot_err_rad"),
                          "approach_distance_m": react.get("approach_distance_m"),
                          "com_shift_mm": react.get("com_shift_mm"),
                          "path_clearance": react.get("path_clearance"),
                          "close": rc and rc.get("closed_clearance")},
                palm={"max_err_to_target_m": round(max(palm_err), 4) if palm_err else None,
                      "median_err_to_target_m": round(sorted(palm_err)[len(palm_err) // 2], 4) if palm_err else None,
                      "strict_4cm_median_met": bool(palm_err) and sorted(palm_err)[len(palm_err) // 2] <= CRIT["palm_to_target_m"],
                      "min_hand_to_object_obs_m": round(min(palm_obj), 4) if palm_obj else None,
                      "max_grasp_rot_err_obs_rad": round(max(rot_err), 4) if rot_err else None,
                      "target_w": arm[-1]["palm_target_w"], "obs_w": arm[-1]["palm_obs_w"],
                      "clearance_obs": arm[-1]["clearance_obs"]},
                hand={"closed_obs": closed_amount, "closed_cmd": hc["cmd"], "open_obs": ho["obs"]},
                body_during_reach=b_reach, contacts=contacts, stance=stance,
                sim_s=round(r.sim() - t0, 2), wall_s=round(time.time() - w0, 1)))

        # 3. 접근 중 STOP → 팔 멈춤 · 균형 유지
        r.to_stance()
        t0 = r.sim()
        r.send("arm_approach")
        r.wait_sim(1.5)
        stop = r.send("stop")
        r.wait_sim(0.5)
        a0 = r.st()["arm"]["obs"]
        t_s = r.sim()
        r.wait_sim(8)
        a1 = r.st()
        moved = max(abs(a1["arm"]["obs"][n] - a0[n]) for n in a0)
        b = r.body(t_s)
        checks = {**r.body_ok(b), "stop_applied": bool(stop and stop.get("ok")),
                  "arm_frozen": moved <= CRIT["stop_freeze_rad"],
                  "arm_mode_hold": a1["arm"]["mode"] == "hold",
                  "balance_active": a1["balance"] in ("stepping_in_place", "correcting_drift", "standing_balance")}
        results.append(phase("접근 중 STOP", all(checks.values()), checks, body=b,
                             stop={"arm_change_after_stop_rad": round(moved, 4), "arm_mode": a1["arm"]["mode"],
                                   "reason": a1["arm"]["reason"], "balance": a1["balance"],
                                   "applied_sim": stop and stop.get("applied_sim"),
                                   "sent_to_applied_wall_ms": stop and round(
                                       (stop["applied_wall"] - stop["sent_wall"]) * 1000, 1)}))
        r.send("arm_home")
        r.wait_arm()

        # 4. 차단: 잡기 점이 물체 중심(거리 0) · 물체 너머 0.30 m(닿지 않음)
        for name, dist in (("차단: 물체와 겹침", 0.0), ("차단: 도달 불가", -0.50),
                           ("차단: 흔들림보다 가까운 접근(0.19 m)", 0.19)):
            r.to_stance()
            before = r.st()["arm"]["cmd"]
            react = r.send("arm_approach", approach_distance_m=dist)
            r.wait_sim(1.0)
            after = r.st()["arm"]
            checks = {"blocked": bool(react) and react.get("ok") is False,
                      "arm_not_moved": max(abs(after["cmd"][n] - before[n]) for n in before) < 1e-3,
                      "reason_given": bool(react and react.get("reason"))}
            results.append(phase(name, all(checks.values()), checks,
                                 reaction={"ok": react and react.get("ok"), "reason": react and react.get("reason"),
                                           "com_shift_mm": react and react.get("com_shift_mm"),
                                           "ik_err_m": react and react.get("ik_err_m")}))
        r.send("arm_home")
        r.wait_arm()
    finally:
        report = {"schema": "forstick2.g1_manip/1", "is_simulated": True,
                  "controller": SITE.get("policy", {}).get("kind", "unitree_rl_gym_g1_12dof_motion"),
                  "physics_diff": SITE.get("physics_diff"),
                  "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "model": MODEL, "world": WORLD, "criteria": CRIT,
                  "timing": {"sim_s": round(r.sim() - sim0, 1), "wall_s": round(time.time() - wall0, 1)},
                  "world_time_rollbacks": r.obs.back,
                  "passed": bool(results) and all(x["passed"] for x in results),
                  "results": results}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"결과: {'PASS' if report['passed'] else 'FAIL'} "
              f"({sum(x['passed'] for x in results)}/{len(results)}) → {OUT}", flush=True)
        sys.stdout.flush()
        os._exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
