"""MuJoCo 참조 실행 — 같은 정책·같은 단계를 unitree_rl_gym 배치 방식(MuJoCo)으로 돌려 Gazebo와 비교한다.

    /home/asd/external/humanoid_venv/bin/python humanoid/g1/mujoco_reference.py          # 단계 비교
    /home/asd/external/humanoid_venv/bin/python humanoid/g1/mujoco_reference.py --hold   # 정책 없이 PD만

판정용이 아니다(판정은 Gazebo 관측, `verify_walk.py`). 정책·모델이 원래 환경에서 어떻게 움직이는지의 기준값이다.
"""
import math
import sys
import warnings

import mujoco
import numpy as np
import torch
import yaml

warnings.filterwarnings("ignore")
R = "/home/asd/external/unitree_rl_gym"
cfg = yaml.safe_load(open(f"{R}/deploy/deploy_mujoco/configs/g1.yaml"))
m = mujoco.MjModel.from_xml_path(f"{R}/resources/robots/g1_description/scene.xml"); d = mujoco.MjData(m)
m.opt.timestep = 0.002
pol = torch.jit.load(f"{R}/deploy/pre_train/g1/motion.pt")
kps = np.array(cfg["kps"]); kds = np.array(cfg["kds"]); q0 = np.array(cfg["default_angles"])
def grav(q):
    w, x, y, z = q
    return np.array([2*(-z*x + w*y), -2*(z*y + w*x), 1 - 2*(w*w + z*z)])
def yaw(q):
    w,x,y,z=q; return math.atan2(2*(w*z+x*y),1-2*(y*y+z*z))
act = np.zeros(12); tgt = q0.copy(); obs = np.zeros(47, np.float32); i = 0
HOLD = "--hold" in sys.argv   # 정책 없이 기본 자세 PD만(서지 못하는지 확인용)
for name, cmd, sec in [("stand",(0,0,0),3),("walk",(0.5,0,0),4),("stop_after_walk",(0,0,0),3),("turn",(0,0,0.6),5),("stop",(0,0,0),4)]:
    cmd = np.array(cmd, float); p0 = d.qpos[:3].copy(); y0 = yaw(d.qpos[3:7]); yl = y0; ytot = 0; zmin = 9
    for _ in range(int(sec/0.002)):
        d.ctrl[:] = (tgt - d.qpos[7:]) * kps - d.qvel[6:] * kds
        mujoco.mj_step(m, d); i += 1
        if i % 10 == 0 and not HOLD:
            ph = (i * 0.002) % 0.8 / 0.8
            obs[:3] = d.qvel[3:6]*0.25; obs[3:6] = grav(d.qpos[3:7]); obs[6:9] = cmd*np.array(cfg["cmd_scale"])
            obs[9:21] = d.qpos[7:]-q0; obs[21:33] = d.qvel[6:]*0.05; obs[33:45] = act; obs[45:47] = [np.sin(2*np.pi*ph), np.cos(2*np.pi*ph)]
            act = pol(torch.from_numpy(obs).unsqueeze(0)).detach().numpy().squeeze(); tgt = act*0.25+q0
        yc = yaw(d.qpos[3:7]); ytot += (yc-yl+math.pi)%(2*math.pi)-math.pi; yl = yc; zmin = min(zmin, d.qpos[2])
    dp = d.qpos[:2]-p0[:2]
    fwd = dp @ [math.cos(y0), math.sin(y0)]
    print(f"{name:16s} fwd={fwd:.3f} drift={np.linalg.norm(dp):.3f} yaw={ytot:.3f} zmin={zmin:.3f}")
