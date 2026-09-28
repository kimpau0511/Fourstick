"""G1 보행 제어기 — Gazebo와 **같은 걸음(lockstep)**으로 돈다. 로봇을 순간 이동시키지 않는다.

    humanoid/g1/run_controller.sh --plan stand:3,walk:0.5:0:0:6,turn:0:0:0.6:6,stop:4

- 물리 한 스텝(0.002 s)마다: Gazebo 관측(관절 위치·속도, 골반 IMU) → PD 토크
  kp(q*−q) − kd·q̇(토크 한계로 자름) → `ApplyJointForce` → 한 스텝 진행.
- 10스텝(50 Hz)마다 정책: unitree_rl_gym `deploy/pre_train/g1/motion.pt`(BSD-3-Clause)에
  MuJoCo 배치와 **같은** 47개 관측(각속도·중력 투영·명령·관절·이전 행동·보행 위상)을 넣는다.
- 모든 단계가 정책으로 돈다. `stand`/`stop` = 명령 0(정책이 제자리 걸음으로 균형을 잡는다),
  `walk`/`turn` = 속도 명령. 기본 자세 PD만으로는 서지 못한다(MuJoCo·Gazebo 모두 약 1.5 s에
  앞으로 넘어진다) — 이 정책에는 정지 자세 모드가 없다.
- 판정은 Gazebo 관측(골반 world pose)으로만 한다(`humanoid/g1/verify_walk.py`).
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

WORLD = "forstick2_humanoid"
MODEL = "g1_12dof"
PARTITION = os.environ.get("GZ_PARTITION_HUMANOID", "forstick2_humanoid")
POLICY = os.environ.get("FORSTICK2_G1_POLICY",
                        "/home/asd/external/unitree_rl_gym/deploy/pre_train/g1/motion.pt")
LEG_JOINTS = ("left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
              "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
              "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
              "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint")
# unitree_rl_gym deploy/deploy_mujoco/configs/g1.yaml 값 그대로.
KPS = np.array([100, 100, 100, 150, 40, 40, 100, 100, 100, 150, 40, 40], dtype=float)
KDS = np.array([2, 2, 2, 4, 2, 2, 2, 2, 2, 4, 2, 2], dtype=float)
DEFAULT = np.array([-0.1, 0, 0, 0.3, -0.2, 0, -0.1, 0, 0, 0.3, -0.2, 0], dtype=float)
EFFORT = np.array([88, 139, 88, 139, 50, 50, 88, 139, 88, 139, 50, 50], dtype=float)
PHYSICS_DT = 0.002
DECIMATION = 10
ANG_VEL_SCALE, DOF_VEL_SCALE, ACTION_SCALE = 0.25, 0.05, 0.25
CMD_SCALE = np.array([2.0, 2.0, 0.25])
GAIT_PERIOD = 0.8


def gravity_in_body(q_wxyz) -> np.ndarray:
    w, x, y, z = q_wxyz
    return np.array([2 * (-z * x + w * y), -2 * (z * y + w * x), 1 - 2 * (w * w + z * z)])


@dataclass
class Observation:
    sim_time: float = -1.0
    q: np.ndarray = field(default_factory=lambda: np.zeros(12))
    dq: np.ndarray = field(default_factory=lambda: np.zeros(12))
    omega: np.ndarray = field(default_factory=lambda: np.zeros(3))
    quat_wxyz: np.ndarray = field(default_factory=lambda: np.array([1.0, 0, 0, 0]))
    imu_time: float = -1.0
    base_pose: tuple | None = None          # (x, y, z, qx, qy, qz, qw) — 검증용
    pose_time: float = -1.0                 # base_pose의 Gazebo 시각


class G1Gazebo:
    """gz-transport로 관측·토크·스텝을 다룬다."""

    def __init__(self):
        os.environ["GZ_PARTITION"] = PARTITION
        from gz.transport import Node
        self.node = Node()
        self.obs = Observation()
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        from gz.msgs.double_pb2 import Double
        self._Double = Double
        self.pubs = [self.node.advertise(f"/model/{MODEL}/joint/{j}/cmd_force", Double)
                     for j in LEG_JOINTS]
        from gz.msgs.imu_pb2 import IMU
        from gz.msgs.model_pb2 import Model
        from gz.msgs.pose_v_pb2 import Pose_V
        self.node.subscribe(Model, "/g1/joint_state", self._on_joints)
        self.node.subscribe(IMU, "/g1/imu", self._on_imu)
        self.node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._on_pose)
        index = {name: i for i, name in enumerate(LEG_JOINTS)}
        self._index = index
        deadline = time.time() + 10
        while time.time() < deadline and not all(p.has_connections() for p in self.pubs):
            time.sleep(0.05)

    def _on_joints(self, msg) -> None:
        q, dq = np.zeros(12), np.zeros(12)
        seen = 0
        for joint in msg.joint:
            i = self._index.get(joint.name)
            if i is not None:
                q[i], dq[i] = joint.axis1.position, joint.axis1.velocity
                seen += 1
        if seen != 12:
            return
        stamp = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
        with self.cond:
            self.obs.q, self.obs.dq, self.obs.sim_time = q, dq, stamp
            self.cond.notify_all()

    def _on_imu(self, msg) -> None:
        with self.cond:
            o = msg.orientation
            self.obs.quat_wxyz = np.array([o.w, o.x, o.y, o.z])
            a = msg.angular_velocity
            self.obs.omega = np.array([a.x, a.y, a.z])
            self.obs.imu_time = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9

    def _on_pose(self, msg) -> None:
        for pose in msg.pose:
            if pose.name == MODEL:
                p, o = pose.position, pose.orientation
                with self.lock:
                    self.obs.base_pose = (p.x, p.y, p.z, o.x, o.y, o.z, o.w)
                    self.obs.pose_time = msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9
                return

    def torques(self, tau: np.ndarray) -> None:
        for pub, value in zip(self.pubs, tau):
            msg = self._Double()
            msg.data = float(value)
            pub.publish(msg)

    def step(self, n: int = 1, timeout: float = 5.0) -> Observation:
        """n 스텝 진행하고 그 시각의 관절 관측을 기다린다(멈춘 세계를 민다)."""
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.world_control_pb2 import WorldControl
        with self.lock:
            start = self.obs.sim_time
        req = WorldControl()
        req.pause = True
        req.multi_step = n
        # 파이썬 바인딩은 이 서비스의 응답을 받지 못한다(FR3 fixture 호출과 같은 현상):
        # 요청만 보내고, 진행 확인은 새 시각이 찍힌 관절 관측으로 한다.
        self.node.request(f"/world/{WORLD}/control", req, WorldControl, Boolean, 1)
        target = start + n * PHYSICS_DT - 1e-6
        with self.cond:
            ok = self.cond.wait_for(lambda: self.obs.sim_time >= target, timeout)
            if not ok:
                raise TimeoutError(f"스텝 관측이 오지 않았다(sim {self.obs.sim_time:.3f} < {target:.3f})")
            return Observation(self.obs.sim_time, self.obs.q.copy(), self.obs.dq.copy(),
                               self.obs.omega.copy(), self.obs.quat_wxyz.copy(),
                               self.obs.imu_time, self.obs.base_pose, self.obs.pose_time)

    def reset(self, timeout: float = 5.0) -> Observation:
        """시험 시작 전 세계를 SDF 초기 상태(시각 0, 초기 자세)로 되돌린다.

        매 시도를 같은 조건에서 시작하기 위한 초기화일 뿐, 보행 중에는 쓰지 않는다."""
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.world_control_pb2 import WorldControl
        self.torques(np.zeros(12))
        req = WorldControl()
        req.pause = True
        req.reset.all = True
        with self.lock:
            before = self.obs.sim_time
        self.node.request(f"/world/{WORLD}/control", req, WorldControl, Boolean, 1)
        time.sleep(0.3)
        step = WorldControl()
        step.pause = True
        step.multi_step = 1
        self.node.request(f"/world/{WORLD}/control", step, WorldControl, Boolean, 1)
        with self.cond:
            # 초기화가 먹었는지는 시각이 되돌아간 관측으로 확인한다.
            ok = self.cond.wait_for(lambda: 0 < self.obs.sim_time < min(before, 0.05) or
                                    (before <= 0.05 and 0 < self.obs.sim_time <= 0.004), timeout)
            if not ok:
                raise TimeoutError(f"초기화 확인 실패(sim {self.obs.sim_time:.3f}, 이전 {before:.3f})")
        return self.step(1)

    def close(self) -> None:
        for topic in ("/g1/joint_state", "/g1/imu", f"/world/{WORLD}/pose/info"):
            try:
                self.node.unsubscribe(topic)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)


class Controller:
    def __init__(self, sim: G1Gazebo):
        import torch
        self.torch = torch
        self.policy = torch.jit.load(POLICY)
        self.policy.eval()
        self.sim = sim
        self.action = np.zeros(12)
        self.target = DEFAULT.copy()
        self.steps = 0
        self.policy_on = False
        self.cmd = np.zeros(3)

    def reset_memory(self) -> None:
        """LSTM 기억을 0으로. 정책은 순환 신경망(PolicyExporterLSTM)이라 hidden/cell 상태를
        버퍼로 들고 있고, TorchScript에는 reset_memory()가 실리지 않는다 — 원본과 같은 일을 한다.
        안 비우면 다음 회차가 이전 회차의 기억으로 시작한다(첫 행동부터 달라짐, 실측)."""
        with self.torch.no_grad():
            self.policy.hidden_state.zero_()
            self.policy.cell_state.zero_()

    def reset(self) -> Observation:
        self.reset_memory()
        self.action = np.zeros(12)
        self.target = DEFAULT.copy()
        self.steps = 0
        self.policy_on = False
        self.cmd = np.zeros(3)
        return self.sim.reset()

    def observation(self, o: Observation) -> np.ndarray:
        phase = (self.steps * PHYSICS_DT) % GAIT_PERIOD / GAIT_PERIOD
        return np.concatenate([
            o.omega * ANG_VEL_SCALE, gravity_in_body(o.quat_wxyz), self.cmd * CMD_SCALE,
            o.q - DEFAULT, o.dq * DOF_VEL_SCALE, self.action,
            [math.sin(2 * math.pi * phase), math.cos(2 * math.pi * phase)]]).astype(np.float32)

    def run(self, seconds: float, *, policy: bool, cmd=(0.0, 0.0, 0.0), on_sample=None,
            sample_every: int = 25):
        """seconds 동안 lockstep. policy=False면 기본 자세 PD 유지."""
        self.cmd = np.asarray(cmd, dtype=float)
        if policy and not self.policy_on:
            self.action = np.zeros(12)
        self.policy_on = policy
        if not policy:
            self.target = DEFAULT.copy()
        o = self.sim.step(1)
        for _ in range(int(round(seconds / PHYSICS_DT))):
            tau = np.clip(KPS * (self.target - o.q) - KDS * o.dq, -EFFORT, EFFORT)
            self.sim.torques(tau)
            o = self.sim.step(1)
            self.steps += 1
            if policy and self.steps % DECIMATION == 0:
                obs = self.observation(o)
                act = self.policy(self.torch.from_numpy(obs).unsqueeze(0))
                self.action = act.detach().numpy().squeeze().astype(float)
                self.target = self.action * ACTION_SCALE + DEFAULT
            if on_sample is not None and self.steps % sample_every == 0:
                on_sample(o, self)
        return o


def main() -> int:
    import argparse
    import warnings
    warnings.filterwarnings("ignore", category=FutureWarning)
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", default="stand:3,walk:0.5:0:0:6,turn:0:0:0.6:6,stop:4")
    args = parser.parse_args()
    sim = G1Gazebo()
    ctl = Controller(sim)
    try:
        o = ctl.reset()
        print(f"reset t={o.sim_time:.3f} base={o.base_pose and [round(v, 3) for v in o.base_pose[:3]]}", flush=True)
        for item in args.plan.split(","):
            kind, *vals = item.split(":")
            vals = [float(v) for v in vals]
            if kind == "hold":                      # 비교용: 정책 없이 기본 자세 PD만
                o = ctl.run(vals[0], policy=False)
            elif kind in ("stand", "stop"):
                o = ctl.run(vals[0], policy=True)
            else:
                o = ctl.run(vals[3], policy=True, cmd=vals[:3])
            print(f"{kind:5s} t={o.sim_time:6.2f} base={o.base_pose and [round(v, 3) for v in o.base_pose[:3]]}"
                  f" upright={-gravity_in_body(o.quat_wxyz)[2]:.3f}", flush=True)
    finally:
        sim.close()
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
