#!/usr/bin/env python3
"""Gazebo 검증 결과를 SQLite에 append-only로 남긴다 (8-03~8-06 · DB 9항).

`verify_fr3_gazebo.py`의 JSON을 읽어 `sim_verification_runs`에 넣는다.

- 목표와 관측을 따로 담는다. 명령 수락과 목표 도달을 합치지 않는다.
- `is_simulated=True`, `arm_only=True`로 남긴다 — 실제 로봇 실행 집계와 섞지
  않는다(그 집계는 `executions` 표가 따로 센다).
- 앱의 승인·실행 경로를 지나지 않은 ROS 수준 검증이므로 `approval_id`·
  `execution_id`·`validation_run_id`는 비운다. 나중에 앱 경로로 실행하면 그
  식별자와 함께 새 행이 쌓인다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import (  # noqa: E402
    load_arm_profile,
    load_asset_manifest,
    load_composite_profile,
    load_gripper_profile,
    load_mounting_profile,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION  # noqa: E402
from core.reason_codes import ReasonCode  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from storage.records import SimVerificationRecord  # noqa: E402
from storage.sqlite.repository import SqliteRepository  # noqa: E402

CONFIG = ROOT / "config"


def environment_versions() -> dict[str, str]:
    """설치된 ROS2·Gazebo·컨트롤러 버전을 실제로 조회한다."""
    def dpkg(name: str) -> str:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", name],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip() or "unknown"

    gz = subprocess.run(
        ["bash", "-lc",
         "source /opt/ros/lyrical/setup.bash >/dev/null 2>&1; gz sim --versions"],
        capture_output=True, text=True, timeout=120,
    ).stdout.strip().splitlines()
    return {
        "ros_distro": "lyrical",
        "gazebo_version": f"gz-sim {gz[0] if gz else 'unknown'}",
        "controller_versions": (
            f"ros2_control {dpkg('ros-lyrical-ros2-control')}; "
            f"controller_manager {dpkg('ros-lyrical-controller-manager')}; "
            f"gz_ros2_control {dpkg('ros-lyrical-gz-ros2-control')}; "
            f"ros_gz {dpkg('ros-lyrical-ros-gz')}; "
            f"moveit {dpkg('ros-lyrical-moveit')}"
        ),
    }


def main() -> int:
    report_path = Path(
        sys.argv[1] if len(sys.argv) > 1 else "/tmp/forstick2_gazebo/verify.json"
    )
    if not report_path.is_file():
        print(f"검증 결과 JSON이 없다: {report_path}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))

    load = lambda name: json.loads(  # noqa: E731
        (CONFIG / "profiles" / name).read_text(encoding="utf-8")
    )
    arm = load_arm_profile(load("fr3wms_arm.json"))
    gripper = load_gripper_profile(load("robotiq_2f85_gripper.json"))
    mounting = load_mounting_profile(load("fr3wms_to_robotiq_2f85_mounting.json"))
    composite = load_composite_profile(
        load("composite_fr3wms_2f85.json"),
        arms={arm.arm_profile_id: arm},
        grippers={gripper.gripper_profile_id: gripper},
        mountings={mounting.mounting_profile_id: mounting},
    )
    manifest = load_asset_manifest(
        json.loads((CONFIG / "assets" / "third_party_assets.json").read_text("utf-8"))
    )
    env = environment_versions()

    config = ServerConfig.from_env()
    repository = SqliteRepository(str(config.db_path), now=time.time())
    written = []
    try:
        for step in report.get("steps", []):
            reason = step.get("reason_code")
            reached = step.get("goal_reached")
            record = SimVerificationRecord(
                verification_id=f"simv_{uuid.uuid4().hex[:12]}",
                recorded_at=report.get("finished_at", time.time()),
                step=step["step"],
                composite_profile_id=composite.composite_profile_id,
                composite_profile_version=composite.composite_profile_version,
                arm_profile_id=arm.arm_profile_id,
                arm_profile_version=arm.arm_profile_version,
                gripper_profile_id=gripper.gripper_profile_id,
                gripper_profile_version=gripper.gripper_profile_version,
                mounting_profile_id=mounting.mounting_profile_id,
                mounting_profile_version=mounting.mounting_profile_version,
                asset_manifest_version=manifest.manifest_version,
                ros_distro=env["ros_distro"],
                gazebo_version=env["gazebo_version"],
                controller_versions=env["controller_versions"],
                adapter_kind="gazebo_sim",
                arm_only=True, is_simulated=True,
                target={
                    "joint_names": ["j1", "j2", "j3", "j4", "j5", "j6"],
                    "joint_positions_rad": step.get("target"),
                    "tolerance_rad": 0.05,
                },
                observed={
                    "joint_positions_rad": step.get("observed_position"),
                    "max_joint_error_rad": step.get("max_joint_error_rad"),
                    "peak_velocity_rad_s": step.get("peak_velocity_rad_s"),
                    "observation_age_s": step.get("observation_age_s"),
                    "controller_status": step.get("controller_status"),
                    "cancel_acked": step.get("cancel_acked"),
                    "observed_stopped": step.get("observed_stopped"),
                    "moved_after_cancel_rad": step.get("moved_after_cancel_rad"),
                },
                # 그리퍼를 붙이지 않았으므로 aperture·접촉·파지 정보가 없다.
                aperture_target=None, aperture_observed=None,
                contact=None,
                grasp={
                    "object_held": None,
                    "detail": "그리퍼를 결합하지 않았다(장착 변환 미확보)."
                              " 파지 확인기는 아직 동작하지 않는다",
                },
                command_accepted=bool(step.get("command_accepted")),
                motion_completed=(
                    None if reached is None else bool(step.get("observed_stopped"))
                ),
                target_reached=None if reached is None else bool(reached),
                task_succeeded=(
                    None if reached is None
                    else bool(reached and step.get("observed_stopped"))
                ),
                state=(
                    "completed" if reached else
                    ("stopped" if step.get("observed_stopped") else "unknown")
                ),
                reason_code=None if reason is None else ReasonCode(reason),
                detail=step.get("accept_detail", ""),
                schema_version=TASK_PLAN_SCHEMA_VERSION,
            )
            repository.append_sim_verification(record)
            written.append(record.verification_id)
        rows = repository.sim_verifications(limit=10)
    finally:
        repository.close()

    print(json.dumps({
        "db": str(config.db_path),
        "written": written,
        "environment": env,
        "latest": [
            {
                "step": r.step, "command_accepted": r.command_accepted,
                "target_reached": r.target_reached, "state": r.state,
                "reason_code": None if r.reason_code is None else r.reason_code.value,
                "adapter_kind": r.adapter_kind, "arm_only": r.arm_only,
                "is_simulated": r.is_simulated,
            }
            for r in rows
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
