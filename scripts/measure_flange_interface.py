#!/usr/bin/env python3
"""장착 계면(플랜지·어댑터) 치수를 **공식 메시에서 측정한다** (8-08).

값을 만들지 않는다. 공식 자산(FAIRINO wrist3_Link.STL, Robotiq
ur_to_robotiq_adapter.stl / robotiq_base.stl)의 삼각형 기하에서 재고, 방법과
출처·checksum을 결과에 함께 남긴다.

측정 방법:
 1. 이진/ASCII STL을 읽어 삼각형과 법선을 얻는다.
 2. 축(z) 방향 양 끝면을 찾는다: 법선이 ±z에 가깝고 z가 최대/최소인 삼각형 묶음.
 3. 그 면의 삼각형만 모아 **경계 에지**(한 번만 나오는 에지)를 찾고 루프로 묶는다.
 4. 각 루프에 원을 최소제곱으로 맞춘다 → 바깥 지름, 구멍 지름, 중심 맞춤 단
    (boss/recess) 지름, 구멍 중심의 반경(= PCD/2).
 5. 구멍 개수와 PCD는 같은 반경(±허용치)에 있는 원 루프의 개수로 센다.

한계(결과에 함께 적는다):
 - 메시는 CAD를 삼각형으로 근사한 것이다. 원은 다각형으로 표현되므로 지름은
   내접/외접 사이에서 흔들린다. 그래서 **허용 오차를 함께 보고한다**.
 - 나사 규격(M6 등)·공차·표면 처리는 메시에서 알 수 없다. 도면이 필요하다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

#: 면으로 볼 법선 기준(축과의 코사인).
FACE_NORMAL_COS = 0.995
#: 면 두께로 볼 z 허용치(m). 메시 근사 오차를 흡수한다.
FACE_SLAB_M = 0.0006
#: 같은 원으로 볼 반경 차이(m).
RADIUS_TOLERANCE_M = 0.0008
#: 원 맞춤 잔차가 이보다 크면 원이 아니라고 본다(m).
CIRCLE_RESIDUAL_M = 0.0006


def read_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """삼각형 꼭짓점 (n,3,3)과 법선 (n,3)을 돌려준다."""
    raw = path.read_bytes()
    if raw[:5] == b"solid" and b"facet" in raw[:2048]:
        normals, triangles, current = [], [], []
        for line in raw.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if parts[:2] == ["facet", "normal"]:
                normals.append([float(v) for v in parts[2:5]])
                current = []
            elif parts[:1] == ["vertex"]:
                current.append([float(v) for v in parts[1:4]])
                if len(current) == 3:
                    triangles.append(current)
        return np.asarray(triangles), np.asarray(normals)
    count = struct.unpack("<I", raw[80:84])[0]
    body = np.frombuffer(raw, dtype=np.uint8, count=count * 50, offset=84)
    body = body.reshape(count, 50)
    values = body[:, :48].copy().view("<f4").reshape(count, 4, 3).astype(float)
    return values[:, 1:4, :], values[:, 0, :]


def read_dae(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """COLLADA에서 삼각형과 법선을 읽는다. 단위·up_axis를 확인한다.

    충돌 메시는 단순화돼 구멍이 없는 경우가 있다(실측). 시각 메시(.dae)에는
    실제 형상이 들어 있어 계면 치수를 재려면 이쪽을 봐야 한다.
    """
    import xml.etree.ElementTree as ET

    namespace = {"c": "http://www.collada.org/2005/11/COLLADASchema"}
    root = ET.parse(path).getroot()
    unit = root.find("c:asset/c:unit", namespace)
    scale = float(unit.get("meter", "1")) if unit is not None else 1.0
    up = root.find("c:asset/c:up_axis", namespace)
    up_axis = (up.text or "Z_UP").strip() if up is not None else "Z_UP"

    triangles: list[np.ndarray] = []
    for mesh in root.iter(f"{{{namespace['c']}}}mesh"):
        sources: dict[str, np.ndarray] = {}
        for source in mesh.findall("c:source", namespace):
            array = source.find("c:float_array", namespace)
            if array is None:
                continue
            stride = int(
                source.find("c:technique_common/c:accessor", namespace).get("stride", "3")
            )
            values = np.fromstring(array.text, sep=" ")
            sources[source.get("id")] = values.reshape(-1, stride)
        vertices_node = mesh.find("c:vertices", namespace)
        vertex_source = None
        if vertices_node is not None:
            reference = vertices_node.find(
                "c:input[@semantic='POSITION']", namespace
            ).get("source").lstrip("#")
            vertex_source = sources.get(reference)
        for primitive in list(mesh.findall("c:triangles", namespace)) + list(
            mesh.findall("c:polylist", namespace)
        ):
            inputs = primitive.findall("c:input", namespace)
            offsets = {inp.get("semantic"): int(inp.get("offset", "0")) for inp in inputs}
            stride = max(offsets.values()) + 1
            indices = np.fromstring(primitive.find("c:p", namespace).text, sep=" ",
                                    dtype=int).reshape(-1, stride)
            position_index = indices[:, offsets["VERTEX"]]
            counts_node = primitive.find("c:vcount", namespace)
            if counts_node is not None:
                counts = np.fromstring(counts_node.text, sep=" ", dtype=int)
                if not np.all(counts == 3):
                    # 삼각형이 아닌 면은 팬 분할한다.
                    start = 0
                    for count in counts:
                        fan = position_index[start:start + count]
                        for i in range(1, count - 1):
                            triangles.append(vertex_source[[fan[0], fan[i], fan[i + 1]]])
                        start += count
                    continue
            triangles.extend(vertex_source[position_index].reshape(-1, 3, 3))
    if not triangles:
        raise ValueError(f"삼각형을 찾지 못했다: {path}")
    array = np.asarray(triangles, dtype=float) * scale
    if up_axis == "Y_UP":
        array = array[:, :, [0, 2, 1]]
        array[:, :, 2] *= -1
    a = array[:, 1, :] - array[:, 0, :]
    b = array[:, 2, :] - array[:, 0, :]
    normals = np.cross(a, b)
    return array, normals


def read_mesh(path: Path, axis: str = "z") -> tuple[np.ndarray, np.ndarray]:
    """메시를 읽고, 플랜지 축이 z가 되도록 회전한다.

    UR5e의 wrist_3 메시는 플랜지가 +y를 향한다(공식 URDF의 flange 조인트가
    rpy로 돌린다). 축을 맞추지 않으면 평면을 찾지 못한다(실측).
    """
    triangles, normals = read_dae(path) if path.suffix.lower() == ".dae" else read_stl(path)
    if axis == "z":
        return triangles, normals
    if axis == "y":
        # x축 +90°: (x, y, z) → (x, -z, y). 오른손 좌표계를 유지한다.
        rotation = np.array([[1.0, 0, 0], [0, 0, -1.0], [0, 1.0, 0]])
    elif axis == "x":
        # y축 -90°: (x, y, z) → (-z, y, x).
        rotation = np.array([[0, 0, -1.0], [0, 1.0, 0], [1.0, 0, 0]])
    else:
        raise ValueError(f"모르는 축: {axis}")
    return triangles @ rotation.T, normals @ rotation.T


def fit_circle(points: np.ndarray) -> tuple[np.ndarray, float, float]:
    """XY 점에 원을 최소제곱으로 맞춘다 → (중심, 반지름, 최대 잔차)."""
    x, y = points[:, 0], points[:, 1]
    matrix = np.column_stack([x, y, np.ones(len(points))])
    target = x**2 + y**2
    solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    center = np.array([solution[0] / 2, solution[1] / 2])
    radius = math.sqrt(max(0.0, solution[2] + center @ center))
    residual = float(np.abs(np.linalg.norm(points - center, axis=1) - radius).max())
    return center, radius, residual


def boundary_loops(triangles: np.ndarray, decimals: int = 6) -> list[np.ndarray]:
    """면 삼각형의 경계 에지를 루프로 묶는다. 각 루프가 원 하나(외곽·구멍)다."""
    key = lambda point: tuple(np.round(point, decimals))  # noqa: E731
    edge_count: dict[tuple, int] = defaultdict(int)
    for triangle in triangles:
        keys = [key(vertex) for vertex in triangle]
        for i in range(3):
            edge = tuple(sorted((keys[i], keys[(i + 1) % 3])))
            edge_count[edge] += 1
    border = [edge for edge, count in edge_count.items() if count == 1]
    graph: dict[tuple, list[tuple]] = defaultdict(list)
    for a, b in border:
        graph[a].append(b)
        graph[b].append(a)

    loops: list[np.ndarray] = []
    seen: set[tuple] = set()
    for start in graph:
        if start in seen:
            continue
        loop, stack = [], [start]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            loop.append(node)
            stack.extend(next_node for next_node in graph[node] if next_node not in seen)
        if len(loop) >= 6:
            loops.append(np.asarray(loop, dtype=float))
    return loops


def triangle_area(triangles: np.ndarray) -> np.ndarray:
    a = triangles[:, 1, :] - triangles[:, 0, :]
    b = triangles[:, 2, :] - triangles[:, 0, :]
    return 0.5 * np.linalg.norm(np.cross(a, b), axis=1)


def flat_planes(triangles: np.ndarray, normals: np.ndarray,
                *, min_area_m2: float = 1e-5) -> list[dict]:
    """축(z)에 수직한 평면을 모두 찾는다. 면적이 작은 평면은 버린다.

    끝면만 보면 돌출된 보스·리세스를 놓친다(실측으로 확인). 그래서 평면을
    전부 열거해 각 평면의 원(외곽·구멍·중심 단)을 측정한다.
    """
    unit = normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    axis_normal = np.abs(unit[:, 2]) >= FACE_NORMAL_COS
    if not axis_normal.any():
        return []
    areas = triangle_area(triangles)
    levels: list[dict] = []
    z_mean = triangles[:, :, 2].mean(axis=1)
    candidates = sorted(set(np.round(z_mean[axis_normal] / FACE_SLAB_M).astype(int)))
    for bucket in candidates:
        plane = bucket * FACE_SLAB_M
        on_plane = axis_normal & (
            np.abs(triangles[:, :, 2] - plane).max(axis=1) <= FACE_SLAB_M
        )
        if not on_plane.any():
            continue
        area = float(areas[on_plane].sum())
        if area < min_area_m2:
            continue
        plane_z = float(np.median(z_mean[on_plane]))
        if any(abs(plane_z - item["plane_m"]) <= FACE_SLAB_M * 2 for item in levels):
            continue
        facing = float(np.sign(unit[on_plane, 2].mean()))
        levels.append({
            "plane_m": round(plane_z, 6),
            "facing": "+z" if facing > 0 else "-z",
            "area_m2": round(area, 8),
            "triangle_count": int(on_plane.sum()),
            **measure_plane(triangles[on_plane]),
        })
    return sorted(levels, key=lambda item: item["plane_m"])


def measure_plane(face: np.ndarray) -> dict:
    """한 평면의 경계 루프를 원으로 맞춰 치수를 낸다."""
    circles = []
    for loop in boundary_loops(face):
        center, radius, residual = fit_circle(loop[:, :2])
        circles.append({
            "center_xy_m": [round(float(v), 6) for v in center],
            "diameter_m": round(radius * 2, 6),
            "points": int(len(loop)),
            "fit_residual_m": round(residual, 6),
            "is_circle": residual <= CIRCLE_RESIDUAL_M,
        })
    circles.sort(key=lambda item: item["diameter_m"])
    round_circles = [c for c in circles if c["is_circle"]]
    outer = max(round_circles, key=lambda c: c["diameter_m"], default=None)
    off_axis = [
        c for c in round_circles
        if c is not outer and float(np.linalg.norm(c["center_xy_m"])) > 0.004
    ]
    groups: dict[float, list[dict]] = defaultdict(list)
    for hole in off_axis:
        pitch_radius = float(np.linalg.norm(hole["center_xy_m"]))
        bucket = next(
            (key for key in groups if abs(key - pitch_radius) <= RADIUS_TOLERANCE_M),
            pitch_radius,
        )
        groups[bucket].append(hole)
    patterns = [
        {
            "pcd_m": round(radius * 2, 6),
            "hole_count": len(items),
            "hole_diameter_m": round(
                float(np.mean([item["diameter_m"] for item in items])), 6
            ),
            "hole_diameter_spread_m": round(
                float(np.ptp([item["diameter_m"] for item in items])), 6
            ),
            "angles_deg": sorted(
                round(math.degrees(math.atan2(item["center_xy_m"][1],
                                              item["center_xy_m"][0])) % 360, 2)
                for item in items
            ),
        }
        for radius, items in sorted(groups.items())
    ]
    centred = [
        c for c in round_circles
        if float(np.linalg.norm(c["center_xy_m"])) <= 0.004
    ]
    return {
        "outer_diameter_m": None if outer is None else outer["diameter_m"],
        "centered_diameters_m": [c["diameter_m"] for c in centred],
        "bolt_patterns": patterns,
        "non_circular_loops": sum(1 for c in circles if not c["is_circle"]),
    }


def face_at(triangles: np.ndarray, normals: np.ndarray, *, top: bool) -> dict:
    """축 방향 끝면을 찾아 원 루프를 측정한다."""
    axis = 2
    unit = normals / np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    direction = 1.0 if top else -1.0
    flat = unit[:, axis] * direction >= FACE_NORMAL_COS
    if not flat.any():
        return {"found": False, "detail": "축에 수직한 면을 찾지 못했다"}
    z_values = triangles[flat][:, :, axis]
    plane = float(z_values.max() if top else z_values.min())
    on_plane = flat & (
        np.abs(triangles[:, :, axis] - plane).max(axis=1) <= FACE_SLAB_M
    )
    face = triangles[on_plane]
    if len(face) < 4:
        return {"found": False, "detail": "끝면 삼각형이 너무 적다", "plane_m": plane}

    circles = []
    for loop in boundary_loops(face):
        center, radius, residual = fit_circle(loop[:, :2])
        circles.append({
            "center_xy_m": [round(float(v), 6) for v in center],
            "radius_m": round(radius, 6),
            "diameter_m": round(radius * 2, 6),
            "points": int(len(loop)),
            "fit_residual_m": round(residual, 6),
            "is_circle": residual <= CIRCLE_RESIDUAL_M,
        })
    circles.sort(key=lambda item: item["radius_m"])

    outer = max((c for c in circles if c["is_circle"]), key=lambda c: c["radius_m"],
                default=None)
    holes = [
        c for c in circles
        if c["is_circle"] and c is not outer
        and np.linalg.norm(c["center_xy_m"]) > 0.004
    ]
    groups: dict[float, list[dict]] = defaultdict(list)
    for hole in holes:
        pitch_radius = float(np.linalg.norm(hole["center_xy_m"]))
        bucket = next(
            (key for key in groups if abs(key - pitch_radius) <= RADIUS_TOLERANCE_M),
            pitch_radius,
        )
        groups[bucket].append(hole)
    patterns = [
        {
            "pitch_radius_m": round(radius, 6),
            "pcd_m": round(radius * 2, 6),
            "hole_count": len(items),
            "hole_diameter_m": round(
                float(np.mean([item["diameter_m"] for item in items])), 6
            ),
            "hole_diameter_spread_m": round(
                float(np.ptp([item["diameter_m"] for item in items])), 6
            ),
            "angles_deg": sorted(
                round(math.degrees(math.atan2(*reversed(item["center_xy_m"]))) % 360, 2)
                for item in items
            ),
        }
        for radius, items in sorted(groups.items())
    ]
    centred = [
        c for c in circles
        if c["is_circle"] and np.linalg.norm(c["center_xy_m"]) <= 0.004
    ]
    return {
        "found": True,
        "plane_m": round(plane, 6),
        "triangle_count": int(len(face)),
        "outer_diameter_m": None if outer is None else outer["diameter_m"],
        "centered_circles_m": [c["diameter_m"] for c in centred],
        "bolt_patterns": patterns,
        "all_circles": circles,
    }


def commit_of(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def measure(path: Path, repo: Path, *, note: str, axis: str = "z") -> dict:
    triangles, normals = read_mesh(path, axis)
    bounds = {
        "min_m": [round(float(v), 6) for v in triangles.reshape(-1, 3).min(axis=0)],
        "max_m": [round(float(v), 6) for v in triangles.reshape(-1, 3).max(axis=0)],
    }
    return {
        "source": {
            "path": str(path),
            "repo": str(repo),
            "commit": commit_of(repo),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "triangles": int(len(triangles)),
            "note": note,
        },
        "flange_axis": axis,
        "bounds": bounds,
        "distal_face": face_at(triangles, normals, top=True),
        "proximal_face": face_at(triangles, normals, top=False),
        "flat_planes": flat_planes(triangles, normals),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fr3-repo", default="/home/asd/external/frcobot_ros2", type=Path
    )
    parser.add_argument(
        "--robotiq-share",
        default="/opt/ros/lyrical/share/robotiq_description",
        type=Path,
    )
    parser.add_argument(
        "--robotiq-repo", default="/home/asd/external/robotiq_ros", type=Path
    )
    parser.add_argument("--out", default=ROOT / "reports/mounting/flange_measurements.json",
                        type=Path)
    args = parser.parse_args()

    targets = [
        (
            "fr3_wrist3_flange",
            args.fr3_repo / "fairino_description/meshes/FR3WMS/collision/wrist3_Link.STL",
            args.fr3_repo,
            "FR3-WMS 손목3 링크. 말단면이 공구 플랜지다(공식 URDF의 tool 조인트"
            " origin z=0.1 m). 라이선스 미확인 자산 — 참조·측정만 한다.",
        ),
        (
            "robotiq_ur_adapter",
            args.robotiq_share / "meshes/collision/ur_to_robotiq_adapter.stl",
            args.robotiq_repo,
            "Robotiq 공식 UR↔2F-85 어댑터. BSD-3-Clause. 로봇 측 계면이 ISO"
            " 9409-1-50-4-M6(UR 말단)이다.",
        ),
        (
            "robotiq_85_base",
            args.robotiq_share / "meshes/collision/robotiq_base.stl",
            args.robotiq_repo,
            "Robotiq 2F-85 본체 base. robotiq_85_base_link 원점 기준 형상.",
        ),
        (
            "robotiq_ur_adapter_visual",
            args.robotiq_share / "meshes/visual/ur_to_robotiq_adapter.dae",
            args.robotiq_repo,
            "같은 어댑터의 시각 메시. 충돌 메시는 단순화돼 구멍이 없다 —"
            " 볼트 패턴·보스는 이쪽에서 재야 한다.",
        ),
        (
            "ur5e_wrist3_flange",
            Path("/opt/ros/lyrical/share/ur_description/meshes/ur5e/collision/wrist3.stl"),
            Path("/opt/ros/lyrical/share/ur_description"),
            "UR5e 손목3. Robotiq 어댑터가 설계된 상대 플랜지다. 공식 URDF에서"
            " flange 프레임이 +y를 향하므로 축을 y로 잡는다.",
            "y",
        ),
        (
            "robotiq_85_base_visual",
            args.robotiq_share / "meshes/visual/robotiq_base.dae",
            args.robotiq_repo,
            "2F-85 base의 시각 메시. 그리퍼 측 장착면과 볼트 패턴을 확인한다.",
        ),
    ]

    report = {"measured_at": None, "method": {
        "face_normal_cos": FACE_NORMAL_COS,
        "face_slab_m": FACE_SLAB_M,
        "radius_tolerance_m": RADIUS_TOLERANCE_M,
        "circle_residual_m": CIRCLE_RESIDUAL_M,
        "limits": [
            "메시는 CAD의 삼각형 근사다. 원 지름은 내접·외접 사이에서 흔들린다",
            "나사 규격·공차·표면 처리는 메시에서 알 수 없다(도면 필요)",
            "구멍이 관통이면 반대 면에서도 같은 원이 보인다",
        ],
    }, "parts": {}}

    import time

    report["measured_at"] = round(time.time(), 3)
    for name, path, repo, note, *rest in targets:
        if not path.is_file():
            report["parts"][name] = {"found": False, "path": str(path),
                                     "detail": "자산이 없다"}
            continue
        axis = rest[0] if rest else "z"
        report["parts"][name] = measure(path, repo, note=note, axis=axis)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"{args.out} 기록")
    for name, part in report["parts"].items():
        if not part.get("bounds"):
            print(f"  {name}: {part.get('detail')}")
            continue
        distal = part["distal_face"]
        proximal = part["proximal_face"]
        print(f"  {name}: z {part['bounds']['min_m'][2]} ~ {part['bounds']['max_m'][2]} m")
        for plane in part.get("flat_planes", []):
            pattern = "".join(
                f" | 구멍 {p['hole_count']}개 PCD {p['pcd_m']} ø{p['hole_diameter_m']}"
                f" {p['angles_deg']}"
                for p in plane["bolt_patterns"]
            )
            print(
                f"    평면 z={plane['plane_m']:+.5f} {plane['facing']}"
                f" 면적={plane['area_m2']:.6f} 외경={plane['outer_diameter_m']}"
                f" 중심원={plane['centered_diameters_m']}{pattern}"
            )
        for label, face in (("distal", distal), ("proximal", proximal)):
            if not face.get("found"):
                print(f"    {label}: {face.get('detail')}")
                continue
            print(
                f"    {label} z={face['plane_m']} 외경={face['outer_diameter_m']}"
                f" 중심원={face['centered_circles_m']}"
            )
            for pattern in face["bolt_patterns"]:
                print(
                    f"      구멍 {pattern['hole_count']}개 · PCD {pattern['pcd_m']}"
                    f" · 지름 {pattern['hole_diameter_m']}"
                    f" (편차 {pattern['hole_diameter_spread_m']})"
                    f" · 각도 {pattern['angles_deg']}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
