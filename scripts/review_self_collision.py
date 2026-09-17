#!/usr/bin/env python3
"""자기충돌 행렬 검토 (md/개발플랜.md 8-07).

`collisions_updater`가 만든 행렬을 **그대로 믿지 않는다.** 이 스크립트는
MoveIt과 독립적으로 공식 collision 메시와 URDF 관절 제한만 써서, 인접하지
않은 링크 쌍이 정말 충돌하지 않는지 확인한다.

방법:
 1. 외부 공식 URDF에서 링크별 collision 메시 경로·origin과 관절 제한을 읽는다.
 2. 이진 STL 꼭짓점을 읽어 링크 좌표계 점군으로 둔다(표본 추출).
 3. 관절 제한 안에서 자세를 샘플링한다(격자 + 접힘 자세 + 난수).
 4. 1패스: 각 자세에서 인접하지 않은 쌍의 AABB 간극을 본다. 임계값보다
    가까우면 **거친 표본**으로 거리를 재고 쌍별 후보 자세를 모은다.
 5. 2패스: 쌍별 후보 자세 상위 몇 개에서만 **전수 계산**으로 최소거리를
    확정한다. 판정은 전수 계산 결과로만 한다.
 6. 최소거리가 임계값 아래인 쌍을 보고한다.

**계획을 통과시키기 위해 쌍을 제외하지 않는다.** 여기서 근접이 나오면 행렬이
잘못된 것이고, 그 사실을 기록한다.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robots.moveit.kinematics import (  # noqa: E402
    link_transforms,
    parse_urdf,
    rpy_matrix,
)

DEFAULT_REPO = Path("/home/asd/external/frcobot_ros2")
URDF_REL = "fairino_description/urdf/FR3WMS.urdf"
#: 링크당 점군 표본 수. 메시가 크므로 균등 간격으로 줄인다.
SAMPLE_POINTS = 4000
#: 근접 판정 임계값(m). 이보다 가까우면 "충돌 가능"으로 보고한다.
PROXIMITY_M = 0.010
#: 격자 자세 표본(관절당). 6관절 전수는 불가능하므로 격자 + 난수를 섞는다.
GRID = 5
RANDOM_SAMPLES = 20000
#: 쌍별 전수 계산 후보 자세 수(거친 표본 기준 최근접 자세들).
CANDIDATES_PER_PAIR = 8
SEED = 20260916


def floats(text, default=(0.0, 0.0, 0.0)):
    return tuple(default) if not text else tuple(float(v) for v in text.split())


def read_stl(path: Path, samples: int) -> np.ndarray:
    """이진 STL의 삼각형 꼭짓점을 읽어 표본 점군을 돌려준다."""
    raw = path.read_bytes()
    if raw[:5] == b"solid" and b"facet" in raw[:512]:
        points = []
        for line in raw.decode("utf-8", "ignore").splitlines():
            parts = line.split()
            if parts[:1] == ["vertex"]:
                points.append([float(p) for p in parts[1:4]])
        cloud = np.asarray(points, dtype=float)
    else:
        count = struct.unpack("<I", raw[80:84])[0]
        body = np.frombuffer(raw, dtype=np.uint8, count=count * 50, offset=84)
        body = body.reshape(count, 50)[:, 12:48].copy()
        cloud = body.view("<f4").reshape(count * 3, 3).astype(float)
    if len(cloud) > samples:
        step = len(cloud) // samples
        cloud = cloud[::step][:samples]
    return cloud


def coarse_distance(a: np.ndarray, b: np.ndarray, *, points: int = 300) -> float:
    """거친 표본 최소거리. 후보 자세를 고르는 데만 쓴다(판정 근거가 아니다)."""
    step_a = max(1, len(a) // points)
    step_b = max(1, len(b) // points)
    return float(np.sqrt(
        ((a[::step_a][:, None, :] - b[::step_b][None, :, :]) ** 2).sum(-1)
    ).min())


def exact_distance(a: np.ndarray, b: np.ndarray) -> float:
    """표본 점군 전수 최소거리. 관통 깊이는 계산하지 않는다."""
    best = math.inf
    for chunk in np.array_split(a, max(1, len(a) // 400)):
        d = np.sqrt(((chunk[:, None, :] - b[None, :, :]) ** 2).sum(-1))
        best = min(best, float(d.min()))
    return best


def world_aabb(cloud: np.ndarray, rotation: np.ndarray, position: np.ndarray):
    """회전한 점군의 축정렬 경계상자. 값싼 배제용이다."""
    corners = (rotation @ np.asarray([
        [x, y, z]
        for x in (cloud[:, 0].min(), cloud[:, 0].max())
        for y in (cloud[:, 1].min(), cloud[:, 1].max())
        for z in (cloud[:, 2].min(), cloud[:, 2].max())
    ]).T).T + position
    return corners.min(0), corners.max(0)


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPO
    urdf = repo / URDF_REL
    if not urdf.is_file():
        print(f"외부 URDF가 없다: {urdf}", file=sys.stderr)
        return 2
    joints, links = parse_urdf(urdf)
    movable = [j for j in joints if j["type"] == "revolute"]
    adjacency = {frozenset((j["parent"], j["child"])) for j in joints}

    clouds = {}
    mesh_info = {}
    for name, info in links.items():
        mesh_path = (urdf.parent / info["mesh"]).resolve()
        if not mesh_path.is_file():
            print(f"collision 메시 없음: {mesh_path}", file=sys.stderr)
            return 3
        cloud = read_stl(mesh_path, SAMPLE_POINTS) * np.asarray(info["scale"])
        cloud = (rpy_matrix(*info["rpy"]) @ cloud.T).T + np.asarray(info["xyz"])
        clouds[name] = cloud
        mesh_info[name] = {
            "mesh": str(mesh_path.relative_to(repo)),
            "sha256": hashlib.sha256(mesh_path.read_bytes()).hexdigest()[:16],
            "points_sampled": int(len(cloud)),
            "extent_m": [round(float(v), 4)
                         for v in (cloud.max(0) - cloud.min(0))],
        }

    names = sorted(clouds)
    pairs = [(a, b) for a, b in itertools.combinations(names, 2)
             if frozenset((a, b)) not in adjacency]

    # 자세 표본: 제한 격자 + 접힘(양 끝) 자세 + 난수
    grids = []
    for joint in movable:
        low, high = joint["limit"]["lower"], joint["limit"]["upper"]
        grids.append([low + (high - low) * i / (GRID - 1) for i in range(GRID)])
    rng = random.Random(SEED)
    poses = [dict(zip((j["name"] for j in movable), combo))
             for combo in itertools.product(*grids)]
    for _ in range(RANDOM_SAMPLES):
        poses.append({
            j["name"]: rng.uniform(j["limit"]["lower"], j["limit"]["upper"])
            for j in movable
        })

    #: 쌍별로 전수 계산할 후보 자세 수. 거친 표본이 최소를 놓칠 수 있어 여러 개 둔다.
    candidates_per_pair = CANDIDATES_PER_PAIR
    rough: dict[tuple[str, str], list[tuple[float, dict]]] = {p: [] for p in pairs}
    aabb_close = 0
    for angles in poses:
        tf = link_transforms(joints, angles)
        boxes = {n: world_aabb(clouds[n], tf[n][0], tf[n][1]) for n in names}
        for pair in pairs:
            a, b = pair
            low_a, high_a = boxes[a]
            low_b, high_b = boxes[b]
            # AABB 간극(음수면 겹침). 임계값보다 멀면 더 볼 필요가 없다.
            gap = float(np.max(np.maximum(low_a - high_b, low_b - high_a)))
            if gap > PROXIMITY_M:
                continue
            aabb_close += 1
            pa = (tf[a][0] @ clouds[a].T).T + tf[a][1]
            pb = (tf[b][0] @ clouds[b].T).T + tf[b][1]
            bucket = rough[pair]
            bucket.append((coarse_distance(pa, pb), dict(angles)))
            bucket.sort(key=lambda item: item[0])
            del bucket[candidates_per_pair:]

    worst: dict[tuple[str, str], dict] = {}
    checked_exact = 0
    for pair, bucket in rough.items():
        if not bucket:
            continue
        a, b = pair
        for coarse_value, angles in bucket:
            tf = link_transforms(joints, angles)
            pa = (tf[a][0] @ clouds[a].T).T + tf[a][1]
            pb = (tf[b][0] @ clouds[b].T).T + tf[b][1]
            distance = exact_distance(pa, pb)
            checked_exact += 1
            if pair not in worst or distance < worst[pair]["min_distance_m"]:
                worst[pair] = {
                    "pair": [a, b], "min_distance_m": round(distance, 5),
                    "coarse_distance_m": round(coarse_value, 5),
                    "angles_rad": {k: round(v, 4) for k, v in angles.items()},
                }

    close = sorted((v for v in worst.values()
                    if v["min_distance_m"] <= PROXIMITY_M),
                   key=lambda v: v["min_distance_m"])
    report = {
        "source": {
            "urdf": f"{URDF_REL} (외부 경로 {repo})",
            "urdf_sha256": hashlib.sha256(urdf.read_bytes()).hexdigest(),
            "collision_meshes": mesh_info,
        },
        "method": {
            "sample_points_per_link": SAMPLE_POINTS,
            "grid_per_joint": GRID,
            "random_samples": RANDOM_SAMPLES,
            "seed": SEED,
            "poses_evaluated": len(poses),
            "proximity_threshold_m": PROXIMITY_M,
            "candidates_per_pair": CANDIDATES_PER_PAIR,
            "aabb_close_events": aabb_close,
            "exact_distance_computations": checked_exact,
            "note": ("표본 점군 최소거리다. 관통 깊이는 계산하지 않는다."
                     " 거친 표본으로 후보 자세를 고르고 전수 계산으로 확정하므로,"
                     " 진짜 최소 자세를 놓칠 가능성이 남는다 — 이 한계를 결론에"
                     " 함께 적는다"),
        },
        "adjacent_pairs": sorted(sorted(p) for p in
                                 (set(x) for x in adjacency) if len(p) == 2),
        "non_adjacent_pairs_checked": [sorted(p) for p in pairs],
        "closest_per_pair": sorted(worst.values(),
                                   key=lambda v: v["min_distance_m"]),
        "pairs_within_threshold": close,
        "closest_pair": (None if not worst else min(
            worst.values(), key=lambda v: v["min_distance_m"])),
        "conclusion": (
            f"표본 {len(poses)}자세 범위에서 인접하지 않은 쌍의 최소거리가"
            f" 임계값 {PROXIMITY_M} m보다 크다 — 근접·충돌을 찾지 못했다."
            " 표본 방법의 한계상 '절대 충돌하지 않는다'는 증명은 아니다"
            if not close else
            f"근접 쌍 {len(close)}개 발견 — 자기충돌 행렬을 재검토해야 한다"
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
