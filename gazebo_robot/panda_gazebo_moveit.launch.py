"""
panda_gazebo.launch.py에 move_group을 추가한 버전.
move_group은 moveit_py를 거치지 않고 순정 C++ 노드로 띄우므로
(RViz 데모에서 계속 잘 됐던 것과 동일한 방식) 플래닝 파이프라인
로딩 버그를 겪지 않는다. 목적은 /compute_ik 서비스 사용.

[버그 수정] move_group의 planning_scene_monitor가
publish_robot_description=True로 인해 자기 mock_components 버전
URDF를 /robot_description 토픽에 재퍼블리시하면서, 우리의 Gazebo용
robot_state_publisher가 퍼블리시한 올바른 URDF를 덮어써버리는
문제가 있었음. controller_manager가 결국 mock URDF(PandaFakeSystem,
mock_components/GenericSystem)를 받아서 gz_ros2_control 플러그인
로딩이 실패했음. publish_robot_description(_semantic)을 꺼서 해결.

[추가] scene_camera 토픽(/scene_camera/image)을 ROS2로 브릿지해서
데모 페이지에서 실시간으로 볼 수 있게 함.
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

PANDA_GAZEBO_XACRO = "/home/asd/forstick/gazebo_robot/panda_gazebo.urdf.xacro"
WORLD_SDF = "/home/asd/forstick/gazebo_worlds/pallets_world.sdf"
# [수정, 2026-09-08] 원본 외부 패키지 yaml 대신 프로젝트 내 사본
# (position_proportional_gain 추가됨 — 그리퍼가 실제 힘을 내도록 함) 사용.
ROS2_CONTROLLERS_YAML = "/home/asd/forstick/gazebo_robot/panda_ros2_controllers.yaml"


def generate_launch_description():
    robot_description_content = Command(
        [
            PathJoinSubstitution([FindExecutable(name="xacro")]),
            " ",
            PANDA_GAZEBO_XACRO,
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
        arguments=["--frame-id", "world", "--child-frame-id", "panda_link0"],
    )

    gz_spawn_entity = Node(
        package="ros_gz_sim",
        executable="create",
        output="screen",
        arguments=["-topic", "robot_description", "-name", "panda", "-allow_renaming", "true"],
    )

    joint_state_broadcaster_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["joint_state_broadcaster"],
    )

    panda_arm_controller_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["panda_arm_controller", "-p", ROS2_CONTROLLERS_YAML],
    )

    panda_hand_controller_spawner = Node(
        package="controller_manager",
        executable="spawner",
        arguments=["panda_hand_controller", "-p", ROS2_CONTROLLERS_YAML],
    )

    bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        arguments=[
            "/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock",
            "/scene_camera/image@sensor_msgs/msg/Image[gz.msgs.Image",
        ],
        output="screen",
    )

    moveit_config = (
        MoveItConfigsBuilder("moveit_resources_panda")
        .robot_description(
            file_path="config/panda.urdf.xacro",
            mappings={"ros2_control_hardware_type": "mock_components"},
        )
        .robot_description_semantic(file_path="config/panda.srdf")
        .planning_scene_monitor(
            publish_robot_description=False, publish_robot_description_semantic=False
        )
        .trajectory_execution(file_path="config/gripper_moveit_controllers.yaml")
        .planning_pipelines(
            pipelines=["ompl", "chomp", "pilz_industrial_motion_planner", "stomp"]
        )
        .to_moveit_configs()
    )
    move_group_node = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=[moveit_config.to_dict()],
        arguments=["--ros-args", "--log-level", "info"],
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
                    on_exit=[panda_arm_controller_spawner, panda_hand_controller_spawner],
                )
            ),
            bridge,
            static_tf_node,
            node_robot_state_publisher,
            gz_spawn_entity,
            move_group_node,
        ]
    )
