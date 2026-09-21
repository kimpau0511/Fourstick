"""로봇별 설정 — taskplan_bridge.TaskPlanExecutor가 여러 로봇(UR5e, 추후 FR3-WMS...)을
같은 스킬 실행 로직(exec_home/move/pick/place/stop, run_plan)으로 다룰 수 있게
하는 어댑터 설정. 로봇마다 다른 건 전부 여기 값으로만 표현한다 — 관절 이름,
그리퍼 열림/닫힘 값, 컨트롤러 액션 이름, IK 플래닝 그룹/기준 프레임, 위치 좌표.

새 로봇을 추가하려면: 이 파일에 RobotConfig 인스턴스를 하나 더 만들고
ROBOT_CONFIGS에 등록하면 된다 — taskplan_bridge.py/api_server.py는
robot_id 문자열로만 다루므로 다른 코드를 고칠 필요는 없다.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RobotConfig:
    robot_id: str
    label: str
    joint_names: list
    ready_pose: list
    gripper_joint_name: str
    gripper_open: float
    gripper_close: float
    gripper_max_effort: float
    arm_action: str
    gripper_action: str
    ik_group_name: str
    ik_frame_id: str
    # {위치 이름: (x, y, hover_z, grasp_z)} — world 프레임 좌표. 두 로봇 다
    # world 원점(0,0,0)에 스폰되므로 같은 좌표를 그대로 공유할 수 있다.
    location_poses: dict = field(default_factory=dict)
    # move/pick/place의 상공 이동(hover approach/복귀)에 쓰는 기본 이동 시간.
    # 관절 공간 이동거리가 큰 로봇은 짧으면 controller의 path tolerance를
    # 넘겨서(FollowJointTrajectory error_code=-4) 실패할 수 있다.
    move_duration_sec: float = 3.0
    # [2026-09-09] True면 exec_pick/exec_place가 마찰 그립 대신 DetachableJoint로
    # 물체를 그리퍼에 강체로 붙였다 뗀다 — 기획서가 "파지 물리는 검증 대상이
    # 아님"을 명시하고 있어서(물건이 미끄러지는지/관성으로 흔들리는지는 검증
    # 범위 밖), 마찰 기반 그립(position_proportional_gain 튜닝 등, Panda에서
    # 이틀간 시도하고 결국 근본 해결 못 함)을 굳이 재현할 필요가 없다는 판단.
    # 해당 로봇의 xacro에 `<child_model>`별 DetachableJoint 플러그인이
    # 선언돼 있어야 함(ur5e_robotiq_gazebo.urdf.xacro 참고).
    use_detachable_joint: bool = False


UR5E_CONFIG = RobotConfig(
    robot_id="ur5e",
    label="UR5e + Robotiq 2F-85",
    joint_names=[
        "shoulder_pan_joint", "shoulder_lift_joint", "elbow_joint",
        "wrist_1_joint", "wrist_2_joint", "wrist_3_joint",
    ],
    # 로봇 스폰 시 기본 초기 자세(ur_ros2_control 매크로 initial_positions)와
    # 동일 — home 스킬 및 IK seed로 사용.
    ready_pose=[0.0, -1.57, 0.0, -1.57, 0.0, 0.0],
    # robotiq_85_left_knuckle_joint 기준, limit [0.0(열림), 0.8(닫힘)] —
    # Panda와 반대 방향 값 관계(Panda는 큰 값이 열림)라 gripper_open/close를
    # 그대로 뒤바꿔서 넣으면 됨 (exec_pick/place 코드는 로봇-무관).
    gripper_joint_name="robotiq_85_left_knuckle_joint",
    gripper_open=0.0,
    gripper_close=0.8,
    gripper_max_effort=20.0,
    arm_action="/joint_trajectory_controller/follow_joint_trajectory",
    gripper_action="/gripper_controller/gripper_cmd",
    ik_group_name="ur_manipulator",
    ik_frame_id="base_link",
    # Panda와 완전히 같은 world-frame 좌표. compute_ik로 직접 검증함(둘 다
    # world 원점 스폰) — hover 높이는 그대로 도달 가능, grasp 높이는 팔레트
    # 표면과의 충돌 검사를 완화해야 풀림 (taskplan_bridge.py의 pick/place
    # 하강 단계 주석 참고).
    location_poses={
        "1번 팔레트": (0.4, 0.3, 0.30, 0.118),
        "2번 팔레트": (0.5, 0.0, 0.30, 0.118),
        "3번 팔레트": (0.4, -0.3, 0.30, 0.118),
        "컨베이어": (0.0, 0.55, 0.40, 0.218),
    },
    # UR5e는 ready_pose 기준으로 팔레트/컨베이어까지 관절 공간 이동거리가
    # Panda보다 커서(예: wrist_1이 2.8rad 가까이 움직임) 3초로는
    # controller의 path tolerance(±0.2rad)를 넘겨 실패했다 — 5초로 완화.
    move_duration_sec=5.0,
    # [2026-09-09] 기획서상 실제 검증 대상 로봇 — DetachableJoint로 그립
    # (ur5e_robotiq_gazebo.urdf.xacro에 material_a/b/c별 플러그인 선언됨).
    use_detachable_joint=True,
)

ROBOT_CONFIGS = {"ur5e": UR5E_CONFIG}
