"""경로 여유 측정 — 검증된 이송 경로가 **지나가며 스치는 자리** (검사 전용).

    source /opt/ros/lyrical/setup.bash && ROS_DOMAIN_ID=44 \
        python3 scripts/derive_path_clearance.py

로봇에 명령을 보내지 않는다. live MoveIt planning scene에 묻기만 한다.

측정: Capability가 푸는 모든 (자재, 출발, 도착) 경로마다 공통 12단계
(`build_route_stages`)를 만들고, 이동 자재 상자를 출발지에 두고, **나머지 모든
자리(출발·도착 제외)에 자재 크기 상자**를 둔 채 단계 자세와 단계 사이 경로 표본
(`check_path`, 0.02 rad)을 검사한다. 충돌은 장애물마다 독립이므로 한 번에 "어느 자리에
자재가 있으면 이 경로가 막히는가"(`blocked_by`)를 얻는다. 장애물이 없는 기준 충돌이
있으면 그 경로는 `status=blocked`다.

이 표는 **계획 보조**다. 실행 직전에는 scene을 실제 자재 위치로 동기화하고 같은 경로
검사를 다시 한다(그 결과가 우선한다). 끝나면 탐침 상자를 빼고 자재 상자를 측정 전
pose로 되돌린다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "config/workcell/fr3_2f85_workcell_path_clearance.json"
PROBE_PREFIX = "forstick2_clearance_probe__"


def main() -> int:
    import rclpy
    from geometry_msgs.msg import Pose
    from moveit_msgs.msg import CollisionObject, PlanningScene
    from moveit_msgs.srv import ApplyPlanningScene
    from rclpy.node import Node
    from shape_msgs.msg import SolidPrimitive

    from core.transfer_skill import TransferRequest, WorldView, validate_transfer
    from robots.fr3_gazebo.adapter import load_workcell_resources
    from robots.fr3_gazebo.transfer_capability import ROBOT_ID, Fr3TransferCapability
    from robots.moveit.ros_client import RosPlanningSceneClient
    from validation.pick_place_plan import PATH_STEP_RAD, check_path, check_stages
    from validation.transfer_stages import build_route_stages

    spec = importlib.util.spec_from_file_location(
        "demo_for_clearance", ROOT / "scripts/demo_workcell_pick_place.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)

    data = json.loads(demo.WORKCELL.read_text(encoding="utf-8"))
    grasp = json.loads(demo.GRASP.read_text(encoding="utf-8"))
    resources = load_workcell_resources(demo.WORKCELL, demo.POSES, state_max_age_sec=0.5,
                                        mounting_path=demo.MOUNTING, grasp_path=demo.GRASP)
    cap = Fr3TransferCapability.from_files(ROOT)
    materials = list(cap.materials())
    locations = sorted(cap.locations())
    center = {loc: demo.location_center(data, grasp, loc) for loc in locations}
    sizes = {tuple(float(v) for v in data["models"][m]["size_m"]) for m in materials}
    if len(sizes) != 1:
        print("자재 크기가 서로 달라 한 탐침 크기로 잴 수 없다", file=sys.stderr)
        return 2
    probe_size = list(next(iter(sizes)))
    limits = {j.get("name"): (float(j.find("limit").get("lower")),
                              float(j.find("limit").get("upper")))
              for j in ET.parse(demo.URDF).getroot().findall("joint")
              if j.find("limit") is not None and j.get("type") == "revolute"}

    rclpy.init()
    node = Node("forstick2_path_clearance")
    client = RosPlanningSceneClient(node, group_name="fr3wms_arm", frame_id="base_link",
                                    joint_limits=limits, model_hash="clearance",
                                    ttl_sec=5.0, source="scripts/derive_path_clearance.py")
    if not client.wait(30.0):
        print("MoveIt planning scene 서비스가 없다", file=sys.stderr)
        return 2
    apply = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    apply.wait_for_service(timeout_sec=15.0)
    before = client.world_object_poses()
    static_hash = client.snapshot().content_hash

    def send(objs) -> bool:
        scene = PlanningScene()
        scene.is_diff = True
        scene.world.collision_objects = objs
        request = ApplyPlanningScene.Request()
        request.scene = scene
        future = apply.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=15.0)
        return bool(future.done() and future.result() and future.result().success)

    def box(object_id, where, op):
        obj = CollisionObject()
        obj.header.frame_id = "world"
        obj.id = object_id
        obj.operation = op
        pose = Pose()
        pose.position.x, pose.position.y, pose.position.z = where
        pose.orientation.w = 1.0
        obj.pose = pose
        if op == CollisionObject.ADD:
            primitive = SolidPrimitive()
            primitive.type = SolidPrimitive.BOX
            primitive.dimensions = probe_size
            origin = Pose()
            origin.orientation.w = 1.0
            obj.primitives = [primitive]
            obj.primitive_poses = [origin]
        return obj

    far = {m: (-2.0, 2.0 + 0.3 * i, 0.05) for i, m in enumerate(materials)}
    routes: dict[str, dict] = {}
    try:
        for material in materials:
            origin_loc = cap.origin_of(material)
            for src in locations:
                for dst in locations:
                    if src == dst:
                        continue
                    where = {m: cap.origin_of(m) for m in materials}
                    where[material] = src
                    free = [loc for loc in locations if loc not in (src, dst)]
                    for other in materials:
                        if other != material and where[other] in (src, dst):
                            where[other] = next(loc for loc in free
                                                if loc not in where.values())
                    plan, findings = validate_transfer(
                        TransferRequest(ROBOT_ID, material, src, dst), cap,
                        WorldView(location_of=where))
                    key = f"{material}:{src}>{dst}"
                    if plan is None:
                        continue      # 자세가 없는 조합 — 표에 넣지 않는다
                    bindings, origin, src_rid = demo._route_bindings(
                        resources, grasp, data, material, plan)
                    dst_rid = dst if not dst.startswith("slot_") else "loc_conveyor"
                    stages, stage_findings = build_route_stages(
                        bindings, object_id=origin.object_id, route=plan.poses,
                        source_resource_id=src_rid, destination_resource_id=dst_rid)
                    if stage_findings:
                        routes[key] = {"status": "blocked",
                                       "detail": "단계를 만들지 못했다"}
                        continue
                    objs = [box(m, center[src] if m == material else far[m],
                                CollisionObject.MOVE) for m in materials]
                    objs += [box(PROBE_PREFIX + loc, center[loc], CollisionObject.ADD)
                             for loc in free]
                    if not send(objs):
                        raise RuntimeError("탐침 상자를 넣지 못했다")
                    time.sleep(0.1)
                    stage_checks, _ = check_stages(stages, bindings=bindings, client=client)
                    path_checks, _, count = check_path(
                        stages, bindings=bindings, client=client,
                        start_joints=stages[0].joint_rad)
                    if not send([box(PROBE_PREFIX + loc, center[loc], CollisionObject.REMOVE)
                                 for loc in free]):
                        raise RuntimeError("탐침 상자를 빼지 못했다")
                    blocked_by: dict[str, int] = {}
                    baseline: list[str] = []
                    for check in tuple(stage_checks) + tuple(path_checks):
                        if check.out_of_bounds:
                            baseline.append(f"{check.label}: 관절 제한")
                        for a, b in check.collisions:
                            probe = next((n for n in (a, b) if n.startswith(PROBE_PREFIX)), None)
                            if probe is None:
                                baseline.append(f"{check.label}: {a}↔{b}")
                            else:
                                loc = probe[len(PROBE_PREFIX):]
                                blocked_by[loc] = blocked_by.get(loc, 0) + 1
                    routes[key] = {
                        "status": "blocked" if baseline else "measured",
                        "material": material, "source": src, "destination": dst,
                        "blocked_by": sorted(blocked_by),
                        "blocking_samples": blocked_by,
                        "stage_count": len(stages), "path_samples": count,
                        "baseline_collisions": baseline[:6],
                        "own_origin": origin_loc}
                    print(f"{key:36s} {routes[key]['status']:8s} 막는 자리 {sorted(blocked_by)}",
                          flush=True)
    finally:
        # 탐침을 모두 빼고 자재 상자를 측정 전 pose로 되돌린다(따로 보낸다 —
        # 없는 탐침 REMOVE가 섞여 전체가 거절되지 않게).
        restore = []
        for m in materials:
            if m not in before:
                continue
            obj = box(m, before[m][1][:3], CollisionObject.MOVE)
            (obj.pose.orientation.x, obj.pose.orientation.y,
             obj.pose.orientation.z, obj.pose.orientation.w) = before[m][1][3:7]
            restore.append(obj)
        restored = send(restore)
        current = client.world_object_poses()
        leftovers = [k for k in current if k.startswith(PROBE_PREFIX)]
        if leftovers:
            send([box(k, (0, 0, 0), CollisionObject.REMOVE) for k in leftovers])
        after_hash = client.snapshot().content_hash
        print(f"[정리] 자재 상자 복원={restored} · scene hash 복원="
              f"{after_hash == static_hash}")
        node.destroy_node()
        rclpy.shutdown()

    record = {
        "schema": "forstick2.path_clearance/1",
        "is_simulated": True, "real_hardware_verified": False,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": ("live MoveIt planning scene · 공통 12단계(build_route_stages) 단계 자세 +"
                   f" 관절 공간 직선 경로 표본 {PATH_STEP_RAD} rad · 출발·도착 외 모든 자리에"
                   f" 자재 크기 상자 {probe_size} m(자리 중심) · 로봇 명령 없음"),
        "static_scene_hash": static_hash,
        "path_step_rad": PATH_STEP_RAD,
        "note": ("계획 보조 표다. 실행 직전 scene 동기화 + 경로 검사가 우선한다."
                 " 자재가 자리 중심에서 벗어나 있으면(허용 0.02 m) 표와 다를 수 있다."),
        "routes": routes,
    }
    OUT.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"기록: {OUT} · 경로 {len(routes)}개 · 막힘 있는 경로"
          f" {sum(1 for r in routes.values() if r.get('blocked_by'))}개 · 기준 충돌"
          f" {sum(1 for r in routes.values() if r.get('status') == 'blocked')}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
