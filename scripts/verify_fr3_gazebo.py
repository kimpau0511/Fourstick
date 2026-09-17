#!/usr/bin/env python3
"""FR3-WMS arm-only Gazebo 검증 (md/개발플랜.md 8-03~8-06).

**명령 전송 성공과 목표 도달을 구분한다.** 컨트롤러가 SUCCEEDED를 돌려줘도,
Gazebo 관측값이 목표 허용치 안에 들어오지 않으면 성공으로 보지 않는다.

확인 항목:
- spawn / robot_state_publisher / joint_states / controller_manager
- joint_state_broadcaster / trajectory controller
- home / move (관측 기준 도달 판정)
- 이동 중 STOP (실제 속도로 정지 확인)
- 특정 실행 취소와 ACK
- 관측 신선도

ROS 환경을 source한 셸에서 실행한다. 결과는 JSON으로 출력한다.
"""

from __future__ import annotations

import json
import math
import sys
import time

import rclpy
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
ACTION = "/arm_trajectory_controller/follow_joint_trajectory"
#: 목표 도달 판정 허용치(rad). 관측값이 이 안에 들어와야 도달로 본다.
POSITION_TOLERANCE = 0.05
#: 정지 판정 허용 속도(rad/s)와 연속 표본 수. 명령이 아니라 **관측 속도**로 본다.
STOP_VELOCITY_TOLERANCE = 0.01
STOP_SAMPLES = 10

HOME = [0.0] * 6
#: 이동 목표. URDF 관절 범위 안에서 눈에 보이게 크게 움직인다.
MOVE_TARGET = [0.8, -0.6, 0.9, -0.5, 0.7, 0.4]


class Verifier(Node):
    def __init__(self) -> None:
        super().__init__("forstick2_fr3_verifier")
        self.states: list[dict] = []
        self.create_subscription(JointState, "/joint_states", self._on_state, 20)
        self.client = ActionClient(self, FollowJointTrajectory, ACTION)

    def _on_state(self, message: JointState) -> None:
        index = {name: i for i, name in enumerate(message.name)}
        if not all(joint in index for joint in JOINTS):
            return
        self.states.append({
            "at": message.header.stamp.sec + message.header.stamp.nanosec * 1e-9,
            "wall": time.time(),
            "position": [message.position[index[j]] for j in JOINTS],
            "velocity": (
                [message.velocity[index[j]] for j in JOINTS]
                if len(message.velocity) >= len(JOINTS) else []
            ),
        })
        if len(self.states) > 4000:
            del self.states[:2000]

    # ── 관측 ────────────────────────────────────────────────────────────
    def spin(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)

    def latest(self) -> dict | None:
        return self.states[-1] if self.states else None

    def wait_for_states(self, seconds: float = 10.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline and not self.states:
            rclpy.spin_once(self, timeout_sec=0.1)
        return bool(self.states)

    def observed_stopped(self) -> tuple[bool, float]:
        """**관측 속도**로 정지를 판정한다. 명령이나 결과 코드로 판정하지 않는다."""
        samples = [s for s in self.states[-STOP_SAMPLES:] if s["velocity"]]
        if len(samples) < STOP_SAMPLES:
            return (False, float("nan"))
        peak = max(max(abs(v) for v in s["velocity"]) for s in samples)
        return (peak < STOP_VELOCITY_TOLERANCE, peak)

    def reached(self, target: list[float]) -> tuple[bool, float]:
        state = self.latest()
        if state is None:
            return (False, float("nan"))
        error = max(abs(a - b) for a, b in zip(state["position"], target))
        return (error <= POSITION_TOLERANCE, error)

    # ── 명령 ────────────────────────────────────────────────────────────
    def send(self, target: list[float], seconds: float):
        point = JointTrajectoryPoint()
        point.positions = list(target)
        point.time_from_start = Duration(
            sec=int(seconds), nanosec=int((seconds % 1) * 1e9)
        )
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(JOINTS)
        goal.trajectory.points = [point]
        if not self.client.wait_for_server(timeout_sec=15.0):
            return None, "action 서버 없음"
        future = self.client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, future, timeout_sec=15.0)
        handle = future.result()
        if handle is None or not handle.accepted:
            return None, "goal 거부"
        return handle, "goal 수락"

    def wait_result(self, handle, seconds: float):
        future = handle.get_result_async()
        rclpy.spin_until_future_complete(self, future, timeout_sec=seconds)
        result = future.result()
        return None if result is None else result.status


