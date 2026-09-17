"""URDF 순기구학 보조 (8-07).

외부 공식 URDF의 값만 읽어 링크 프레임을 계산한다. **수치를 만들지 않는다.**
검증 스크립트가 "이 자세에서 링크가 어디에 있는가"를 알아야 할 때 쓴다
(예: 환경 충돌 검사용 물체를 실제로 팔이 지나가는 위치에 놓기).

MoveIt의 기구학을 대신하지 않는다 — 판정은 MoveIt이 하고, 여기 값은 검증
입력을 만드는 데만 쓴다.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np


def rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch),
                              math.sin(pitch), math.cos(yaw), math.sin(yaw))
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def axis_matrix(axis: Sequence[float], angle: float) -> np.ndarray:
    vector = np.asarray(axis, dtype=float)
    norm = float(np.linalg.norm(vector)) or 1.0
    x, y, z = vector / norm
    c, s, t = math.cos(angle), math.sin(angle), 1 - math.cos(angle)
    return np.array([
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ])


def _floats(text: str | None, default=(0.0, 0.0, 0.0)) -> tuple[float, ...]:
    return tuple(default) if not text else tuple(float(v) for v in text.split())


def parse_urdf(path: Path | str) -> tuple[list[dict[str, Any]], dict[str, dict]]:
    """조인트 목록과 collision 메시 정보를 읽는다. URDF 값을 그대로 옮긴다."""
    root = ET.parse(str(path)).getroot()
    joints: list[dict[str, Any]] = []
    for joint in root.findall("joint"):
        origin, limit, axis = (joint.find("origin"), joint.find("limit"),
                               joint.find("axis"))
        joints.append({
            "name": joint.get("name"), "type": joint.get("type"),
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "xyz": _floats(None if origin is None else origin.get("xyz")),
            "rpy": _floats(None if origin is None else origin.get("rpy")),
            "axis": _floats(None if axis is None else axis.get("xyz"), (0, 0, 1.0)),
            "limit": None if limit is None else {
                "lower": float(limit.get("lower")),
                "upper": float(limit.get("upper")),
                "velocity": (float(limit.get("velocity"))
                             if limit.get("velocity") else None),
                "effort": (float(limit.get("effort"))
                           if limit.get("effort") else None),
            },
        })
    links: dict[str, dict] = {}
    for link in root.findall("link"):
        collision = link.find("collision")
        mesh = None if collision is None else collision.find("geometry/mesh")
        if mesh is None:
            continue
        origin = collision.find("origin")
        links[link.get("name")] = {
            "mesh": mesh.get("filename"),
            "scale": _floats(mesh.get("scale"), (1.0, 1.0, 1.0)),
            "xyz": _floats(None if origin is None else origin.get("xyz")),
            "rpy": _floats(None if origin is None else origin.get("rpy")),
        }
    return joints, links


def link_transforms(
    joints: Sequence[Mapping[str, Any]], angles: Mapping[str, float],
) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """링크별 (회전, 위치)를 트리 뿌리 기준으로 계산한다.

    **트리를 따라 계산한다.** 파일 순서대로 직렬 연결이라고 가정하면 분기가
    있는 모델(좌·우 손가락, TCP 분기)에서 값이 전부 틀린다 — Gazebo 실측과
    대조해 실제로 확인한 문제다(손가락 끝 위치 0.13 m, TCP 0.23 m 오차).

    `angles`에 없는 회전 조인트는 0으로 둔다(mimic은 호출자가 채운다).
    """
    children: dict[str, list[Mapping[str, Any]]] = {}
    child_links = set()
    for joint in joints:
        children.setdefault(joint["parent"], []).append(joint)
        child_links.add(joint["child"])

    roots = [name for name in children if name not in child_links]
    if not roots:
        roots = [joints[0]["parent"]]

    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for root in roots:
        out[root] = (np.eye(3), np.zeros(3))
        stack = [root]
        while stack:
            parent = stack.pop()
            parent_rotation, parent_position = out[parent]
            for joint in children.get(parent, []):
                position = parent_position + parent_rotation @ np.asarray(joint["xyz"])
                rotation = parent_rotation @ rpy_matrix(*joint["rpy"])
                if joint["type"] in ("revolute", "continuous"):
                    rotation = rotation @ axis_matrix(
                        joint["axis"], float(angles.get(joint["name"], 0.0))
                    )
                out[joint["child"]] = (rotation, position)
                stack.append(joint["child"])
    return out


def revolute_limits(joints: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    return {j["name"]: j["limit"] for j in joints
            if j["type"] == "revolute" and j["limit"] is not None}
