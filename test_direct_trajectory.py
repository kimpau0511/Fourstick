"""
플래닝 없이, panda_arm_controller의 FollowJointTrajectory 액션에 직접
목표 관절 값을 보내서 코드로 팔을 움직이는 테스트.
(moveit_py의 플래닝 파이프라인 로딩 버그를 우회하기 위한 방식.)

사전 조건: 데모가 이미 떠 있어야 함 (다른 터미널에서 계속 실행 중이어야 함)
  ros2 launch ~/forstick/ros2_patches/demo_launch_patched.py

실행: python3 test_direct_trajectory.py
"""

import time

import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from control_msgs.action import FollowJointTrajectory
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from builtin_interfaces.msg import Duration

JOINT_NAMES = [
    "panda_joint1", "panda_joint2", "panda_joint3",
    "panda_joint4", "panda_joint5", "panda_joint6", "panda_joint7",
]

# panda.srdf의 group_state 값 그대로
NAMED_POSES = {
    "ready": [0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785],
    "extended": [0.0, 0.0, 0.0, 0.0, 0.0, 1.571, 0.785],
    "transport": [0.0, -0.5599, 0.0, -2.97, 0.0, 0.0, 0.785],
}


class ArmMover(Node):
    def __init__(self):
        super().__init__("arm_mover_test")
        self._client = ActionClient(
            self, FollowJointTrajectory,
            "/panda_arm_controller/follow_joint_trajectory",
        )

    def move_to(self, name: str, duration_sec: float = 3.0) -> bool:
        positions = NAMED_POSES[name]
        print(f"[테스트] '{name}' 자세로 이동 요청")

        if not self._client.wait_for_server(timeout_sec=5.0):
            print("[테스트] 액션 서버 연결 실패 (/panda_arm_controller/follow_joint_trajectory)")
            return False

        goal = FollowJointTrajectory.Goal()
        goal.trajectory = JointTrajectory()
        goal.trajectory.joint_names = JOINT_NAMES

        point = JointTrajectoryPoint()
        point.positions = positions
        point.time_from_start = Duration(sec=int(duration_sec), nanosec=0)
        goal.trajectory.points = [point]

        send_future = self._client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future)
        goal_handle = send_future.result()

        if not goal_handle.accepted:
            print(f"[테스트] '{name}' 목표 거부됨")
            return False

        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future)
        result = result_future.result().result

        print(f"[테스트] '{name}' 이동 완료 (error_code={result.error_code})")
        return result.error_code == FollowJointTrajectory.Result.SUCCESSFUL


def main():
    rclpy.init()
    mover = ArmMover()

    mover.move_to("ready", duration_sec=3.0)
    time.sleep(1)
    mover.move_to("extended", duration_sec=3.0)

    mover.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
