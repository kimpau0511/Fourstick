"""UR5e를 Gazebo(pallets_world.sdf)에 스폰하고 ros2_control로 관절을 움직일 수
있는지 검증하기 위한 launch 파일.

panda_gazebo.launch.py와 같은 구조: 로봇 설명(URDF)/컨트롤러 설정은 로컬에 새로
만들지 않고 apt로 설치한 ur_description/ur_simulation_gz 패키지를 그대로
참조한다.

Phase 1(모델 스폰 + 관절 제어 검증)만 다룬다 — MoveIt, Task Plan 파이프라인
연동은 아직 없음 (taskplan_bridge.py는 여전히 Panda 전용이고 이 launch 파일과
무관).

[버그 우회] 설치된 ur_simulation_gz가 제공하는 ur_sim_control.launch.py를 그대로
include했더니, controller_manager가 joint_trajectory_controller 노드를
초기화할 때 "Length of parameter 'joints' is '0'"로 실패했다 — Panda 쪽에서
이미 겪은 것과 같은 종류의 버그(controller_manager가 컨트롤러 노드에 YAML을
제대로 못 넘김, CLAUDE.md 기록)로 보인다. Panda와 동일하게 우회: 컨트롤러
스포너에 `-p <yaml경로>`를 직접 지정.
[버그] 설치된 ros-lyrical-ur-controllers 6.0.0의 controller_plugins.xml에
ur_controllers/ScaledJointTrajectoryController 클래스 등록이 빠져있어서, UR
기본 컨트롤러 대신 표준 joint_trajectory_controller/JointTrajectoryController를
쓴다.

사용:
  source /opt/ros/lyrical/setup.bash
  export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
  export GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/ros/lyrical/lib
  ros2 launch /home/asd/forstick/gazebo_robot/ur5e_gazebo.launch.py
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription, RegisterEventHandler
from launch.conditions import IfCondition, UnlessCondition
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import Command, FindExecutable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare

UR_GZ_XACRO = "/opt/ros/lyrical/share/ur_simulation_gz/urdf/ur_gz.urdf.xacro"
WORLD_SDF = "/home/asd/forstick/gazebo_worlds/pallets_world.sdf"
UR_CONTROLLERS_YAML = "/opt/ros/lyrical/share/ur_simulation_gz/config/ur_controllers.yaml"


def generate_launch_description():
    robot_description_content = Command(
        [
            PathJoinSubstitution([FindExecutable(name="xacro")]),
            " ",
            UR_GZ_XACRO,
            " ",
            "name:=ur5e",
            " ",
            "ur_type:=ur5e",
            " ",
            "simulation_controllers:=",
            UR_CONTROLLERS_YAML,
        ]
    )
    robot_description = {"robot_description": robot_description_content}

    node_robot_state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[robot_description],
    )

    # ur_gz.urdf.xacro 안에서 이미 world->base_link 고정 조인트를 만들어 물리적으로
    # 고정하지만, TF 쪽 static_transform_publisher도 Panda와 동일하게 별도로 둔다.
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
        arguments=["-topic", "robot_description", "-name", "ur5e", "-allow_renaming", "true"],
    )

    joint_state_broadcaster_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_state_broadcaster"],
    )

    joint_trajectory_controller_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_trajectory_controller", "-p", UR_CONTROLLERS_YAML],
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

    return LaunchDescription(
        [
            gazebo_gui_arg,
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [PathJoinSubstitution([FindPackageShare("ros_gz_sim"), "launch", "gz_sim.launch.py"])]
                ),
                launch_arguments=[("gz_args", f"-r -v 1 {WORLD_SDF}")],
                condition=IfCondition(LaunchConfiguration("gazebo_gui")),
            ),
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    [PathJoinSubstitution([FindPackageShare("ros_gz_sim"), "launch", "gz_sim.launch.py"])]
                ),
                launch_arguments=[("gz_args", f"-s -r -v 1 {WORLD_SDF}")],
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
                    on_exit=[joint_trajectory_controller_spawner],
                )
            ),
            bridge,
            static_tf_node,
            node_robot_state_publisher,
            gz_spawn_entity,
        ]
    )
