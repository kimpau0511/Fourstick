"""FR3-WMS + GRP-CPL-062 + 2F-85 **Gazebo 작업 셀** Adapter (8-08 우선순위 6).

실제 하드웨어에 연결하지 않는다. `is_simulated=True`를 유지한다.
"""

from pathlib import Path
from typing import Callable

#: 공통 코드가 이 진입점만 알면 된다. 제조사·로봇 이름은 **설정에만** 있고
#: 공통 코드(server/·core/·validation/…)에는 나타나지 않는다(계획.md 27장).
ADAPTER_ENTRY_POINT = "build_workcell_adapter"


def build_workcell_adapter(
    *,
    robot_id: str,
    profile,
    workcell_config: Path,
    workcell_poses: Path,
    now: Callable[[], float],
    stop_velocity_rad_s: float,
    max_sample_gap_sec: float,
) -> tuple[Callable, dict]:
    """Adapter factory와 상태 요약을 만든다.

    공통 코드는 이 함수를 **설정에 적힌 모듈 경로**로 찾아 호출한다.
    실패는 예외로 올려 보내고, 호출자가 이유를 화면에 남긴다 — 없는 연결을
    있는 것처럼 두지 않는다.
    """
    from robots.fr3_gazebo.adapter import Fr3GazeboAdapter, load_workcell_resources
    from robots.fr3_gazebo.ros_transport import RosWorkcellTransport

    resources = load_workcell_resources(
        workcell_config, workcell_poses,
        state_max_age_sec=float(profile.extras.get("state_max_age_sec", 0.5)),
        # 개구 표는 장착 Profile의 공식 FK 값이다. 없으면 개구를 주장하지 않는다.
        mounting_path=(Path(__file__).resolve().parents[2]
                       / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"),
        # 파지 자세 측정 결과. 없으면 pick/place 계획을 펼칠 자세가 없다.
        grasp_path=(Path(__file__).resolve().parents[2]
                    / "config/workcell/fr3_2f85_workcell_grasp.json"))

    def factory(rid: str, prof):
        transport = RosWorkcellTransport(
            world_name=resources.world_name,
            gz_partition=resources.gz_partition,
            ros_domain_id=resources.ros_domain_id)
        return Fr3GazeboAdapter(
            rid, prof, transport=transport, resources=resources, now=now,
            stop_velocity_rad_s=stop_velocity_rad_s,
            max_sample_gap_sec=max_sample_gap_sec)

    def resolve_motion(plan) -> dict:
        """계획 스텝 → **검증된 관절값**. 값을 만들지 않는다.

        기호 계획(`move.to = loc_pallet_1`)을 이 셀의 검증된 자세로 바꾼다.
        대응하는 자세가 없으면 그 스텝을 넣지 않는다 — 기하 검사가 "수치가
        없는 스텝"으로 보고 ASK가 된다(통과시키지 않는다).
        """
        out: dict[int, dict] = {}
        for index, step in enumerate(getattr(plan, "steps", ()), start=1):
            skill = getattr(step, "skill", "")
            args = dict(getattr(step, "args", {}) or {})
            pose_name = None
            if skill == "home":
                pose_name = resources.safe_home_pose
            elif skill == "move":
                target = args.get("to") or args.get("from")
                if target:
                    pose_name = resources.move_pose.get(target)
            elif skill == "stop":
                # 정지는 자세로 가지 않는다. 기하 검사 대상이 아니다.
                continue
            joints = None if pose_name is None else resources.poses.get(pose_name)
            if joints:
                out[index] = {"joints": dict(joints), "pose": pose_name}
        return out

    def make_scene_client():
        """MoveIt planning scene 클라이언트. 서비스가 없으면 None이다."""
        import rclpy
        from rclpy.node import Node

        from core.frames import FrameKind
        from robots.moveit.ros_client import RosPlanningSceneClient

        if not rclpy.ok():
            rclpy.init()
        node = Node("forstick2_workcell_scene_client")
        limits = {j.name: (j.lower, j.upper) for j in profile.joint_limits}
        client = RosPlanningSceneClient(
            node, group_name="fr3wms_arm",
            # 좌표계 이름은 **Profile이 유일한 출처다**(Runtime.frame_id와 같다).
            frame_id=profile.frame_name(FrameKind.BASE),
            joint_limits=limits,
            model_hash=f"{resources.workcell_id}:{resources.workcell_version}",
            ttl_sec=5.0,
            source=f"workcell:{resources.workcell_id}")
        if not client.wait(timeout_sec=20.0):
            node.destroy_node()
            return None
        return client

    def pick_place_bindings():
        """pick/place 계획 검증에 넘길 **셀 선언**(8-10).

        공통 검증 계층은 모델 이름·자세 이름을 모른다. 여기서 설정을 그대로
        옮겨 담는다 — 없는 값을 채우지 않는다. 파지 자세 측정 결과가 없으면
        `grasp_pose`가 비어 있고, 검증은 "검증된 자세가 없다"로 막는다.
        """
        from validation.pick_place_plan import CellBindings

        declaration = dict(resources.grasp_declaration)
        return CellBindings(
            resources={rid: dict(row) for rid, row in resources.resources.items()},
            object_support=dict(resources.object_support),
            approach_pose=dict(resources.move_pose),
            grasp_pose=dict(resources.grasp_pose),
            place_pose=dict(resources.place_pose),
            home_pose=resources.safe_home_pose,
            poses={name: dict(joints) for name, joints in resources.poses.items()},
            gripper_joint=str(declaration.get("gripper_joint") or ""),
            gripper_open_rad=declaration.get("gripper_open_rad"),
            gripper_grasp_rad=declaration.get("gripper_grasp_rad"),
            gripper_mimic=dict(declaration.get("gripper_mimic") or {}),
            pad_links=tuple(declaration.get("pad_links") or ()),
            attached={rid: dict(spec) for rid, spec
                      in (declaration.get("attached") or {}).items()},
            pose_evidence={name: dict(item) for name, item
                           in resources.pose_evidence.items()},
            sources={
                "grasp": str(declaration.get("source") or ""),
                "pad_links": str(declaration.get("pad_links_source") or ""),
                "gripper_open": str(declaration.get("gripper_open_source") or ""),
                "workcell": str(workcell_config),
                "poses": str(workcell_poses),
            },
        )

    def make_geometry_validator(scene_client):
        """MoveIt planning scene 기반 기하 검사기."""
        from robots.moveit.validator import MoveItGeometryValidator

        return MoveItGeometryValidator(
            scene_client,
            validator_id="moveit-planning-scene",
            validator_version=f"workcell {resources.workcell_version}")

    status = {
        "robot_id": robot_id,
        "workcell_id": resources.workcell_id,
        "workcell_version": resources.workcell_version,
        "world": resources.world_name,
        "gz_partition": resources.gz_partition,
        "ros_domain_id": resources.ros_domain_id,
        "adapter": f"{Fr3GazeboAdapter.__module__}.{Fr3GazeboAdapter.__name__}",
        "adapter_kind_label": Fr3GazeboAdapter.adapter_kind,
        "resources": {rid: dict(m) for rid, m in resources.resources.items()},
        "move_poses": dict(resources.move_pose),
        "pick_poses": dict(resources.pick_pose),
        "place_poses": dict(resources.place_pose),
        "pose_evidence": {k: dict(v) for k, v in resources.pose_evidence.items()},
        "supported_skills": list(profile.supported_skills),
        "safe_home_pose": resources.safe_home_pose,
        "state_max_age_sec": resources.state_max_age_sec,
        "aperture_model": (None if resources.aperture_model is None
                           else resources.aperture_model.to_dict()),
        "grasp_poses": dict(resources.grasp_pose),
        "object_support": dict(resources.object_support),
        "grasp_declaration": {
            key: value for key, value in resources.grasp_declaration.items()
            if key != "attached"
        },
        "is_simulated": True,
        "real_hardware_verified": False,
    }
    return {
        "factory": factory,
        "status": status,
        # 기호 계획을 이 셀의 검증된 관절값으로 바꾼다(공통 계층은 이 함수만 안다).
        "motion_resolver": resolve_motion,
        # MoveIt planning scene 기반 기하 검사. 붙지 않으면 기하 검사는 ASK다.
        "scene_client_factory": make_scene_client,
        "planning_group": "fr3wms_arm",
        "geometry_validator_factory": make_geometry_validator,
        # pick/place 계획 검증용 셀 선언. 실행 경로를 열지 않는다(8-10).
        "pick_place_bindings": pick_place_bindings,
        "grasp_limitations": tuple(
            resources.grasp_declaration.get("limitations") or ()),
    }
