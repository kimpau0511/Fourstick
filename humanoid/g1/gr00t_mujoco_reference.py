"""GR00T-WholeBodyControl Balance 정책 — MuJoCo 참조 실행(정책의 원래 배치 환경).

    /home/asd/external/humanoid_venv/bin/python humanoid/g1/gr00t_mujoco_reference.py [--reach 0.40] [--seconds 10]

- 정책: NVlabs/GR00T-WholeBodyControl `decoupled_wbc/sim2mujoco` Balance.onnx(NVIDIA Open Model License, 코드 Apache-2.0).
  하체 12 + 허리 3 관절을 제어하고, 팔 14는 밖에서 PD로 준다(원 스크립트와 같은 kp 100·kd 0.5).
- 관측(86×6): 명령 7(속도·키 0.74·몸통 rpy) · 골반 각속도 · 중력 · 29관절 위치·속도 · 이전 행동 15. 원 스크립트와 같게 만든다.
- 시험: 서기 → 오른팔을 천천히(0.3 rad/s) 목표 자세로 → 유지. 골반 이동·기울기·손 흔들림(1.6 s 이동 평균에서 벗어난 최대)을 잰다.
  목표 팔 자세는 조작 제어기와 같은 역기구학(잡기 점이 골반 앞 reach m, 오른쪽 0.25 m, 위 0.08 m).
판정용이 아니다(판정은 Gazebo). 정책이 원래 환경에서 팔을 뻗었을 때 어떻게 버티는지의 기준값이다.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import sys
from pathlib import Path

import mujoco
import numpy as np
import onnxruntime as ort
import yaml

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("FORSTICK2_GR00T_WORK", "/home/asd/external/gr00t_work"))
WEIGHTS = Path(os.environ.get("FORSTICK2_GR00T_WEIGHTS", "/home/asd/external/gr00t_weights"))


def quat_rotate_inverse(q, v):
    """원 스크립트(run_mujoco_gear_wbc.py)와 같은 식 — 쿼터니언 (w,x,y,z)의 역회전."""
    w, x, y, z = q
    c = np.array([w, -x, -y, -z])
    return np.array([
        v[0] * (c[0] ** 2 + c[1] ** 2 - c[2] ** 2 - c[3] ** 2) + v[1] * 2 * (c[1] * c[2] - c[0] * c[3])
        + v[2] * 2 * (c[1] * c[3] + c[0] * c[2]),
        v[0] * 2 * (c[1] * c[2] + c[0] * c[3]) + v[1] * (c[0] ** 2 - c[1] ** 2 + c[2] ** 2 - c[3] ** 2)
        + v[2] * 2 * (c[2] * c[3] - c[0] * c[1]),
        v[0] * 2 * (c[1] * c[3] - c[0] * c[2]) + v[1] * 2 * (c[2] * c[3] + c[0] * c[1])
        + v[2] * (c[0] ** 2 - c[1] ** 2 - c[2] ** 2 + c[3] ** 2)])


def arm_target(reach: float) -> dict:
    sys.path.insert(0, str(HERE))
    os.environ.setdefault("FORSTICK2_G1_SITE", str(HERE / "config/manip_site.json"))
    from urdf_kin import Robot, ik_pose, rpy_matrix  # noqa: F401
    import manip_controller as mc
    q, ep, er = ik_pose(mc.ROBOT, "right_hand_palm_link", mc.RIGHT_ARM, np.array([reach, -0.25, 0.08]),
                        mc.R_HOME, {n: 0.0 for n in mc.RIGHT_ARM}, tip_offset=mc.GRASP_OFFSET)
    return q, ep


def run(reach: float, seconds: float, move_speed=0.3) -> dict:
    cfg = yaml.safe_load(open(WORK / "g1_gear_wbc.yaml"))
    m = mujoco.MjModel.from_xml_path(str(WORK / "g1_gear_wbc.xml"))
    d = mujoco.MjData(m)
    m.opt.timestep = cfg["simulation_dt"]
    sess = ort.InferenceSession(str(WEIGHTS / "gr00t_Balance.onnx"))
    names = [m.joint(i).name for i in range(1, m.njnt)]
    n = len(names)
    kps = np.array(cfg["kps"], np.float32)
    kds = np.array(cfg["kds"], np.float32)
    q0 = np.array(cfg["default_angles"], np.float32)
    na = cfg["num_actions"]
    action = np.zeros(na, np.float32)
    target = q0.copy()
    hist = collections.deque([np.zeros(86, np.float32)] * cfg["obs_history_len"], maxlen=cfg["obs_history_len"])
    cmd = np.zeros(7, np.float32)
    cmd[3] = cfg["height_cmd"]
    goal_arm, ik_err = arm_target(reach) if reach > 0 else ({}, 0.0)
    arm_names = names[na:]
    arm_cmd = np.zeros(len(arm_names))
    arm_goal = np.array([goal_arm.get(a, 0.0) for a in arm_names])
    palm = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "right_hand_palm_link")
    dt = m.opt.timestep
    steps = int(seconds / dt)
    rows = []
    phase_move = int(3.0 / dt)                     # 3 s 서 있다가 팔을 움직인다
    for i in range(steps * 3):
        if i >= phase_move:
            arm_cmd += np.clip(arm_goal - arm_cmd, -move_speed * dt, move_speed * dt)
        tau = (target - d.qpos[7:7 + na]) * kps - d.qvel[6:6 + na] * kds
        d.ctrl[:na] = tau
        d.ctrl[na:] = (arm_cmd - d.qpos[7 + na:7 + n]) * 100.0 - d.qvel[6 + na:6 + n] * 0.5
        mujoco.mj_step(m, d)
        if (i + 1) % cfg["control_decimation"] == 0:
            obs = np.zeros(86, np.float32)
            obs[0:3] = cmd[:3] * np.array(cfg["cmd_scale"])
            obs[3] = cmd[3]
            obs[4:7] = cmd[4:7]
            obs[7:10] = d.qvel[3:6] * cfg["ang_vel_scale"]
            obs[10:13] = quat_rotate_inverse(d.qpos[3:7], np.array([0.0, 0.0, -1.0]))
            pad = np.zeros(n, np.float32)
            pad[:na] = q0
            obs[13:13 + n] = (d.qpos[7:7 + n] - pad) * cfg["dof_pos_scale"]
            obs[13 + n:13 + 2 * n] = d.qvel[6:6 + n] * cfg["dof_vel_scale"]
            obs[13 + 2 * n:13 + 2 * n + na] = action
            hist.append(obs)
            action = sess.run(None, {"input": np.concatenate(list(hist))[None, :]})[0][0].astype(np.float32)
            target = action * cfg["action_scale"] + q0
        if i % 10 == 0:
            w, x, y, z = d.qpos[3:7]
            rows.append((i * dt, *d.qpos[:3], 1 - 2 * (x * x + y * y), *d.xpos[palm],
                         float(np.max(np.abs(arm_goal - arm_cmd)))))
    a = np.array(rows)
    t = a[:, 0]
    settled = t >= t[-1] - seconds                # 마지막 seconds 동안(팔 도착 뒤) 측정
    hold = a[settled]
    ts, hand = hold[:, 0], hold[:, 5:8]
    devs = [np.linalg.norm(hand[k] - hand[(ts >= ts[k] - 0.8) & (ts <= ts[k] + 0.8)].mean(0))
            for k in range(len(ts)) if ts[k] - 0.8 >= ts[0] and ts[k] + 0.8 <= ts[-1]]
    return {"reach_m": reach, "ik_err_m": round(float(ik_err), 4),
            "arm_arrived": bool(hold[0, 8] < 1e-3),
            "fell": bool(a[:, 3].min() < 0.5),
            "pelvis_z_min": round(float(a[:, 3].min()), 3),
            "max_tilt_deg": round(math.degrees(math.acos(min(1.0, a[:, 4].min()))), 2),
            "hold_tilt_deg_max": round(math.degrees(math.acos(min(1.0, hold[:, 4].min()))), 2),
            "body_disp_total_m": round(float(np.linalg.norm(a[-1, 1:3] - a[0, 1:3])), 3),
            "body_disp_hold_m": round(float(np.max(np.linalg.norm(hold[:, 1:3] - hold[0, 1:3], axis=1))), 3),
            "hand_sway_m": round(float(max(devs)), 4) if devs else None,
            "hand_range_hold_m": round(float(np.max(np.linalg.norm(hand - hand.mean(0), axis=1))), 4)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--reach", type=float, nargs="*", default=[0.0, 0.33, 0.40])
    p.add_argument("--seconds", type=float, default=10.0)
    args = p.parse_args()
    for r in args.reach:
        print(json.dumps(run(r, args.seconds), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
