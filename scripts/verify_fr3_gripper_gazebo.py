#!/usr/bin/env python3
"""FR3-WMS + 2F-85 조립 Gazebo 검증 (md/개발플랜.md 8-08).

**명령 수락과 관측 결과를 구분한다.** 컨트롤러가 성공을 돌려줘도 관측 개구가
허용치 안에 들어오지 않으면 성공으로 보지 않는다.

확인 항목:
 1. 조립 모델 적재(링크·조인트·컨트롤러·TCP 프레임)
 2. 개구 관측(공식 FK 표 보간, 표 범위 밖은 관측 불가로 처리)
 3. 그리퍼 open — 관측 개구 기준
 4. 그리퍼 close — 관측 개구 기준
 5. 팔 home·move(조립 후 재확인)
 6. 순서 재현: home → move → open → 접근 → close → 후퇴 → open
 7. 이동 중 STOP — **팔과 그리퍼가 모두** 멈추는지 관측으로 확인
 8. STOP 후 재명령
 9. 차단 상태: 개구 관측 불가 / 그리퍼 제어 실패 / 관절 제한 위반

TCP·장착 값은 config/profiles의 장착 Profile에서 읽는다. **여기서 수치를
만들지 않는다.** yaw가 미확보이므로 pick/place는 판정하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import rclpy
from builtin_interfaces.msg import Duration
from control_msgs.action import FollowJointTrajectory, ParallelGripperCommand
from rclpy.action import ActionClient
from rclpy.node import Node
from sensor_msgs.msg import JointState
from trajectory_msgs.msg import JointTrajectoryPoint

from core.aperture_model import ApertureModel

ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
ARM_ACTION = "/arm_trajectory_controller/follow_joint_trajectory"
GRIPPER_ACTION = "/gripper_action_controller/gripper_cmd"
LOG_DIR = Path("/tmp/forstick2_gazebo")
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"

#: 팔 관절 도달 허용치(rad) — 8-04·8-05와 같은 값.
ARM_TOLERANCE_RAD = 0.05
#: 개구 도달 허용치(m). 보간 오차(15 µm)와 컨트롤러 목표 허용치(0.02 rad ≈
#: 2 mm)를 함께 흡수한다. 값의 근거를 기록에 남긴다.
APERTURE_TOLERANCE_M = 0.004
#: 빈 그리퍼를 닫을 때 쓰는 **목표 개구(m)**.
#:
#: 실측 근거: 제한 근처(0.75 rad, 개구 5.9 mm)로 닫으면 자세에 따라 관절이
#: 제한(0.8)으로 튀어 잠긴다. 접근 자세(TCP z=0.065 m)에서 재현했고, 그 뒤에는
#: 열기 명령도 듣지 않는다(gz_ros_control이 명령을 줄여도 관절이 0.8에서
#: 움직이지 않는다). **시뮬레이션 모델의 한계**이며 제품 특성이 아니다.
#: 그래서 빈 그리퍼 검증은 중간 개구로 닫고, 물체를 잡을 때는 **물체 폭에서
#: 목표 개구를 만든다**(아래 OBJECT_WIDTH_M).
EMPTY_CLOSE_APERTURE_M = 0.030
#: 시험 물체 폭(m). config/gazebo/test_object.json의 치수에서 온다.
OBJECT_WIDTH_M = 0.05
#: (참고) 관절 제한에 닿지 않게 남기는 여유(rad).
#:
#: 실측 근거: 공식 닫힘 위치 0.7929를 명령하면 시뮬레이션 관절이 0.8(URDF 제한)에
#: 올라앉고, 그 상태에서 **다시 열리지 않는다**(gz_ros_control 로그:
#: "Command ... out of limits ... actual 0.800000, command 0.000000,
#: limited 0.475" — 명령은 줄어드는데 실제 관절은 0.8에서 움직이지 않는다).
#: mimic 조인트 4개와 관절 제한 구속이 함께 걸려 잠기는 **시뮬레이션 모델의
#: 한계**다. 제품의 특성이 아니다. 그래서 검증은 제한에서 떨어진 값으로 닫는다.
#: 물체를 잡을 때는 물체 접촉이 먼저 멈추므로 이 여유가 필요 없다.
GRIPPER_CLOSE_MARGIN_RAD = 0.05
#: 관측 관절값이 표 끝을 넘어설 수 있다(부동소수 잡음·정지 오차). 컨트롤러의
#: goal_tolerance와 같은 값을 경계 허용치로 쓴다 — 명령을 자르는 것이 아니라
#: 관측을 해석하는 값이며, 잘랐다는 사실이 기록에 남는다.
APERTURE_EDGE_TOLERANCE_RAD = 0.02
#: 정지 판정: 관측 속도가 이 값 아래로 연속 표본 수만큼 유지돼야 한다.
STOP_VELOCITY_RAD_S = 0.01
STOP_SAMPLES = 10

HOME = dict.fromkeys(ARM_JOINTS, 0.0)
#: 이동 자세. 관절 제한 안이고, 조립 후 TCP가 바닥 위에 있다(FK z=+0.132 m).
MOVE_POSE = dict(zip(ARM_JOINTS, [0.6, -0.5, 0.8, -0.4, 0.6, 0.3]))
#: 접근 자세. **조립 후 FK로 확인한 값**이다 — TCP z=+0.2005 m, 손가락끝
#: z=+0.1821 m로 닫는 동안에도 패드가 바닥에 닿지 않는다.
#:
#: 실측 근거: TCP z=+0.065 m인 낮은 자세에서 닫으면 패드가 바닥면에 닿고
#: 관절이 제한(0.8)으로 튀어 잠긴다(개구 30 mm 목표에서도 재현). 그리퍼를
#: 붙이면 **닫는 동작에도 바닥 여유가 필요하다** — 팔만 있을 때는 없던 조건이다.
APPROACH_POSE = dict(zip(ARM_JOINTS, [0.6, -0.55, 0.75, -0.4, 0.6, 0.3]))
#: 바닥을 파고드는 자세. 차단 상태 검사에 쓴다(TCP z=-0.063 m).
FLOOR_COLLISION_POSE = dict(zip(ARM_JOINTS, [0.6, -0.35, 0.95, -0.4, 0.6, 0.3]))


class GripperVerifier(Node):
    def __init__(self, aperture: ApertureModel) -> None:
        super().__init__("forstick2_fr3_gripper_verifier")
        self.aperture = aperture
        self.states: list[dict] = []
        self.create_subscription(JointState, "/joint_states", self._on_state, 20)
        self.arm = ActionClient(self, FollowJointTrajectory, ARM_ACTION)
        self.gripper = ActionClient(self, ParallelGripperCommand, GRIPPER_ACTION)

    def _on_state(self, message: JointState) -> None:
        index = {name: i for i, name in enumerate(message.name)}
        if not all(joint in index for joint in ARM_JOINTS):
            return
        gripper_value = (
            message.position[index[GRIPPER_JOINT]] if GRIPPER_JOINT in index else None
        )
        reading = (
            None if gripper_value is None
            else self.aperture.observe(
                gripper_value, edge_tolerance_rad=APERTURE_EDGE_TOLERANCE_RAD
            )
        )
        velocities = {
            name: (message.velocity[index[name]]
                   if index[name] < len(message.velocity) else 0.0)
            for name in [*ARM_JOINTS, *( [GRIPPER_JOINT] if GRIPPER_JOINT in index else [])]
        }
        self.states.append({
            "wall": time.time(),
            "arm": {name: message.position[index[name]] for name in ARM_JOINTS},
            "gripper_joint_rad": gripper_value,
            "aperture_m": None if reading is None else reading["aperture_m"],
            "aperture_reading": reading,
            "velocity": velocities,
        })

    def spin(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            rclpy.spin_once(self, timeout_sec=0.05)

    def latest(self) -> dict | None:
        return self.states[-1] if self.states else None

    # ── 팔 ──────────────────────────────────────────────────────────────
    def move_arm(self, target: dict, seconds: float = 4.0,
                 *, cancel_after: float | None = None) -> dict:
        if not self.arm.wait_for_server(timeout_sec=20.0):
            return {"accepted": False, "detail": "팔 액션 서버 없음"}
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(ARM_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [float(target[name]) for name in ARM_JOINTS]
        point.time_from_start = Duration(sec=int(seconds),
                                         nanosec=int((seconds % 1) * 1e9))
        goal.trajectory.points = [point]
        send = self.arm.send_goal_async(goal)
        while not send.done():
            rclpy.spin_once(self, timeout_sec=0.05)
        handle = send.result()
        if not handle.accepted:
            return {"accepted": False, "detail": "팔 goal 거부"}
        result_future = handle.get_result_async()
        cancel_at = None if cancel_after is None else time.monotonic() + cancel_after
        cancel_ack = None
        deadline = time.monotonic() + seconds + 20.0
        while not result_future.done():
            if cancel_at is not None and time.monotonic() >= cancel_at:
                cancel_at = None
                cancel = handle.cancel_goal_async()
                sent = time.monotonic()
                while not cancel.done():
                    rclpy.spin_once(self, timeout_sec=0.02)
                cancel_ack = {
                    "ack_sec": round(time.monotonic() - sent, 4),
                    "goals_canceling": len(cancel.result().goals_canceling),
                }
            if time.monotonic() > deadline:
                return {"accepted": True, "detail": "결과 대기 초과",
                        "cancel_ack": cancel_ack}
            rclpy.spin_once(self, timeout_sec=0.05)
        result = result_future.result()
        return {
            "accepted": True,
            "status": int(result.status),
            "error_code": int(result.result.error_code),
            "cancel_ack": cancel_ack,
        }

    # ── 그리퍼 ──────────────────────────────────────────────────────────
    def command_gripper(self, joint_rad: float, *, wait: float = 18.0,
                        cancel_after: float | None = None) -> dict:
        if not self.gripper.wait_for_server(timeout_sec=20.0):
            return {"accepted": False, "detail": "그리퍼 액션 서버 없음"}
        goal = ParallelGripperCommand.Goal()
        goal.command.name = [GRIPPER_JOINT]
        goal.command.position = [float(joint_rad)]
        # velocity·effort는 비워 둔다. **effort를 파지력으로 쓰지 않는다.**
        send = self.gripper.send_goal_async(goal)
        while not send.done():
            rclpy.spin_once(self, timeout_sec=0.05)
        handle = send.result()
        if not handle.accepted:
            return {"accepted": False, "detail": "그리퍼 goal 거부"}
        result_future = handle.get_result_async()
        cancel_at = None if cancel_after is None else time.monotonic() + cancel_after
        cancel_ack = None
        deadline = time.monotonic() + wait
        while not result_future.done():
            if cancel_at is not None and time.monotonic() >= cancel_at:
                cancel_at = None
                cancel = handle.cancel_goal_async()
                while not cancel.done():
                    rclpy.spin_once(self, timeout_sec=0.02)
                cancel_ack = {"goals_canceling": len(cancel.result().goals_canceling)}
            if time.monotonic() > deadline:
                return {"accepted": True, "detail": "결과 대기 초과(정지·스톨 가능)",
                        "cancel_ack": cancel_ack}
            rclpy.spin_once(self, timeout_sec=0.05)
        result = result_future.result()
        state = result.result.state
        return {
            "accepted": True,
            "status": int(result.status),
            "stalled": bool(result.result.stalled),
            "reached_goal": bool(result.result.reached_goal),
            "reported_position_rad": (list(state.position)[0]
                                      if state.position else None),
            "cancel_ack": cancel_ack,
        }

    # ── 관측 ────────────────────────────────────────────────────────────
    def observe_aperture(self, target_joint: float) -> dict:
        sample = self.latest()
        if sample is None:
            return {"observed": False, "detail": "joint_states 표본이 없다"}
        expected = self.aperture.aperture_for(target_joint)
        observed = sample["aperture_m"]
        reading = sample.get("aperture_reading") or {}
        if observed is None or expected is None:
            return {
                "observed": False,
                "gripper_joint_rad": sample["gripper_joint_rad"],
                "edge_offset_rad": reading.get("edge_offset_rad"),
                "detail": "개구 표 범위를 벗어났다 — 관측값을 만들지 않는다",
                "reason_code": "exec.unverifiable",
            }
        return {
            "observed": True,
            "gripper_joint_rad": round(sample["gripper_joint_rad"], 6),
            "at_table_edge": reading.get("at_table_edge"),
            "edge_offset_rad": reading.get("edge_offset_rad"),
            "target_joint_rad": round(target_joint, 6),
            "expected_aperture_m": round(expected, 6),
            "observed_aperture_m": round(observed, 6),
            "error_m": round(abs(observed - expected), 6),
            "tolerance_m": APERTURE_TOLERANCE_M,
            "within_tolerance": abs(observed - expected) <= APERTURE_TOLERANCE_M,
            "observation_age_sec": round(time.time() - sample["wall"], 4),
        }

    def arm_error(self, target: dict) -> dict:
        sample = self.latest()
        if sample is None:
            return {"observed": False}
        errors = {name: abs(sample["arm"][name] - target[name]) for name in target}
        return {
            "observed": True,
            "max_error_rad": round(max(errors.values()), 5),
            "tolerance_rad": ARM_TOLERANCE_RAD,
            "reached": max(errors.values()) <= ARM_TOLERANCE_RAD,
        }

    def stop_confirmed(self, samples: int = STOP_SAMPLES) -> dict:
        """팔과 그리퍼가 모두 멈췄는지 **관측 속도**로 확인한다."""
        self.states.clear()
        self.spin(2.5)
        recent = self.states[-samples:]
        if len(recent) < samples:
            return {"stopped": False, "samples": len(recent),
                    "detail": "표본 부족 — 정지를 확인할 수 없다"}
        arm_peak = max(
            max(abs(sample["velocity"][name]) for name in ARM_JOINTS)
            for sample in recent
        )
        gripper_values = [
            abs(sample["velocity"].get(GRIPPER_JOINT, 0.0)) for sample in recent
        ]
        gripper_peak = max(gripper_values) if gripper_values else None
        return {
            "stopped": arm_peak < STOP_VELOCITY_RAD_S
            and (gripper_peak is None or gripper_peak < STOP_VELOCITY_RAD_S),
            "samples": len(recent),
            "arm_peak_rad_s": round(arm_peak, 6),
            "gripper_peak_rad_s": None if gripper_peak is None else round(gripper_peak, 6),
            "tolerance_rad_s": STOP_VELOCITY_RAD_S,
        }


def main() -> int:
    urdf = LOG_DIR / "urdf/fr3wms_with_2f85.urdf"
    if not urdf.is_file():
        print("조립 URDF가 없다. run_gazebo_fr3_gripper.sh를 먼저 실행한다.",
              file=sys.stderr)
        return 2
    profile = json.loads(MOUNTING.read_text(encoding="utf-8"))
    aperture = ApertureModel.from_rows(
        profile["transform_derivation"]["aperture_table"],
        source=f"{profile['mounting_profile_id']} {profile['mounting_profile_version']}",
    )
    official = profile["gripper_official"]
    official_closed_rad = official["closed_position_rad"]
    joint_limit_rad = official["command_range_rad"][1]
    open_rad = official["command_range_rad"][0]
    # 닫힘 목표를 **개구에서** 만든다(관절 제한에서 역산하지 않는다).
    closed_rad = aperture.joint_for(EMPTY_CLOSE_APERTURE_M)
    object_close_rad = aperture.joint_for(OBJECT_WIDTH_M)
    if closed_rad is None or object_close_rad is None:
        print("개구 표에서 닫힘 목표를 만들 수 없다", file=sys.stderr)
        return 3

    rclpy.init()
    verifier = GripperVerifier(aperture)
    results: dict[str, dict] = {}
    try:
        verifier.spin(4.0)

        # 1) 조립 모델 적재
        text = urdf.read_text(encoding="utf-8")
        sample = verifier.latest()
        results["01_assembly_loaded"] = {
            "link_count": text.count("<link name="),
            "joint_count": text.count("<joint name="),
            "mimic_count": text.count("<mimic"),
            "has_tcp_frame": "robotiq_85_tcp" in text,
            "has_adapter_link": "ur_to_robotiq_link" in text,
            "joint_states_received": len(verifier.states),
            "gripper_joint_in_state": sample is not None
            and sample["gripper_joint_rad"] is not None,
            "initial_gripper_joint_rad": None if sample is None
            else round(sample["gripper_joint_rad"], 6),
            "initial_aperture_m": None if sample is None or sample["aperture_m"] is None
            else round(sample["aperture_m"], 6),
            "passed": sample is not None and sample["gripper_joint_rad"] is not None
            and "robotiq_85_tcp" in text,
        }

        # 2) 개구 관측 모델
        results["02_aperture_model"] = {
            **aperture.to_dict(),
            "official_max_opening_m": 0.085,
            "model_open_aperture_m": aperture.aperture_for(open_rad),
            "model_closed_aperture_m": aperture.aperture_for(closed_rad),
            "tolerance_m": APERTURE_TOLERANCE_M,
            "tolerance_basis": "보간 오차 상한 + 컨트롤러 goal_tolerance(0.02 rad)",
            "passed": abs((aperture.aperture_for(open_rad) or 0) - 0.085) <= 0.001,
        }

        # 3) 그리퍼 open
        open_command = verifier.command_gripper(open_rad)
        verifier.spin(2.0)
        open_observed = verifier.observe_aperture(open_rad)
        results["03_gripper_open"] = {
            "target_joint_rad": open_rad,
            "command": open_command,
            "observation": open_observed,
            "passed": open_command.get("accepted", False)
            and open_observed.get("within_tolerance", False),
        }

        # 4) 그리퍼 close (제한에서 여유를 둔 목표)
        close_command = verifier.command_gripper(closed_rad)
        verifier.spin(2.0)
        close_observed = verifier.observe_aperture(closed_rad)
        results["04_gripper_close"] = {
            "target_joint_rad": closed_rad,
            "official_closed_position_rad": official_closed_rad,
            "joint_limit_rad": joint_limit_rad,
            "target_aperture_m": EMPTY_CLOSE_APERTURE_M,
            "target_basis": "빈 그리퍼는 중간 개구로 닫는다. 제한 근처로 닫으면"
                            " 자세에 따라 관절이 제한으로 튀어 잠긴다(09 항목)",
            "aperture_at_official_closed_m": aperture.aperture_for(official_closed_rad),
            "aperture_at_sim_closed_m": aperture.aperture_for(closed_rad),
            "command": close_command,
            "observation": close_observed,
            "passed": close_command.get("accepted", False)
            and close_observed.get("within_tolerance", False),
        }

        # 4b) 물체 폭에서 만든 닫힘 목표 — pick 진입에 쓰는 값이다
        object_command = verifier.command_gripper(object_close_rad)
        verifier.spin(2.0)
        object_observed = verifier.observe_aperture(object_close_rad)
        results["04b_close_for_object_width"] = {
            "object_width_m": OBJECT_WIDTH_M,
            "object_source": "config/gazebo/test_object.json (시험 물체 치수)",
            "target_joint_rad": round(object_close_rad, 6),
            "expected_aperture_m": aperture.aperture_for(object_close_rad),
            "command": object_command,
            "observation": object_observed,
            "passed": object_observed.get("within_tolerance", False),
            "note": "물체를 잡지 않는다 — 개구를 물체 폭에 맞출 수 있는지만 본다",
        }

        # 5) 팔 home·move (조립 후 재확인)
        home_command = verifier.move_arm(HOME)
        verifier.spin(1.5)
        home_observed = verifier.arm_error(HOME)
        move_command = verifier.move_arm(MOVE_POSE)
        verifier.spin(1.5)
        move_observed = verifier.arm_error(MOVE_POSE)
        results["05_arm_home_move"] = {
            "home": {"command": home_command, "observation": home_observed},
            "move": {"command": move_command, "observation": move_observed},
            "passed": home_observed.get("reached", False)
            and move_observed.get("reached", False),
        }

        # 6) 순서 재현: home → move → open → 접근 → close → 후퇴 → open
        sequence = []
        def gripper_path(start_index: int) -> dict:
            """단계 동안의 그리퍼 관절 경로 요약. 원인 추적에 쓴다."""
            values = [
                sample["gripper_joint_rad"] for sample in verifier.states[start_index:]
                if sample["gripper_joint_rad"] is not None
            ]
            if not values:
                return {"samples": 0}
            return {
                "samples": len(values),
                "first_rad": round(values[0], 6),
                "min_rad": round(min(values), 6),
                "max_rad": round(max(values), 6),
                "last_rad": round(values[-1], 6),
            }

        for label, action in (
            ("home", lambda: verifier.move_arm(HOME)),
            ("move", lambda: verifier.move_arm(MOVE_POSE)),
            ("gripper_open", lambda: verifier.command_gripper(open_rad)),
            ("approach", lambda: verifier.move_arm(APPROACH_POSE, seconds=5.0)),
            ("gripper_close", lambda: verifier.command_gripper(closed_rad)),
            ("retreat", lambda: verifier.move_arm(MOVE_POSE, seconds=5.0)),
            ("gripper_open_again", lambda: verifier.command_gripper(open_rad)),
        ):
            step_start = len(verifier.states)
            command = action()
            # 그리퍼는 램프가 느리다(0→0.53 rad에 약 10초). 명령이 끝난 뒤 관측한다.
            verifier.spin(3.0)
            path = gripper_path(step_start)
            arm_now = verifier.latest()
            if label.startswith("gripper"):
                target = open_rad if "open" in label else closed_rad
                observation = verifier.observe_aperture(target)
                ok = observation.get("within_tolerance", False)
            else:
                target_pose = {"home": HOME, "move": MOVE_POSE,
                               "approach": APPROACH_POSE, "retreat": MOVE_POSE}[label]
                observation = verifier.arm_error(target_pose)
                ok = observation.get("reached", False)
            sequence.append({
                "step": label,
                "command_accepted": command.get("accepted", False),
                "observation": observation,
                "observed_ok": ok,
                "gripper_joint_path": path,
                "arm_at_step_end": (None if arm_now is None else
                                    {k: round(v, 4) for k, v in arm_now["arm"].items()}),
                "command_result": {k: v for k, v in command.items()
                                   if k in ("status", "stalled", "reached_goal",
                                            "detail", "error_code")},
            })
        results["06_sequence"] = {
            "steps": sequence,
            "passed": all(step["observed_ok"] for step in sequence),
            "note": "물체를 집지 않는다 — 장착 yaw 미확보로 pick/place는 진입하지 않는다",
        }

        # 7) 이동 중 STOP — 팔과 그리퍼를 함께 멈춘다
        verifier.move_arm(HOME)
        verifier.spin(1.0)
        verifier.command_gripper(open_rad)
        verifier.spin(1.0)
        arm_stop = verifier.move_arm(MOVE_POSE, seconds=8.0, cancel_after=1.5)
        gripper_stop = verifier.command_gripper(closed_rad, wait=2.0, cancel_after=0.3)
        stop_check = verifier.stop_confirmed()
        stop_sample = verifier.latest()
        arm_reached = verifier.arm_error(MOVE_POSE)
        results["07_stop_arm_and_gripper"] = {
            "arm_cancel": arm_stop.get("cancel_ack"),
            "gripper_cancel": gripper_stop.get("cancel_ack"),
            "gripper_result": {k: v for k, v in gripper_stop.items() if k != "cancel_ack"},
            "stop_observation": stop_check,
            "arm_target_not_reached": not arm_reached.get("reached", True),
            "gripper_joint_at_stop_rad": None if stop_sample is None
            else round(stop_sample["gripper_joint_rad"], 6),
            "aperture_at_stop_m": None if stop_sample is None
            or stop_sample["aperture_m"] is None
            else round(stop_sample["aperture_m"], 6),
            "passed": stop_check.get("stopped", False)
            and not arm_reached.get("reached", True),
        }

        # 8) STOP 후 재명령
        replan = verifier.move_arm(HOME, seconds=4.0)
        verifier.spin(1.5)
        replan_observed = verifier.arm_error(HOME)
        regrip = verifier.command_gripper(open_rad)
        verifier.spin(1.5)
        regrip_observed = verifier.observe_aperture(open_rad)
        results["08_recommand_after_stop"] = {
            "arm": {"command": replan, "observation": replan_observed},
            "gripper": {"command": regrip, "observation": regrip_observed},
            "passed": replan_observed.get("reached", False)
            and regrip_observed.get("within_tolerance", False),
        }

        # 9) 차단 상태
        out_of_range = (aperture.joint_range_rad[1] + 0.2)
        blocked_aperture = {
            "requested_joint_rad": out_of_range,
            "aperture_for": aperture.aperture_for(out_of_range),
            "reason_code": "exec.unverifiable",
            "blocked": aperture.aperture_for(out_of_range) is None,
        }
        limit_command = verifier.command_gripper(out_of_range, wait=3.0)
        verifier.spin(1.5)
        limit_observed = verifier.observe_aperture(min(out_of_range,
                                                       aperture.joint_range_rad[1]))
        # 바닥 침범 자세: 그리퍼가 바닥을 파고들어 도달하지 못한다.
        verifier.move_arm(MOVE_POSE)
        verifier.spin(1.5)
        floor_command = verifier.move_arm(FLOOR_COLLISION_POSE, seconds=6.0)
        verifier.spin(2.5)
        floor_observed = verifier.arm_error(FLOOR_COLLISION_POSE)
        results["09_blocked_states"] = {
            "gripper_joint_limit_lock": {
                "finding": "닫힘 명령이 목표를 **지나쳐** 관절 제한(0.8 rad)까지"
                           " 가고, 거기서 잠겨 다시 열리지 않는다. 그 뒤"
                           " 0.79·0.7·0.5·0.0 명령이 모두 듣지 않는다(재시작 필요)",
                "evidence": [
                    "MOVE_POSE에서 0.0 → 0.537991 rad 명령: 관절이 일정 속도"
                    " 약 0.357 rad/s로 램프하며 목표를 지나 0.8에서 멈춘다"
                    " (관측 표본 간격 0.07 s, 증분 0.025 rad)",
                    "URDF 속도 제한 0.5 rad/s · position_proportional_gain 0.1"
                    " (gz_sim_gripper.log)",
                    "잠긴 뒤 open(0.0) 명령: reached_goal 결과가 오지 않고"
                    " 관절이 0.8에서 속도 0으로 고정된다",
                    "gz_sim_gripper.log: 'Command of at least one joint is out of"
                    " limits ... actual 0.800000, command 0.000000, limited 0.475'",
                ],
                "ruled_out": [
                    "바닥 접촉: reports/gripper/floor_clearance.json — MOVE_POSE에서"
                    " 그리퍼 충돌 메시 최저점이 +0.0705 m다(바닥에 닿지 않는다)."
                    " 이전 기록의 'TCP z=-0.063 m로 바닥을 파고든다'는 직렬 체인을"
                    " 가정한 FK 버그에서 나온 값이며, 트리 순회로 고친 뒤"
                    " Gazebo 실측과 0.000000 m로 일치한다",
                    "mimic 오적용: Gazebo 실측 자세가 FK와 일치한다"
                    " (scripts/diagnose_gripper_frames.py)",
                ],
                "remaining_candidates": [
                    "ros2_control 위치 명령 제한기(속도 제한 0.5 rad/s)가 만드는"
                    " 명령 램프가 목표에서 멈추지 않는다",
                    "physics 엔진이 mimic 제약을 만들지 못한다"
                    " (gz_sim_gripper.log: 'the chosen physics engine does not"
                    " support mimic constraints, so no constraint will be created')."
                    " mimic 관절은 state_interface가 없어 /joint_states로"
                    " 관측할 수 없다 — 추종 여부를 확인하지 못했다",
                    "parallel_gripper_action_controller의 stall·goal_tolerance 처리",
                ],
                "cause": None,
                "cause_status": "미확정 — 근거 없이 단정하지 않는다",
                "handling": f"빈 그리퍼는 개구 {EMPTY_CLOSE_APERTURE_M} m로 닫고,"
                            " 물체를 잡을 때는 물체 폭에서 목표 개구를 만든다."
                            " **이 결함이 남아 있는 동안 pick/place를 열지 않는다.**",
                "blocked": True,
            },
            "floor_collision_pose": {
                "pose_rad": {k: round(v, 4) for k, v in FLOOR_COLLISION_POSE.items()},
                # 수정된 트리 순회 FK + 충돌 메시 점군 기준
                # (reports/gripper/floor_clearance.json).
                "tcp_origin_z_from_fk_m": -0.0276,
                "lowest_collision_mesh_z_m": -0.0686,
                "lowest_link": "robotiq_85_right_finger_link",
                "command": floor_command,
                "observation": floor_observed,
                "blocked_by_physics": not floor_observed.get("reached", True),
                "note": "명령은 수락됐지만 관측으로 도달하지 못했다. 그리퍼를"
                        " 붙이면 도달 영역이 바뀐다는 근거다 — MoveIt 충돌 검사로"
                        " 계획 단계에서 막아야 한다",
            },
            "aperture_out_of_table": blocked_aperture,
            "gripper_limit_violation": {
                "command": limit_command,
                "observation": limit_observed,
                "joint_stayed_within_limit": (
                    limit_observed.get("gripper_joint_rad") is not None
                    and limit_observed["gripper_joint_rad"] <= aperture.joint_range_rad[1] + 1e-6
                ),
                "note": "URDF 제한을 넘는 목표는 물리적으로 따라갈 수 없다."
                        " 컨트롤러 결과를 성공으로 쓰지 않는다",
            },
            "passed": blocked_aperture["blocked"]
            and not floor_observed.get("reached", True),
        }

        report = {
            "recorded_at": time.time(),
            "environment": {
                "urdf": str(urdf),
                "urdf_sha256": hashlib.sha256(urdf.read_bytes()).hexdigest(),
                "mounting_profile": f"{profile['mounting_profile_id']}"
                                    f" {profile['mounting_profile_version']}",
                "mounting_complete": False,
                "declared_yaw_rad": "FORSTICK2_ASSEMBLY_YAW_RAD (실행 시 선언)",
                "gripper_command_joint": GRIPPER_JOINT,
                "is_simulated": True,
                "arm_only": False,
                "adapter_included": True,
                "note": "Gazebo simulation. 실제 하드웨어·실제 파지력은 검증하지 않았다",
            },
            "checks": results,
            "passed_count": sum(1 for value in results.values() if value.get("passed")),
            "total_count": len(results),
            "pick_place": {
                "enabled": False,
                "reason": "장착 yaw 미확보 + 커플링 실질량 미확보"
                          " (config/profiles 장착 Profile의 missing 참조)",
            },
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    finally:
        verifier.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
