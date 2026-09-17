#!/usr/bin/env python3
"""작업 셀의 **검증된 자세**로 팔을 보낸다 (8-09 우선 작업 3).

`config/workcell/fr3_2f85_workcell_poses.json`의 `status=verified` 자세만 쓴다.
검증되지 않은 자세나 임의 관절값으로는 움직이지 않는다.

시연·확인용이다. 안전 판단을 거치는 실행 경로는 웹 UI다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
ACTION = "/arm_trajectory_controller/follow_joint_trajectory"
ARM_TOLERANCE_RAD = 0.05


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pose", default="safe_home",
                        help="safe_home 또는 유도된 자세 이름")
    parser.add_argument("--seconds", type=int, default=6)
    parser.add_argument("--list", action="store_true", help="쓸 수 있는 자세 목록")
    args = parser.parse_args()

    if not POSES.is_file():
        print(f"유도된 자세 파일이 없다: {POSES}", file=sys.stderr)
        return 2
    data = json.loads(POSES.read_text(encoding="utf-8"))
    available = {"safe_home": data.get("safe_home", {})}
    available.update(data.get("poses", {}))
    verified = {name: entry for name, entry in available.items()
                if entry.get("status") == "verified"}
    if args.list:
        for name in sorted(verified):
            print(f"  {name}")
        return 0
    entry = verified.get(args.pose)
    if entry is None:
        print(f"검증된 자세가 아니다: {args.pose!r}"
              f" (가능: {', '.join(sorted(verified))})", file=sys.stderr)
        return 3
    target = dict(entry["joint_rad"])

    import rclpy
    from builtin_interfaces.msg import Duration
    from control_msgs.action import FollowJointTrajectory
    from rclpy.action import ActionClient
    from rclpy.node import Node
    from sensor_msgs.msg import JointState
    from trajectory_msgs.msg import JointTrajectoryPoint

    rclpy.init()
    node = Node("forstick2_goto_workcell_pose")
    latest: dict = {}
    node.create_subscription(
        JointState, "/joint_states",
        lambda m: latest.update(dict(zip(m.name, m.position))), 20)
    client = ActionClient(node, FollowJointTrajectory, ACTION)
    try:
        if not client.wait_for_server(timeout_sec=25.0):
            print(f"팔 액션 서버가 없다: {ACTION}", file=sys.stderr)
            return 4
        goal = FollowJointTrajectory.Goal()
        goal.trajectory.joint_names = list(ARM_JOINTS)
        point = JointTrajectoryPoint()
        point.positions = [float(target[name]) for name in ARM_JOINTS]
        point.velocities = [0.0] * len(ARM_JOINTS)
        point.time_from_start = Duration(sec=args.seconds)
        goal.trajectory.points = [point]
        send = client.send_goal_async(goal)
        while not send.done():
            rclpy.spin_once(node, timeout_sec=0.05)
        handle = send.result()
        if not handle.accepted:
            print("goal이 거부됐다", file=sys.stderr)
            return 5
        result_future = handle.get_result_async()
        deadline = time.monotonic() + args.seconds + 25
        while not result_future.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.05)
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.05)
        missing = [name for name in ARM_JOINTS if name not in latest]
        if missing:
            print(f"관절 관측이 없다: {missing} — 도달을 확인할 수 없다",
                  file=sys.stderr)
            return 6
        errors = {name: abs(latest[name] - target[name]) for name in ARM_JOINTS}
        worst = max(errors.values())
        reached = worst <= ARM_TOLERANCE_RAD
        print(f"[goto] {args.pose}: 관측 최대 오차 {worst:.5f} rad"
              f" (허용 {ARM_TOLERANCE_RAD}) — {'도달' if reached else '미도달'}")
        return 0 if reached else 7
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
