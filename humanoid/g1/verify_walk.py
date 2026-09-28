"""G1 Gazebo 보행 반복 검증 — 판정은 **Gazebo 관측**으로만 한다.

    humanoid/g1/run_gazebo.sh                                   # 멈춘 세계 서버
    humanoid/g1/run_controller.sh humanoid/g1/verify_walk.py --repeats 5

한 회차 = 세계 초기화 → 서기(명령 0) → 짧게 걷기 → 정지 → 제자리 회전 → 정지.
로봇을 옮기는 호출은 회차 시작의 세계 초기화(`WorldControl.reset`)뿐이다.

관측원(모두 Gazebo가 낸 것):
- 골반(모델) world pose: `/world/forstick2_humanoid/pose/info` + 그 메시지의 시각.
- 발 접촉: 발 충돌 구 4개에 단 contact 센서(지면과 닿은 접촉만 센다).

판정 기준은 결과를 보기 **전에** 정해 `CRITERIA`에 둔다. 제어기 내부 추정치(IMU·관절)는
판정에 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import warnings
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from controller import MODEL, PHYSICS_DT, POLICY, WORLD, Controller, G1Gazebo  # noqa: E402

OUT = ROOT / "reports/humanoid/g1_gazebo_walk.json"
# (이름, 종류, 명령 (vx, vy, yaw_rate), 시간 s)
PHASES = (
    ("stand", "stand", (0.0, 0.0, 0.0), 3.0),
    ("walk", "walk", (0.5, 0.0, 0.0), 4.0),
    ("stop_after_walk", "stop", (0.0, 0.0, 0.0), 3.0),
    ("turn", "turn", (0.0, 0.0, 0.6), 5.0),
    ("stop", "stop", (0.0, 0.0, 0.0), 4.0),
)
CRITERIA = {
    # 모든 단계
    "min_base_z_m": 0.65,            # 서 있을 때 ~0.78, 넘어지면 ~0.32
    "min_upright_cos": 0.94,         # 골반 기울기 20° 이내
    "max_planar_speed_mps": 2.0,     # 연속 관측 사이 속도(순간 이동 감지)
    # 서기
    "stand_max_drift_m": 0.30,
    "stand_min_contact_ratio": 0.30,  # 발마다 접촉 시간 비율
    # 걷기(명령 방향 = 시작 시 몸 방향)
    "walk_min_forward_ratio": 0.5,    # 전진 ≥ 명령 속도 × 시간 × 0.5
    "walk_max_lateral_m": 0.35,
    "walk_min_swings_per_foot": 3,    # 발이 떨어졌다 다시 닿은 횟수
    "min_swing_s": 0.08,
    # 회전
    "turn_min_yaw_ratio": 0.5,        # 회전각 ≥ 명령 각속도 × 시간 × 0.5, 같은 방향
    "turn_max_drift_m": 0.60,
    # 정지(단계 마지막 2 s)
    "stop_window_s": 2.0,
    "stop_max_speed_mps": 0.12,
    "stop_max_yaw_rate": 0.15,
}


def yaw_of(qx, qy, qz, qw) -> float:
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def upright_of(qx, qy, qz, qw) -> float:
    """골반 z축과 world z축 사이 cos(= 회전행렬 R[2,2])."""
    return 1 - 2 * (qx * qx + qy * qy)


class Contacts:
    """발별 지면 접촉 시각(ms 단위 정수) 모음."""

    def __init__(self, node):
        from gz.msgs.contacts_pb2 import Contacts as ContactsMsg
        self.lock = threading.Lock()
        self.stamps = {"left": set(), "right": set()}
        self.topics = {}
        for side in ("left", "right"):
            topic = (f"/world/{WORLD}/model/{MODEL}/link/{side}_ankle_roll_link/sensor/"
                     f"{side}_foot_contact/contact")
            self.topics[side] = topic
            node.subscribe(ContactsMsg, topic, lambda msg, s=side: self._on(s, msg))

    def _on(self, side: str, msg) -> None:
        ground = any("ground" in (c.collision1.name + c.collision2.name) for c in msg.contact)
        if not ground:
            return
        ms = round((msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9) * 1000)
        with self.lock:
            self.stamps[side].add(ms)

    def clear(self) -> None:
        """세계 초기화로 시각이 0부터 다시 시작한다 — 이전 회차 접촉과 섞이지 않게 비운다."""
        with self.lock:
            for stamps in self.stamps.values():
                stamps.clear()

    def series(self, side: str, t0: float, t1: float) -> np.ndarray:
        """[t0, t1) 물리 스텝마다 접촉 여부."""
        steps = np.arange(round(t0 * 1000), round(t1 * 1000), round(PHYSICS_DT * 1000))
        with self.lock:
            have = self.stamps[side]
            return np.array([int(s) in have for s in steps], dtype=bool)


def swings(contact: np.ndarray, min_steps: int) -> int:
    """떨어졌다(≥ min_steps) 다시 닿은 횟수."""
    count, run, seen_contact = 0, 0, False
    for c in contact:
        if c:
            if seen_contact and run >= min_steps:
                count += 1
            run = 0
            seen_contact = True
        else:
            run += 1
    return count


def judge_phase(kind: str, cmd, seconds: float, rows: list[dict], contacts: dict) -> dict:
    c = CRITERIA
    t = np.array([r["t"] for r in rows])
    xy = np.array([[r["x"], r["y"]] for r in rows])
    z = np.array([r["z"] for r in rows])
    up = np.array([r["upright"] for r in rows])
    yaw = np.unwrap(np.array([r["yaw"] for r in rows]))
    dt = np.diff(t)
    speed = (np.linalg.norm(np.diff(xy, axis=0), axis=1) / np.where(dt > 0, dt, np.nan)
             if len(rows) > 1 else np.zeros(0))
    checks = {
        "samples": len(rows) >= max(10, int(seconds * 20)),
        "no_fall_z": float(z.min()) >= c["min_base_z_m"],
        "upright": float(up.min()) >= c["min_upright_cos"],
        "no_teleport": bool(np.nanmax(speed) <= c["max_planar_speed_mps"]) if speed.size else True,
    }
    heading0 = yaw[0]
    disp = xy[-1] - xy[0]
    forward = float(disp @ [math.cos(heading0), math.sin(heading0)])
    lateral_path = (xy - xy[0]) @ [-math.sin(heading0), math.cos(heading0)]
    metrics = {
        "t0": round(float(t[0]), 3), "t1": round(float(t[-1]), 3),
        "min_z_m": round(float(z.min()), 3), "max_z_m": round(float(z.max()), 3),
        "min_upright_cos": round(float(up.min()), 4),
        "max_planar_speed_mps": round(float(np.nanmax(speed)), 3) if speed.size else 0.0,
        "displacement_m": [round(float(v), 3) for v in disp],
        "planar_drift_m": round(float(np.linalg.norm(disp)), 3),
        "forward_m": round(forward, 3),
        "max_lateral_m": round(float(np.abs(lateral_path).max()), 3),
        "yaw_change_rad": round(float(yaw[-1] - yaw[0]), 3),
        "contact_ratio": {s: round(float(v.mean()), 3) if v.size else 0.0
                          for s, v in contacts.items()},
        "swings": {s: swings(v, round(c["min_swing_s"] / PHYSICS_DT))
                   for s, v in contacts.items()},
    }
    if kind == "stand":
        checks["drift"] = metrics["planar_drift_m"] <= c["stand_max_drift_m"]
        checks["both_feet_contact"] = all(v >= c["stand_min_contact_ratio"]
                                          for v in metrics["contact_ratio"].values())
    elif kind == "walk":
        need = c["walk_min_forward_ratio"] * cmd[0] * seconds
        metrics["forward_needed_m"] = round(need, 3)
        checks["forward"] = forward >= need
        checks["lateral"] = metrics["max_lateral_m"] <= c["walk_max_lateral_m"]
        checks["alternating_steps"] = all(v >= c["walk_min_swings_per_foot"]
                                          for v in metrics["swings"].values())
    elif kind == "turn":
        need = c["turn_min_yaw_ratio"] * cmd[2] * seconds
        metrics["yaw_needed_rad"] = round(need, 3)
        checks["yaw"] = metrics["yaw_change_rad"] * math.copysign(1, cmd[2]) >= abs(need)
        checks["turn_drift"] = metrics["planar_drift_m"] <= c["turn_max_drift_m"]
    elif kind == "stop":
        w = t >= t[-1] - c["stop_window_s"]
        tw, xyw, yw = t[w], xy[w], yaw[w]
        span = float(tw[-1] - tw[0]) or 1e-9
        metrics["stop_window_speed_mps"] = round(float(np.linalg.norm(xyw[-1] - xyw[0]) / span), 3)
        metrics["stop_window_yaw_rate"] = round(float(abs(yw[-1] - yw[0]) / span), 3)
        checks["settled_speed"] = metrics["stop_window_speed_mps"] <= c["stop_max_speed_mps"]
        checks["settled_yaw"] = metrics["stop_window_yaw_rate"] <= c["stop_max_yaw_rate"]
    return {"kind": kind, "cmd": list(cmd), "seconds": seconds, "passed": all(checks.values()),
            "checks": checks, "metrics": metrics}


def run_once(ctl: Controller, contacts: Contacts, index: int) -> dict:
    o = ctl.reset()
    time.sleep(0.2)
    contacts.clear()
    o = ctl.sim.step(1)
    t_start = o.sim_time
    wall0 = time.time()
    phases = []
    for name, kind, cmd, seconds in PHASES:
        rows: list[dict] = []
        seen: set[float] = set()

        def sample(o, _ctl, rows=rows, seen=seen):
            if o.base_pose is None or o.pose_time in seen:
                return
            seen.add(o.pose_time)
            x, y, z, qx, qy, qz, qw = o.base_pose
            rows.append({"t": o.pose_time, "x": x, "y": y, "z": z,
                         "yaw": yaw_of(qx, qy, qz, qw), "upright": upright_of(qx, qy, qz, qw)})

        t0 = ctl.sim.obs.sim_time
        o = ctl.run(seconds, policy=True, cmd=cmd, on_sample=sample, sample_every=5)
        # 접촉 메시지가 모두 도착할 시간을 조금 준다(세계는 멈춰 있다).
        time.sleep(0.2)
        feet = {s: contacts.series(s, t0, o.sim_time) for s in ("left", "right")}
        result = judge_phase(kind, cmd, seconds, rows, feet)
        result["name"] = name
        phases.append(result)
        m = result["metrics"]
        print(f"  [{index}] {name:16s} {'PASS' if result['passed'] else 'FAIL'} "
              f"z≥{m['min_z_m']} up≥{m['min_upright_cos']} fwd={m['forward_m']} "
              f"yaw={m['yaw_change_rad']} drift={m['planar_drift_m']} swings={m['swings']}"
              + ("" if result["passed"] else f" 실패={[k for k, v in result['checks'].items() if not v]}"),
              flush=True)
    return {"repeat": index, "passed": all(p["passed"] for p in phases),
            "sim_start": round(t_start, 3), "sim_end": round(ctl.sim.obs.sim_time, 3),
            "wall_s": round(time.time() - wall0, 1), "phases": phases}


def main() -> int:
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    sim = G1Gazebo()
    contacts = Contacts(sim.node)
    ctl = Controller(sim)
    runs = []
    try:
        for i in range(1, args.repeats + 1):
            print(f"회차 {i}/{args.repeats}", flush=True)
            runs.append(run_once(ctl, contacts, i))
    finally:
        report = {
            "schema": "forstick2.humanoid_walk/1", "is_simulated": True,
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "engine": "Gazebo (gz-sim, DART), 중력 -9.81, 지면 평면 마찰 1.0, 물리 0.002 s lockstep",
            "robot": "Unitree G1 12자유도 다리(unitree_rl_gym g1_12dof.urdf, BSD-3-Clause)",
            "policy": POLICY, "observation_source": {
                "base_pose": f"/world/{WORLD}/pose/info (모델 {MODEL})",
                "foot_contact": list(contacts.topics.values())},
            "moves_robot_only_by": "관절 토크(ApplyJointForce). 세계 초기화는 회차 시작에만",
            "phases": [{"name": n, "kind": k, "cmd": list(c), "seconds": s}
                       for n, k, c, s in PHASES],
            "criteria": CRITERIA, "repeats": len(runs),
            "passed": bool(runs) and len(runs) == args.repeats and all(r["passed"] for r in runs),
            "runs": runs,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                                  encoding="utf-8")
        print(f"결과: {'PASS' if report['passed'] else 'FAIL'} "
              f"({sum(r['passed'] for r in runs)}/{args.repeats}) → {args.out}", flush=True)
        sim.close()
        os._exit(0 if report["passed"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())
