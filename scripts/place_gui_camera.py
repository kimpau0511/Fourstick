#!/usr/bin/env python3
"""GUI 초기 시야 구성 (md/개발플랜.md 8-08 우선순위 4).

두 가지를 한다.

1. 시험 물체(fixture)를 world에 놓는다 — 이미 있으면 다시 놓지 않는다.
2. GUI 카메라를 로봇·커플링·그리퍼·바닥·fixture가 함께 보이는 자세로 옮긴다.
   기본 자세는 world SDF의 `<camera_pose>`가 이미 정한다. 여기서는 그 값을
   **읽어서 다시 적용**한다(사용자가 시야를 돌려 놓은 뒤 재실행하는 경우).

카메라 서비스는 GUI의 CameraTracking 플러그인이 제공한다. GUI가 없으면
서비스가 없고, 그 사실을 실패가 아니라 `camera_service_available: false`로
기록한다 — **headless 시뮬레이터를 종료하지 않는다.**
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def gz_call(service: str, reqtype: str, request: str, timeout_ms: int = 20000) -> dict:
    process = subprocess.run(
        ["gz", "service", "-s", service, "--reqtype", reqtype,
         "--reptype", "gz.msgs.Boolean", "--timeout", str(timeout_ms),
         "--req", request],
        capture_output=True, text=True, timeout=timeout_ms / 1000 + 20,
    )
    return {
        "ok": "data: true" in process.stdout,
        "stdout": process.stdout.strip()[:200],
        "stderr": process.stderr.strip()[:200],
    }


def services() -> list[str]:
    process = subprocess.run(["gz", "service", "-l"], capture_output=True,
                             text=True, timeout=40)
    return [line.strip() for line in process.stdout.splitlines() if line.strip()]


def models() -> list[str]:
    process = subprocess.run(["gz", "model", "--list"], capture_output=True,
                             text=True, timeout=40)
    return [line.strip("- ").strip() for line in process.stdout.splitlines()
            if line.strip().startswith("-")]


def camera_pose_from_world(world_sdf: Path) -> list[float] | None:
    """world SDF가 정한 GUI 카메라 자세(x y z roll pitch yaw)."""
    root = ET.parse(world_sdf).getroot()
    node = root.find(".//gui//camera_pose")
    if node is None or not (node.text or "").strip():
        return None
    return [float(value) for value in node.text.split()]


def quaternion_from_rpy(roll: float, pitch: float, yaw: float) -> tuple:
    cr, sr = math.cos(roll / 2), math.sin(roll / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    return (
        sr * cp * cy - cr * sp * sy,
        cr * sp * cy + sr * cp * sy,
        cr * cp * sy - sr * sp * cy,
        cr * cp * cy + sr * sp * sy,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world", required=True)
    parser.add_argument("--world-sdf", required=True,
                        help="카메라 초기 자세를 읽을 world SDF")
    parser.add_argument("--object", default=None,
                        help="추가로 놓을 fixture SDF(작업 셀은 world에 이미 있다)")
    args = parser.parse_args()

    report: dict = {"world": args.world}
    available = services()
    report["camera_service_available"] = "/gui/move_to/pose" in available
    report["create_service_available"] = f"/world/{args.world}/create" in available

    existing = models()
    report["models_before"] = existing

    fixture_name = "sim_test_box_a"
    if args.object is None:
        report["fixture"] = {"spawned": False,
                             "reason": "world에 이미 포함돼 있다"}
    elif fixture_name in existing:
        report["fixture"] = {"spawned": False, "reason": "이미 놓여 있다"}
    elif not report["create_service_available"]:
        report["fixture"] = {"spawned": False,
                             "reason": f"/world/{args.world}/create 서비스가 없다"}
    else:
        result = gz_call(
            f"/world/{args.world}/create", "gz.msgs.EntityFactory",
            f'sdf_filename: "{args.object}", name: "{fixture_name}",'
            " allow_renaming: false",
        )
        report["fixture"] = {"spawned": result["ok"], "detail": result["stdout"],
                             "sdf": args.object}

    pose = camera_pose_from_world(Path(args.world_sdf))
    report["camera_pose_from_world_sdf"] = pose
    if pose is None:
        report["camera"] = {"moved": False, "reason": "world SDF에 camera_pose가 없다"}
    elif not report["camera_service_available"]:
        report["camera"] = {
            "moved": False,
            "reason": "/gui/move_to/pose 서비스가 없다 — GUI가 없거나"
                      " CameraTracking 플러그인이 로드되지 않았다",
            "note": "world SDF의 camera_pose가 이미 초기 시야를 정한다."
                    " **시뮬레이터를 종료하지 않는다**",
        }
    else:
        x, y, z, roll, pitch, yaw = pose
        qx, qy, qz, qw = quaternion_from_rpy(roll, pitch, yaw)
        result = gz_call(
            "/gui/move_to/pose", "gz.msgs.GUICamera",
            f"pose: {{position: {{x: {x}, y: {y}, z: {z}}},"
            f" orientation: {{x: {qx}, y: {qy}, z: {qz}, w: {qw}}}}}",
        )
        report["camera"] = {"moved": result["ok"], "detail": result["stdout"],
                            "pose_xyzrpy": pose}

    report["models_after"] = models()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
