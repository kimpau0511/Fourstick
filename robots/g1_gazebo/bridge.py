"""G1 연속 제어기(`humanoid/g1/nav_controller.py`)와 웹 서버 사이 다리.

- 휴머노이드 파티션(`site.json`의 `gz_partition`)에 **따로** 노드를 연다. FR3 파티션과 섞이지 않는다.
- 구독: 제어기 상태 `/g1/status`, 관절 `/g1/joint_state`, Gazebo `pose/info`(골반 world pose).
  모두 받은 벽시계 시각과 Gazebo 시뮬레이션 시각을 **따로** 적는다.
- 발행: `/g1/command`(제어기가 받는 JSON). 좌표를 만들어 보내지 않는다 — 선언된 지점 이름과
  제어기가 관측으로 저장한 출발점 id만 보낸다.
- 건강 상태: 제어기 상태가 최근에 왔는가(벽시계), 시뮬레이션 시각이 실제로 나아가는가.
  세계는 제어기가 스텝을 미는 방식이라 Gazebo 자체는 '일시정지' 표시다 — 판단은 시각 진행으로 한다.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SITE_PATH = ROOT / "humanoid/g1/config/site.json"
MODEL = "g1_12dof"
LEG_JOINTS = ("left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
              "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
              "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
              "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint")
#: 제어기 상태가 이보다 오래되면(벽시계) 꺼졌거나 멈춘 것으로 본다.
STATUS_STALE_SEC = 1.0
#: 관측(관절·pose)이 이보다 오래되면 화면을 멈춘다(벽시계).
VIEW_STALE_SEC = 0.6


def load_site(path: Path = SITE_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def yaw_of(qx, qy, qz, qw) -> float:
    return math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz))


class G1Bridge:
    def __init__(self, site: dict | None = None, *, now=time.time):
        self.site = site or load_site()
        self.world = self.site["world"]
        self.partition = self.site.get("gz_partition", "forstick2_humanoid_nav")
        self.now = now
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.status: dict | None = None
        self.status_wall: float | None = None
        self.status_history: deque = deque(maxlen=50)      # (wall, sim)
        self.joints: dict[str, float] | None = None
        self.joint_sim: float | None = None
        self.joint_wall: float | None = None
        self.joint_seq = 0
        self.base: tuple | None = None                     # x, y, z, qx, qy, qz, qw
        self.base_sim: float | None = None
        self.base_wall: float | None = None
        self.base_seq = 0
        self.pose_log: deque = deque(maxlen=3000)          # (sim, x, y, yaw, z, upright)
        self.time_resets: list[dict] = []                   # 세계 시각이 되돌아간 기록
        self.node = None
        self._pub = None
        self.started = False
        self.error: str | None = None

    # ── 연결 ────────────────────────────────────────────────────────────
    def start(self) -> None:
        try:
            from gz.msgs.model_pb2 import Model
            from gz.msgs.pose_v_pb2 import Pose_V
            from gz.msgs.stringmsg_pb2 import StringMsg
            from gz.transport import Node, NodeOptions, SubscribeOptions
            opts = NodeOptions()
            opts.partition = self.partition
            self.node = Node(opts)
            self._StringMsg = StringMsg
            self._pub = self.node.advertise("/g1/command", StringMsg)
            self.node.subscribe(StringMsg, "/g1/status", self._on_status)
            sub = SubscribeOptions()
            sub.msgs_per_sec = 50
            self.node.subscribe(Model, "/g1/joint_state", self._on_joints, sub)
            self.node.subscribe(Pose_V, f"/world/{self.world}/pose/info", self._on_pose, sub)
            self.started = True
        except Exception as exc:  # noqa: BLE001 — 이유를 화면에 남긴다
            self.error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        if self.node is None:
            return
        for topic in ("/g1/status", "/g1/joint_state", f"/world/{self.world}/pose/info"):
            try:
                self.node.unsubscribe(topic)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _stamp(msg) -> float:
        return msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9

    def _on_status(self, msg) -> None:
        try:
            status = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        wall = self.now()
        with self.cond:
            sim = status.get("sim_time_s")
            if self.status_history and sim is not None and self.status_history[-1][1] is not None \
                    and sim < self.status_history[-1][1] - 1e-6:
                self.status_history.clear()               # 세계 초기화 — 진행률 계산을 새로 한다
            self.status, self.status_wall = status, wall
            self.status_history.append((wall, sim))
            self.cond.notify_all()

    def _on_joints(self, msg) -> None:
        values = {j.name: j.axis1.position for j in msg.joint if j.name in LEG_JOINTS}
        if len(values) != len(LEG_JOINTS):
            return
        with self.cond:
            self.joints, self.joint_sim, self.joint_wall = values, self._stamp(msg), self.now()
            self.joint_seq += 1

    def _on_pose(self, msg) -> None:
        for p in msg.pose:
            if p.name == MODEL:
                o = p.orientation
                sim = self._stamp(msg)
                with self.cond:
                    self.base = (p.position.x, p.position.y, p.position.z, o.x, o.y, o.z, o.w)
                    self.base_sim, self.base_wall = sim, self.now()
                    self.base_seq += 1
                    if self.pose_log and sim < self.pose_log[-1][0] - 1e-6:
                        # 세계 시각이 되돌아갔다(제어기 시작 때의 세계 초기화). 이전 기록과 섞지 않는다.
                        self.pose_log.clear()
                        self.time_resets.append({"wall": self.now(), "sim": sim})
                    if not self.pose_log or sim > self.pose_log[-1][0]:
                        self.pose_log.append((sim, p.position.x, p.position.y,
                                              yaw_of(o.x, o.y, o.z, o.w), p.position.z,
                                              1 - 2 * (o.x * o.x + o.y * o.y)))
                    self.cond.notify_all()
                return

    # ── 명령 ────────────────────────────────────────────────────────────
    def send(self, kind: str, **fields) -> dict:
        if self._pub is None:
            raise RuntimeError(f"휴머노이드 연결이 없다: {self.error or '시작 안 됨'}")
        cmd = {"type": kind, "id": uuid.uuid4().hex[:10], "sent_wall": self.now(), **fields}
        msg = self._StringMsg()
        msg.data = json.dumps(cmd)
        self._pub.publish(msg)
        return cmd

    def reaction(self, cmd_id: str, timeout: float = 3.0) -> dict | None:
        """제어기가 그 명령을 적용한 기록(상태의 reactions_last)."""
        deadline = time.time() + timeout
        with self.cond:
            while True:
                status = self.status or {}
                for row in status.get("reactions_last") or []:
                    if row.get("id") == cmd_id:
                        return row
                left = deadline - time.time()
                if left <= 0:
                    return None
                self.cond.wait(left)

    # ── 상태 ────────────────────────────────────────────────────────────
    def health(self) -> dict:
        """실행해도 되는 상태인지(제어기·시각 진행·넘어짐). 정상처럼 꾸미지 않는다."""
        wall = self.now()
        with self.lock:
            status = self.status
            status_wall = self.status_wall
            history = list(self.status_history)
            base_age = None if self.base_wall is None else wall - self.base_wall
        out: dict[str, Any] = {"bridge": self.started, "bridge_error": self.error,
                               "partition": self.partition, "world": self.world}
        if status is None or status_wall is None:
            out.update(ready=False, controller="absent",
                       reason="G1 제어기 상태가 오지 않는다 — 제어기·Gazebo가 꺼져 있다")
            return out
        age = wall - status_wall
        recent = [(w, s) for w, s in history if wall - w <= 2.0 and s is not None]
        advancing = len(recent) >= 2 and recent[-1][1] > recent[0][1]
        rate = None
        if len(recent) >= 2 and recent[-1][0] > recent[0][0]:
            rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0])
        out.update({
            "controller": "alive" if age <= STATUS_STALE_SEC else "stale",
            "status_age_wall_s": round(age, 3),
            "sim_time_s": status.get("sim_time_s"),
            "sim_advancing": advancing and age <= STATUS_STALE_SEC,
            "sim_rate_measured": None if rate is None else round(rate, 3),
            "rtf_measured": status.get("rtf_measured"),
            "mode": status.get("mode"), "balance": status.get("balance"),
            "policy_active": status.get("policy_active"), "hold": status.get("hold"),
            "pose": status.get("pose"), "nav": status.get("nav"),
            "base_obs_age_wall_s": None if base_age is None else round(base_age, 3),
            "timing_ms_last500": status.get("timing_ms_last500"),
            "memory_resets": status.get("memory_resets"),
            "world_time_resets_seen": len(self.time_resets),
        })
        if age > STATUS_STALE_SEC:
            out.update(ready=False, reason=f"제어기 상태가 {age:.1f} s(벽시계) 동안 오지 않았다 — 꺼졌거나 멈췄다")
        elif not advancing:
            out.update(ready=False, reason="시뮬레이션 시각이 나아가지 않는다(Gazebo가 진행되지 않음)")
        elif status.get("mode") == "fallen":
            out.update(ready=False, reason="G1이 넘어진 상태다 — 세계 초기화가 필요하다(웹에서 하지 않음)")
        elif base_age is None or base_age > VIEW_STALE_SEC:
            out.update(ready=False, reason="Gazebo 골반 pose 관측이 오지 않는다")
        else:
            out.update(ready=True, reason=None)
        return out

    def view_state(self) -> dict:
        wall = self.now()
        with self.lock:
            joints, jsim, jwall, jseq = self.joints, self.joint_sim, self.joint_wall, self.joint_seq
            base, bsim, bwall, bseq = self.base, self.base_sim, self.base_wall, self.base_seq
            status = self.status
            swall = self.status_wall
        joint_age = None if jwall is None else wall - jwall
        base_age = None if bwall is None else wall - bwall
        stale = joint_age is None or base_age is None or joint_age > VIEW_STALE_SEC \
            or base_age > VIEW_STALE_SEC
        return {
            "server_time": wall, "stale_after_sec": VIEW_STALE_SEC, "stale": stale,
            "joints": joints, "joint_sim_time": jsim, "joint_time": jwall, "joint_seq": jseq,
            "base_pose": base, "base_sim_time": bsim, "base_time": bwall, "base_seq": bseq,
            "joint_age_wall_s": None if joint_age is None else round(joint_age, 3),
            "base_age_wall_s": None if base_age is None else round(base_age, 3),
            "controller": None if status is None else {
                "mode": status.get("mode"), "balance": status.get("balance"),
                "sim_time_s": status.get("sim_time_s"), "rtf_measured": status.get("rtf_measured"),
                "nav": status.get("nav"), "cmd": status.get("cmd"),
                "status_age_wall_s": None if swall is None else round(wall - swall, 3)},
        }

    def poses_since(self, sim_t0: float) -> list[tuple]:
        with self.lock:
            return [r for r in self.pose_log if r[0] >= sim_t0]

    def latest_pose(self) -> tuple | None:
        with self.lock:
            return self.pose_log[-1] if self.pose_log else None
