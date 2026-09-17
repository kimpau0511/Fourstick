#!/usr/bin/env python3
"""Robotiq 2F-85 개방폭을 **URDF 기구학으로** 계산한다 (md/개발플랜.md 8-02).

왜 필요한가: 명령 관절 각도(0.0~0.8 rad)와 패드 간격(mm)은 **선형 동일값이
아니다.** 2F-85는 4절 링크이므로 각도-간격 관계가 비선형이다. 카탈로그의
"85mm"를 관절 상한에 그대로 붙이지 않고, 공식 URDF의 링크·조인트 원점으로
정기구학을 계산해 근거 있는 값을 만든다.

한계(그대로 기록한다):
- 여기서 계산하는 것은 **양쪽 finger_tip 링크 원점 사이의 거리**다. 실제 패드
  표면 간격은 메시(STL) 형상에 있고, 이 계산에는 포함되지 않는다. 그래서
  결과는 `tip_frame_separation`이라는 이름으로만 쓰고 `pad_aperture`로
  승격하지 않는다.
- 제조사 데이터시트를 확보하면 그 값과 대조해야 한다.

자산은 **외부 경로**에서 읽는다. 저장소에 복사하지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

DEFAULT_REPO = Path("/home/asd/external/robotiq_ros")
MACRO = "grippers/robotiq_description/urdf/robotiq_2f_85_macro.urdf.xacro"
GRIPPER = "grippers/robotiq_description/urdf/robotiq_2f_85_gripper.urdf.xacro"
#: 명령 관절과 상한(URDF limit). 계산은 이 값을 읽어 쓴다.
COMMAND_JOINT = "robotiq_85_left_knuckle_joint"


def rpy_matrix(roll: float, pitch: float, yaw: float) -> list[list[float]]:
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def axis_matrix(axis: tuple[float, float, float], angle: float) -> list[list[float]]:
    """축-각 회전(로드리게스). URDF revolute/continuous 조인트에 쓴다."""
    norm = math.sqrt(sum(a * a for a in axis)) or 1.0
    x, y, z = (a / norm for a in axis)
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def mat_mul(a, b):
    return [
        [sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
        for i in range(3)
    ]


def mat_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def compose(parent_r, parent_p, child_r, child_p):
    return mat_mul(parent_r, child_r), tuple(
        p + q for p, q in zip(parent_p, mat_vec(parent_r, child_p))
    )


def parse_floats(text: str | None, default=(0.0, 0.0, 0.0)):
    if not text:
        return default
    return tuple(float(v) for v in text.split())


def expand(repo: Path) -> str:
    """xacro를 URDF로 펼친다. ROS 환경을 source해서 공식 도구를 쓴다."""
    command = (
        "source /opt/ros/lyrical/setup.bash >/dev/null 2>&1 && "
        f"AMENT_PREFIX_PATH=$AMENT_PREFIX_PATH ros2 run xacro xacro "
        f"'{repo / GRIPPER}'"
    )
    result = subprocess.run(
        ["bash", "-lc", command], capture_output=True, text=True, timeout=180
    )
    if result.returncode != 0:
        raise SystemExit(f"xacro 확장 실패:\n{result.stderr[-2000:]}")
    return result.stdout


def build_tree(urdf_text: str):
    root = ET.fromstring(urdf_text)
    joints = {}
    for joint in root.findall("joint"):
        name = joint.get("name")
        origin = joint.find("origin")
        mimic = joint.find("mimic")
        limit = joint.find("limit")
        joints[name] = {
            "name": name,
            "type": joint.get("type"),
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "xyz": parse_floats(None if origin is None else origin.get("xyz")),
            "rpy": parse_floats(None if origin is None else origin.get("rpy")),
            "axis": parse_floats(
                None if joint.find("axis") is None else joint.find("axis").get("xyz"),
                (1.0, 0.0, 0.0),
            ),
            "mimic": None if mimic is None else {
                "joint": mimic.get("joint"),
                "multiplier": float(mimic.get("multiplier", "1")),
                "offset": float(mimic.get("offset", "0")),
            },
            "limit": None if limit is None else {
                "lower": float(limit.get("lower", "0")),
                "upper": float(limit.get("upper", "0")),
                "velocity": float(limit.get("velocity", "0")),
                "effort": float(limit.get("effort", "0")),
            },
        }
    masses = {}
    for link in root.findall("link"):
        inertial = link.find("inertial")
        if inertial is None:
            continue
        mass = inertial.find("mass")
        com = inertial.find("origin")
        masses[link.get("name")] = {
            "mass": None if mass is None else float(mass.get("value")),
            "com": parse_floats(None if com is None else com.get("xyz")),
        }
    return joints, masses


def chain_to(joints, link: str) -> list[dict]:
    """링크까지의 조인트 사슬(루트 → 링크)."""
    by_child = {j["child"]: j for j in joints.values()}
    out = []
    while link in by_child:
        joint = by_child[link]
        out.append(joint)
        link = joint["parent"]
    return list(reversed(out))


def link_pose(joints, link: str, q: float):
    """명령 관절 값 q에서 링크 원점 위치. mimic 조인트를 반영한다."""
    rotation = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    position = (0.0, 0.0, 0.0)
    for joint in chain_to(joints, link):
        rotation, position = compose(
            rotation, position, rpy_matrix(*joint["rpy"]), joint["xyz"]
        )
        angle = 0.0
        if joint["type"] in ("revolute", "continuous"):
            if joint["name"] == COMMAND_JOINT:
                angle = q
            elif joint["mimic"] and joint["mimic"]["joint"] == COMMAND_JOINT:
                angle = joint["mimic"]["multiplier"] * q + joint["mimic"]["offset"]
        if angle:
            rotation = mat_mul(rotation, axis_matrix(joint["axis"], angle))
    return rotation, position


def separation(joints, q: float) -> dict:
    _, left = link_pose(joints, "robotiq_85_left_finger_tip_link", q)
    _, right = link_pose(joints, "robotiq_85_right_finger_tip_link", q)
    delta = tuple(a - b for a, b in zip(left, right))
    return {
        "q_rad": q,
        "left_tip_xyz_m": [round(v, 6) for v in left],
        "right_tip_xyz_m": [round(v, 6) for v in right],
        "tip_frame_separation_m": round(
            math.sqrt(sum(d * d for d in delta)), 6
        ),
        "separation_axis_m": [round(v, 6) for v in delta],
    }


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_REPO
    if not repo.is_dir():
        print(f"외부 자산 경로가 없다: {repo}", file=sys.stderr)
        return 2
    macro_path = repo / MACRO
    checksum = hashlib.sha256(macro_path.read_bytes()).hexdigest()
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    tag = subprocess.run(
        ["git", "-C", str(repo), "describe", "--tags"],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()

    joints, masses = build_tree(expand(repo))
    command = joints[COMMAND_JOINT]
    lower = command["limit"]["lower"]
    upper = command["limit"]["upper"]

    samples = [separation(joints, q) for q in (lower, upper)]
    mid = separation(joints, (lower + upper) / 2)
    open_sep = samples[0]["tip_frame_separation_m"]
    closed_sep = samples[1]["tip_frame_separation_m"]
    midpoint_linear = (open_sep + closed_sep) / 2

    gripper_mass = sum(
        info["mass"] for name, info in masses.items()
        if name.startswith("robotiq_85") and info["mass"]
    )

    report = {
        "source": {
            "repo": str(repo), "tag": tag, "commit": commit,
            "file": MACRO, "sha256": checksum,
            "method": "URDF 링크·조인트 원점으로 정기구학 계산(mimic 반영)",
        },
        "command_joint": {
            "name": COMMAND_JOINT, "lower_rad": lower, "upper_rad": upper,
            "velocity_rad_s": command["limit"]["velocity"],
            "effort_Nm": command["limit"]["effort"],
        },
        "mimic_joints": {
            name: joint["mimic"]["multiplier"]
            for name, joint in joints.items()
            if joint["mimic"] and joint["mimic"]["joint"] == COMMAND_JOINT
        },
        "tip_frame_separation": {
            "at_lower_limit_m": open_sep,
            "at_upper_limit_m": closed_sep,
            "at_midpoint_m": mid["tip_frame_separation_m"],
            "linear_midpoint_m": round(midpoint_linear, 6),
            "nonlinearity_m": round(
                abs(mid["tip_frame_separation_m"] - midpoint_linear), 6
            ),
        },
        "mass_kg": {
            "sum_of_gripper_links": round(gripper_mass, 6),
            "note": "URDF inertial mass 합. 제조사 표기 질량과 대조해야 한다",
        },
        "samples": samples + [mid],
        "limits": {
            "note": "tip_frame_separation은 패드 표면 간격이 아니다."
                    " 패드 두께·형상은 메시(STL)에 있고 이 계산에 없다.",
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
