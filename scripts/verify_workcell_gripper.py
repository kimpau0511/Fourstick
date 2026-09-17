#!/usr/bin/env python3
"""작업 셀 그리퍼 제어·STOP 검증 (8-08 우선순위 2).

**명령값과 관측 개구를 따로 판정한다.** 컨트롤러가 성공을 보고해도 관측
개구가 목표와 다르면 실패다. 대기 시간을 늘려 실패를 통과시키지 않는다.

검증 항목:
  01 open            (명령 0.0 rad → 개구 0.084997 m)
  02 close 30 mm     (개구 30 mm에서 역산한 관절값)
  03 close 완전닫힘  (공식 닫힘 위치 0.7929 rad)
  04 목표 초과 없음  (각 단계에서 관절이 목표를 지나치지 않았는지)
  05 arm·gripper 동시 STOP (관측 속도로 확인)
  06 STOP 후 재명령

mimic 관절 5개는 `state_interface`가 없어 `/joint_states`로 **관측할 수 없다.**
무엇을 관측했고 무엇이 관측 불가인지 보고서에 명시한다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.aperture_model import ApertureModel  # noqa: E402

MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
URDF = Path("/tmp/forstick2_workcell/workcell/fr3wms_with_2f85.urdf")
OUT = ROOT / "reports/workcell/gripper_control.json"

ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
ARM_ACTION = "/arm_trajectory_controller/follow_joint_trajectory"
GRIPPER_ACTION = "/gripper_trajectory_controller/follow_joint_trajectory"

APERTURE_TOLERANCE_M = 0.004
APERTURE_EDGE_TOLERANCE_RAD = 0.02
#: 목표 초과 허용치. 컨트롤러 goal_tolerance(0.02 rad)와 같은 근거를 쓴다.
OVERSHOOT_TOLERANCE_RAD = 0.02
STOP_VELOCITY_RAD_S = 0.01
STOP_SAMPLES = 10
ARM_TOLERANCE_RAD = 0.05
DEMO_APERTURE_M = 0.030
MOVE_SEC = 5
STOP_AFTER_SEC = 1.5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    profile = json.loads(MOUNTING.read_text(encoding="utf-8"))
    aperture = ApertureModel.from_rows(
        profile["transform_derivation"]["aperture_table"],
        source=f"{profile['mounting_profile_id']}"
               f" {profile['mounting_profile_version']}")
    poses = json.loads(POSES.read_text(encoding="utf-8"))
    open_rad = profile["gripper_official"]["command_range_rad"][0]
    official_closed_rad = profile["gripper_official"]["closed_position_rad"]
    limit_rad = profile["gripper_official"]["command_range_rad"][1]
    close30_rad = aperture.joint_for(DEMO_APERTURE_M)
    if close30_rad is None:
        print(f"개구 표에서 {DEMO_APERTURE_M} m 목표를 만들 수 없다", file=sys.stderr)
        return 3

    import rclpy
    from builtin_interfaces.msg import Duration
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectoryPoint

    rclpy.init()
    node = Node("forstick2_workcell_gripper_verify")
    samples: list[dict] = []

    def on_state(message: JointState) -> None:
        index = {name: i for i, name in enumerate(message.name)}
        if GRIPPER_JOINT not in index:
            return
        samples.append({
            "t": time.time(),
            "gripper_rad": message.position[index[GRIPPER_JOINT]],
            "gripper_vel": (message.velocity[index[GRIPPER_JOINT]]
                            if index[GRIPPER_JOINT] < len(message.velocity) else 0.0),
            "arm": {name: message.position[index[name]] for name in ARM_JOINTS
                    if name in index},
            "arm_vel": {name: (message.velocity[index[name]]
                               if index[name] < len(message.velocity) else 0.0)
                        for name in ARM_JOINTS if name in index},
        })

    node.create_subscription(JointState, "/joint_states", on_state, 50)
    arm_client = ActionClient(node, FollowJointTrajectory, ARM_ACTION)
    gripper_client = ActionClient(node, FollowJointTrajectory, GRIPPER_ACTION)

    def spin(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.02)

    def send(client, names, values, seconds):
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(names)
        point = JointTrajectoryPoint()
        point.positions = [float(v) for v in values]
        point.velocities = [0.0] * len(values)
        point.time_from_start = Duration(sec=seconds)
        goal.trajectory.points = [point]
        future = client.send_goal_async(goal)
        while not future.done():
            rclpy.spin_once(node, timeout_sec=0.02)
        return future.result()

    def await_result(handle, seconds):
        future = handle.get_result_async()
        deadline = time.monotonic() + seconds + 20
        while not future.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.02)
        return future.result()

    spin(3.0)
    if not (arm_client.wait_for_server(timeout_sec=25.0)
            and gripper_client.wait_for_server(timeout_sec=25.0)):
        print(f"액션 서버가 없다 — {ARM_ACTION} / {GRIPPER_ACTION}", file=sys.stderr)
        node.destroy_node()
        rclpy.shutdown()
        return 2

    checks: dict[str, dict] = {}

    def gripper_step(label: str, target: float, *, note: str = "") -> dict:
        start = len(samples)
        handle = send(gripper_client, [GRIPPER_JOINT], [target], MOVE_SEC)
        result = await_result(handle, MOVE_SEC)
        spin(2.5)
        path = [s["gripper_rad"] for s in samples[start:]]
        observed_joint = samples[-1]["gripper_rad"] if samples else None
        reading = (aperture.observe(observed_joint,
                                    edge_tolerance_rad=APERTURE_EDGE_TOLERANCE_RAD)
                   if observed_joint is not None else None)
        expected = aperture.aperture_for(target)
        observed = None if reading is None else reading["aperture_m"]
        # 목표를 지나쳤는지: 진행 방향에 따라 최대·최소를 본다.
        overshoot = None
        if path:
            first = path[0]
            overshoot = (max(path) - target if target >= first
                         else target - min(path))
        within = (observed is not None and expected is not None
                  and abs(observed - expected) <= APERTURE_TOLERANCE_M)
        no_overshoot = overshoot is not None and overshoot <= OVERSHOOT_TOLERANCE_RAD
        entry = {
            "commanded_joint_rad": round(target, 6),
            "expected_aperture_m": None if expected is None else round(expected, 6),
            "observed_joint_rad": None if observed_joint is None
            else round(observed_joint, 6),
            "observed_aperture_m": None if observed is None else round(observed, 6),
            "aperture_error_m": (None if observed is None or expected is None
                                 else round(abs(observed - expected), 6)),
            "aperture_tolerance_m": APERTURE_TOLERANCE_M,
            "aperture_within_tolerance": within,
            "joint_path": {"samples": len(path),
                           "first_rad": round(path[0], 6) if path else None,
                           "min_rad": round(min(path), 6) if path else None,
                           "max_rad": round(max(path), 6) if path else None,
                           "last_rad": round(path[-1], 6) if path else None},
            "overshoot_rad": None if overshoot is None else round(overshoot, 6),
            "overshoot_tolerance_rad": OVERSHOOT_TOLERANCE_RAD,
            "reached_joint_limit": bool(path) and max(path) >= limit_rad - 1e-6,
            "no_overshoot": no_overshoot,
            "controller_error_code": (None if result is None
                                      else int(result.result.error_code)),
            "controller": GRIPPER_ACTION,
            "passed": bool(within and no_overshoot),
            "note": note or "명령값과 관측 개구를 따로 판정한다."
                            " 컨트롤러 결과를 성공 근거로 쓰지 않는다",
        }
        if not entry["passed"]:
            entry["reason_code"] = "exec.unverifiable"
        checks[label] = entry
        mark = "통과" if entry["passed"] else "실패"
        print(f"  [{mark}] {label}: 명령 {target:.6f} rad ·"
              f" 관측 개구 {entry['observed_aperture_m']} m"
              f" (기대 {entry['expected_aperture_m']})"
              f" · 초과 {entry['overshoot_rad']} rad")
        return entry

    # 시작 자세: 안전 home
    home = poses["safe_home"]["joint_rad"]
    handle = send(arm_client, ARM_JOINTS, [home[a] for a in ARM_JOINTS], MOVE_SEC + 2)
    await_result(handle, MOVE_SEC + 2)
    spin(2.0)

    gripper_step("01_open", open_rad)
    gripper_step("02_close_30mm", close30_rad,
                 note=f"목표 관절값은 개구 {DEMO_APERTURE_M} m에서 역산했다."
                      " 관절 제한에서 만들지 않았다")
    gripper_step("03_close_official", official_closed_rad,
                 note="공식 닫힘 위치. 관절 제한(0.8)보다 안쪽이다")
    gripper_step("04_reopen", open_rad,
                 note="닫은 뒤 다시 열린다 — 제한에 잠기지 않았음을 확인한다")

    # 05 arm·gripper 동시 STOP
    target = poses["poses"]["pallet_1_approach"]["joint_rad"]
    arm_handle = send(arm_client, ARM_JOINTS,
                      [target[a] for a in ARM_JOINTS], MOVE_SEC + 5)
    gripper_handle = send(gripper_client, [GRIPPER_JOINT], [official_closed_rad],
                          MOVE_SEC + 5)
    spin(STOP_AFTER_SEC)
    arm_cancel = arm_handle.cancel_goal_async()
    gripper_cancel = gripper_handle.cancel_goal_async()
    deadline = time.monotonic() + 15
    while not (arm_cancel.done() and gripper_cancel.done()) \
            and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.02)
    samples.clear()
    spin(3.0)
    recent = samples[-STOP_SAMPLES:]
    if len(recent) < STOP_SAMPLES:
        stop_entry = {"passed": False, "samples": len(recent),
                      "detail": "표본 부족 — 정지를 확인할 수 없다",
                      "reason_code": "exec.unverifiable"}
    else:
        arm_peak = max(max(abs(v) for v in s["arm_vel"].values()) for s in recent)
        gripper_peak = max(abs(s["gripper_vel"]) for s in recent)
        arm_error = max(abs(recent[-1]["arm"][a] - target[a]) for a in ARM_JOINTS)
        stop_entry = {
            "arm_cancel_goals_canceling": (
                len(arm_cancel.result().goals_canceling) if arm_cancel.done() else None),
            "gripper_cancel_goals_canceling": (
                len(gripper_cancel.result().goals_canceling)
                if gripper_cancel.done() else None),
            "samples": len(recent),
            "arm_peak_rad_s": round(arm_peak, 6),
            "gripper_peak_rad_s": round(gripper_peak, 6),
            "tolerance_rad_s": STOP_VELOCITY_RAD_S,
            "arm_stopped": arm_peak < STOP_VELOCITY_RAD_S,
            "gripper_stopped": gripper_peak < STOP_VELOCITY_RAD_S,
            "arm_target_not_reached": arm_error > ARM_TOLERANCE_RAD,
            "arm_error_rad": round(arm_error, 6),
            "gripper_joint_at_stop_rad": round(recent[-1]["gripper_rad"], 6),
            "gripper_reached_target": abs(recent[-1]["gripper_rad"]
                                          - official_closed_rad) <= 0.02,
            "criterion": f"연속 {STOP_SAMPLES}표본의 |속도| < {STOP_VELOCITY_RAD_S} rad/s",
            "note": "그리퍼 속도가 남아 있으면 정지 미확인이다."
                    " 관절이 제한에 잠겨 속도가 0인 경우도 정지 근거로 쓰지 않는다",
        }
        locked = recent[-1]["gripper_rad"] >= limit_rad - 1e-6
        stop_entry["gripper_locked_at_limit"] = locked
        stop_entry["passed"] = bool(
            stop_entry["arm_stopped"] and stop_entry["gripper_stopped"]
            and stop_entry["arm_target_not_reached"] and not locked)
        if not stop_entry["passed"]:
            stop_entry["reason_code"] = "exec.unverifiable"
    checks["05_stop_arm_and_gripper"] = stop_entry
    print(f"  [{'통과' if stop_entry['passed'] else '실패'}] 05_stop_arm_and_gripper:"
          f" 팔 {stop_entry.get('arm_peak_rad_s')} · 그리퍼"
          f" {stop_entry.get('gripper_peak_rad_s')} rad/s")

    # 06 STOP 후 재명령
    handle = send(arm_client, ARM_JOINTS, [home[a] for a in ARM_JOINTS], MOVE_SEC + 2)
    await_result(handle, MOVE_SEC + 2)
    spin(2.0)
    arm_error = max(abs(samples[-1]["arm"][a] - home[a]) for a in ARM_JOINTS)
    regrip = gripper_step("06b_reopen_after_stop", open_rad,
                          note="STOP 뒤 그리퍼가 다시 명령을 받는지 확인한다")
    checks["06_recommand_after_stop"] = {
        "arm_error_rad": round(arm_error, 6),
        "arm_tolerance_rad": ARM_TOLERANCE_RAD,
        "arm_reached": arm_error <= ARM_TOLERANCE_RAD,
        "gripper_passed": regrip["passed"],
        "passed": bool(arm_error <= ARM_TOLERANCE_RAD and regrip["passed"]),
    }
    print(f"  [{'통과' if checks['06_recommand_after_stop']['passed'] else '실패'}]"
          f" 06_recommand_after_stop: 팔 오차 {arm_error:.5f} rad")

    node.destroy_node()
    rclpy.shutdown()

    passed = sum(1 for v in checks.values() if v.get("passed"))
    report = {
        "schema": "forstick2.workcell_gripper_control/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "controller": {
            "arm": "joint_trajectory_controller/JointTrajectoryController",
            "gripper": "joint_trajectory_controller/JointTrajectoryController",
            "gripper_change_reason":
                "parallel_gripper_action_controller/GripperActionController는 목표를"
                " 지나 관절 제한 0.8 rad까지 닫히고 거기서 잠긴다. 같은 하드웨어"
                " 인터페이스·같은 mimic·같은 물리에서 컨트롤러만 바꿔 실측했다:"
                " 0.0 -> 0.537991 rad 명령에서 최대 0.800000(초과 +0.262) ->"
                " 0.537991(초과 +0.000)",
        },
        "observed_joints": [GRIPPER_JOINT, *ARM_JOINTS],
        "unobservable_joints": {
            "joints": [
                "robotiq_85_right_knuckle_joint",
                "robotiq_85_left_inner_knuckle_joint",
                "robotiq_85_right_inner_knuckle_joint",
                "robotiq_85_left_finger_tip_joint",
                "robotiq_85_right_finger_tip_joint",
            ],
            "reason": "공식 ros2_control 블록이 이 mimic 관절에 state_interface를"
                      " 선언하지 않는다. joint_state_broadcaster가 발행하지 않아"
                      " /joint_states로 관측할 수 없다",
            "consequence": "mimic 추종을 직접 확인하지 못한다. 개구는 명령 관절값과"
                           " 공식 FK 개구 표로 계산한 값이며, Gazebo 링크 자세"
                           " 실측과는 2 µm까지 일치한다"
                           " (scripts/diagnose_gripper_frames.py)",
            "reason_code": "exec.unverifiable",
        },
        "aperture_model": aperture.to_dict(),
        "checks": checks,
        "passed_count": passed,
        "total_count": len(checks),
        "note": "Gazebo simulation 결과다. 실제 파지력은 검증하지 않았다",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"  {out.relative_to(ROOT)} 기록 — {passed}/{len(checks)} 통과")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
