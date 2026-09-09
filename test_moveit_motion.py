"""
MoveItPy를 이용해서 사람이 RViz에서 드래그하지 않고, 코드로 Panda 팔을
움직여보는 테스트. 이게 되면 Task Plan JSON -> MoveIt2 실행 브릿지의
핵심 메커니즘이 검증된 것.

MoveItConfigsBuilder().to_dict()를 config_dict로 바로 넘기면 이번 Lyrical
릴리스에서 플래닝 파이프라인 파라미터가 제대로 안 실리는 문제가 있어서,
실제 yaml 파일로 써서 launch_params_filepaths로 넘기는 방식으로 우회한다.

사전 조건: 데모가 이미 떠 있어야 함 (다른 터미널에서 계속 실행 중이어야 함)
  ros2 launch ~/forstick/ros2_patches/demo_launch_patched.py

실행: python3 test_moveit_motion.py
"""

import tempfile
import time

import rclpy
import yaml
from moveit.planning import MoveItPy
from moveit_configs_utils import MoveItConfigsBuilder


def move_to(panda_arm, name: str) -> bool:
    print(f"[테스트] 현재 상태 -> '{name}' 자세로 이동")
    panda_arm.set_start_state_to_current_state()
    panda_arm.set_goal_state(configuration_name=name)
    plan_result = panda_arm.plan()
    if not plan_result:
        print(f"[테스트] '{name}' 플래닝 실패")
        return False
    panda_arm.execute(plan_result.trajectory, controllers=[])
    print(f"[테스트] '{name}' 이동 성공")
    return True


def main():
    rclpy.init()

    moveit_config_dict = (
        MoveItConfigsBuilder("moveit_resources_panda")
        .robot_description(file_path="config/panda.urdf.xacro")
        .robot_description_semantic(file_path="config/panda.srdf")
        .trajectory_execution(file_path="config/gripper_moveit_controllers.yaml")
        .planning_pipelines(pipelines=["ompl"])
        .to_moveit_configs()
    ).to_dict()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False
    ) as tmp:
        yaml.safe_dump({"/**": {"ros__parameters": moveit_config_dict}}, tmp)
        params_path = tmp.name

    print(f"[테스트] 파라미터 파일: {params_path}")

    try:
        panda = MoveItPy(
            node_name="moveit_py_test",
            launch_params_filepaths=[params_path],
        )
    except TypeError as e:
        print(f"[테스트] launch_params_filepaths 인자 실패: {e}")
        raise

    panda_arm = panda.get_planning_component("panda_arm")

    move_to(panda_arm, "ready")
    time.sleep(2)
    move_to(panda_arm, "extended")

    panda.shutdown()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
