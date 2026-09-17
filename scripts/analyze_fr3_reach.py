#!/usr/bin/env python3
"""FR3-WMS 도달거리 정의별 계산 (8단계 도달거리 불일치 해소).

공식 표기 0.622 m와 이전 계산 0.8065 m가 다른 이유를 찾는다.
**단순 링크 길이 합을 reach로 쓰지 않는다.** 정의를 나눠 FK로 계산한다.

계산 정의:
 1. base axis 기준 최대 수평 반경 (J1 회전축에서 각 프레임까지의 수평거리)
 2. shoulder 기준 flange 도달거리 (J2 원점 → flange 3D 거리)
 3. flange(tool_Link 부모=wrist3_Link) vs tool frame(tool_Link) 차이
 4. TCP가 추가된 경우 (장착 변환 미확보이므로 계산하지 않고 표시만)
 5. 관절 제한을 적용한 실제 작업공간 (URDF limit 안에서만 샘플링)
 6. URDF tree의 고정 base·tool offset

입력: 외부 경로의 공식 URDF(관절 제한 포함). 기준 frame을 항목마다 명시한다.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path("/home/asd/external/frcobot_ros2")
URDF = "fairino_description/urdf/FR3WMS.urdf"
GRID = 17          # 관절당 표본 수(제한 범위를 균등 분할)
OFFICIAL_REACH_M = 0.622


def rpy(r: float, p: float, y: float):
    cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                              math.sin(p), math.cos(y), math.sin(y))
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def axis_rot(axis, angle):
    n = math.sqrt(sum(a * a for a in axis)) or 1.0
    x, y, z = (a / n for a in axis)
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def mv(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def floats(text, default=(0.0, 0.0, 0.0)):
    return default if not text else tuple(float(v) for v in text.split())


def read(path: Path):
    root = ET.parse(path).getroot()
    joints = []
    for joint in root.findall("joint"):
        origin, limit, axis = (joint.find("origin"), joint.find("limit"),
                               joint.find("axis"))
        joints.append({
            "name": joint.get("name"), "type": joint.get("type"),
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "xyz": floats(None if origin is None else origin.get("xyz")),
            "rpy": floats(None if origin is None else origin.get("rpy")),
            "axis": floats(None if axis is None else axis.get("xyz"), (0, 0, 1.0)),
            "limit": None if limit is None else {
                "lower": float(limit.get("lower")), "upper": float(limit.get("upper")),
            },
        })
    return joints


def frames(joints, angles: dict) -> dict[str, tuple]:
    """각 링크 원점 위치(base_link 기준)를 한 번에 계산한다."""
    rot = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    pos = (0.0, 0.0, 0.0)
    out = {joints[0]["parent"]: pos}
    for joint in joints:
        pos = tuple(p + q for p, q in zip(pos, mv(rot, joint["xyz"])))
        rot = mm(rot, rpy(*joint["rpy"]))
        if joint["type"] == "revolute":
            rot = mm(rot, axis_rot(joint["axis"], angles.get(joint["name"], 0.0)))
        out[joint["child"]] = pos
    return out


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO
    path = repo / URDF
    if not path.is_file():
        print(f"외부 URDF가 없다: {path}", file=sys.stderr)
        return 2
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=60).stdout.strip()
    joints = read(path)
    movable = [j for j in joints if j["type"] == "revolute"]
    fixed = [j for j in joints if j["type"] == "fixed"]
    flange_link = fixed[0]["parent"]        # tool 조인트의 부모 = 플랜지 링크
    tool_link = fixed[0]["child"]
    shoulder_link = movable[1]["parent"]    # J2의 부모

    # 고정 오프셋(트리에 이미 들어 있는 값)
    offsets = {
        "base_to_j2_z_m": movable[1]["xyz"][2],
        "j3_x_m": movable[2]["xyz"][0],
        "j4_x_m": movable[3]["xyz"][0],
        "j5_z_m": movable[4]["xyz"][2],
        "j6_z_m": movable[5]["xyz"][2],
        "tool_offset_z_m": fixed[0]["xyz"][2],
    }
    limits = {j["name"]: j["limit"] for j in movable}

    # 관절 제한 안에서 격자 샘플링. J1은 base z축 회전이라 반경에 영향이 없다.
    sweep = movable[1:]
    grids = [
        [j["limit"]["lower"] + (j["limit"]["upper"] - j["limit"]["lower"]) * i
         / (GRID - 1) for i in range(GRID)]
        for j in sweep
    ]

    per_link_radius: dict[str, float] = {}
    best = {
        "horizontal_radius_flange": (0.0, None),
        "horizontal_radius_tool": (0.0, None),
        "distance_base_origin_to_flange": (0.0, None),
        "distance_base_origin_to_tool": (0.0, None),
        "distance_shoulder_to_flange": (0.0, None),
        "distance_shoulder_to_tool": (0.0, None),
    }
    z_range = [float("inf"), -float("inf")]
    for combo in itertools.product(*grids):
        angles = {j["name"]: v for j, v in zip(sweep, combo)}
        pose = frames(joints, angles)
        flange, tool, shoulder = (pose[flange_link], pose[tool_link],
                                  pose[shoulder_link])
        candidates = {
            "horizontal_radius_flange": math.hypot(flange[0], flange[1]),
            "horizontal_radius_tool": math.hypot(tool[0], tool[1]),
            "distance_base_origin_to_flange": math.dist((0, 0, 0), flange),
            "distance_base_origin_to_tool": math.dist((0, 0, 0), tool),
            "distance_shoulder_to_flange": math.dist(shoulder, flange),
            "distance_shoulder_to_tool": math.dist(shoulder, tool),
        }
        for key, value in candidates.items():
            if value > best[key][0]:
                best[key] = (value, {k: round(v, 4) for k, v in angles.items()})
        for link, point in pose.items():
            radius = math.hypot(point[0], point[1])
            if radius > per_link_radius.get(link, 0.0):
                per_link_radius[link] = radius
        z_range[0] = min(z_range[0], tool[2])
        z_range[1] = max(z_range[1], tool[2])

    report = {
        "source": {
            "urdf": f"{URDF} (외부 경로 {repo})", "commit": commit,
            "sha256": checksum, "grid_points_per_joint": GRID,
            "swept_joints": [j["name"] for j in sweep],
            "note": "J1은 base z축 회전이라 수평 반경·거리에 영향이 없어 고정",
        },
        "frames": {
            "base_reference": joints[0]["parent"],
            "shoulder_link": shoulder_link,
            "flange_link": flange_link,
            "tool_link": tool_link,
            "flange_to_tool_offset_m": list(fixed[0]["xyz"]),
        },
        "joint_limits_rad": limits,
        "fixed_offsets_m": offsets,
        "definitions": {
            key: {
                "value_m": round(value, 6),
                "angles_rad": angles,
            }
            for key, (value, angles) in best.items()
        },
        "link_length_sum_m": {
            "j3_x + j4_x + j5_z": round(
                abs(offsets["j3_x_m"]) + abs(offsets["j4_x_m"])
                + abs(offsets["j5_z_m"]), 6
            ),
            "note": "단순 합은 reach 정의가 아니다. 비교용으로만 적는다",
        },
        "max_horizontal_radius_per_link_m": {
            link: round(value, 6) for link, value in sorted(per_link_radius.items())
        },
        "tool_z_range_m": [round(z_range[0], 6), round(z_range[1], 6)],
        "official_reach_m": OFFICIAL_REACH_M,
        "tcp_added_reach_m": None,
        "tcp_note": ("장착 변환(TCP)이 미확보다. TCP를 더한 도달거리는 계산하지"
                     " 않는다 — 값을 만들지 않는다"),
    }
    for key, entry in report["definitions"].items():
        entry["delta_vs_official_m"] = round(
            entry["value_m"] - OFFICIAL_REACH_M, 6
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
