#!/usr/bin/env python3
"""그리퍼 제어·프레임 진단 (md/개발플랜.md 8-08 우선순위 1·2).

Gazebo의 **실제 링크 자세**(`/world/*/pose/info`)를 기준(ground truth)으로 삼아
두 가지를 따로 확인한다.

1. 제어: 명령 관절값 → 관측 관절값 → 관측 개구(패드면 사이 거리).
   `reached_goal`을 믿지 않는다. 관측 개구가 목표와 다르면 실패다.
2. 프레임: FR3 flange → 커플링 → 그리퍼 base → 패드 접촉면 → TCP 변환을
   **FK 예측과 Gazebo 실측으로 대조**한다. 축 방향이 어긋나면 여기서 드러난다.

중요: URDF→SDF 변환은 **고정 조인트를 접는다**(fixed joint reduction). 그래서
Gazebo에는 `tool_Link`·`ur_to_robotiq_link`·`robotiq_85_base_link`·
`robotiq_85_*_finger_link`가 개별 링크로 존재하지 않는다 — 부모 링크에 합쳐진다.
이 스크립트는 **살아 있는 링크**(`wrist3_Link`, knuckle, finger_tip)만 읽고,
합쳐진 프레임은 FK로 계산해 비교한다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robots.moveit.kinematics import link_transforms, parse_urdf

URDF = Path("/tmp/forstick2_gazebo/urdf/fr3wms_with_2f85.urdf")
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
WORLD = "forstick2_fr3_cell"
MODEL = "fr3wms_2f85"
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"


def quaternion_matrix(orientation: dict) -> np.ndarray:
    x = orientation.get("x", 0.0)
    y = orientation.get("y", 0.0)
    z = orientation.get("z", 0.0)
    w = orientation.get("w", 0.0)
    norm = (x * x + y * y + z * z + w * w) ** 0.5
    if norm == 0:
        return np.eye(3)
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def gazebo_poses(timeout_sec: int = 30) -> dict:
    """Gazebo가 발행하는 링크 자세를 읽는다. **관측 기준값이다.**

    pose/info는 부모 프레임 기준 상대 자세를 준다. 모델 트리를 따라 곱해
    world 기준으로 만든다.
    """
    result = subprocess.run(
        ["gz", "topic", "-e", "-t", f"/world/{WORLD}/pose/info", "-n", "1",
         "--json-output"],
        capture_output=True, text=True, timeout=timeout_sec,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"pose/info를 읽지 못했다: {result.stderr[:200]}")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    out = {}
    for entry in payload.get("pose", []):
        position = entry.get("position", {})
        out[entry["name"]] = {
            "xyz": np.array([position.get("x", 0.0), position.get("y", 0.0),
                             position.get("z", 0.0)]),
            "rotation": quaternion_matrix(entry.get("orientation", {})),
            "id": entry.get("id"),
        }
    return out


def joint_states(timeout_sec: float = 5.0) -> dict:
    """ros2_control이 보고하는 관절값. rclpy로 직접 읽는다.

    (CLI `ros2 topic echo --field`는 출력 형식이 달라 파싱이 어긋났다 — 실측으로
    확인하고 rclpy로 바꿨다.)
    """
    import time

    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import JointState

    started_here = not rclpy.ok()
    if started_here:
        rclpy.init()
    node = Node("forstick2_frame_diag")
    latest: dict = {}

    def on_state(message: JointState) -> None:
        latest.clear()
        latest.update(dict(zip(message.name, message.position)))

    node.create_subscription(JointState, "/joint_states", on_state, 20)
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and not latest:
        rclpy.spin_once(node, timeout_sec=0.1)
    node.destroy_node()
    if started_here:
        rclpy.shutdown()
    return dict(latest)


def main() -> int:
    if not URDF.is_file():
        print(f"조립 URDF가 없다: {URDF}", file=sys.stderr)
        return 2
    joints, links = parse_urdf(URDF)
    profile = json.loads(MOUNTING.read_text(encoding="utf-8"))
    pad_face = profile["gripper_official"]["pad_face"]
    tcp_xyz = [item["value"] for item in profile["tcp_xyz"]]

    states = joint_states()
    poses = gazebo_poses()
    gripper_value = states.get(GRIPPER_JOINT)
    angles = {name: states.get(name, 0.0) for name in ARM_JOINTS}
    if gripper_value is not None:
        angles[GRIPPER_JOINT] = gripper_value
        # mimic 관절도 같은 값에서 파생한다(URDF의 multiplier를 그대로 쓴다).
        import xml.etree.ElementTree as ET

        root = ET.parse(URDF).getroot()
        for joint in root.findall("joint"):
            mimic = joint.find("mimic")
            if mimic is not None and mimic.get("joint") == GRIPPER_JOINT:
                angles[joint.get("name")] = (
                    gripper_value * float(mimic.get("multiplier", "1"))
                    + float(mimic.get("offset", "0"))
                )

    transforms = link_transforms(joints, angles)

    # ── 프레임 체인 표 ──────────────────────────────────────────────────
    chain = ["wrist3_Link", "tool_Link", "ur_to_robotiq_link", "gripper_mount_link",
             "robotiq_85_base_link", "robotiq_85_left_finger_tip_link",
             "robotiq_85_tcp"]
    rows = []
    for name in chain:
        if name not in transforms:
            rows.append({"frame": name, "present_in_urdf": False})
            continue
        rotation, position = transforms[name]
        observed = poses.get(name)
        row = {
            "frame": name,
            "present_in_urdf": True,
            "fk_world_xyz_m": [round(float(v), 6) for v in position],
            "fk_world_z_axis": [round(float(v), 4) for v in rotation[:, 2]],
            "in_gazebo": observed is not None,
        }
        if observed is not None:
            row["gazebo_world_xyz_m"] = [round(float(v), 6) for v in observed["xyz"]]
            row["position_error_m"] = round(
                float(np.linalg.norm(observed["xyz"] - position)), 6
            )
            row["gazebo_world_z_axis"] = [
                round(float(v), 4) for v in observed["rotation"][:, 2]
            ]
            row["z_axis_angle_deg"] = round(float(np.degrees(np.arccos(
                np.clip(rotation[:, 2] @ observed["rotation"][:, 2], -1, 1)
            ))), 3)
        else:
            row["note"] = ("고정 조인트가 접혀 Gazebo 링크로 남지 않았다"
                           " (fixed joint reduction). FK로만 확인한다")
        rows.append(row)

    # ── 패드면·개구: Gazebo 실측 기준 ──────────────────────────────────
    pad_local = np.array([pad_face["x_local_m"], 0.0,
                          sum(pad_face["z_range_local_m"]) / 2])
    aperture = {}
    left = poses.get("robotiq_85_left_finger_tip_link")
    right = poses.get("robotiq_85_right_finger_tip_link")
    if left is not None and right is not None:
        left_pad = left["rotation"] @ pad_local + left["xyz"]
        # 오른쪽 패드는 x가 반대다(대칭).
        right_pad = right["rotation"] @ (pad_local * np.array([-1, 1, 1])) + right["xyz"]
        aperture["gazebo_pad_gap_m"] = round(
            float(np.linalg.norm(left_pad - right_pad)), 6
        )
        aperture["left_pad_world_m"] = [round(float(v), 6) for v in left_pad]
        aperture["right_pad_world_m"] = [round(float(v), 6) for v in right_pad]
        aperture["tcp_from_pads_world_m"] = [
            round(float(v), 6) for v in (left_pad + right_pad) / 2
        ]
    if "robotiq_85_tcp" in transforms:
        aperture["tcp_from_urdf_world_m"] = [
            round(float(v), 6) for v in transforms["robotiq_85_tcp"][1]
        ]
        if "tcp_from_pads_world_m" in aperture:
            aperture["tcp_definition_error_m"] = round(float(np.linalg.norm(
                np.array(aperture["tcp_from_pads_world_m"])
                - np.array(aperture["tcp_from_urdf_world_m"])
            )), 6)

    # 표에서 기대되는 개구(관측 관절값 기준)
    from core.aperture_model import ApertureModel

    model = ApertureModel.from_rows(
        profile["transform_derivation"]["aperture_table"],
        source=profile["mounting_profile_version"],
    )
    if gripper_value is not None:
        reading = model.observe(gripper_value, edge_tolerance_rad=0.02)
        aperture["joint_rad"] = round(gripper_value, 6)
        aperture["table_aperture_m"] = reading["aperture_m"]
        if reading["aperture_m"] is not None and "gazebo_pad_gap_m" in aperture:
            aperture["table_vs_gazebo_error_m"] = round(
                abs(reading["aperture_m"] - aperture["gazebo_pad_gap_m"]), 6
            )

    report = {
        "world": WORLD,
        "model": MODEL,
        "joint_states": {k: round(v, 6) for k, v in states.items()},
        "tcp_offset_from_profile_m": tcp_xyz,
        "frame_chain": rows,
        "aperture": aperture,
        "gazebo_links": sorted(poses),
        "notes": [
            "Gazebo 링크 자세가 기준값이다. FK와의 차이가 프레임 정의 오차다",
            "고정 조인트로 이어진 프레임은 SDF 변환에서 접혀 Gazebo에 없다",
            "개구는 **패드 접촉면 사이 거리**다. 관절값에서 만든 표와 대조한다",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
