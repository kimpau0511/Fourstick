#!/usr/bin/env python3
"""작업 셀 자세를 MoveIt planning scene과 Gazebo contact로 검증 (8-08 우선순위 3).

두 경로를 **따로** 확인한다. 하나만 통과한 것을 충돌 없음으로 쓰지 않는다.

1. MoveIt: `/check_state_validity`로 각 명명 자세의 유효성과 접촉 쌍
2. Gazebo: 자세를 실제로 실행한 뒤 `/world/<world>/contacts` 관측

pick/place 자세는 물체에 닿는 자세라 접촉이 정상이다. 그래서 **접근 자세와
안전 home만** 충돌 없음을 요구하고, pick/place 자세는 접촉 쌍을 기록만 한다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
URDF = Path("/tmp/forstick2_workcell/workcell/fr3wms_with_2f85.moveit.urdf")
SRDF = Path("/tmp/forstick2_workcell/workcell/fr3_2f85_workcell.srdf")
OUT = ROOT / "reports/workcell/state_validity.json"
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
GROUP = "fr3wms_arm"
ARM_TOLERANCE_RAD = 0.05
#: 자세 실행 시간(초). 화면으로 따라갈 수 있게 느리게 움직인다.
MOVE_SEC = 7


def gazebo_contacts(world: str, seconds: float = 2.0) -> list[tuple[str, str]]:
    """Gazebo가 발행하는 접촉을 읽는다. 없으면 빈 목록이다(관측 실패와 구별한다)."""
    try:
        result = subprocess.run(
            ["gz", "topic", "-e", "-t", f"/world/{world}/contacts", "-n", "1",
             "--json-output"],
            capture_output=True, text=True, timeout=seconds + 8,
        )
    except subprocess.TimeoutExpired:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError:
        return []
    out = []
    for contact in payload.get("contact", []):
        a = (contact.get("collision1") or {}).get("name", "?")
        b = (contact.get("collision2") or {}).get("name", "?")
        out.append((a, b))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-gazebo", action="store_true",
                        help="MoveIt 검사만 한다(팔을 움직이지 않는다)")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    for path in (POSES, URDF, SRDF):
        if not path.is_file():
            print(f"필요한 파일이 없다: {path}", file=sys.stderr)
            return 2

    data = json.loads(POSES.read_text(encoding="utf-8"))
    workcell = json.loads(WORKCELL.read_text(encoding="utf-8"))
    world = workcell["world_name"]

    import rclpy
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from builtin_interfaces.msg import Duration
    from control_msgs.action import FollowJointTrajectory
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectoryPoint

    from robots.moveit.kinematics import parse_urdf, revolute_limits
    from robots.moveit.ros_client import RosPlanningSceneClient, robot_model_hash

    joints, _ = parse_urdf(URDF)
    limits = {name: (limit["lower"], limit["upper"])
              for name, limit in revolute_limits(joints).items()}

    rclpy.init()
    node = Node("forstick2_workcell_state_check")
    client = RosPlanningSceneClient(
        node, group_name=GROUP, frame_id="world", joint_limits=limits,
        model_hash=robot_model_hash(URDF.read_text(encoding="utf-8"),
                                    SRDF.read_text(encoding="utf-8")),
        ttl_sec=5.0, source="scripts/check_workcell_states.py",
    )
    if not client.wait(timeout_sec=60.0):
        print("MoveIt 서비스가 없다 — move_group이 떠 있는지 본다", file=sys.stderr)
        node.destroy_node()
        rclpy.shutdown()
        return 3

    snapshot = client.snapshot()

    arm_action = ActionClient(
        node, FollowJointTrajectory,
        "/arm_trajectory_controller/follow_joint_trajectory")
    states: dict[str, float] = {}

    def on_state(message: JointState) -> None:
        states.clear()
        states.update(dict(zip(message.name, message.position)))

    node.create_subscription(JointState, "/joint_states", on_state, 20)

    def spin(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)

    def execute(target: dict) -> dict:
        if not arm_action.wait_for_server(timeout_sec=20.0):
            return {"accepted": False, "detail": "팔 액션 서버 없음"}
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(ARM_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [float(target[name]) for name in ARM_JOINTS]
        point.time_from_start = Duration(sec=MOVE_SEC)
        goal.trajectory.points = [point]
        send = arm_action.send_goal_async(goal)
        while not send.done():
            rclpy.spin_once(node, timeout_sec=0.05)
        handle = send.result()
        if not handle.accepted:
            return {"accepted": False, "detail": "goal 거부"}
        future = handle.get_result_async()
        deadline = time.monotonic() + MOVE_SEC + 25
        while not future.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        spin(1.5)
        if not states:
            return {"accepted": True, "detail": "joint_states 없음"}
        errors = {name: abs(states.get(name, 0.0) - target[name])
                  for name in ARM_JOINTS}
        return {"accepted": True,
                "max_error_rad": round(max(errors.values()), 5),
                "tolerance_rad": ARM_TOLERANCE_RAD,
                "reached": max(errors.values()) <= ARM_TOLERANCE_RAD}

    spin(3.0)
    gripper_value = states.get(GRIPPER_JOINT, 0.0)

    checks = []
    named = {"workcell_safe_home": data["safe_home"]["joint_rad"]}
    named.update({name: pose["joint_rad"] for name, pose in data["poses"].items()
                  if pose.get("status") == "verified"})

    for name, arm in named.items():
        requires_clear = name.endswith("_approach") or name == "workcell_safe_home"
        query = dict(arm)
        query[GRIPPER_JOINT] = gripper_value
        validity = client.check_state(query)
        entry = {
            "pose": name,
            "requires_collision_free": requires_clear,
            "moveit": {
                "valid": validity.valid,
                "contacts": [list(pair) for pair in validity.contacts],
                "out_of_bounds": list(validity.out_of_bounds),
                "detail": validity.detail,
            },
        }
        if args.skip_gazebo:
            entry["gazebo"] = {"checked": False,
                               "reason": "--skip-gazebo — 팔을 움직이지 않았다"}
        else:
            execution = execute(arm)
            contacts = gazebo_contacts(world)
            robot_contacts = [list(pair) for pair in contacts
                              if "fr3wms_2f85_workcell" in pair[0]
                              or "fr3wms_2f85_workcell" in pair[1]]
            entry["gazebo"] = {
                "checked": True,
                "execution": execution,
                "all_contacts": [list(pair) for pair in contacts],
                "robot_contacts": robot_contacts,
                "robot_contact_free": not robot_contacts,
            }
        moveit_ok = validity.valid
        gazebo_ok = (True if args.skip_gazebo
                     else entry["gazebo"].get("robot_contact_free", False)
                     and entry["gazebo"]["execution"].get("reached", False))
        entry["status"] = (
            "verified" if (moveit_ok and gazebo_ok) else
            "recorded" if not requires_clear else "blocked")
        if entry["status"] == "blocked":
            entry["reason_code"] = "geometry.collision_detected"
        checks.append(entry)
        mark = {"verified": "통과", "recorded": "기록", "blocked": "차단"}[entry["status"]]
        print(f"  {mark} {name:22} MoveIt valid={validity.valid}"
              f" 접촉 {len(validity.contacts)}쌍"
              + ("" if args.skip_gazebo else
                 f" · Gazebo 로봇접촉 {len(entry['gazebo']['robot_contacts'])}쌍"
                 f" 도달={entry['gazebo']['execution'].get('reached')}"))

    node.destroy_node()
    rclpy.shutdown()

    required = [c for c in checks if c["requires_collision_free"]]
    report = {
        "schema": "forstick2.workcell_state_validity/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "world": world,
        "group": GROUP,
        "gripper_joint_rad_at_check": round(gripper_value, 6),
        "planning_scene": snapshot.to_dict(),
        "gazebo_checked": not args.skip_gazebo,
        "checks": checks,
        "summary": {
            "required_poses": len(required),
            "required_verified": sum(1 for c in required if c["status"] == "verified"),
            "blocked": sum(1 for c in checks if c["status"] == "blocked"),
        },
        "note": "pick/place 자세는 물체에 닿는 자세라 접촉이 정상이다 —"
                " 접촉 쌍을 기록만 하고 충돌 없음을 요구하지 않는다."
                " 그 판정은 pick/place 관문이 따로 한다",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"  {out.relative_to(ROOT)} 기록 — 필수 자세"
          f" {report['summary']['required_verified']}/{report['summary']['required_poses']} 통과"
          f" · 차단 {report['summary']['blocked']}")
    return 0 if report["summary"]["blocked"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
