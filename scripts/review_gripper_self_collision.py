#!/usr/bin/env python3
"""그리퍼 내부 자기충돌 검토 (md/개발플랜.md 8-08).

그리퍼는 **자유도가 1개**다(명령 조인트 하나 + mimic 4개). 그래서 표본이 아니라
**전 구간을 훑어** 내부 링크 쌍의 최소거리를 구할 수 있다 — 팔(6자유도)과 달리
결과가 표본 한계를 갖지 않는다.

`collisions_updater`가 "Never"로 비활성화한 쌍을 그대로 믿지 않는다. 여기서
확인된 쌍만 비활성 상태로 남기고, 나머지는 검사 대상으로 되살린다
(`scripts/prune_self_collision_srdf.py`).

**팔↔그리퍼 쌍은 여기서 판정하지 않는다.** 6자유도 전수 검토를 하지 않았으므로
그 쌍들은 검사 대상으로 남긴다(모르면 검사한다).
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "scripts"))

from robots.moveit.kinematics import link_transforms, parse_urdf, rpy_matrix  # noqa: E402
from scripts_support_stl import read_stl_points  # noqa: E402

URDF = Path("/tmp/forstick2_gazebo/urdf/fr3wms_with_2f85.moveit.urdf")
OUT = ROOT / "reports/gripper/self_collision_review.json"
GRIPPER_PREFIX = "robotiq_85"
ADAPTER_LINK = "ur_to_robotiq_link"
#: 명령 조인트 훑는 간격(rad). 0.8 / 0.005 = 161점.
SWEEP_STEP_RAD = 0.005
#: 근접 판정 임계값(m).
PROXIMITY_M = 0.010
SAMPLE_POINTS = 1500


def mimic_map(joints) -> dict:
    out = {}
    for joint in joints:
        mimic = joint.get("mimic")
        if mimic:
            out[joint["name"]] = mimic
    return out


def main() -> int:
    if not URDF.is_file():
        print(f"조립 URDF가 없다: {URDF}", file=sys.stderr)
        return 2
    joints, links = parse_urdf(URDF)
    # mimic 정보는 parse_urdf가 담지 않으므로 URDF에서 직접 읽는다.
    import xml.etree.ElementTree as ET

    root = ET.parse(URDF).getroot()
    mimics = {}
    for joint in root.findall("joint"):
        mimic = joint.find("mimic")
        if mimic is not None:
            mimics[joint.get("name")] = {
                "joint": mimic.get("joint"),
                "multiplier": float(mimic.get("multiplier", "1")),
                "offset": float(mimic.get("offset", "0")),
            }

    targets = {
        name: info for name, info in links.items()
        if name.startswith(GRIPPER_PREFIX) or name == ADAPTER_LINK
    }
    if not targets:
        print("그리퍼 링크의 충돌 메시를 찾지 못했다", file=sys.stderr)
        return 3

    clouds, mesh_info = {}, {}
    for name, info in targets.items():
        mesh = info["mesh"]
        path = Path(mesh.replace("file://", "").replace(
            "package://robotiq_description",
            "/tmp/forstick2_gazebo/overlay/share/robotiq_description",
        ))
        if not path.is_file():
            print(f"메시 없음: {path}", file=sys.stderr)
            return 4
        points = read_stl_points(path, SAMPLE_POINTS) * np.asarray(info["scale"])
        points = (rpy_matrix(*info["rpy"]) @ points.T).T + np.asarray(info["xyz"])
        clouds[name] = points
        mesh_info[name] = {
            "mesh": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
            "points": int(len(points)),
        }

    adjacency = {frozenset((j["parent"], j["child"])) for j in joints}
    names = sorted(clouds)
    pairs = [
        (a, b) for a, b in itertools.combinations(names, 2)
        if frozenset((a, b)) not in adjacency
    ]

    command = "robotiq_85_left_knuckle_joint"
    limits = next(j["limit"] for j in joints if j["name"] == command)
    steps = int(round((limits["upper"] - limits["lower"]) / SWEEP_STEP_RAD)) + 1
    sweep = [limits["lower"] + i * SWEEP_STEP_RAD for i in range(steps)]

    def angles_for(value: float) -> dict:
        out = {command: value}
        for name, mimic in mimics.items():
            if mimic["joint"] == command:
                out[name] = value * mimic["multiplier"] + mimic["offset"]
        return out

    worst: dict[tuple, dict] = {}
    # 임계값 안으로 들어오지 않은 쌍도 **하한(AABB 간극)을 기록**한다.
    # 기록하지 않으면 가지치기가 "검토되지 않음"으로 보고 검사 대상으로
    # 되살린다 — 멀다는 증거를 버리는 셈이다.
    bounds: dict[tuple, float] = {}
    exact = 0
    for value in sweep:
        transforms = link_transforms(joints, angles_for(value))
        world = {
            name: (transforms[name][0] @ clouds[name].T).T + transforms[name][1]
            for name in names
        }
        for a, b in pairs:
            box_a = (world[a].min(0), world[a].max(0))
            box_b = (world[b].min(0), world[b].max(0))
            gap = float(np.max(np.maximum(box_a[0] - box_b[1], box_b[0] - box_a[1])))
            key = (a, b)
            if key not in bounds or gap < bounds[key]:
                bounds[key] = gap
            if gap > PROXIMITY_M:
                continue
            step_a = max(1, len(world[a]) // 300)
            step_b = max(1, len(world[b]) // 300)
            distance = float(np.sqrt((
                (world[a][::step_a][:, None, :] - world[b][::step_b][None, :, :]) ** 2
            ).sum(-1)).min())
            exact += 1
            if key not in worst or distance < worst[key]["min_distance_m"]:
                worst[key] = {
                    "pair": [a, b],
                    "min_distance_m": round(distance, 5),
                    "joint_rad": round(value, 4),
                }

    # 근접한 적 없는 쌍은 AABB 하한을 최소거리로 기록한다(멀다는 증거).
    # **경계상자가 겹친 적이 있으면 기록하지 않는다.** 이 스크립트의 점군은
    # STL **꼭짓점 표본**이라, 면이 서로를 관통하는 경우를 감지하지 못한다.
    # 겹친 AABB에 대해 양수 거리를 적으면 "멀다"는 잘못된 근거가 된다 —
    # 실측으로 확인했다: 2F-85의 루프 폐쇄 쌍을 이 방식이 +13.4 mm로 적었지만
    # MoveIt(정확한 삼각형 검사)은 전 구간에서 관통으로 판정한다.
    unverifiable = []
    for pair, gap in bounds.items():
        if pair in worst:
            continue
        if gap <= 0.0:
            unverifiable.append({
                "pair": list(pair),
                "aabb_gap_m": round(gap, 5),
                "status": "unverifiable",
                "reason": "경계상자가 겹치는데 꼭짓점 표본으로는 관통을"
                          " 판정할 수 없다 — 거리를 주장하지 않는다",
                "reason_code": "exec.unverifiable",
            })
            continue
        worst[pair] = {
            "pair": list(pair),
            "min_distance_m": round(gap, 5),
            "bound": "aabb_lower_bound",
            "note": "전 구간에서 경계상자 간극이 이 값보다 작아진 적이 없다"
                    " (분리된 경계상자이므로 하한으로 쓸 수 있다)",
        }

    close = sorted(
        (item for item in worst.values() if item["min_distance_m"] <= PROXIMITY_M),
        key=lambda item: item["min_distance_m"],
    )
    report = {
        "reviewed_at": round(time.time(), 3),
        "source": {
            "urdf": str(URDF),
            "urdf_sha256": hashlib.sha256(URDF.read_bytes()).hexdigest(),
            "meshes": mesh_info,
        },
        "method": {
            "degrees_of_freedom": 1,
            "command_joint": command,
            "joint_range_rad": [limits["lower"], limits["upper"]],
            "sweep_step_rad": SWEEP_STEP_RAD,
            "sweep_points": len(sweep),
            "mimic_joints": mimics,
            "sample_points_per_link": SAMPLE_POINTS,
            "proximity_threshold_m": PROXIMITY_M,
            "exact_distance_computations": exact,
            "note": "자유도가 1개라 전 구간을 훑었다 — 표본 한계가 없다."
                    " 다만 점군 표본이므로 관통 깊이는 계산하지 않는다",
            "scope": "그리퍼 내부 + 어댑터 쌍만. **팔↔그리퍼 쌍은 판정하지 않는다**",
        },
        "pairs_checked": [list(pair) for pair in pairs],
        "closest_per_pair": sorted(worst.values(),
                                   key=lambda item: item["min_distance_m"]),
        "pairs_within_threshold": close,
        "unverifiable_pairs": unverifiable,
        "method_limit": "점군은 STL 꼭짓점 표본이다. 경계상자가 분리된 쌍에"
                        " 대해서만 거리 하한으로 쓸 수 있고, 겹친 쌍의 관통은"
                        " 판정하지 못한다 — 그 경우 unverifiable로 남긴다."
                        " 정확한 판정은 MoveIt(FCL 삼각형 검사)이 한다",
        "conclusion": (
            f"그리퍼 1자유도 전 구간({len(sweep)}점)에서 비인접 쌍 {len(close)}개가"
            f" {PROXIMITY_M} m 안으로 접근한다"
            if close else
            "그리퍼 1자유도 전 구간에서 비인접 쌍의 근접을 찾지 못했다"
        ),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"{OUT} 기록 — {report['conclusion']}")
    print(f"  검사 쌍 {len(pairs)}개 · 정확 계산 {exact}회")
    for item in close[:10]:
        print(f"   근접 {item['pair']} {item['min_distance_m']} m"
              f" (관절 {item['joint_rad']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
