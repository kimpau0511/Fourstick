#!/usr/bin/env python3
"""GUI 세션 관측 점검 (md/개발플랜.md 8-08 우선순위 4).

**GUI 창이 떴다는 사실은 성공 근거가 아니다.** 실제로 확인해야 하는 것은
모델이 world에 있고, 컨트롤러가 살아 있고, 관절 상태와 개구가 관측되는지다.
이 스크립트는 그것만 확인해 보고서로 남긴다. 판정하지 못한 항목은
`unavailable`로 남기고 성공으로 올리지 않는다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.aperture_model import ApertureModel  # noqa: E402
from robots.moveit.kinematics import link_transforms, parse_urdf  # noqa: E402

URDF = Path("/tmp/forstick2_gazebo/urdf/fr3wms_with_2f85.urdf")
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
WORLD = "forstick2_fr3_cell"
MODEL = "fr3wms_2f85"
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
EXPECTED_CONTROLLERS = ["joint_state_broadcaster", "arm_trajectory_controller",
                        "gripper_action_controller"]


def run(command: list[str], timeout: float = 40.0) -> str:
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 timeout=timeout)
        return process.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def joint_states(timeout_sec: float = 15.0) -> dict:
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import JointState

    started_here = not rclpy.ok()
    if started_here:
        rclpy.init()
    node = Node("forstick2_gui_check")
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    checks: list[dict] = []

    def record(name: str, status: str, detail) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1) world와 모델
    models = [line.strip("- ").strip() for line in run(["gz", "model", "--list"]).splitlines()
              if line.strip().startswith("-")]
    record("world_model_present", "pass" if MODEL in models else "fail",
           {"models": models, "expected": MODEL})

    # 2) 컨트롤러
    controllers_text = run(["ros2", "control", "list_controllers"], timeout=40)
    active = [line.split()[0] for line in controllers_text.splitlines()
              if " active" in line]
    missing = [name for name in EXPECTED_CONTROLLERS if name not in active]
    record("controllers_active", "pass" if not missing else "fail",
           {"active": active, "missing": missing, "raw": controllers_text.strip()})

    # 3) 관절 상태 관측
    states = joint_states()
    arm_missing = [name for name in ARM_JOINTS if name not in states]
    record("arm_joint_states_observed", "pass" if not arm_missing else "fail",
           {"missing": arm_missing,
            "values_rad": {name: round(states[name], 6) for name in ARM_JOINTS
                           if name in states}})

    gripper_value = states.get(GRIPPER_JOINT)
    record("gripper_joint_state_observed",
           "pass" if gripper_value is not None else "fail",
           {"joint": GRIPPER_JOINT,
            "value_rad": None if gripper_value is None else round(gripper_value, 6)})

    # 4) 개구 관측 (관절값 → 패드 간극 표)
    profile = json.loads(MOUNTING.read_text(encoding="utf-8"))
    model = ApertureModel.from_rows(
        profile["transform_derivation"]["aperture_table"],
        source=profile["mounting_profile_version"],
    )
    if gripper_value is None:
        record("aperture_observed", "unavailable",
               {"reason": "그리퍼 관절값이 관측되지 않아 개구를 낼 수 없다"})
        aperture_m = None
    else:
        reading = model.observe(gripper_value, edge_tolerance_rad=0.02)
        aperture_m = reading["aperture_m"]
        record("aperture_observed",
               "pass" if aperture_m is not None else "unavailable", reading)

    # 5) TCP 자세와 바닥 간극 (관측 관절값 기준 FK)
    if URDF.is_file() and not arm_missing:
        joints, _ = parse_urdf(URDF)
        angles = {name: states[name] for name in ARM_JOINTS}
        if gripper_value is not None:
            import xml.etree.ElementTree as ET

            root = ET.parse(URDF).getroot()
            angles[GRIPPER_JOINT] = gripper_value
            for joint in root.findall("joint"):
                mimic = joint.find("mimic")
                if mimic is not None and mimic.get("joint") == GRIPPER_JOINT:
                    angles[joint.get("name")] = (
                        gripper_value * float(mimic.get("multiplier", "1"))
                        + float(mimic.get("offset", "0"))
                    )
        transforms = link_transforms(joints, angles)
        lowest_name, lowest_z = None, None
        for name, (_, position) in transforms.items():
            if lowest_z is None or position[2] < lowest_z:
                lowest_name, lowest_z = name, float(position[2])
        tcp = transforms.get("robotiq_85_tcp")
        record("tcp_above_floor",
               "pass" if tcp is not None and tcp[1][2] > 0 else "fail",
               {"tcp_world_m": None if tcp is None
                else [round(float(v), 6) for v in tcp[1]],
                "lowest_frame": lowest_name,
                "lowest_frame_z_m": None if lowest_z is None else round(lowest_z, 6),
                "note": "프레임 원점 기준이다. 링크 형상 간극이 아니다"})
    else:
        record("tcp_above_floor", "unavailable",
               {"reason": "조립 URDF 또는 팔 관절 관측이 없다"})

    # 6) GUI 창 존재 (창만으로 성공 처리하지 않는다 — 기록용 사실)
    gui_pids = run(["pgrep", "-f", "gz-sim-gui-client"]).split()
    record("gui_client_process", "pass" if gui_pids else "unavailable",
           {"pids": gui_pids,
            "note": "창이 떴다는 사실만으로 검증 성공이 아니다"})

    report = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "world": WORLD,
        "model": MODEL,
        "is_simulated": True,
        "real_hardware_verified": False,
        "mounting_yaw_source": "declared_by_operator",
        "pick_place_enabled": False,
        "pick_place_reason": "장착 yaw 근거·커플링 실측 질량·파지 관측 미확보",
        "checks": checks,
        "summary": {
            "pass": sum(1 for item in checks if item["status"] == "pass"),
            "fail": sum(1 for item in checks if item["status"] == "fail"),
            "unavailable": sum(1 for item in checks
                               if item["status"] == "unavailable"),
            "total": len(checks),
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    for item in checks:
        print(f"  [{item['status']:11}] {item['check']}")
    print(f"  {out} 기록 — pass {report['summary']['pass']}"
          f" / fail {report['summary']['fail']}"
          f" / unavailable {report['summary']['unavailable']}")
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
