#!/usr/bin/env python3
"""MoveIt2 검증 결과를 SQLite에 append-only로 남긴다 (8-07 · DB 8항).

`verify_fr3_moveit.py`의 JSON을 읽어 `sim_verification_runs`에 넣는다.

- MoveIt 설정 버전·기구학 solver·planning scene snapshot(id·버전·hash·시각)·
  충돌 검사 판정과 이유·목표와 관측을 **따로** 남긴다.
- `is_simulated=True`, `arm_only=True`, `adapter_kind="gazebo_sim"`이다 —
  실제 실행 집계(`executions`)와 섞지 않는다.
- 앱의 승인·실행 경로를 지나지 않은 ROS 수준 검증이므로 approval_id·
  execution_id·validation_run_id는 비운다.
- 환경 데이터 본문을 넣지 않는다. scene은 식별자와 지문만 남긴다.
"""

from __future__ import annotations

import hashlib
import json
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
MOVEIT_DIR = CONFIG / "moveit"
#: 검증용 fixture 구성 버전. 시뮬레이션 전용이며 실제 셀 환경이 아니다.
SIM_FIXTURE_VERSION = "moveit-verify-fixtures-1.0"

sys.path.insert(0, str(ROOT / "scripts"))
from record_gazebo_verification import environment_versions  # noqa: E402


def moveit_config_version() -> str:
    """MoveIt 설정 조각들의 지문. 설정이 바뀌면 값이 달라진다."""
    digest = hashlib.sha256()
    for path in sorted(MOVEIT_DIR.iterdir()):
        if path.is_file():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
    return f"config/moveit@{digest.hexdigest()[:16]}"


def kinematics_solver() -> tuple[str, str]:
    import yaml

    data = yaml.safe_load((MOVEIT_DIR / "kinematics.yaml").read_text("utf-8"))
    group = next(iter(data))
    return data[group]["kinematics_solver"], "moveit_kinematics 2.15.0"


def geometry_of(check: dict) -> dict | None:
    """검사 결과에서 기하 판정을 꺼낸다. 없으면 None이다."""
    for key in ("geometry", "geometry_with_fixture", "geometry_safe_with_box",
                "geometry_with_stale_snapshot"):
        if isinstance(check.get(key), dict):
            return check[key]
    detector = check.get("detector_check") or {}
    if isinstance(detector.get("geometry_after_override"), dict):
        return detector["geometry_after_override"]
    return None


def main() -> int:
    report_path = Path(
        sys.argv[1] if len(sys.argv) > 1
        else ROOT / "reports/moveit/verify_moveit.json"
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
    solver, solver_version = kinematics_solver()
    config_version = moveit_config_version()

    config = ServerConfig.from_env()
    repository = SqliteRepository(str(config.db_path), now=time.time())
    written = []
    try:
        for name, check in report["checks"].items():
            geometry = geometry_of(check)
            decision = None if geometry is None else geometry["decision"]
            reasons = [] if geometry is None else geometry["reason_codes"]
            record = SimVerificationRecord(
                verification_id=f"simv_{uuid.uuid4().hex[:12]}",
                recorded_at=report["recorded_at"],
                step=f"moveit/{name}",
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
                adapter_kind="gazebo_sim", arm_only=True, is_simulated=True,
                target={"check": name,
                        "target": check.get("target"),
                        "goal": check.get("goal"),
                        "tolerance_rad": check.get("tolerance_rad")},
                observed={k: v for k, v in check.items()
                          if k not in ("geometry", "geometry_with_fixture",
                                       "geometry_safe_with_box", "detector_check",
                                       "geometry_with_stale_snapshot")},
                # 그리퍼를 결합하지 않았다 — aperture·접촉·파지 정보가 없다.
                aperture_target=None, aperture_observed=None, contact=None,
                grasp={"object_held": None,
                       "detail": "2F-85를 결합하지 않았다(장착 변환 미확보)."
                                 " 파지 확인기는 동작하지 않는다"},
                command_accepted=bool(check.get("passed")),
                target_reached=check.get("reached"),
                state="verified" if check.get("passed") else "unverified",
                reason_code=(ReasonCode(reasons[0]) if reasons else None),
                detail=check.get("note", "") or check.get("finding", ""),
                schema_version=TASK_PLAN_SCHEMA_VERSION,
                moveit_config_version=config_version,
                kinematics_solver=solver,
                kinematics_solver_version=solver_version,
                planning_scene_snapshot_id=(
                    None if geometry is None else geometry.get("snapshot_id")),
                planning_scene_snapshot_version=(
                    None if geometry is None else geometry.get("snapshot_version")),
                planning_scene_snapshot_hash=(
                    None if geometry is None else geometry.get("snapshot_hash")),
                planning_scene_checked_at=(
                    None if geometry is None else geometry.get("checked_at")),
                collision_decision=decision,
                collision_reason_code=(ReasonCode(reasons[0]) if reasons else None),
                geometry_validator_id=(
                    None if geometry is None else geometry.get("validator_id")),
                geometry_validator_version=(
                    None if geometry is None else geometry.get("validator_version")),
                commanded_joints=check.get("target") or check.get("goal"),
                observed_joints=check.get("observed"),
                target_pose=None, observed_pose=None,
                sim_fixture_version=(
                    SIM_FIXTURE_VERSION if check.get("fixtures")
                    or check.get("fixture_added_midplan") else None),
            )
            repository.append_sim_verification(record)
            written.append(record.verification_id)
        rows = repository.sim_verifications(limit=len(written))
    finally:
        repository.close()

    print(json.dumps({
        "db": str(config.db_path),
        "written": len(written),
        "moveit_config_version": config_version,
        "kinematics_solver": solver,
        "rows": [
            {"step": r.step, "state": r.state,
             "collision_decision": r.collision_decision,
             "collision_reason": (None if r.collision_reason_code is None
                                  else r.collision_reason_code.value),
             "scene": r.planning_scene_snapshot_hash and
                      r.planning_scene_snapshot_hash[:12],
             "is_simulated": r.is_simulated, "arm_only": r.arm_only}
            for r in rows
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
