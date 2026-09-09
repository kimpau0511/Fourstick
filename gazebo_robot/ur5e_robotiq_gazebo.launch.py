"""UR5e + Robotiq 2F-85 그리퍼를 Gazebo(pallets_world.sdf)에 스폰하고
ros2_control로 팔 관절 + 그리퍼 개폐를 검증하기 위한 launch 파일.

ur5e_gazebo.launch.py(Phase 1)와 같은 구조 — 벤더 launch 파일을 그대로
include하는 대신 각 노드를 직접 선언한다 (Phase 1에서 벤더 include 방식이
controller_manager 파라미터 전달 버그를 만난 뒤 이 방식으로 정착함).

[버그 우회] 기본 물리엔진(dartsim)은 URDF <mimic> 조인트의 물리 제약을
지원하지 않는다("physics engine does not support mimic constraints" 경고,
gz-physics 9.4.0 확인). 그 결과 Robotiq 그리퍼의 mimic 관절 5개가 마스터
관절(robotiq_85_left_knuckle_joint)을 따라가지 못해 손가락 링크들이 서로
충돌/간섭하면서 그리퍼 자체가 못 움직이는 문제가 있었다(gripper_cmd 액션이
항상 stalled=true, reached_goal=false). `--physics-engine
gz-physics-bullet-featherstone-plugin`으로 물리엔진을 바꿔서 해결함 —
mimic 제약을 지원하는 엔진이라 경고 자체가 사라지고, 그리퍼가 정상적으로
닫히고(mimic 관절들도 실제로 같이 움직임) 열린다. 공유 월드 파일
(pallets_world.sdf, Panda도 씀)은 건드리지 않고 이 launch 파일에서만
`gz_args`로 엔진을 오버라이드하므로 Panda 쪽엔 영향 없음.

Phase 2a(그리퍼 스폰 + 개폐 검증) 완료 — 팔 제어, 그리퍼 개폐(mimic 관절
동기화 포함) 모두 확인됨. Task Plan 파이프라인 연동(Phase 2b)은 아직 없음.

사용:
  source /opt/ros/lyrical/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  export GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/ros/lyrical/lib
  ros2 launch /home/asd/forstick/gazebo_robot/ur5e_robotiq_gazebo.launch.py
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, RegisterEventHandler
from launch.conditions import IfCondition, UnlessCondition
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare
from moveit_configs_utils import MoveItConfigsBuilder

UR5E_ROBOTIQ_XACRO = "/home/asd/forstick/gazebo_robot/ur5e_robotiq_gazebo.urdf.xacro"
WORLD_SDF = "/home/asd/forstick/gazebo_worlds/pallets_world.sdf"
CONTROLLERS_YAML = "/home/asd/forstick/gazebo_robot/ur5e_controllers.yaml"


def generate_launch_description():
    robot_description_content = Command(
        [
            PathJoinSubstitution([FindExecutable(name="xacro")]),
            " ",
            UR5E_ROBOTIQ_XACRO,
            " ",
            "name:=ur5e",
            " ",
            "ur_type:=ur5e",
            " ",
            "simulation_controllers:=",
            CONTROLLERS_YAML,
        ]
    )
    robot_description = {"robot_description": robot_description_content}

    node_robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[robot_description],
    )

    static_tf_node = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="static_transform_publisher",
        output="log",
        arguments=["--frame-id", "world", "--child-frame-id", "base_link"],
    )

    gz_spawn_entity = Node(
        package="ros_gz_sim",
        executable="create",
        output="screen",
        arguments=[
            "-topic",
            "robot_description",
            "-name",
            "ur5e_robotiq",
            "-allow_renaming",
            "true",
        ],
    )

    joint_state_broadcaster_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_state_broadcaster"],
    )

    joint_trajectory_controller_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_trajectory_controller", "-p", CONTROLLERS_YAML],
    )

    gripper_controller_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["gripper_controller", "-p", CONTROLLERS_YAML],
    )

    bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        arguments=[
            "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock",
        ],
        output="screen",
    )

    gazebo_gui_arg = DeclareLaunchArgument(
        "gazebo_gui", default_value="true", description="Start gazebo with GUI?"
    )

    # move_group — taskplan_bridge.py의 /compute_ik 호출에 필요.
    # Panda(panda_gazebo_moveit.launch.py)와 같은 이유로
    # publish_robot_description(_semantic)=False: 켜두면 move_group의
    # planning_scene_monitor가 자기 robot_description을 다시 퍼블리시해서
    # robot_state_publisher가 이미 낸 (그리퍼 포함) 올바른 URDF를 덮어써버림.
    moveit_config = (
        MoveItConfigsBuilder(robot_name="ur", package_name="ur_moveit_config")
        .robot_description(
            file_path=UR5E_ROBOTIQ_XACRO,
            mappings={
                "name": "ur5e",
                "ur_type": "ur5e",
                "simulation_controllers": CONTROLLERS_YAML,
            },
        )
        .robot_description_semantic(
            file_path="/home/asd/forstick/gazebo_robot/ur5e_robotiq.srdf.xacro",
            mappings={"name": "ur5e"},
        )
        .robot_description_kinematics(file_path="config/kinematics.yaml")
        .trajectory_execution(file_path="config/moveit_controllers.yaml")
        .planning_scene_monitor(
            publish_robot_description=False, publish_robot_description_semantic=False
        )
        .planning_pipelines(pipelines=["ompl"])
        .to_moveit_configs()
    )
    move_group_node = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=[moveit_config.to_dict()],
        arguments=["--ros-args", "--log-level", "info"],
    )

    return LaunchDescription(
        [
            gazebo_gui_arg,
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [PathJoinSubstitution([FindPackageShare("ros_gz_sim"), "launch", "gz_sim.launch.py"])]
                ),
                launch_arguments=[("gz_args", f"-r -v 1 --physics-engine gz-physics-bullet-featherstone-plugin {WORLD_SDF}")],
                condition=IfCondition(LaunchConfiguration("gazebo_gui")),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [PathJoinSubstitution([FindPackageShare("ros_gz_sim"), "launch", "gz_sim.launch.py"])]
                ),
                launch_arguments=[("gz_args", f"-s -r -v 1 --physics-engine gz-physics-bullet-featherstone-plugin {WORLD_SDF}")],
                condition=UnlessCondition(LaunchConfiguration("gazebo_gui")),
            ),
            RegisterEventHandler(
                event_handler=OnProcessExit(
                    target_action=gz_spawn_entity,
                    on_exit=[joint_state_broadcaster_spawner],
                )
            ),
            RegisterEventHandler(
                event_handler=OnProcessExit(
                    target_action=joint_state_broadcaster_spawner,
                    on_exit=[joint_trajectory_controller_spawner, gripper_controller_spawner],
                )
            ),
            bridge,
            static_tf_node,
            node_robot_state_publisher,
            gz_spawn_entity,
            move_group_node,
        ]
    )
