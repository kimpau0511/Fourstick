#!/usr/bin/env python3
"""작업 셀 물체를 MoveIt planning scene에 등록 (8-08 우선순위 4).

Gazebo world와 **같은 설정 파일**에서 박스를 만들어 `/apply_planning_scene`로
올린다. 손으로 두 번 적지 않는다 — Gazebo에 있는 작업대와 MoveIt이 아는
작업대가 달라지면 충돌 검사가 뜻을 잃는다.

받침대도 등록한다. base_link가 그 위에 놓이므로 SRDF의 자기충돌 행렬이
아니라 **부착 링크 제외 목록**으로 다뤄야 하는데, MoveIt은 world 물체와
로봇 링크의 충돌을 링크별로 끌 수 없다. 그래서 받침대는 로봇 base가 닿는
윗면을 2 mm 낮춰 등록하고 그 사실을 기록한다 — **간극을 만들어 통과시키는
것이 아니라, 지지 구조물과의 접촉을 충돌로 세지 않기 위한 명시적 처리다.**
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "reports/workcell/planning_scene.json"
#: 받침대 윗면을 이만큼 낮춰 등록한다(지지 접촉을 충돌로 세지 않기 위해).
PEDESTAL_TOP_RELIEF_M = 0.002
PLANNING_FRAME = "world"


def resolve(frames: dict, name: str) -> np.ndarray:
    out = np.zeros(3)
    while name is not None:
        out = out + np.asarray(frames[name]["xyz_m"])
        name = frames[name]["parent"]
    return out


def boxes_from_config(data: dict) -> list[dict]:
    frames = data["frames"]
    out: list[dict] = []
    for model_name, model in data["models"].items():
        if model["kind"] == "ground_plane":
            continue
        origin = resolve(frames, model["frame"])
        if model["kind"] == "material":
            size = list(model["size_m"])
            out.append({"id": model_name, "size_m": size,
                        "center_xyz_m": [round(float(v), 6) for v in origin],
                        "gazebo_model": model_name,
                        "resource_id": model.get("resource_id")})
            continue
        for part in model["parts"]:
            center = origin + np.asarray(part["center_xyz_m"])
            size = list(part["size_m"])
            relief = 0.0
            if model["kind"] == "pedestal" and part["name"] == "column":
                # 윗면만 낮춘다: 높이를 줄이고 중심을 그만큼 내린다.
                relief = PEDESTAL_TOP_RELIEF_M
                size = [size[0], size[1], size[2] - relief]
                center = center - np.array([0.0, 0.0, relief / 2])
            out.append({"id": f"{model_name}__{part['name']}", "size_m": size,
                        "center_xyz_m": [round(float(v), 6) for v in center],
                        "gazebo_model": model_name,
                        "resource_id": model.get("resource_id"),
                        "top_relief_m": relief or None})
    return out


def main() -> int:
    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    boxes = boxes_from_config(data)

    import rclpy
    from geometry_msgs.msg import Pose
    from moveit_msgs.msg import CollisionObject, PlanningScene
    from moveit_msgs.srv import ApplyPlanningScene, GetPlanningScene
    from rclpy.node import Node
    from shape_msgs.msg import SolidPrimitive

    rclpy.init()
    node = Node("forstick2_workcell_scene")
    apply_client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    get_client = node.create_client(GetPlanningScene, "/get_planning_scene")

    deadline = time.monotonic() + 90.0
    while not apply_client.wait_for_service(timeout_sec=2.0):
        if time.monotonic() > deadline:
            print("/apply_planning_scene 서비스가 없다 — move_group이 떠 있는지 본다",
                  file=sys.stderr)
            node.destroy_node()
            rclpy.shutdown()
            return 2

    scene = PlanningScene()
    scene.is_diff = True
    for box in boxes:
        obj = CollisionObject()
        obj.header.frame_id = PLANNING_FRAME
        obj.id = box["id"]
        obj.operation = CollisionObject.ADD
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.BOX
        primitive.dimensions = [float(v) for v in box["size_m"]]
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = (
            float(v) for v in box["center_xyz_m"])
        pose.orientation.w = 1.0
        obj.primitives = [primitive]
        obj.primitive_poses = [pose]
        scene.world.collision_objects.append(obj)

    request = ApplyPlanningScene.Request()
    request.scene = scene
    future = apply_client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=40.0)
    applied = bool(future.done() and future.result() and future.result().success)

    # 등록 결과를 **되읽어** 확인한다. 요청 성공만으로 등록됐다고 하지 않는다.
    registered: list[str] = []
    if get_client.wait_for_service(timeout_sec=10.0):
        get_request = GetPlanningScene.Request()
        get_request.components.components = (
            get_request.components.WORLD_OBJECT_NAMES
            | get_request.components.WORLD_OBJECT_GEOMETRY)
        get_future = get_client.call_async(get_request)
        rclpy.spin_until_future_complete(node, get_future, timeout_sec=40.0)
        if get_future.done() and get_future.result():
            registered = [o.id for o in
                          get_future.result().scene.world.collision_objects]

    node.destroy_node()
    rclpy.shutdown()

    expected = {box["id"] for box in boxes}
    missing = sorted(expected - set(registered))
    report = {
        "schema": "forstick2.workcell_planning_scene/1",
        "published_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "planning_frame": PLANNING_FRAME,
        "source": str(WORKCELL.relative_to(ROOT)),
        "apply_succeeded": applied,
        "objects_requested": len(boxes),
        "objects_registered": len(registered),
        "registered_ids": sorted(registered),
        "missing_ids": missing,
        "pedestal_top_relief_m": PEDESTAL_TOP_RELIEF_M,
        "pedestal_relief_note":
            "받침대 기둥 윗면을 2 mm 낮춰 등록했다. base_link가 그 위에 놓이므로"
            " 지지 접촉을 충돌로 세지 않기 위한 명시적 처리다. 로봇을 띄우거나"
            " 바닥을 내린 것이 아니다",
        "objects": boxes,
        "status": "verified" if (applied and not missing) else "blocked",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"{OUT.relative_to(ROOT)} 기록 — 요청 {len(boxes)}개 ·"
          f" 등록 확인 {len(registered)}개 · 상태 {report['status']}")
    if missing:
        print(f"  등록되지 않은 물체: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
