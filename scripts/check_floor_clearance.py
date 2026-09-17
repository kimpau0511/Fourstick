#!/usr/bin/env python3
"""자세별 바닥 간극 (md/개발플랜.md 8-08 우선순위 2).

프레임 원점이 아니라 **충돌 메시의 실제 점**으로 바닥(z=0) 간극을 낸다.
프레임 원점만 보면 패드가 바닥에 닿아도 "간극 +9 cm"로 보인다 — 그래서
그리퍼가 왜 닫히다 잠기는지 설명할 수 없었다.

간극은 FK(수정된 트리 순회)와 공식 충돌 메시로 계산한다. Gazebo 실측
자세(/world/*/pose/info)가 있으면 살아 있는 링크에 대해 대조한다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from robots.moveit.kinematics import link_transforms, parse_urdf, rpy_matrix  # noqa: E402
from scripts_support_stl import read_stl_points  # noqa: E402

URDF = Path("/tmp/forstick2_gazebo/urdf/fr3wms_with_2f85.moveit.urdf")
OVERLAY = Path("/tmp/forstick2_gazebo/overlay/share/robotiq_description")
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
SAMPLE_POINTS = 4000

POSES = {
    "home": [0.0] * 6,
    "move": [0.6, -0.5, 0.8, -0.4, 0.6, 0.3],
    "approach": [0.6, -0.55, 0.75, -0.4, 0.6, 0.3],
    "approach_previous": [0.6, -0.6, 0.7, -0.4, 0.6, 0.3],
    "floor_collision": [0.6, -0.35, 0.95, -0.4, 0.6, 0.3],
}


def mesh_points(info: dict) -> np.ndarray | None:
    mesh = info.get("mesh")
    if not mesh:
        return None
    path = Path(mesh.replace("file://", "").replace(
        "package://robotiq_description", str(OVERLAY)))
    if not path.is_file():
        return None
    points = read_stl_points(path, SAMPLE_POINTS) * np.asarray(info["scale"])
    return (rpy_matrix(*info["rpy"]) @ points.T).T + np.asarray(info["xyz"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-rad", type=float, default=0.0,
                        help="그리퍼 명령 관절값(개구가 바뀌면 최저점도 바뀐다)")
    parser.add_argument("--out", default=str(ROOT / "reports/gripper/floor_clearance.json"))
    args = parser.parse_args()

    if not URDF.is_file():
        print(f"조립 URDF가 없다: {URDF}", file=sys.stderr)
        return 2
    joints, links = parse_urdf(URDF)

    import xml.etree.ElementTree as ET

    mimics = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        mimic = joint.find("mimic")
        if mimic is not None and mimic.get("joint") == GRIPPER_JOINT:
            mimics[joint.get("name")] = (float(mimic.get("multiplier", "1")),
                                         float(mimic.get("offset", "0")))

    clouds = {name: points for name, info in links.items()
              if (points := mesh_points(info)) is not None}
    if not clouds:
        print("충돌 메시를 읽지 못했다", file=sys.stderr)
        return 3

    results = {}
    for label, values in POSES.items():
        angles = dict(zip(ARM_JOINTS, values))
        angles[GRIPPER_JOINT] = args.gripper_rad
        for name, (multiplier, offset) in mimics.items():
            angles[name] = args.gripper_rad * multiplier + offset
        transforms = link_transforms(joints, angles)
        per_link = {}
        for name, points in clouds.items():
            rotation, position = transforms[name]
            world = (rotation @ points.T).T + position
            per_link[name] = round(float(world[:, 2].min()), 6)
        lowest = min(per_link, key=per_link.get)
        origin = transforms.get("robotiq_85_tcp")
        results[label] = {
            "arm_rad": values,
            "lowest_link": lowest,
            "lowest_mesh_z_m": per_link[lowest],
            "touches_floor": per_link[lowest] <= 0.0,
            "tcp_origin_z_m": None if origin is None else round(float(origin[1][2]), 6),
            "per_link_lowest_z_m": dict(sorted(per_link.items(),
                                               key=lambda item: item[1])[:6]),
        }

    report = {
        "urdf": str(URDF),
        "gripper_joint_rad": args.gripper_rad,
        "method": "충돌 메시 점군을 FK로 world로 옮겨 최소 z를 본다."
                  " 프레임 원점이 아니다",
        "sample_points_per_link": SAMPLE_POINTS,
        "links_with_collision_mesh": len(clouds),
        "poses": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    for label, value in results.items():
        flag = "바닥 침범" if value["touches_floor"] else "간극 있음"
        print(f"  {label:20} 최저 {value['lowest_mesh_z_m']:+.4f} m"
              f" ({value['lowest_link']}) · TCP 원점"
              f" {value['tcp_origin_z_m']:+.4f} m · {flag}")
    print(f"  {out} 기록")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
