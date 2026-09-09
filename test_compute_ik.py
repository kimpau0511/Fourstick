"""
move_group의 /compute_ik 서비스로 목표 좌표를 관절각으로 변환하고,
바로 panda_arm_controller에 실행까지 시켜서 눈으로 확인하는 테스트.

[수정] 목표 orientation을 identity(임의 방향)에서 "그리퍼가 아래를
향하는" 표준 픽업 자세 쿼터니언으로 변경. identity였을 때는 IK가
불필요하게 부자연스러운 방향을 만족시키려다 관절 한계(joint2)까지
밀리는 문제가 있었음.
"""
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from moveit_msgs.srv import GetPositionIK
from moveit_msgs.msg import PositionIKRequest, RobotState
from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import JointState
from control_msgs.action import FollowJointTrajectory
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from builtin_interfaces.msg import Duration

JOINT_NAMES = [
    "panda_joint1", "panda_joint2", "panda_joint3",
    "panda_joint4", "panda_joint5", "panda_joint6", "panda_joint7",
]
# ready 자세 (panda.srdf 값) - IK seed 및 시작 상태로 사용
READY_POSE = [0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785]


class IKAndMove(Node):
    def __init__(self):
        super().__init__("ik_and_move_tester")
        self.ik_client = self.create_client(GetPositionIK, "/compute_ik")
        self.traj_client = ActionClient(
            self, FollowJointTrajectory,
            "/panda_arm_controller/follow_joint_trajectory",
        )

    def compute_ik(self, x, y, z):
        if not self.ik_client.wait_for_service(timeout_sec=20.0):
            print("[테스트] /compute_ik 서비스 연결 실패")
            return None
        req = GetPositionIK.Request()
        req.ik_request = PositionIKRequest()
        req.ik_request.group_name = "panda_arm"
        req.ik_request.avoid_collisions = True

        seed = RobotState()
        seed.joint_state = JointState()
        seed.joint_state.name = JOINT_NAMES
        seed.joint_state.position = READY_POSE
        req.ik_request.robot_state = seed

        pose = PoseStamped()
        pose.header.frame_id = "panda_link0"
        pose.pose.position.x = x
        pose.pose.position.y = y
        pose.pose.position.z = z
        # 그리퍼가 아래(팔레트 쪽)를 향하도록: Panda 표준 top-down 픽업 자세
        pose.pose.orientation.x = 1.0
        pose.pose.orientation.y = 0.0
        pose.pose.orientation.z = 0.0
        pose.pose.orientation.w = 0.0
        req.ik_request.pose_stamped = pose

        future = self.ik_client.call_async(req)
        rclpy.spin_until_future_complete(self, future)
        return future.result()

    def move_to_joints(self, positions, duration_sec=3.0):
        if not self.traj_client.wait_for_server(timeout_sec=20.0):
            print("[테스트] 액션 서버 연결 실패")
            return False
        goal = FollowJointTrajectory.Goal()
        goal.trajectory = JointTrajectory()
        goal.trajectory.joint_names = JOINT_NAMES
        point = JointTrajectoryPoint()
        point.positions = positions
        point.time_from_start = Duration(sec=int(duration_sec), nanosec=0)
        goal.trajectory.points = [point]
        send_future = self.traj_client.send_goal_async(goal)
        rclpy.spin_until_future_complete(self, send_future)
        goal_handle = send_future.result()
        if not goal_handle.accepted:
            print("[테스트] 목표 거부됨")
            return False
        result_future = goal_handle.get_result_async()
        rclpy.spin_until_future_complete(self, result_future)
        result = result_future.result().result
        print(f"[테스트] 이동 완료 (error_code={result.error_code})")
        return result.error_code == FollowJointTrajectory.Result.SUCCESSFUL


def main():
    rclpy.init()
    tester = IKAndMove()

    # 1) 먼저 ready 자세로 (시작점 통일)
    print("[테스트] 'ready' 자세로 먼저 이동")
    tester.move_to_joints(READY_POSE)

    # 2) 2번 팔레트(x=0.5, y=0.0) 위쪽 0.3m 지점으로 IK 계산
    print("[테스트] 2번 팔레트 위쪽으로 IK 계산 (seed=ready, top-down)")
    result = tester.compute_ik(x=0.5, y=0.0, z=0.3)
    if result is None or result.error_code.val != 1:
        print(f"[테스트] IK 실패 (error_code={getattr(result, 'error_code', None)})")
        return

    names = result.solution.joint_state.name
    positions_dict = dict(zip(names, result.solution.joint_state.position))
    positions = [positions_dict[n] for n in JOINT_NAMES]
    print("[테스트] IK 결과:")
    for n, p in zip(JOINT_NAMES, positions):
        print(f"  {n}: {p:.4f}")

    # 3) 그 자세로 실제 이동
    print("[테스트] IK 결과 자세로 이동")
    tester.move_to_joints(positions)

    tester.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
