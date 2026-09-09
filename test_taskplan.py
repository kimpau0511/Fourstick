"""
Task Plan JSON(pipeline.py의 process_utterance() 출력)을 읽어서
실제(Gazebo 물리 시뮬레이션) Panda 로봇을 움직이는 실행 브릿지.

사용법:
  1) 발화 텍스트로 바로 실행 (vLLM 서버가 떠 있어야 함, pipeline.py를
     import할 수 있게 이 스크립트를 pipeline.py와 같은 디렉토리에 두거나
     PYTHONPATH에 ~/forstick를 추가):
       python3 taskplan_bridge.py --utterance "1번 팔레트에서 A자재를 집어서 컨베이어에 옮겨"

  2) 이미 만들어진 Task Plan JSON 파일로 실행 (vLLM 필요 없음, Gazebo
     연동만 테스트할 때 유용):
       python3 taskplan_bridge.py --plan-file my_plan.json

사전 조건 (panda_gazebo_moveit.launch.py가 떠 있어야 함):
  source /opt/ros/lyrical/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  (이 두 줄은 launch 터미널뿐 아니라 이 스크립트를 실행하는 터미널에도 필요)

주의: 지금 pallets_world.sdf에는 A/B/C자재를 나타내는 실제 물체 모델이
없다. 그래서 pick/place는 "해당 위치로 내려가서 그리퍼를 닫는/여는" 동작
까지만 수행하고, 실제로 뭔가를 붙잡아 옮기는 물리적 효과는 아직 없다.
나중에 월드에 물체 모델을 추가하면 자연스럽게 채워질 부분.
"""
import argparse
import json
import sys
import threading

import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from moveit_msgs.srv import GetPositionIK
from moveit_msgs.msg import PositionIKRequest, RobotState
from geometry_msgs.msg import PoseStamped
from sensor_msgs.msg import JointState
from control_msgs.action import FollowJointTrajectory, ParallelGripperCommand
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from builtin_interfaces.msg import Duration

JOINT_NAMES = [
    "panda_joint1", "panda_joint2", "panda_joint3",
    "panda_joint4", "panda_joint5", "panda_joint6", "panda_joint7",
]
READY_POSE = [0.0, -0.785, 0.0, -2.356, 0.0, 1.571, 0.785]

GRIPPER_JOINT_NAME = "panda_finger_joint1"
GRIPPER_OPEN = 0.04
GRIPPER_CLOSE = 0.0
GRIPPER_MAX_EFFORT = 20.0

LOCATION_POSES = {
    "1번 팔레트": (0.4, 0.3, 0.30, 0.15),
    "2번 팔레트": (0.5, 0.0, 0.30, 0.15),
    "3번 팔레트": (0.4, -0.3, 0.30, 0.15),
    "컨베이어": (0.0, 0.55, 0.40, 0.25),
}


