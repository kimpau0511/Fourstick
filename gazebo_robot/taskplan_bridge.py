"""
Task Plan JSON(pipeline.py의 process_utterance() 출력)을 읽어서
실제(Gazebo 물리 시뮬레이션) 로봇을 움직이는 실행 브릿지.
관절 이름/그리퍼/컨트롤러 액션/IK 그룹 등 로봇마다 다른 값은
robot_config.py의 RobotConfig로 분리돼 있음 — TaskPlanExecutor 자체는
로봇-무관 스킬 실행 로직(exec_home/move/pick/place/stop)만 갖고 있다.
지금 지원하는 로봇: panda(기본값), ur5e(robot_config.UR5E_CONFIG).

사용법:
  1) 발화 텍스트로 바로 실행 (vLLM 서버가 떠 있어야 함, pipeline.py를
     import할 수 있게 이 스크립트를 pipeline.py와 같은 디렉토리에 두거나
     PYTHONPATH에 ~/forstick를 추가):
       python3 taskplan_bridge.py --utterance "1번 팔레트에서 A자재를 집어서 컨베이어에 옮겨"
       python3 taskplan_bridge.py --robot ur5e --utterance "..."

  2) 이미 만들어진 Task Plan JSON 파일로 실행 (vLLM 필요 없음, Gazebo
     연동만 테스트할 때 유용):
       python3 taskplan_bridge.py --plan-file my_plan.json

사전 조건 (로봇에 맞는 launch 파일이 떠 있어야 함 —
panda_gazebo_moveit.launch.py 또는 ur5e_robotiq_gazebo.launch.py):
  source /opt/ros/lyrical/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  (이 두 줄은 launch 터미널뿐 아니라 이 스크립트를 실행하는 터미널에도 필요)

주의: pallets_world.sdf에 A/B/C자재 실물 모델(material_a/b/c)이 각각
1/2/3번 팔레트 위에 하나씩 있다. pick/place는 그 높이에 맞춰 그리퍼를
닫는/여는 동작을 하며, 마찰력으로 붙잡는 방식이라(별도 attach/detach
플러그인 없음) 팔을 빠르게 움직이면 미끄러져 빠질 수도 있다.
"""
import argparse
import json
import subprocess
import sys
import threading
import time
from collections import deque

import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from moveit_msgs.srv import GetPositionIK, GetCartesianPath
from moveit_msgs.msg import PositionIKRequest, RobotState
from geometry_msgs.msg import PoseStamped, Pose
from sensor_msgs.msg import JointState, Image as RosImage
from control_msgs.action import FollowJointTrajectory, ParallelGripperCommand
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from builtin_interfaces.msg import Duration
from rosgraph_msgs.msg import Clock
import numpy as np

from robot_config import PANDA_CONFIG, ROBOT_CONFIGS, RobotConfig
import vision_locator

# Task Plan의 물체 슬롯(CAPABILITY_PROFILE["objects"], pipeline.py) ->
# pallets_world.sdf의 실물 모델 이름. 그리퍼에 effort/force 센서가 없어서
# (robot_config.py 상단 주석 참고) "실제로 들렸는지"를 관절 토크로는 알 수
# 없으므로, gz transport로 물체 자체의 world-frame z를 직접 읽어 대신
# 판정한다(exec_pick의 grasp 검증/재시도 참고).
OBJECT_MODEL_MAP = {
    "A자재": "material_a",
    "B자재": "material_b",
    "C자재": "material_c",
}
GZ_WORLD_NAME = "pallets_world"
# 안착 높이(grasp_z) 대비 이만큼 이상 떠 있어야 "실제로 들렸다"로 판정.
# 안착 상태에서도 물리 노이즈로 ±1mm 정도 흔들리므로(실측: 0.1175 근처),
# hover 높이와의 차이(수십 cm)에 비하면 3cm는 충분히 보수적인 여유값.
GRASP_LIFT_THRESHOLD_M = 0.03


