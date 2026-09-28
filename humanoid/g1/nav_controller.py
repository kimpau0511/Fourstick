"""G1 연속 제어기 — 실행 중에도 명령을 받고, Gazebo 위치 관측으로 지정 지점을 오간다.

    humanoid/g1/run_gazebo.sh --nav
    humanoid/g1/run_controller.sh humanoid/g1/nav_controller.py [--rtf 1.0] [--max-wall-s 0]

제어 구조(한 제어 주기 = 시뮬레이션 0.02 s = 물리 10스텝):
  관측(시각 t의 관절·IMU·골반 pose) → 대기 명령 적용 → 이동 목표가 있으면 관측 pose로 속도 명령 계산
  → 정책(LSTM) → 목표 자세 q*를 Gazebo PD 플러그인에 보냄(seq 확인 응답을 받은 뒤)
  → 10스텝 진행 → 벽시계에 맞춰 대기(`--rtf`, 1.0 = 실시간 목표).
PD(τ = kp(q*−q) − kd·q̇)는 Gazebo 안 플러그인이 500 Hz로 계산한다. 로봇을 움직이는 수단은 이것뿐이다.

명령(`/g1/command`, gz.msgs.StringMsg JSON) — 실행 중 아무 때나:
  {"type": "velocity", "vx": .., "vy": .., "wz": ..}   수동 속도(이동 목표 취소)
  {"type": "stop"}                                    이동 목표 취소. **균형 제어는 계속**, 감속 뒤 그 자리를
                                                      기준으로 제자리 걸음 표류만 보정(HoldKeeper)
  {"type": "save_start"}                              지금 관측 pose를 출발점으로 저장(start_id 발급)
  {"type": "goto", "target": "conveyor_front"}        site.json의 안전 지점으로
  {"type": "return", "start_id": ..}                  저장한 출발점으로(위치·방향). start_id가 없으면
                                                      마지막 출발점. 좌표를 받지 않는다(관측으로 저장한 것만)
  {"type": "shutdown"}
  모든 명령에 "id"와 보낸 벽시계 "sent_wall"을 붙이면 반응 시간을 잰다.
상태(`/g1/status`, JSON, 시뮬레이션 0.1 s마다): 시각(sim·wall 구분), 모드, 명령, 목표, 관측 pose,
도착 여부, 제어 주기 통계, 명령 반응 기록.

정책 기억(LSTM hidden/cell)은 속도·회전·정지 명령이 바뀔 때 **유지**한다(학습·배치와 같이 연속 실행).
초기화하는 때: 제어기 시작, 세계 시각이 되돌아감(세계 초기화 감지), 넘어짐 뒤 재시작 — 몸 상태가
기억과 이어지지 않는 경우뿐이다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from controller import (  # noqa: E402
    ACTION_SCALE, ANG_VEL_SCALE, CMD_SCALE, DEFAULT, DOF_VEL_SCALE, GAIT_PERIOD, LEG_JOINTS,
    MODEL, PHYSICS_DT, POLICY, gravity_in_body)

# 세계 설정. 기본은 왕복 세계(site.json). 조작 실험은 FORSTICK2_G1_SITE로 다른 설정을 준다.
SITE = json.loads(Path(os.environ.get("FORSTICK2_G1_SITE", HERE / "config/site.json"))
                  .read_text(encoding="utf-8"))
WORLD = SITE["world"]
if SITE.get("model"):
    MODEL = SITE["model"]                     # 이 세계의 로봇 모델 이름(pose/info)
DECIMATION = 10
TICK_SIM = PHYSICS_DT * DECIMATION           # 0.02 s
MAX_VX, MAX_WZ = 0.5, 0.6                    # 검증된 명령 범위(verify_walk와 같음)
GUARD_M = SITE.get("keep_out", {}).get("guard_m", 0.25)   # 수동 속도·자리 유지의 금지선 앞 여유
FALLEN_UPRIGHT = 0.5                         # 골반 기울기 cos이 이보다 작으면 넘어짐
LOG_DIR = Path(os.environ.get("FORSTICK2_HUMANOID_DIR", "/tmp/forstick2_humanoid"))


def yaw_of(qx, qy, qz, qw) -> float:
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class Link:
    """gz-transport: 관측 구독, PD 목표 발행(확인 응답), 스텝, 명령·상태."""

    def __init__(self):
        from gz.msgs.double_v_pb2 import Double_V
        from gz.msgs.imu_pb2 import IMU
        from gz.msgs.int32_pb2 import Int32
        from gz.msgs.model_pb2 import Model
        from gz.msgs.pose_v_pb2 import Pose_V
        from gz.msgs.stringmsg_pb2 import StringMsg
        from gz.transport import Node
        self.node = Node()
        self.cond = threading.Condition()
        self.index = {n: i for i, n in enumerate(LEG_JOINTS)}
        self.q = np.zeros(12); self.dq = np.zeros(12); self.joint_t = -1.0
        self.quat = np.array([1.0, 0, 0, 0]); self.omega = np.zeros(3); self.imu_t = -1.0
        self.pose = None; self.pose_t = -1.0
        self.ack = -1
        self._Double_V, self._StringMsg = Double_V, StringMsg
        self.target_pub = self.node.advertise("/g1/pd/target", Double_V)
        self.status_pub = self.node.advertise("/g1/status", StringMsg)
        self.commands: queue.Queue = queue.Queue()
        # 멈춘 세계에서도 JointStatePublisher·pose/info는 같은 시각의 메시지를 계속(벽시계 ~2 ms마다)
        # 다시 보낸다. 조작 세계(관절 43)에서는 이 반복이 파이썬 수신을 밀어 작은 확인 응답이
        # 버려졌다(실측). 설정에 subscribe_hz가 있으면 그 두 구독을 전송 단계에서 줄인다 — 멈춘 동안
        # 최신 시각 메시지가 계속 반복되므로 새 관측을 놓치지 않는다(지연 ≤ 1/subscribe_hz).
        from gz.transport import SubscribeOptions
        opts = None
        if SITE.get("subscribe_hz"):
            opts = SubscribeOptions()
            opts.msgs_per_sec = int(SITE["subscribe_hz"])
        if opts is None:
            self.node.subscribe(Model, "/g1/joint_state", self._on_joints)
            self.node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._on_pose)
        else:
            self.node.subscribe(Model, "/g1/joint_state", self._on_joints, opts)
            self.node.subscribe(Pose_V, f"/world/{WORLD}/pose/info", self._on_pose, opts)
        self.node.subscribe(IMU, "/g1/imu", self._on_imu)
        self.node.subscribe(Int32, "/g1/pd/ack", self._on_ack)
        self.node.subscribe(StringMsg, "/g1/command", self._on_command)
        deadline = time.time() + 10
        while time.time() < deadline and not self.target_pub.has_connections():
            time.sleep(0.05)

    @staticmethod
    def _stamp(msg) -> float:
        return msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9

    def _on_joints(self, msg):
        q, dq, seen = np.zeros(12), np.zeros(12), 0
        for j in msg.joint:
            i = self.index.get(j.name)
            if i is not None:
                q[i], dq[i] = j.axis1.position, j.axis1.velocity
                seen += 1
        if seen == 12:
            with self.cond:
                self.q, self.dq, self.joint_t = q, dq, self._stamp(msg)
                self.cond.notify_all()

    def _on_imu(self, msg):
        with self.cond:
            o, a = msg.orientation, msg.angular_velocity
            self.quat = np.array([o.w, o.x, o.y, o.z])
            self.omega = np.array([a.x, a.y, a.z])
            self.imu_t = self._stamp(msg)
            self.cond.notify_all()

    def _on_pose(self, msg):
        for p in msg.pose:
            if p.name == MODEL:
                with self.cond:
                    self.pose = (p.position.x, p.position.y, p.position.z,
                                 p.orientation.x, p.orientation.y, p.orientation.z,
                                 p.orientation.w)
                    self.pose_t = self._stamp(msg)
                    self.cond.notify_all()
                return

    def _on_ack(self, msg):
        with self.cond:
            self.ack = msg.data
            self.cond.notify_all()

    def _on_command(self, msg):
        try:
            cmd = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        cmd["accepted_wall"] = time.time()
        self.commands.put(cmd)

    def send_target(self, seq: int, target: np.ndarray, timeout=2.0) -> None:
        msg = self._Double_V()
        msg.data.extend([float(seq)] + [float(v) for v in target])
        # 시작 직후에는 확인 응답 구독이 아직 연결되지 않아 첫 응답을 놓칠 수 있다(실측: 조작 세계
        # 첫 목표). 같은 seq를 0.2 s마다 다시 보낸다 — 같은 목표라 다시 받아도 결과가 같다.
        deadline = time.time() + timeout
        with self.cond:
            while True:
                self.target_pub.publish(msg)
                if self.cond.wait_for(lambda: self.ack == seq, min(0.2, max(0.0, deadline - time.time()))):
                    return
                if time.time() >= deadline:
                    raise TimeoutError(f"PD 목표 확인 응답이 없다(seq {seq})")

    def step(self, n: int, t_now: float, timeout=5.0) -> None:
        """n스텝 진행하고 관절·IMU 관측이 새 시각에 닿을 때까지 기다린다.

        골반 world pose(`pose/info`)는 관절보다 ~12 ms(벽시계) 늦게 온다(실측). 정책 입력에는 쓰지
        않으므로 기다리지 않고, 이동 목표 계산은 가장 최근 pose를 **그 시각과 함께** 쓴다(나이 기록)."""
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.world_control_pb2 import WorldControl
        req = WorldControl()
        req.pause = True
        req.multi_step = n
        # 파이썬 바인딩은 이 서비스 응답을 받지 못한다 — 진행은 관측 시각으로 확인한다.
        self.node.request(f"/world/{WORLD}/control", req, WorldControl, Boolean, 1)
        target = t_now + n * PHYSICS_DT - 1e-6
        with self.cond:
            ok = self.cond.wait_for(lambda: min(self.joint_t, self.imu_t) >= target, timeout)
            if not ok:
                raise TimeoutError(f"스텝 관측이 오지 않았다(joint {self.joint_t:.3f} imu "
                                   f"{self.imu_t:.3f} pose {self.pose_t:.3f} < {target:.3f})")

    def snapshot(self):
        with self.cond:
            return (self.joint_t, self.q.copy(), self.dq.copy(), self.quat.copy(),
                    self.omega.copy(), self.pose, self.pose_t)

    def reset_world(self, timeout=5.0) -> None:
        """세계를 SDF 초기 상태로. **제어기 시작 때만** 쓴다(왕복 중에는 쓰지 않는다)."""
        from gz.msgs.boolean_pb2 import Boolean
        from gz.msgs.world_control_pb2 import WorldControl
        with self.cond:
            before = self.joint_t
        req = WorldControl()
        req.pause = True
        req.reset.all = True
        self.node.request(f"/world/{WORLD}/control", req, WorldControl, Boolean, 1)
        time.sleep(0.3)
        req = WorldControl()
        req.pause = True
        # 한 제어 주기만큼 민다 — 관측 발행 주기가 50 Hz인 세계(조작 세계)도 첫 관측이 온다.
        req.multi_step = DECIMATION
        self.node.request(f"/world/{WORLD}/control", req, WorldControl, Boolean, 1)
        with self.cond:
            if not self.cond.wait_for(
                    lambda: 0 < self.joint_t <= 0.03 and self.imu_t <= 0.03 and self.pose_t <= 0.03,
                    timeout):
                raise TimeoutError(f"세계 초기화 확인 실패(시각 {self.joint_t:.3f}, 이전 {before:.3f})")

    def publish_status(self, status: dict) -> None:
        msg = self._StringMsg()
        msg.data = json.dumps(status, ensure_ascii=False)
        self.status_pub.publish(msg)


class Navigator:
    """관측 pose → (vx, vy, wz). 도착 판정은 이동 시간 계산이 아니라 관측으로 한다.

    - 멀 때(> NEAR_M): 목표 쪽으로 방향을 맞추고(제자리 회전) 앞으로 걷는다.
    - 가까울 때: 몸 좌표계 (vx, vy)로 목표 쪽으로 옆·앞 이동 + 목표 방향으로 회전.
    - 도착 뒤: 같은 방식으로 자리를 지킨다(제자리 걸음 표류 보정).
    선속도 명령은 0이거나 MIN_V 이상이다 — 학습(legged_gym)에서 크기 0.2 m/s 이하의 선속도 명령은
    0으로 바뀌어 학습되지 않았다(`legged_robot.py` `_resample_commands`). 회전 속도는 연속으로 학습됐다.
    도착: 위치 오차 < position_tol, 방향 오차 < yaw_tol 이 settle_s(시뮬레이션 시간) 동안 유지.
    """

    NEAR_M = 0.5
    ALIGN_RAD = 0.35           # 멀 때 이보다 방향이 틀리면 먼저 돈다
    MIN_V, MAX_NEAR_V = 0.25, 0.35
    STOP_IN_M, RESUME_M = 0.06, 0.09     # 선속도 끄기/다시 켜기(히스테리시스)
    YAW_DEAD = 0.04
    K_YAW, K_DIST = 1.2, 0.8

    def __init__(self, arrival: dict):
        self.pos_tol = arrival["position_tol_m"]
        self.yaw_tol = arrival["yaw_tol_rad"]
        self.settle = arrival["settle_s"]
        self.goal = None
        self.stage = "idle"
        self.inside_since = None
        self.arrived_at = None
        self.linear_on = True

    def set_goal(self, name: str, x: float, y: float, yaw: float, sim_t: float):
        self.goal = {"name": name, "xy": [x, y], "yaw": yaw, "set_sim": sim_t}
        self.stage = "align"
        self.inside_since = None
        self.arrived_at = None
        self.linear_on = True

    def cancel(self):
        self.goal = None
        self.stage = "idle"
        self.inside_since = None

    def command(self, x, y, yaw, sim_t):
        if self.goal is None:
            return None
        gx, gy = self.goal["xy"]
        dx, dy = gx - x, gy - y
        dist = math.hypot(dx, dy)
        yaw_err = wrap(self.goal["yaw"] - yaw)
        if dist < self.pos_tol and abs(yaw_err) < self.yaw_tol:
            self.inside_since = sim_t if self.inside_since is None else self.inside_since
            if sim_t - self.inside_since >= self.settle and self.arrived_at is None:
                self.arrived_at = sim_t
        else:
            self.inside_since = None
        if dist > self.NEAR_M and self.arrived_at is None:
            head_err = wrap(math.atan2(dy, dx) - yaw)
            if self.stage != "drive" or abs(head_err) > self.ALIGN_RAD:
                self.stage = "drive" if abs(head_err) < self.ALIGN_RAD * 0.5 else "align"
            wz = float(np.clip(self.K_YAW * head_err, -MAX_WZ, MAX_WZ))
            if self.stage == "align":
                return 0.0, 0.0, wz
            vx = float(np.clip(self.K_DIST * dist, self.MIN_V, MAX_VX))
            return vx, 0.0, wz
        # 가까움 또는 도착 뒤 자리 지키기.
        self.stage = "holding" if self.arrived_at is not None else "approach"
        if self.linear_on and dist < self.STOP_IN_M:
            self.linear_on = False
        elif not self.linear_on and dist > self.RESUME_M:
            self.linear_on = True
        vx = vy = 0.0
        if self.linear_on:
            speed = float(np.clip(self.K_DIST * dist, self.MIN_V, self.MAX_NEAR_V))
            c, s_ = math.cos(yaw), math.sin(yaw)
            bx, by = c * dx + s_ * dy, -s_ * dx + c * dy       # 몸 좌표계
            vx, vy = speed * bx / dist, speed * by / dist
        wz = 0.0 if abs(yaw_err) < self.YAW_DEAD else float(
            np.clip(self.K_YAW * yaw_err, -MAX_WZ, MAX_WZ))
        return vx, vy, wz

    def to_dict(self):
        return {"goal": self.goal, "stage": self.stage, "arrived_sim": self.arrived_at}


class HoldKeeper:
    """목표가 없을 때 자리 지키기. 명령 0의 제자리 걸음은 표류한다(실측 0.03 m/s, 걷다가 멈춘 뒤
    0.1–0.15 m/s) — 그대로 두면 정지·대기 중에 수 m 움직인다(컨베이어 쪽으로도).

    기준 위치: 정지(또는 대기 시작) 뒤 감속이 끝난 관측 pose(0.5 s 창 평균 속도 < 0.1 m/s, 최대 1.5 s).
    기준에서 0.15 m를 넘으면 기준 쪽으로 학습 범위 속도(0.25 m/s)로 옮기고 0.06 m 안이면 멈춘다.
    방향은 0.15 rad를 넘으면 돌려 0.05 rad 안으로. 이동 목표가 아니다 — 새 목적지로 가지 않는다."""

    ON_M, OFF_M, V = 0.15, 0.06, 0.25
    YAW_ON, YAW_OFF = 0.15, 0.05

    def __init__(self):
        self.anchor = None
        self.since = None
        self.linear = False
        self.turning = False
        self.trace: list[tuple] = []

    def clear(self):
        self.anchor = None
        self.since = None
        self.linear = self.turning = False
        self.trace = []

    def command(self, x, y, yaw, sim_t):
        self.trace.append((sim_t, x, y))
        self.trace = [r for r in self.trace if sim_t - r[0] <= 0.6]
        if self.anchor is None:
            if self.since is None:
                self.since = sim_t
            old = self.trace[0]
            span = sim_t - old[0]
            slow = span >= 0.45 and math.hypot(x - old[1], y - old[2]) / span < 0.1
            if (slow and sim_t - self.since >= 0.5) or sim_t - self.since >= 1.5:
                self.anchor = {"xy": [x, y], "yaw": yaw, "sim": round(sim_t, 3)}
            return 0.0, 0.0, 0.0
        ax, ay = self.anchor["xy"]
        dx, dy = ax - x, ay - y
        dist = math.hypot(dx, dy)
        if self.linear and dist < self.OFF_M:
            self.linear = False
        elif not self.linear and dist > self.ON_M:
            self.linear = True
        yaw_err = wrap(self.anchor["yaw"] - yaw)
        if self.turning and abs(yaw_err) < self.YAW_OFF:
            self.turning = False
        elif not self.turning and abs(yaw_err) > self.YAW_ON:
            self.turning = True
        vx = vy = 0.0
        if self.linear:
            c, s_ = math.cos(yaw), math.sin(yaw)
            vx = self.V * (c * dx + s_ * dy) / dist
            vy = self.V * (-s_ * dx + c * dy) / dist
        wz = float(np.clip(1.2 * yaw_err, -MAX_WZ, MAX_WZ)) if self.turning else 0.0
        return vx, vy, wz

    def to_dict(self, x=None, y=None):
        if self.anchor is None:
            return {"anchor": None, "settling": self.since is not None}
        d = None if x is None else round(math.hypot(self.anchor["xy"][0] - x,
                                                    self.anchor["xy"][1] - y), 3)
        return {"anchor": self.anchor, "dist_m": d, "correcting": self.linear or self.turning}


class NavController:
    def __init__(self, link: Link, rtf: float):
        import torch
        torch.set_num_threads(1)          # 작은 LSTM — 스레드 나눔은 지연 흔들림만 키운다(실측 p95)
        self.torch = torch
        self.policy = torch.jit.load(POLICY)
        self.policy.eval()
        self.link = link
        self.rtf = rtf
        self.nav = Navigator(SITE["arrival"])
        self.hold = HoldKeeper()
        self.cmd = np.zeros(3)
        self.mode = "stand"             # stand | velocity | goto | fallen
        self.action = np.zeros(12)
        self.ticks = 0
        self.seq = 0
        self.start_pose = None
        self.starts: dict[str, dict] = {}     # start_id → 관측으로 저장한 출발점
        self.memory_resets: list[dict] = []
        self.reactions: list[dict] = []
        self.events: list[dict] = []
        self.keep_out_x = SITE["conveyor"]["front_face_x_m"] - \
            SITE["keep_out"]["min_pelvis_to_front_face_m"]
        self.min_front_clearance = None
        self.timing = {"tick_wall_ms": [], "policy_ms": [], "step_wait_ms": [], "pose_age_sim_ms": [],
                       "overruns": 0}
        self.last_sim = None

    # ── 정책 기억 ───────────────────────────────────────────────────────
    def reset_memory(self, why: str, sim_t: float):
        with self.torch.no_grad():
            self.policy.hidden_state.zero_()
            self.policy.cell_state.zero_()
        self.action = np.zeros(12)
        self.ticks = 0
        self.memory_resets.append({"why": why, "sim": round(sim_t, 3), "wall": time.time()})

    # ── 명령 ────────────────────────────────────────────────────────────
    def apply_command(self, c: dict, sim_t: float, pose) -> bool:
        kind = c.get("type")
        rec = {"id": c.get("id"), "type": kind, "sent_wall": c.get("sent_wall"),
               "accepted_wall": c["accepted_wall"], "applied_wall": time.time(),
               "applied_sim": round(sim_t, 3), "ok": True}
        x, y, _z, qx, qy, qz, qw = pose
        yaw = yaw_of(qx, qy, qz, qw)
        if self.mode == "fallen" and kind not in ("shutdown",):
            rec.update(ok=False, reason="넘어진 상태 — 세계 초기화가 필요하다")
        elif kind == "velocity":
            self.nav.cancel()
            self.cmd = np.array([np.clip(c.get("vx", 0.0), -MAX_VX, MAX_VX),
                                 np.clip(c.get("vy", 0.0), -0.3, 0.3),
                                 np.clip(c.get("wz", 0.0), -MAX_WZ, MAX_WZ)])
            self.mode = "velocity"
        elif kind == "stop":
            # 목표 취소 + 명령 0. 정책·PD는 계속 돈다(토크를 끄지 않는다).
            rec["cancelled_goal"] = self.nav.goal["name"] if self.nav.goal else None
            rec["was_moving"] = bool(np.any(self.cmd)) or self.mode in ("goto", "velocity")
            self.nav.cancel()
            # 걷던 중이면 감속이 끝난 곳을 새 기준으로 잡는다. 이미 서 있던 중(자리 유지)이면 기준을
            # 그대로 둔다 — 정지 명령이 거듭될 때마다 표류한 자리로 기준이 옮겨 가 조금씩 밀려났다
            # (실측: 조작 실험에서 STOP 여러 번 뒤 골반이 기준에서 0.37 m).
            if rec["was_moving"]:
                self.hold.clear()
            self.cmd = np.zeros(3)
            self.mode = "stand"
        elif kind == "save_start":
            start_id = f"start_{len(self.starts) + 1}_{int(sim_t * 1000)}"
            self.start_pose = {"id": start_id, "xy": [x, y], "yaw": yaw, "sim": round(sim_t, 3)}
            self.starts[start_id] = self.start_pose
            if len(self.starts) > 200:
                self.starts.pop(next(iter(self.starts)))
            rec["start"] = self.start_pose
        elif kind == "goto":
            point = SITE["safe_points"].get(c.get("target", ""))
            if point is None:
                rec.update(ok=False, reason=f"모르는 지점: {c.get('target')}")
            else:
                self.nav.set_goal(c["target"], *point["xy_m"], point["yaw_rad"], sim_t)
                self.mode = "goto"
        elif kind == "return":
            start = self.starts.get(c["start_id"]) if c.get("start_id") else self.start_pose
            if start is None:
                rec.update(ok=False, reason="저장한 출발점이 없다" if not c.get("start_id")
                           else f"모르는 출발점: {c['start_id']}")
            else:
                self.nav.set_goal("start", *start["xy"], start["yaw"], sim_t)
                self.nav.goal["start_id"] = start.get("id")
                self.mode = "goto"
        elif kind == "shutdown":
            pass
        else:
            rec.update(ok=False, reason=f"모르는 명령: {kind}")
        self.reactions.append(rec)
        self.events.append({"sim": round(sim_t, 3), "event": "command", **{
            k: rec[k] for k in ("id", "type", "ok")}})
        return kind == "shutdown"

    def observation(self, q, dq, quat, omega) -> np.ndarray:
        phase = (self.ticks * TICK_SIM) % GAIT_PERIOD / GAIT_PERIOD
        return np.concatenate([
            omega * ANG_VEL_SCALE, gravity_in_body(quat), self.cmd * CMD_SCALE,
            q - DEFAULT, dq * DOF_VEL_SCALE, self.action,
            [math.sin(2 * math.pi * phase), math.cos(2 * math.pi * phase)]]).astype(np.float32)

    def compute_leg_target(self, q, dq, quat, omega) -> np.ndarray:
        """정책 한 번 → 다리 PD 목표. 하위 클래스가 다른 정책으로 바꿀 수 있다."""
        with self.torch.no_grad():
            act = self.policy(self.torch.from_numpy(
                self.observation(q, dq, quat, omega)).unsqueeze(0))
        self.action = act.numpy().squeeze().astype(float)
        return self.action * ACTION_SCALE + DEFAULT

    def fallen_target(self) -> np.ndarray:
        return DEFAULT.copy()

    def before_step(self, sim_t: float, pose) -> None:
        """스텝 직전 훅. 보행 제어기는 할 일이 없다."""

    def status(self, sim_t, pose, wall0, sim0) -> dict:
        x, y, z, qx, qy, qz, qw = pose
        t = self.timing
        def stats(v):
            if not v:
                return None
            a = np.array(v[-500:])
            return {"mean": round(float(a.mean()), 2), "p95": round(float(np.percentile(a, 95)), 2),
                    "max": round(float(a.max()), 2)}
        wall_el = time.time() - wall0
        return {
            "schema": "forstick2.g1_nav_status/1", "is_simulated": True,
            "sim_time_s": round(sim_t, 3), "wall_time": time.time(),
            "sim_elapsed_s": round(sim_t - sim0, 3), "wall_elapsed_s": round(wall_el, 3),
            "rtf_measured": round((sim_t - sim0) / wall_el, 3) if wall_el > 0 else None,
            "rtf_target": self.rtf,
            "mode": self.mode, "cmd": [round(float(v), 3) for v in self.cmd],
            # 균형 제어: 정책이 돌고 있으면 명령 0이어도 제자리 걸음으로 균형을 잡는다(정지 자세 아님).
            "policy_active": self.mode != "fallen",
            "balance": ("fallen" if self.mode == "fallen" else
                        "stepping_in_place" if not np.any(self.cmd) else
                        "correcting_drift" if self.mode == "stand" else "walking"),
            "hold": self.hold.to_dict(x, y) if self.mode == "stand" else None,
            "nav": self.nav.to_dict(),
            "pose": {"xy": [round(x, 4), round(y, 4)], "z": round(z, 4),
                     "yaw": round(yaw_of(qx, qy, qz, qw), 4),
                     "upright_cos": round(1 - 2 * (qx * qx + qy * qy), 4)},
            "start_pose": self.start_pose,
            "min_front_clearance_m": self.min_front_clearance,
            "timing_ms_last500": {"tick_wall": stats(t["tick_wall_ms"]), "policy": stats(t["policy_ms"]),
                                  "step_wait": stats(t["step_wait_ms"]),
                                  "pose_age_sim": stats(t["pose_age_sim_ms"]),
                                  "overruns_total": t["overruns"],
                                  "tick_sim_ms": TICK_SIM * 1000},
            "reactions_last": self.reactions[-10:],
            "memory_resets": self.memory_resets[-5:],
        }

    def run(self, max_wall_s: float = 0.0, reset_world: bool = False) -> None:
        link = self.link
        if reset_world:
            link.reset_world()
            self.events.append({"sim": 0.0, "event": "world_reset_at_start"})
        else:
            # 첫 관측: 세계가 멈춰 있으면 한 번 민다.
            link.step(1, link.snapshot()[0])
        sim_t, *_rest, pose, _pt = link.snapshot()
        self.reset_memory("제어기 시작", sim_t)
        self.last_sim = sim_t
        wall0, sim0 = time.time(), sim_t
        next_status = sim_t
        while True:
            tick_start = time.time()
            sim_t, q, dq, quat, omega, pose, pose_t = link.snapshot()
            self.timing["pose_age_sim_ms"].append((sim_t - pose_t) * 1000)
            if sim_t < self.last_sim - 1e-6:
                self.reset_memory("세계 시각이 되돌아감(세계 초기화)", sim_t)
                self.nav.cancel(); self.cmd = np.zeros(3); self.mode = "stand"
                self.start_pose = None
                wall0, sim0 = time.time(), sim_t
            self.last_sim = sim_t
            x, y, z, qx, qy, qz, qw = pose
            upright = 1 - 2 * (qx * qx + qy * qy)
            if self.mode != "fallen" and (upright < FALLEN_UPRIGHT or z < 0.5):
                self.mode = "fallen"
                self.nav.cancel(); self.cmd = np.zeros(3)
                self.events.append({"sim": round(sim_t, 3), "event": "fallen"})
            shutdown = False
            while not link.commands.empty():
                shutdown |= self.apply_command(link.commands.get(), sim_t, pose)
            if shutdown:
                break
            # 안전 거리: 골반이 컨베이어 앞면 기준선을 넘으면 목표를 취소하고 멈춘다.
            clearance = SITE["conveyor"]["front_face_x_m"] - x \
                if abs(y - SITE["conveyor"]["center_m"][1]) < SITE["conveyor"]["size_m"][1] / 2 + 0.3 \
                else None
            if clearance is not None:
                self.min_front_clearance = round(clearance if self.min_front_clearance is None
                                                 else min(self.min_front_clearance, clearance), 4)
            if x > self.keep_out_x and self.mode == "goto" and self.cmd[0] > 0:
                self.nav.cancel(); self.cmd = np.zeros(3); self.mode = "stand"
                self.events.append({"sim": round(sim_t, 3), "event": "keep_out_stop",
                                    "x": round(x, 3)})
            if self.mode == "goto":
                self.hold.clear()
                c = self.nav.command(x, y, yaw_of(qx, qy, qz, qw), sim_t)
                self.cmd = np.array(c) if c is not None else np.zeros(3)
            elif self.mode == "stand":
                # 목표 없음(정지·대기): 균형 제어는 계속, 표류만 기준 위치로 보정한다.
                self.cmd = np.array(self.hold.command(x, y, yaw_of(qx, qy, qz, qw), sim_t))
            else:
                self.hold.clear()
            # 수동 속도·자리 유지는 금지선 앞 GUARD_M에서부터 컨베이어 쪽(+x) 선속도를 막는다.
            # 걷던 관성으로 0.2 m쯤 더 가기 때문이다(실측: 선을 넘은 뒤 막으면 앞면 0.13 m까지 감).
            if self.mode in ("velocity", "stand") and clearance is not None:
                c_, s_ = math.cos(yaw_of(qx, qy, qz, qw)), math.sin(yaw_of(qx, qy, qz, qw))
                world_vx = c_ * self.cmd[0] - s_ * self.cmd[1]
                if x > self.keep_out_x - GUARD_M and world_vx > 0:
                    self.cmd = np.array([0.0, 0.0, self.cmd[2]])
                    if not self.events or self.events[-1].get("event") != "keep_out_guard":
                        self.events.append({"sim": round(sim_t, 3), "event": "keep_out_guard",
                                            "x": round(x, 3), "mode": self.mode})
            if self.mode == "fallen":
                target = self.fallen_target()     # 정책 없이 기본 자세 PD(토크는 끄지 않는다)
                p_ms = 0.0
            else:
                tp = time.perf_counter()
                target = self.compute_leg_target(q, dq, quat, omega)
                p_ms = (time.perf_counter() - tp) * 1000
            self.seq = (self.seq + 1) % 1_000_000_000
            ts = time.perf_counter()
            link.send_target(self.seq, target)
            self.before_step(sim_t, pose)          # 하위 클래스(팔·손)가 상체 목표를 보낸다
            link.step(DECIMATION, sim_t)
            s_ms = (time.perf_counter() - ts) * 1000
            self.ticks += 1
            sim_now = sim_t + TICK_SIM
            if sim_now >= next_status:
                link.publish_status(self.status(sim_now, link.snapshot()[5], wall0, sim0))
                next_status = sim_now + 0.1
            # 벽시계 맞추기: rtf 1.0이면 시뮬레이션 0.02 s = 벽시계 0.02 s.
            if self.rtf > 0:
                due = wall0 + (sim_now - sim0) / self.rtf
                slack = due - time.time()
                if slack > 0:
                    time.sleep(slack)
                elif slack < -TICK_SIM / self.rtf:
                    self.timing["overruns"] += 1
            tw = (time.time() - tick_start) * 1000
            self.timing["tick_wall_ms"].append(tw)
            self.timing["policy_ms"].append(p_ms)
            self.timing["step_wait_ms"].append(s_ms)
            for key in ("tick_wall_ms", "policy_ms", "step_wait_ms", "pose_age_sim_ms"):
                if len(self.timing[key]) > 5000:
                    del self.timing[key][:2500]
            if max_wall_s and time.time() - wall0 > max_wall_s:
                break
        final = self.status(link.snapshot()[0], link.snapshot()[5], wall0, sim0)
        final["reactions"] = self.reactions
        final["events"] = self.events
        final["memory_resets"] = self.memory_resets
        (LOG_DIR / "nav_controller_final.json").write_text(
            json.dumps(final, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main() -> int:
    import warnings
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtf", type=float, default=1.0,
                        help="목표 실시간 배율(1.0 = 실시간, 0 = 가능한 빨리)")
    parser.add_argument("--max-wall-s", type=float, default=0.0)
    parser.add_argument("--reset-world", action="store_true",
                        help="시작할 때 한 번 세계 초기화(왕복 중에는 하지 않는다)")
    args = parser.parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # torch를 gz 노드보다 **먼저** 불러온다. gz 구독을 만든 뒤 torch를 불러오면 조작 세계에서 PD 확인
    # 응답(Int32)이 더는 들어오지 않았다(실측 — 불러오는 순서만 바꾸면 된다. 원인 기전은 미확인).
    import torch  # noqa: F401
    link = Link()
    ctl = NavController(link, args.rtf)
    code = 0
    try:
        ctl.run(args.max_wall_s, args.reset_world)
    except Exception as exc:  # noqa: BLE001
        print(f"제어기 중단: {type(exc).__name__}: {exc}", flush=True)
        code = 1
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