class TaskPlanExecutor(Node):
    def __init__(self):
        super().__init__("taskplan_bridge")
        self.ik_client = self.create_client(GetPositionIK, "/compute_ik")
        self.traj_client = ActionClient(
            self, FollowJointTrajectory,
            "/panda_arm_controller/follow_joint_trajectory",
        )
        self.gripper_client = ActionClient(
            self, ParallelGripperCommand,
            "/panda_hand_controller/gripper_cmd",
        )
        self._last_joint_positions = list(READY_POSE)
        self._stopped = False
        # 기본은 이 노드가 직접 spin_until_future_complete를 부르는 방식
        # (독립 스크립트로 실행할 때, main()에서 단일 스레드로 도는 상황에
        # 맞음). enable_background_spin()을 부르면 대신 전용 executor가
        # 백그라운드 스레드에서 계속 이 노드를 spin하고, future 완료는
        # threading.Event로 기다리는 방식으로 전환된다 — API 서버처럼
        # 다른 노드(카메라 뷰어 등)도 동시에 spin되는 환경에서, rclpy의
        # 스레드별 기본 executor를 서로 다른 노드가 공유하다가
        # "Executor is already spinning" 에러가 나는 걸 피하기 위함.
        self._background_spinning = False
        self._spin_executor = None
        self._spin_thread = None

    def enable_background_spin(self):
        """여러 노드가 동시에 떠 있는 서버 환경(api_server.py)에서 호출.
        이 노드 전용 SingleThreadedExecutor를 만들어 별도 스레드에서
        계속 spin시킨다. 이후 compute_ik/move_to_joints/set_gripper는
        직접 spin하지 않고 이 백그라운드 spin이 처리해주는 결과를
        기다리기만 한다."""
        self._spin_executor = SingleThreadedExecutor()
        self._spin_executor.add_node(self)
        self._spin_thread = threading.Thread(target=self._spin_executor.spin, daemon=True)
        self._spin_thread.start()
        self._background_spinning = True

    def _wait_for_future(self, future, timeout_sec=None):
        if self._background_spinning:
            done_event = threading.Event()
            future.add_done_callback(lambda _f: done_event.set())
            if not done_event.wait(timeout=timeout_sec):
                print("[브릿지] 응답 대기 타임아웃")
                return None
            return future.result()
        else:
            rclpy.spin_until_future_complete(self, future, timeout_sec=timeout_sec)
            return future.result()

    def compute_ik(self, x, y, z, seed=None):
        if not self.ik_client.wait_for_service(timeout_sec=20.0):
            print("[브릿지] /compute_ik 서비스 연결 실패")
            return None
        req = GetPositionIK.Request()
        req.ik_request = PositionIKRequest()
        req.ik_request.group_name = "panda_arm"
        req.ik_request.avoid_collisions = True

        seed_state = RobotState()
        seed_state.joint_state = JointState()
        seed_state.joint_state.name = JOINT_NAMES
        seed_state.joint_state.position = seed if seed is not None else self._last_joint_positions
        req.ik_request.robot_state = seed_state

        pose = PoseStamped()
        pose.header.frame_id = "panda_link0"
        pose.pose.position.x = x
        pose.pose.position.y = y
        pose.pose.position.z = z
        pose.pose.orientation.x = 1.0
        pose.pose.orientation.y = 0.0
        pose.pose.orientation.z = 0.0
        pose.pose.orientation.w = 0.0
        req.ik_request.pose_stamped = pose

        future = self.ik_client.call_async(req)
        result = self._wait_for_future(future)
        if result is None or result.error_code.val != 1:
            print(f"[브릿지] IK 실패 (x={x}, y={y}, z={z}, error_code="
                  f"{getattr(result, 'error_code', None)})")
            return None
        names = result.solution.joint_state.name
        positions_dict = dict(zip(names, result.solution.joint_state.position))
        return [positions_dict[n] for n in JOINT_NAMES]

    def move_to_joints(self, positions, duration_sec=3.0):
        if not self.traj_client.wait_for_server(timeout_sec=20.0):
            print("[브릿지] 팔 액션 서버 연결 실패")
            return False
        goal = FollowJointTrajectory.Goal()
        goal.trajectory = JointTrajectory()
        goal.trajectory.joint_names = JOINT_NAMES
        point = JointTrajectoryPoint()
        point.positions = positions
        point.time_from_start = Duration(sec=int(duration_sec), nanosec=0)
        goal.trajectory.points = [point]
        send_future = self.traj_client.send_goal_async(goal)
        goal_handle = self._wait_for_future(send_future)
        if goal_handle is None or not goal_handle.accepted:
            print("[브릿지] 팔 이동 목표 거부됨")
            return False
        result_future = goal_handle.get_result_async()
        action_result = self._wait_for_future(result_future)
        result = action_result.result
        ok = result.error_code == FollowJointTrajectory.Result.SUCCESSFUL
        if ok:
            self._last_joint_positions = list(positions)
        print(f"[브릿지] 팔 이동 완료 (error_code={result.error_code})")
        return ok

    def move_to_pose(self, x, y, z, label=""):
        print(f"[브릿지] {label} IK 계산 중 (x={x}, y={y}, z={z})")
        positions = self.compute_ik(x, y, z)
        if positions is None:
            return False
        return self.move_to_joints(positions)

    def set_gripper(self, position, label=""):
        if not self.gripper_client.wait_for_server(timeout_sec=20.0):
            print("[브릿지] 그리퍼 액션 서버 연결 실패")
            return False
        goal = ParallelGripperCommand.Goal()
        goal.command = JointState()
        goal.command.name = [GRIPPER_JOINT_NAME]
        goal.command.position = [position]
        goal.command.effort = [GRIPPER_MAX_EFFORT]
        send_future = self.gripper_client.send_goal_async(goal)
        goal_handle = self._wait_for_future(send_future)
        if goal_handle is None or not goal_handle.accepted:
            print(f"[브릿지] 그리퍼 목표 거부됨 ({label})")
            return False
        result_future = goal_handle.get_result_async()
        self._wait_for_future(result_future)
        print(f"[브릿지] 그리퍼 {label} 완료")
        return True

    def exec_home(self, args):
        print("[스킬] home")
        return self.move_to_joints(READY_POSE)

    def exec_move(self, args):
        target = args["target"]
        if target not in LOCATION_POSES:
            print(f"[스킬] move 실패: 알 수 없는 위치 '{target}'")
            return False
        x, y, hover_z, _ = LOCATION_POSES[target]
        print(f"[스킬] move -> {target}")
        return self.move_to_pose(x, y, hover_z, label=f"'{target}' 상공으로 이동")

    def exec_pick(self, args):
        obj = args.get("object")
        loc = args.get("from")
        print(f"[스킬] pick <- {loc} ({obj})")
        if loc not in LOCATION_POSES:
            print(f"[스킬] pick 실패: 알 수 없는 위치 '{loc}'")
            return False
        x, y, hover_z, grasp_z = LOCATION_POSES[loc]
        if not self.move_to_pose(x, y, hover_z, label=f"'{loc}' 상공으로 접근"):
            return False
        if not self.move_to_pose(x, y, grasp_z, label=f"'{loc}' 집기 높이로 하강"):
            return False
        if not self.set_gripper(GRIPPER_CLOSE, label="닫기(집기)"):
            return False
        return self.move_to_pose(x, y, hover_z, label=f"'{loc}' 상공으로 복귀")

    def exec_place(self, args):
        obj = args.get("object")
        loc = args.get("to")
        print(f"[스킬] place -> {loc} ({obj})")
        if loc not in LOCATION_POSES:
            print(f"[스킬] place 실패: 알 수 없는 위치 '{loc}'")
            return False
        x, y, hover_z, grasp_z = LOCATION_POSES[loc]
        if not self.move_to_pose(x, y, hover_z, label=f"'{loc}' 상공으로 접근"):
            return False
        if not self.move_to_pose(x, y, grasp_z, label=f"'{loc}' 놓기 높이로 하강"):
            return False
        if not self.set_gripper(GRIPPER_OPEN, label="열기(놓기)"):
            return False
        return self.move_to_pose(x, y, hover_z, label=f"'{loc}' 상공으로 복귀")

    def exec_stop(self, args):
        print("[스킬] stop — 남은 스텝 실행 중단")
        self._stopped = True
        return True

    def run_plan(self, task_plan: dict):
        print(f"[브릿지] plan_id={task_plan.get('plan_id')} "
              f"intent={task_plan.get('intent')!r}")
        handlers = {
            "home": self.exec_home,
            "move": self.exec_move,
            "pick": self.exec_pick,
            "place": self.exec_place,
            "stop": self.exec_stop,
        }
        for i, step in enumerate(task_plan["steps"]):
            if self._stopped:
                print(f"[브릿지] stop으로 인해 스텝 {i} 이후 중단")
                break
            skill = step["skill"]
            args = step.get("args", {})
            handler = handlers.get(skill)
            if handler is None:
                print(f"[브릿지] 알 수 없는 skill '{skill}', 건너뜀")
                continue
            ok = handler(args)
            if not ok:
                print(f"[브릿지] 스텝 {i} ('{skill}') 실패 — 실행 중단")
                return False
        print("[브릿지] Task Plan 실행 완료")
        return True


def load_plan_from_utterance(utterance: str) -> dict:
    sys.path.insert(0, "/home/asd/forstick")
    from pipeline import process_utterance
    result = process_utterance(utterance, confidence=1.0)
    if result["status"] != "success":
        print(f"[브릿지] Task Plan 생성 실패: status={result['status']}, "
              f"{result.get('message')}")
        sys.exit(1)
    return result["task_plan"]


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--utterance", help="자연어 지시문 (pipeline.py L1/L2를 거쳐 Task Plan 생성)")
    group.add_argument("--plan-file", help="이미 만들어진 Task Plan JSON 파일 경로")
    cli_args = parser.parse_args()

    if cli_args.utterance:
        task_plan = load_plan_from_utterance(cli_args.utterance)
    else:
        with open(cli_args.plan_file, "r", encoding="utf-8") as f:
            task_plan = json.load(f)

    rclpy.init()
    executor = TaskPlanExecutor()
    try:
        executor.run_plan(task_plan)
    finally:
        executor.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
