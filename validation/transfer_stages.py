"""공통 `transfer` 계획 → 팔 작업 셀 12단계. 자세는 계약이 검증한 `RoutePoses`만 쓴다.

공통 단계와 12단계의 대응:

| 공통 | 12단계 |
|---|---|
| approach | home_start · pick_approach · pre_grasp · gripper_open_before_grasp |
| grasp | grasp_approach · gripper_close(고정) |
| lift | lift(출발지 접근 자세로) |
| move | place_approach(도착지 접근 자세로, 든 채) |
| place | place_descend · gripper_open_release(해제) |
| retreat | retreat · home_end |
| observe | 실행기가 Gazebo 관측으로 도착 판정 |

지금은 기존 경로(팔레트→칸, 칸→원래 팔레트)가 검증된 자기 빌더를 그대로 쓰고,
새 경로(칸→칸)가 이 빌더를 쓴다. 기존 빌더가 고르는 자세가 계약의 자세와 같다는
것은 테스트가 고정한다(`tests/unit/test_transfer_stages.py`).
"""

from __future__ import annotations

from dataclasses import replace

from core.reason_codes import ReasonCode
from core.transfer_skill import RoutePoses

PHASE_OF_STAGE = {
    "home_start": "approach", "pick_approach": "approach", "pre_grasp": "approach",
    "gripper_open_before_grasp": "approach", "grasp_approach": "grasp",
    "gripper_close": "grasp", "lift": "lift", "place_approach": "move",
    "place_descend": "place", "gripper_open_release": "place",
    "retreat": "retreat", "home_end": "retreat",
}

ROUTE_LABELS = {
    "home_start": "안전 home", "pick_approach": "출발지 접근", "pre_grasp": "pre-grasp",
    "gripper_open_before_grasp": "그리퍼 열기", "grasp_approach": "집기 접근",
    "gripper_close": "그리퍼 닫기", "lift": "들어 올리기", "place_approach": "도착지 접근",
    "place_descend": "놓기 접근", "gripper_open_release": "그리퍼 열기(해제)",
    "retreat": "이탈", "home_end": "안전 home 복귀",
}


def bind_route(bindings, route: RoutePoses):
    """계약이 돌려준 자세를 바인딩에 **이름 그대로** 올린다. 값을 바꾸지 않는다."""
    poses = {name: dict(joints) for name, joints in bindings.poses.items()}
    for pose in (route.source_approach, route.grasp, route.destination_approach,
                 route.place):
        existing = poses.get(pose.name)
        if existing is not None and existing != dict(pose.joint_rad):
            raise ValueError(f"자세 이름 {pose.name}이 다른 값으로 이미 있다")
        poses[pose.name] = dict(pose.joint_rad)
    return replace(bindings, poses=poses)


def build_route_stages(bindings, *, object_id: str, route: RoutePoses,
                       source_resource_id: str, destination_resource_id: str):
    """12단계. 반환: (단계들, [(이유 코드, 설명)])."""
    from validation.pick_place_plan import GRIPPER_STAGES, STAGE_SEQUENCE, PickPlaceStage

    open_rad = bindings.gripper_open_rad
    grasp_rad = bindings.gripper_grasp_rad
    if open_rad is None or grasp_rad is None:
        return (), [(ReasonCode.CONFIG_MISSING, "그리퍼 열림·파지 명령값의 근거가 없다")]
    if route.place.name == route.grasp.name:
        return (), [(ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE,
                     "놓기 단계에 출발지 파지 자세를 쓰지 않는다")]
    if object_id not in bindings.attached:
        return (), [(ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE,
                     f"{object_id}를 든 물체 선언이 없다 — 든 채 검사를 할 수 없다")]
    home = bindings.home_pose
    src, dst = source_resource_id, destination_resource_id
    sa, g = route.source_approach.name, route.grasp.name
    da, pl = route.destination_approach.name, route.place.name
    layout = (
        ("home_start", home, open_rad, None, ()),
        ("pick_approach", sa, open_rad, None, (src,)),
        ("pre_grasp", sa, open_rad, None, (src,)),
        ("gripper_open_before_grasp", sa, open_rad, None, (src,)),
        ("grasp_approach", g, open_rad, None, (object_id, src)),
        ("gripper_close", g, grasp_rad, object_id, (object_id, src)),
        ("lift", sa, grasp_rad, object_id, (object_id, src)),
        ("place_approach", da, grasp_rad, object_id, (object_id, dst)),
        ("place_descend", pl, grasp_rad, object_id, (object_id, dst)),
        ("gripper_open_release", pl, open_rad, None, (dst,)),
        ("retreat", da, open_rad, None, (dst,)),
        ("home_end", home, open_rad, None, ()),
    )
    assert tuple(row[0] for row in layout) == STAGE_SEQUENCE
    missing = sorted({pose for _, pose, *_ in layout if pose not in bindings.poses})
    if missing:
        return (), [(ReasonCode.GEOMETRY_GRASP_POSE_UNAVAILABLE,
                     f"검증된 자세가 바인딩에 없다: {', '.join(missing)}")]
    stages = []
    for no, (stage, pose, gripper, holds, resources) in enumerate(layout, start=1):
        stages.append(PickPlaceStage(
            no=no, stage=stage, label=ROUTE_LABELS[stage],
            kind="gripper" if stage in GRIPPER_STAGES else "arm_motion",
            pose_name=pose, joint_rad=dict(bindings.poses[pose]),
            gripper_joint_rad=gripper, holds_object=holds,
            resources=tuple(resources),
            scene_models=tuple(m for m in (bindings.scene_model(r) for r in resources) if m),
            frames=tuple(f for f in (bindings.frame(r) for r in resources) if f),
            source_step=None,
            detail=f"transfer {route.grasp.location} → {route.place.location}"
                   f" ({PHASE_OF_STAGE[stage]})"))
    return tuple(stages), []