def step(verifier: Verifier, label: str, target: list[float], seconds: float) -> dict:
    handle, accepted = verifier.send(target, seconds)
    entry = {"step": label, "target": target, "command_accepted": handle is not None,
             "accept_detail": accepted}
    if handle is None:
        entry.update({"goal_reached": False, "reason_code": "exec.goal_rejected"})
        return entry
    status = verifier.wait_result(handle, seconds + 15.0)
    verifier.spin(1.0)
    reached, error = verifier.reached(target)
    stopped, peak = verifier.observed_stopped()
    state = verifier.latest() or {}
    entry.update({
        "controller_status": status,
        "goal_reached": reached,            # 관측 기준
        "max_joint_error_rad": round(error, 5),
        "observed_stopped": stopped,
        "peak_velocity_rad_s": None if math.isnan(peak) else round(peak, 5),
        "observed_position": [round(v, 5) for v in state.get("position", [])],
        "observation_age_s": round(time.time() - state.get("wall", time.time()), 3),
        # 명령 성공과 도달을 분리해 기록한다.
        "reason_code": None if reached else "exec.goal_not_reached",
    })
    return entry


def stop_during_motion(verifier: Verifier) -> dict:
    """이동 중 정지. 취소 ACK와 **관측 속도 기준** 정지 확인을 함께 본다."""
    handle, accepted = verifier.send(MOVE_TARGET, 8.0)
    if handle is None:
        return {"step": "stop_during_motion", "command_accepted": False,
                "accept_detail": accepted}
    verifier.spin(2.0)                      # 움직이는 중간까지 기다린다
    moving_peak = verifier.observed_stopped()[1]
    before = verifier.latest()["position"]
    cancel_future = handle.cancel_goal_async()
    rclpy.spin_until_future_complete(verifier, cancel_future, timeout_sec=10.0)
    cancel_response = cancel_future.result()
    acked = bool(cancel_response and cancel_response.goals_canceling)
    status = verifier.wait_result(handle, 10.0)
    verifier.spin(1.5)
    stopped, peak = verifier.observed_stopped()
    after = verifier.latest()["position"]
    reached, error = verifier.reached(MOVE_TARGET)
    return {
        "step": "stop_during_motion",
        "command_accepted": True,
        "was_moving_peak_velocity_rad_s": (
            None if math.isnan(moving_peak) else round(moving_peak, 5)
        ),
        "cancel_acked": acked,
        "controller_status": status,
        "observed_stopped": stopped,
        "peak_velocity_rad_s": None if math.isnan(peak) else round(peak, 5),
        "position_before_cancel": [round(v, 5) for v in before],
        "position_after_cancel": [round(v, 5) for v in after],
        "moved_after_cancel_rad": round(
            max(abs(a - b) for a, b in zip(before, after)), 5
        ),
        "goal_reached": reached,
        "max_joint_error_rad": round(error, 5),
        # 정지는 목표 도달이 아니다. 성공으로 기록하지 않는다.
        "reason_code": "exec.stopped" if stopped else "exec.stop_unconfirmed",
    }


def main() -> int:
    rclpy.init()
    verifier = Verifier()
    report: dict = {"started_at": time.time(), "steps": []}
    try:
        report["joint_states_received"] = verifier.wait_for_states(15.0)
        if not report["joint_states_received"]:
            report["reason_code"] = "robot.state_unavailable"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        first = verifier.latest()
        report["first_observation"] = {
            "position": [round(v, 5) for v in first["position"]],
            "has_velocity": bool(first["velocity"]),
        }
        report["steps"].append(step(verifier, "home", HOME, 5.0))
        report["steps"].append(step(verifier, "move", MOVE_TARGET, 6.0))
        report["steps"].append(step(verifier, "home_again", HOME, 6.0))
        report["steps"].append(stop_during_motion(verifier))
        report["steps"].append(step(verifier, "home_after_stop", HOME, 6.0))
    finally:
        verifier.destroy_node()
        rclpy.shutdown()
    report["finished_at"] = time.time()
    report["summary"] = {
        "commands_accepted": sum(
            1 for s in report["steps"] if s.get("command_accepted")
        ),
        "goals_reached": sum(1 for s in report["steps"] if s.get("goal_reached")),
        "stop_confirmed_by_velocity": any(
            s.get("step") == "stop_during_motion" and s.get("observed_stopped")
            for s in report["steps"]
        ),
        "cancel_acked": any(s.get("cancel_acked") for s in report["steps"]),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