class TaskPlanExecutor(Node):
    def __init__(self, config: RobotConfig = None, context=None):
        self.config = config or PANDA_CONFIG
        super().__init__(f"taskplan_bridge_{self.config.robot_id}", context=context)
        self.ik_client = self.create_client(GetPositionIK, "/compute_ik")
        self.cartesian_client = self.create_client(GetCartesianPath, "/compute_cartesian_path")
        self.traj_client = ActionClient(
            self, FollowJointTrajectory, self.config.arm_action,
        )
        self.gripper_client = ActionClient(
            self, ParallelGripperCommand, self.config.gripper_action,
        )
        self._last_joint_positions = list(self.config.ready_pose)
        self._stopped = False
        # 실측 결과 이 환경(WSL2 + Gazebo Jetty)은 real_time_factor가 1.0이
        # 아니라 부하에 따라 0.6 정도(심하면 더 낮게)로 떨어짐 — 팔/그리퍼
        # 액션의 duration_sec(트래젝토리 목표 시간)은 시뮬레이션 시간
        # 기준인데, 클라이언트 쪽 응답 대기는 실제 벽시계 기준이라 RTF가
        # 낮으면 서버가 (goal_time 등으로) 정상적으로 abort하기도 전에
        # 클라이언트가 먼저 포기해버리는 문제가 있었다(무한 대기 자체는
        # ur5e_controllers.yaml의 goal_time으로 별도 해결함). /clock을
        # 구독해서 최근 구간의 sim-time/wall-time 비율로 RTF를 추정하고,
        # 응답 대기 타임아웃에 반영한다.
        # (wall_time, sim_time) 표본 — 개수가 아니라 최근 RTF_WINDOW_SEC초
        # "시간" 기준으로 유지한다. /clock은 물리 스텝마다 오므로(이 환경
        # 기준 초당 수백~수천 개) 개수로 자르면(예: 최근 50개) 실제로는
        # 0.5초도 안 되는 구간만 보게 돼서 RTF 추정이 사실상 항상
        # get_real_time_factor()의 default(1.0)로 새버리는 버그가 있었음
        # (실측으로 확인함 — RTF가 실제로는 0.4~0.6인데 계속 1.000으로 나옴).
        self._clock_samples = deque()
        self.RTF_WINDOW_SEC = 3.0
        self.create_subscription(Clock, "/clock", self._on_clock, qos_profile_sensor_data)
        # scene_camera 프레임 — exec_pick에서 vision_locator로 물체의 실제
        # 위치를 추정하는 데 씀(하드코딩 좌표 대신/보정용). api_server.py의
        # CameraViewer와 동일하게 rgb8, 패딩 없음을 가정.
        self._latest_camera_frame = None
        self._camera_frame_lock = threading.Lock()
        self.create_subscription(RosImage, "/scene_camera/image", self._on_camera_image, 5)
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
        """background_spin 모드면 threading.Event로, 아니면 기존처럼
        spin_until_future_complete로 future 완료를 기다리고 결과를 반환."""
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

    def _on_camera_image(self, msg: RosImage):
        try:
            frame = np.frombuffer(bytes(msg.data), dtype=np.uint8).reshape(msg.height, msg.width, 3)
        except ValueError as exc:
            print(f"[브릿지] scene_camera 프레임 변환 실패: {exc}")
            return
        with self._camera_frame_lock:
            self._latest_camera_frame = frame

    def _locate_object(self, obj, resting_z):
        """vision_locator로 물체의 실제 world (x, y)를 추정. 카메라 프레임이
        아직 없거나(막 켜진 직후), 물체가 화면 밖/가려짐/색상 매핑이 없어서
        vision_locator가 못 찾으면 None — 호출부(exec_pick)가 하드코딩된
        location_poses 좌표로 폴백해야 한다."""
        with self._camera_frame_lock:
            frame = self._latest_camera_frame
        if frame is None:
            print("[브릿지] scene_camera 프레임 없음 — vision 위치 추정 생략")
            return None
        result = vision_locator.locate_object(frame, obj, resting_z=resting_z)
        if result is None:
            print(f"[브릿지] vision으로 '{obj}' 위치를 못 찾음(화면 밖/가려짐/색상 불일치 가능)")
        return result

    def _on_clock(self, msg: Clock):
        now = time.monotonic()
        sim_time = msg.clock.sec + msg.clock.nanosec * 1e-9
        self._clock_samples.append((now, sim_time))
        while self._clock_samples and now - self._clock_samples[0][0] > self.RTF_WINDOW_SEC:
            self._clock_samples.popleft()

    def get_real_time_factor(self, default=1.0, min_rtf=0.15, max_rtf=2.0) -> float:
        """최근 /clock 표본 구간의 (sim 경과시간 / 벽시계 경과시간)으로
        real_time_factor를 추정. 표본이 부족하거나(막 연결된 직후) 구간이
        너무 짧으면(잡음 큼) 재는 의미가 없어 default를 그대로 반환한다.
        극단값(0 근처거나 비정상적으로 큼)은 min/max_rtf로 clamp해서 응답
        대기 타임아웃 계산이 무한대로 늘어나거나 0에 가까워지는 걸 막는다."""
        if len(self._clock_samples) < 2:
            return default
        wall_start, sim_start = self._clock_samples[0]
        wall_end, sim_end = self._clock_samples[-1]
        wall_elapsed = wall_end - wall_start
        # 표본 구간이 너무 짧으면(막 연결된 직후, 또는 CLI 모드처럼 spin이
        # 간헐적으로만 도는 환경이라 메시지가 몰려서 도착한 경우) 잡음이
        # 커서 재는 의미가 없다 — default 반환. background spin 모드
        # (api_server.py)에서는 계속 spin하니 이 구간이 정상적으로
        # RTF_WINDOW_SEC에 가깝게 채워짐.
        if wall_elapsed < 0.5:
            return default
        rtf = (sim_end - sim_start) / wall_elapsed
        return max(min_rtf, min(max_rtf, rtf))

    def _rtf_scaled_timeout(self, sim_time_budget: float, extra_wall_sec: float = 2.0) -> float:
        """sim_time_budget(초, 시뮬레이션 시간 기준 — 예: 트래젝토리
        duration_sec + 컨트롤러 goal_time)을 현재 추정 real_time_factor로
        나눠서 실제 기다려야 할 벽시계 시간으로 환산. extra_wall_sec는
        RTF 추정 자체의 오차/DDS 통신 지연을 위한 순수 벽시계 여유분."""
        return sim_time_budget / self.get_real_time_factor() + extra_wall_sec

    # ---------- 저수준 헬퍼 ----------

    def compute_ik(self, x, y, z, seed=None, avoid_collisions=True):
        if not self.ik_client.wait_for_service(timeout_sec=20.0):
            print("[브릿지] /compute_ik 서비스 연결 실패")
            return None
        req = GetPositionIK.Request()
        req.ik_request = PositionIKRequest()
        req.ik_request.group_name = self.config.ik_group_name
        req.ik_request.avoid_collisions = avoid_collisions
        # kinematics.yaml의 kinematics_solver_timeout이 매우 짧아서(0.005s)
        # timeout을 명시 안 하면(기본 0 -> 그 값 사용) KDL이 풀 수 있는
        # 포즈도 NO_IK_SOLUTION(-31)으로 실패하는 경우가 있었다.
        req.ik_request.timeout = Duration(sec=1, nanosec=0)

        seed_state = RobotState()
        seed_state.joint_state = JointState()
        seed_state.joint_state.name = self.config.joint_names
        seed_state.joint_state.position = seed if seed is not None else self._last_joint_positions
        req.ik_request.robot_state = seed_state

        pose = PoseStamped()
        pose.header.frame_id = self.config.ik_frame_id
        pose.pose.position.x = x
        pose.pose.position.y = y
        pose.pose.position.z = z
        # 그리퍼가 아래를 향하는 표준 top-down 픽업 자세
        pose.pose.orientation.x = 1.0
        pose.pose.orientation.y = 0.0
        pose.pose.orientation.z = 0.0
        pose.pose.orientation.w = 0.0
        req.ik_request.pose_stamped = pose

        # 서비스가 discover된 직후 첫 호출이 (DDS 매칭이 아직 안 끝나서로
        # 추정) 응답 없이 무한 대기하는 경우가 있었다. _wait_for_future에
        # 타임아웃 없이 걸면 그대로 영원히 멈추므로, 5초 타임아웃 + 1회
        # 재시도로 방어한다.
        result = None
        for attempt in range(2):
            future = self.ik_client.call_async(req)
            result = self._wait_for_future(future, timeout_sec=5.0)
            if result is not None:
                break
            print(f"[브릿지] compute_ik 응답 타임아웃 (attempt {attempt + 1}/2), 재시도")
        if result is None or result.error_code.val != 1:
            print(f"[브릿지] IK 실패 (x={x}, y={y}, z={z}, error_code="
                  f"{getattr(result, 'error_code', None)})")
            return None
        names = result.solution.joint_state.name
        positions_dict = dict(zip(names, result.solution.joint_state.position))
        return [positions_dict[n] for n in self.config.joint_names]

    def move_to_joints(self, positions, duration_sec=None):
        """단일 목표(관절각 리스트) 또는 다중 waypoint(관절각 리스트의 리스트,
        move_cartesian()이 넘겨줌)를 받아 하나의 FollowJointTrajectory 목표로
        실행한다. 다중 waypoint일 땐 duration_sec을 waypoint 개수만큼 균등
        분할해서 각 포인트의 time_from_start로 쓴다."""
        if duration_sec is None:
            duration_sec = self.config.move_duration_sec
        if not self.traj_client.wait_for_server(timeout_sec=20.0):
            print("[브릿지] 팔 액션 서버 연결 실패")
            return False
        # 단일 목표(list[float])면 다중 waypoint와 같은 형태(list[list[float]])로 감싼다.
        if positions and isinstance(positions[0], (int, float)):
            waypoints = [positions]
        else:
            waypoints = positions
        goal = FollowJointTrajectory.Goal()
        goal.trajectory = JointTrajectory()
        goal.trajectory.joint_names = self.config.joint_names
        n = len(waypoints)
        points = []
        for i, wp_positions in enumerate(waypoints, start=1):
            point = JointTrajectoryPoint()
            point.positions = list(wp_positions)
            # int(duration_sec)로 초 단위 이하를 그냥 버리면(예: 4.5 -> 4) 일부러
            # 늦춘 하강 속도가 의도보다 빨라진다 — nanosec까지 정확히 채운다.
            t = duration_sec * i / n
            point.time_from_start = Duration(sec=int(t), nanosec=int(round((t - int(t)) * 1e9)))
            points.append(point)
        goal.trajectory.points = points
        # compute_ik()와 같은 이유(서비스/액션이 discover된 직후 첫 호출이 응답
        # 없이 무한 대기하는 DDS 매칭 레이스)로 타임아웃 없이 걸면 영원히 멈출 수
        # 있어, 목표 전송에도 5초 타임아웃 + 1회 재시도를 둔다.
        goal_handle = None
        for attempt in range(2):
            send_future = self.traj_client.send_goal_async(goal)
            goal_handle = self._wait_for_future(send_future, timeout_sec=5.0)
            if goal_handle is not None:
                break
            print(f"[브릿지] 팔 이동 목표 전송 응답 타임아웃 (attempt {attempt + 1}/2), 재시도")
        if goal_handle is None or not goal_handle.accepted:
            print("[브릿지] 팔 이동 목표 거부됨")
            return False
        # 결과 대기는 실제 물리 이동 시간(duration_sec, 시뮬레이션 시간 기준)만큼
        # 걸리는 게 정상이므로 넉넉히 duration_sec + 15초를 기본 예산으로 두되,
        # real_time_factor가 1.0보다 낮으면(이 환경에서 흔함) 그만큼 벽시계
        # 기준으로 늘려서 컨트롤러가 정상적으로 abort하기 전에 클라이언트가
        # 먼저 포기해버리는 걸 방지한다(_rtf_scaled_timeout 참고).
        result_future = goal_handle.get_result_async()
        action_result = self._wait_for_future(
            result_future, timeout_sec=self._rtf_scaled_timeout(duration_sec + 15.0)
        )
        if action_result is None:
            print("[브릿지] 팔 이동 결과 응답 타임아웃")
            return False
        result = action_result.result
        ok = result.error_code == FollowJointTrajectory.Result.SUCCESSFUL
        if ok:
            self._last_joint_positions = list(waypoints[-1])
        print(f"[브릿지] 팔 이동 완료 (error_code={result.error_code})")
        return ok

    def move_to_pose(self, x, y, z, label="", duration_sec=None, avoid_collisions=True):
        if duration_sec is None:
            duration_sec = self.config.move_duration_sec
        print(f"[브릿지] {label} IK 계산 중 (x={x}, y={y}, z={z})")
        positions = self.compute_ik(x, y, z, avoid_collisions=avoid_collisions)
        if positions is None:
            return False
        return self.move_to_joints(positions, duration_sec=duration_sec)

    def _compute_cartesian_waypoints(self, target_xyz, max_step=0.005):
        """현재 팔 자세(FK는 서비스가 start_state로부터 직접 계산)에서
        target_xyz까지 직선 경로를 여러 관절각 waypoint로 풀어서 반환한다.
        move_to_pose()의 단일 IK 포인트 이동은 두 관절각 사이를 컨트롤러가
        임의로(직선이 아니게) 보간하므로, 하강/상승처럼 물체와 접촉이 걸린
        구간에서 옆으로 흐르며 물체를 밀거나(2번 팔레트 pick 하강) 미는 힘을
        줘 놓는 위치가 틀어지는(컨베이어 place) 문제가 있었다 —
        /compute_cartesian_path는 move_group이 MoveItPy 없이도 직접 제공하는
        서비스라(이 배포판에서 moveit_py 자체는 로드 실패, CLAUDE.md 참고)
        compute_ik와 동일하게 그냥 서비스 호출로 우회 없이 쓸 수 있다.
        fraction이 1.0에 못 미치면(경로 중간에 IK 불가 지점이 있었다는 뜻)
        None을 반환해서 호출부가 기존 단일 포인트 이동으로 폴백하게 한다."""
        if not self.cartesian_client.wait_for_service(timeout_sec=5.0):
            print("[브릿지] /compute_cartesian_path 서비스 연결 실패")
            return None
        req = GetCartesianPath.Request()
        req.header.frame_id = self.config.ik_frame_id
        req.group_name = self.config.ik_group_name
        req.max_step = max_step
        # 하강 끝에서 표면/물체에 바짝 붙는 게 정상이라 다른 스텝과 동일하게 끔.
        req.avoid_collisions = False
        req.start_state.joint_state.name = self.config.joint_names
        req.start_state.joint_state.position = self._last_joint_positions
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = target_xyz
        pose.orientation.x = 1.0
        pose.orientation.y = 0.0
        pose.orientation.z = 0.0
        pose.orientation.w = 0.0
        req.waypoints = [pose]

        future = self.cartesian_client.call_async(req)
        result = self._wait_for_future(future, timeout_sec=10.0)
        if result is None:
            print("[브릿지] compute_cartesian_path 응답 타임아웃")
            return None
        if result.fraction < 0.99:
            print(f"[브릿지] compute_cartesian_path 경로 불완전(fraction={result.fraction:.2f}) — 폴백")
            return None
        traj = result.solution.joint_trajectory
        try:
            name_to_idx = {n: i for i, n in enumerate(traj.joint_names)}
            order = [name_to_idx[n] for n in self.config.joint_names]
        except KeyError as exc:
            print(f"[브릿지] compute_cartesian_path 응답 관절 이름 불일치: {exc}")
            return None
        waypoints = [[pt.positions[i] for i in order] for pt in traj.points]
        if len(waypoints) < 2:
            print("[브릿지] compute_cartesian_path 경로가 너무 짧음 — 폴백")
            return None
        return waypoints

    def move_cartesian(self, x, y, z, label="", duration_sec=None, max_step=0.005):
        """직선 경로로 (x, y, z)까지 이동. 계산 실패 시 기존 단일 포인트
        IK 이동(move_to_pose)으로 조용히 폴백한다."""
        if duration_sec is None:
            duration_sec = self.config.move_duration_sec
        print(f"[브릿지] {label} 직선 경로 계산 중 (x={x}, y={y}, z={z})")
        waypoints = self._compute_cartesian_waypoints((x, y, z), max_step=max_step)
        if waypoints is None:
            print(f"[브릿지] {label} 직선 경로 실패 — 단일 목표 이동으로 폴백")
            return self.move_to_pose(x, y, z, label=label, duration_sec=duration_sec, avoid_collisions=False)
        return self.move_to_joints(waypoints, duration_sec=duration_sec)

    def set_gripper(self, position, label=""):
        if not self.gripper_client.wait_for_server(timeout_sec=20.0):
            print(f"[브릿지] 그리퍼 액션 서버 연결 실패 "
                  f"({self.config.gripper_action} 이름이 맞는지 "
                  "'ros2 action list | grep gripper'로 확인 필요)")
            return False
        goal = ParallelGripperCommand.Goal()
        goal.command = JointState()
        goal.command.name = [self.config.gripper_joint_name]
        goal.command.position = [position]
        goal.command.effort = [self.config.gripper_max_effort]
        # move_to_joints()와 동일한 이유로 타임아웃 + 재시도를 둔다.
        goal_handle = None
        for attempt in range(2):
            send_future = self.gripper_client.send_goal_async(goal)
            goal_handle = self._wait_for_future(send_future, timeout_sec=5.0)
            if goal_handle is not None:
                break
            print(f"[브릿지] 그리퍼 목표 전송 응답 타임아웃 (attempt {attempt + 1}/2), 재시도")
        if goal_handle is None or not goal_handle.accepted:
            print(f"[브릿지] 그리퍼 목표 거부됨 ({label})")
            return False
        result_future = goal_handle.get_result_async()
        result = self._wait_for_future(result_future, timeout_sec=self._rtf_scaled_timeout(10.0))
        if result is None:
            print(f"[브릿지] 그리퍼 결과 응답 타임아웃 ({label})")
            return False
        print(f"[브릿지] 그리퍼 {label} 완료")
        return True

    def _gz_topic_pub_empty(self, topic, timeout_sec=3.0):
        """gz transport 토픽에 빈 메시지(gz.msgs.Empty)를 한 번 쏜다.
        DetachableJoint의 attach/detach 토픽은 ROS2 토픽이 아니라 Gazebo
        Transport 토픽이라(ros_gz_bridge 매핑 안 돼 있음) gz CLI를 서브프로세스로
        직접 호출한다 — _get_object_pose()가 물체 위치를 읽을 때 쓰는 것과
        동일한 방식(GZ_PARTITION 등 환경변수는 이 프로세스가 물려받은 걸
        서브프로세스도 그대로 상속하므로 듀얼 로봇 모드에서도 각자 자기
        world만 본다)."""
        try:
            proc = subprocess.run(
                ["gz", "topic", "-t", topic, "-m", "gz.msgs.Empty", "-p", "unused: true"],
                capture_output=True, text=True, timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"[브릿지] gz topic 발행 실패({topic}): {exc}")
            return False
        if proc.returncode != 0:
            print(f"[브릿지] gz topic 발행 실패({topic}, returncode={proc.returncode}): "
                  f"{proc.stderr.strip()[:200]}")
            return False
        return True

    def _get_object_pose(self, model_name, timeout_sec=5.0):
        """gz transport로 /world/{world}/pose/info를 1개 스냅샷 조회해서
        model_name의 world-frame (x, y, z)를 반환. ROS2 토픽이 아니라 gz CLI를
        서브프로세스로 부르는 이유는, 이 물체들이 로봇 쪽 ros_gz_bridge
        설정에 안 걸려 있고(카메라/시계만 브릿지됨) 검증 하나 때문에
        launch 파일(브릿지 설정)까지 바꾸는 대신 필요할 때만 값을 찍어보는
        방식이 더 가볍기 때문. GZ_PARTITION 등 gz transport 관련 환경변수는
        이 프로세스(taskplan_bridge)가 물려받은 걸 서브프로세스도 그대로
        상속하므로 듀얼 로봇 모드에서도 각자 자기 world만 본다."""
        try:
            proc = subprocess.run(
                ["gz", "topic", "-e", "-t", f"/world/{GZ_WORLD_NAME}/pose/info",
                 "-n", "1", "--json-output"],
                capture_output=True, text=True, timeout=timeout_sec,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            print(f"[브릿지] '{model_name}' 위치 조회 실패(gz topic 실행 오류): {exc}")
            return None
        if proc.returncode != 0 or not proc.stdout.strip():
            print(f"[브릿지] '{model_name}' 위치 조회 실패 "
                  f"(returncode={proc.returncode}, stderr={proc.stderr.strip()[:200]})")
            return None
        # "-n 1"을 줘도 gz topic이 타이밍에 따라 메시지를 2개 이어붙여
        # 찍는 경우가 실측으로 확인됨(json.loads가 "Extra data" 에러).
        # raw_decode로 앞쪽의 완전한 JSON 값 하나만 파싱하고 나머지는 버린다.
        try:
            data, _end = json.JSONDecoder().raw_decode(proc.stdout.strip())
        except json.JSONDecodeError as exc:
            print(f"[브릿지] '{model_name}' 위치 조회 실패(JSON 파싱 오류): {exc}")
            return None
        for entry in data.get("pose", []):
            if entry.get("name") == model_name:
                pos = entry.get("position", {})
                return pos.get("x", 0.0), pos.get("y", 0.0), pos.get("z", 0.0)
        print(f"[브릿지] pose/info에서 '{model_name}' 모델을 찾지 못함")
        return None

    def _get_object_z(self, model_name, timeout_sec=5.0):
        pose = self._get_object_pose(model_name, timeout_sec=timeout_sec)
        return pose[2] if pose is not None else None

    def _is_object_lifted(self, model_name, resting_z):
        """grasp 직후 hover 높이로 복귀한 상태에서 물체가 실제로 그리퍼를
        따라 들렸는지 확인. 힘/토크 센서가 없어 관절 쪽에서는 무게를 전혀
        못 느끼므로(안전_가드/컨트롤러 어느 쪽도 effort 인터페이스 없음),
        물체 자체의 world 좌표 z를 안착 높이와 비교하는 방식으로 대신한다
        — 마찰만으로 못 붙잡으면 팔이 올라가도 물체는 안착 높이 그대로 남아
        있으므로("무게가 0으로 느껴짐"과 동일한 증상) 이걸로 충분히 구분됨."""
        z = self._get_object_z(model_name)
        if z is None:
            # 조회 자체가 실패하면(gz CLI 없음/타임아웃 등) 판정 불가 상태이므로,
            # 검증 로직이 없던 기존 동작(액션 성공=pick 성공)으로 안전하게
            # 폴백한다 — 못 살펴봤다고 무한 재시도에 빠지면 안 되기 때문.
            print(f"[브릿지] '{model_name}' 높이 확인 불가 — grasp 검증 생략(성공으로 간주)")
            return True
        lifted = z >= resting_z + GRASP_LIFT_THRESHOLD_M
        print(f"[브릿지] '{model_name}' 현재 z={z:.4f} "
              f"(안착 z={resting_z:.3f}, 판정 기준={resting_z + GRASP_LIFT_THRESHOLD_M:.3f}) "
              f"-> {'들림(무게 감지)' if lifted else '안 들림(무게 0)'}")
        return lifted

    # ---------- 스킬 실행 ----------

    def exec_home(self, args):
        print("[스킬] home")
        return self.move_to_joints(self.config.ready_pose)

    def exec_move(self, args):
        target = args["target"]
        if target not in self.config.location_poses:
            print(f"[스킬] move 실패: 알 수 없는 위치 '{target}'")
            return False
        x, y, hover_z, _ = self.config.location_poses[target]
        print(f"[스킬] move -> {target}")
        return self.move_to_pose(x, y, hover_z, label=f"'{target}' 상공으로 이동")

    def exec_pick(self, args, max_attempts=3):
        obj = args.get("object")
        loc = args.get("from")
        print(f"[스킬] pick <- {loc} ({obj})")
        if loc not in self.config.location_poses:
            print(f"[스킬] pick 실패: 알 수 없는 위치 '{loc}'")
            return False
        x, y, hover_z, grasp_z = self.config.location_poses[loc]
        model_name = OBJECT_MODEL_MAP.get(obj)
        if model_name is None:
            print(f"[브릿지] '{obj}'는 grasp 검증용 모델 매핑이 없어 무게 확인 없이 진행합니다.")
        # location_poses의 (x, y)는 사람이 world sdf 스폰 위치에 맞춰 박아둔
        # 값이라, 물체가 (이전 pick 시도로 밀렸거나 등) 실제로 조금이라도
        # 벗어나 있으면 그대로 허공을 집게 된다. scene_camera로 실제 위치를
        # 추정해서 있으면 그 좌표를 쓰고, 못 찾으면(화면 밖/가려짐) 하드코딩
        # 좌표로 폴백한다. 팔이 그 자리로 내려가면 그리퍼가 카메라 시야를
        # 가려버리므로(실측으로 확인됨 — 접근한 뒤엔 vision이 못 찾음),
        # 반드시 팔을 움직이기 "전에" 먼저 봐야 한다. pick_x/pick_y는 이후
        # 재시도에서도 유지되는 "최선으로 아는 위치"로, 나중 시도에서 가려져
        # 다시 못 찾더라도 하드코딩 값으로 되돌리지 않고 이전에 알아낸 값을
        # 그대로 쓴다.
        pick_x, pick_y = x, y
        vision_xy = self._locate_object(obj, resting_z=grasp_z) if model_name else None
        if vision_xy is not None:
            pick_x, pick_y = vision_xy
            print(f"[브릿지] vision으로 '{obj}' 위치 보정: 하드코딩({x:.3f},{y:.3f}) "
                  f"-> 실측({pick_x:.3f},{pick_y:.3f})")
        # 이전 스텝에서 그리퍼가 어떤 상태였는지 모르니(전에 place를 안 했을
        # 수도 있음), 접근 전에 확실히 완전히 열어둔다 — 안 그러면 손가락이
        # 덜 벌어진 채로 접근하다가 물체를 밀어버릴 수 있다.
        if not self.set_gripper(self.config.gripper_open, label="열기(접근 전 확보)"):
            return False
        if not self.move_to_pose(pick_x, pick_y, hover_z, label=f"'{loc}' 상공으로 접근"):
            return False

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # 재시도 전이라 그리퍼를 막 열고 hover로 올라온 상태 —
                # 아까보단 시야가 트여 있을 수 있으니 한 번 더 시도. 여기서도
                # 못 찾으면(여전히 팔에 가려짐 등) pick_x/pick_y를 직전 값
                # 그대로 유지한다(하드코딩으로 되돌리지 않음).
                vision_xy = self._locate_object(obj, resting_z=grasp_z) if model_name else None
                if vision_xy is not None:
                    print(f"[브릿지] vision 재확인: ({pick_x:.3f},{pick_y:.3f}) "
                          f"-> ({vision_xy[0]:.3f},{vision_xy[1]:.3f})")
                    pick_x, pick_y = vision_xy
            # 상공 -> 집기 높이 하강은 단일 IK 포인트 이동(move_to_pose)이면
            # 컨트롤러가 두 관절각 사이를 임의로(직선이 아니게) 보간해서
            # 물체를 스치고 지나가며 밀어버릴 위험이 있었다(2번 팔레트에서
            # 실측 재현). move_cartesian으로 실제 직선 경로를 여러 waypoint로
            # 풀어서 내려가면 이 옆방향 흔들림 자체가 없어진다(계산 실패 시
            # 기존 단일 포인트 방식으로 자동 폴백). duration은 그대로 늘려서
            # 천천히(=충격 작게) 내려가게 했다. avoid_collisions=False: 집는
            # 순간엔 그리퍼가 물체·받침대 표면에 바짝 붙는 게 정상이라, 다른
            # 로봇(예: UR5e+Robotiq)은 이 근접 접촉이 self/environment 충돌로
            # 오인돼 NO_IK_SOLUTION이 났었다 — 실제 로봇 pick/place에서도 흔히
            # 쓰는 방식대로 하강 단계에서만 충돌 검사를 완화한다.
            if not self.move_cartesian(pick_x, pick_y, grasp_z, label=f"'{loc}' 집기 높이로 하강 (시도 {attempt}/{max_attempts})", duration_sec=4.5):
                return False
            if not self.set_gripper(self.config.gripper_close, label="닫기(집기)"):
                return False

            # [2026-09-09] use_detachable_joint 로봇(UR5e)은 여기서 그리퍼
            # 베이스에 물체를 강체로 붙인다 — 기획서가 "파지 물리는 검증
            # 대상이 아님"을 명시하고 있어서(물건이 미끄러지는지/관성으로
            # 흔들리는지는 검증 범위 밖), Panda에서 이틀간 시도하고 결국
            # 근본 해결 못한 마찰 그립(position_proportional_gain 튜닝,
            # effort 인터페이스 등, CLAUDE.md 참고)을 반복할 필요가 없다는
            # 판단. Panda는 계속 순수 마찰(mu=1.8)로 간다(아래 검증 로직이
            # 그대로 적용됨).
            if self.config.use_detachable_joint and model_name is not None:
                self._gz_topic_pub_empty(f"/{model_name}/attach")

            # 붙잡은(또는 Panda라면 마찰로 쥔) 상태로 올라가는 구간 — 경로가
            # 옆으로 흔들리면 그리퍼 안에서 물체가 미끄러져(중심이 어긋나)
            # place 시 놓는 위치가 틀어질 수 있어 이동도 직선 경로로 바꾼다.
            if not self.move_cartesian(pick_x, pick_y, hover_z, label=f"'{loc}' 상공으로 복귀"):
                return False

            if model_name is None:
                return True

            # 힘/토크 센서가 없어서(robot_config.py 상단 주석) 관절 쪽에서는
            # 무게를 전혀 못 느끼므로, 물체 자체의 world 좌표로 대신 확인.
            # DetachableJoint 로봇은 위에서 이미 강체로 붙였으니 사실상 항상
            # 통과하지만, 붙이는 토픽 자체가 실패했을 경우까지 마지막
            # 안전망으로 그대로 둔다.
            if self._is_object_lifted(model_name, resting_z=grasp_z):
                return True

            if attempt < max_attempts:
                print(f"[브릿지] pick 재시도 {attempt}/{max_attempts} — 다시 내려가서 집기 시도")
                if not self.set_gripper(self.config.gripper_open, label="열기(재시도 전 놓기)"):
                    return False

        print(f"[브릿지] pick 실패 — {max_attempts}번 시도했지만 '{obj}'를 들어올리지 못함(무게 미검출)")
        return False

    def exec_place(self, args):
        obj = args.get("object")
        loc = args.get("to")
        print(f"[스킬] place -> {loc} ({obj})")
        if loc not in self.config.location_poses:
            print(f"[스킬] place 실패: 알 수 없는 위치 '{loc}'")
            return False
        x, y, hover_z, grasp_z = self.config.location_poses[loc]
        if not self.move_to_pose(x, y, hover_z, label=f"'{loc}' 상공으로 접근"):
            return False
        # pick과 동일한 이유(단일 IK 포인트 이동은 직선 보간을 보장 안 함)로
        # 놓기 하강도 move_cartesian으로 바꿔서, 물체를 쥔 채로 목표 (x, y)
        # 바로 위에서 곧장 수직으로 내려가게 한다 — 하강 중 옆으로 흐르며
        # 그리퍼 안에서 물체가 밀리는 걸 막아 실제 놓이는 위치가 목표에서
        # 크게 벗어나는 문제(컨베이어에서 x로 14cm 어긋남 실측)를 줄인다.
        if not self.move_cartesian(x, y, grasp_z, label=f"'{loc}' 놓기 높이로 하강", duration_sec=4.5):
            return False
        if not self.set_gripper(self.config.gripper_open, label="열기(놓기)"):
            return False
        # exec_pick과 대칭 — DetachableJoint 로봇(UR5e)은 여기서 그리퍼
        # 베이스에 붙여둔 물체를 뗀다.
        model_name = OBJECT_MODEL_MAP.get(obj)
        if self.config.use_detachable_joint and model_name is not None:
            self._gz_topic_pub_empty(f"/{model_name}/detach")
        # 놓은 직후 복귀도 직선 경로로 — 옆으로 흔들리며 방금 놓은 물체를
        # 다시 쳐서 더 밀어버리는 걸 막는다.
        return self.move_cartesian(x, y, hover_z, label=f"'{loc}' 상공으로 복귀")

    def exec_stop(self, args):
        print("[스킬] stop — 남은 스텝 실행 중단")
        self._stopped = True
        return True

    def run_plan(self, task_plan: dict):
        # 중요: _stopped는 exec_stop()이 한 번 True로 세팅하면 계속 True로
        # 남는다. 이 노드(특히 api_server.py처럼 서버 생명주기 동안 하나의
        # 객체를 재사용하는 경우)가 이전에 stop 스킬이 포함된 계획을 한 번이라도
        # 실행했다면, 리셋 안 해줄 경우 이후의 모든 계획이 스텝 0에서 곧바로
        # 멈춰버린다. 그래서 매 실행 시작 시 항상 초기화한다.
        self._stopped = False
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
    parser.add_argument("--robot", default="ur5e", choices=list(ROBOT_CONFIGS),
                         help="실행할 로봇 (기본값: ur5e)")
    cli_args = parser.parse_args()

    if cli_args.utterance:
        task_plan = load_plan_from_utterance(cli_args.utterance)
    else:
        with open(cli_args.plan_file, "r", encoding="utf-8") as f:
            task_plan = json.load(f)

    rclpy.init()
    executor = TaskPlanExecutor(ROBOT_CONFIGS[cli_args.robot])
    try:
        executor.run_plan(task_plan)
    finally:
        executor.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
