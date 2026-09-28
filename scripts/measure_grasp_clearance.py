"""파지 자세의 **위치 오차 여유** 측정 (검사 전용 — 로봇을 움직이지 않는다).

    source /opt/ros/lyrical/setup.bash && ROS_DOMAIN_ID=44 \\
        python3 scripts/measure_grasp_clearance.py [--only material_c_grasp_at_loc_pallet_1 ...]

반복 시연에서 자재가 자리 중심에서 조금씩 벗어나면(물리 배치 오차) 검증된 파지 자세가
MoveIt 검사에서 막혔다(내측 너클 ↔ 자재). 파지마다 자재 상자를 그 자리 중심에서
수평으로 옮기고(±6 mm 격자, yaw ±1°) 다음 두 상태를 묻는다.

- 연 상태(파지 접근): 자재와 **아무것도** 닿으면 안 된다.
- 닫은 상태(파지): 자재와는 **선언된 패드만** 닿아도 된다(기존 규칙 그대로).

모든 방향에서 깨끗한 가장 큰 반경(`clean_radius_mm`)과 방향별 첫 실패를 기록한다.
live planning scene의 자재 상자를 잠시 옮기고, 끝나면 측정 전 pose로 되돌린다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

GRASP = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "reports/workcell/grasp_clearance.json"
STEP_MM = 0.5
MAX_MM = float(__import__('os').environ.get('CLEARANCE_MAX_MM', '6.0'))
YAW_DEG = tuple(float(v) for v in __import__("os").environ.get("CLEARANCE_YAW_DEG", "0,-1,1").split(","))


def grasp_targets(grasp: dict, data: dict) -> list[dict]:
    """측정할 파지: 원래 팔레트 · 다른 팔레트 위치 파지 · 컨베이어 칸 파지."""
    import demo_workcell_pick_place as demo

    out = []
    for name, row in (grasp.get("poses") or {}).items():
        if name.endswith("_grasp") and row.get("status") == "verified":
            model = name[: -len("_grasp")]
            out.append({"name": name, "material": model,
                        "location": row.get("support_resource_id"),
                        "arm": row["joint_rad"],
                        "gripper": row["gripper_grasp_joint_rad"],
                        "center": demo.location_center(data, grasp,
                                                       row.get("support_resource_id"))})
    for name, row in (grasp.get("pallet_grasp_poses") or {}).items():
        if row.get("status") == "verified":
            model, _, loc = name.partition("_grasp_at_")
            out.append({"name": name, "material": model, "location": loc,
                        "arm": row["joint_rad"],
                        "gripper": row.get("gripper_grasp_joint_rad",
                                           grasp["poses"][f"{model}_grasp"]
                                           ["gripper_grasp_joint_rad"]),
                        "center": demo.location_center(data, grasp, loc)})
    slots = (grasp.get("conveyor_slots") or {}).get("slots") or {}
    for model_key, row in (grasp.get("conveyor_grasp_poses") or {}).items():
        if row.get("status") != "verified":
            continue
        model = model_key[: -len("_conveyor_grasp")]
        for slot, srow in slots.items():
            out.append({"name": f"{model}_conveyor_grasp__{slot}", "material": model,
                        "location": slot, "arm": srow["grasp_joint_rad_arm"],
                        "gripper": row["gripper_grasp_joint_rad"],
                        "center": tuple(srow["object_world_center_m"])})
    return out


def yaw_quat(deg: float):
    half = math.radians(deg) / 2
    return (0.0, 0.0, math.sin(half), math.cos(half))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    from derive_grasp_poses import _joints_with, build_client
    from derive_workcell_poses import GRIPPER_OPEN_RAD, load_urdf
    from validation.pick_place_plan import classify_contacts

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    grasp = json.loads(GRASP.read_text(encoding="utf-8"))
    spec = data["grasp"]
    pad_links = tuple(spec["pad_links"])
    _joints, _links, mimics, limits = load_urdf()
    client, node = build_client({**limits, "robotiq_85_left_knuckle_joint":
                                 {"lower": 0.0, "upper": 0.8}})
    materials = [m for m, item in data["models"].items() if item.get("kind") == "material"]
    before = client.world_object_poses()
    far = {m: (-2.0, 2.0 + 0.3 * i, 0.05) for i, m in enumerate(materials)}
    targets = grasp_targets(grasp, data)
    if args.only:
        targets = [t for t in targets if t["name"] in args.only]

    def place(model: str, xyz, quat=(0.0, 0.0, 0.0, 1.0)):
        moves = {m: ("world", tuple(far[m]) + (0.0, 0.0, 0.0, 1.0))
                 for m in materials if m != model}
        moves[model] = ("world", tuple(xyz) + tuple(quat))
        if not client.move_world_objects(moves):
            raise RuntimeError("scene 상자를 옮기지 못했다")

    def state_ok(target, gripper: float, holding_pads: bool):
        validity = client.check_state(_joints_with(target["arm"], gripper, mimics, spec))
        pairs = [list(p) for p in validity.contacts
                 if target["material"] in p]           # 자재가 낀 접촉만 본다
        allowed, bad = classify_contacts(pairs, object_ids=(target["material"],),
                                         pad_links=pad_links if holding_pads else ())
        return (not bad and not validity.out_of_bounds), [list(b) for b in bad]

    open_rad = float(GRIPPER_OPEN_RAD)
    report = {"schema": "forstick2.grasp_clearance/1", "is_simulated": True,
              "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "step_mm": STEP_MM, "max_mm": MAX_MM, "yaw_deg": list(YAW_DEG),
              "method": "live MoveIt scene · 자재 상자를 자리 중심에서 옮겨 연/닫은 상태 접촉 검사",
              "grasps": {}}
    try:
        for target in targets:
            center = np.asarray(target["center"], dtype=float)
            first_fail: dict[str, dict] = {}
            clean_radius = None
            radius = 0.0
            directions = [tuple(int(v) for v in d.split(":")) for d in
                          os.environ.get("CLEARANCE_DIRS", "1:0,-1:0,0:1,0:-1,1:1,1:-1,-1:1,-1:-1")
                          .split(",")]
            while radius <= MAX_MM + 1e-9:
                ring_ok = True
                for ux, uy in ([(0, 0)] if radius == 0 else directions):
                    norm = math.hypot(ux, uy) or 1.0
                    dx, dy = ux / norm * radius, uy / norm * radius
                    for yaw in YAW_DEG:
                        place(target["material"], center + np.array([dx, dy, 0.0]) / 1000,
                              yaw_quat(yaw))
                        ok_open, bad_open = state_ok(target, open_rad, False)
                        ok_closed, bad_closed = state_ok(
                            target, float(os.environ.get("CLEARANCE_GRIPPER_RAD",
                                                         target["gripper"])), True)
                        if not (ok_open and ok_closed):
                            ring_ok = False
                            first_fail.setdefault(f"{ux:+d},{uy:+d}", {
                                "dx_mm": round(dx, 2), "dy_mm": round(dy, 2),
                                "yaw_deg": yaw, "open": bad_open, "closed": bad_closed})
                if not ring_ok:
                    break
                clean_radius = radius
                radius += STEP_MM
            row = {"material": target["material"], "location": target["location"],
                   "clean_radius_mm": clean_radius, "first_failures": first_fail}
            report["grasps"][target["name"]] = row
            print(f"{target['name']:42s} 깨끗한 반경 {clean_radius} mm ·"
                  f" 첫 실패 {list(first_fail.values())[:1]}", flush=True)
    finally:
        restore = {m: (before[m][0], before[m][1]) for m in materials if m in before}
        client.move_world_objects(restore)
        node.destroy_node()
        import rclpy
        rclpy.try_shutdown()
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
    print(f"기록: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
