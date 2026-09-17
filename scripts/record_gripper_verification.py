#!/usr/bin/env python3
"""조립(그리퍼) 검증 결과를 SQLite에 append-only로 남긴다 (8-08 · DB).

`verify_fr3_gripper_gazebo.py`의 JSON을 읽어 `sim_verification_runs`에 넣는다.

- `arm_only=False`, `is_simulated=True`, `adapter_kind="gazebo_sim"`.
  **실제 하드웨어 실행으로 집계하지 않는다.**
- 개구 목표·관측을 따로 남기고(`aperture_target`/`aperture_observed`),
  파지 판정은 근거가 없으므로 `object_held=None`으로 둔다.
- 장착 Profile 버전을 함께 남긴다 — yaw 미확보 상태가 기록에 남아야 한다.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from config.loader import (  # noqa: E402
    load_arm_profile,
    load_asset_manifest,
    load_composite_profile,
    load_gripper_profile,
    load_mounting_profile,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION  # noqa: E402
from core.reason_codes import ReasonCode  # noqa: E402
from record_gazebo_verification import environment_versions  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from storage.records import SimVerificationRecord  # noqa: E402
from storage.sqlite.repository import SqliteRepository  # noqa: E402
from validation.pick_place_gate import evaluate  # noqa: E402

CONFIG = ROOT / "config"


def main() -> int:
    report_path = Path(
        sys.argv[1] if len(sys.argv) > 1
        else ROOT / "reports/gripper/verify_gripper.json"
    )
    if not report_path.is_file():
        print(f"검증 결과가 없다: {report_path}", file=sys.stderr)
        return 2
    report = json.loads(report_path.read_text(encoding="utf-8"))

    load = lambda name: json.loads(  # noqa: E731
        (CONFIG / "profiles" / name).read_text(encoding="utf-8"))
    arm = load_arm_profile(load("fr3wms_arm.json"))
    gripper = load_gripper_profile(load("robotiq_2f85_gripper.json"))
    mounting = load_mounting_profile(load("fr3wms_to_robotiq_2f85_mounting.json"))
    composite = load_composite_profile(
        load("composite_fr3wms_2f85.json"),
        arms={arm.arm_profile_id: arm},
        grippers={gripper.gripper_profile_id: gripper},
        mountings={mounting.mounting_profile_id: mounting},
    )
    manifest = load_asset_manifest(json.loads(
        (CONFIG / "assets" / "third_party_assets.json").read_text("utf-8")))
    env = environment_versions()
    gate = evaluate(mounting=mounting, verification=report,
                    geometry_decision=None, revalidated=False)

    config = ServerConfig.from_env()
    repository = SqliteRepository(str(config.db_path), now=time.time())
    written = []
    try:
        for name, check in report["checks"].items():
            observation = check.get("observation") or {}
            aperture_target = observation.get("expected_aperture_m")
            aperture_observed = observation.get("observed_aperture_m")
            passed = bool(check.get("passed"))
            reached = observation.get("within_tolerance",
                                      observation.get("reached"))
            record = SimVerificationRecord(
                verification_id=f"simv_{uuid.uuid4().hex[:12]}",
                recorded_at=report["recorded_at"],
                step=f"gripper/{name}",
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
                # **조립이므로 arm_only가 아니다.**
                arm_only=False, is_simulated=True,
                target={"check": name,
                        "target_joint_rad": check.get("target_joint_rad"),
                        "target_aperture_m": check.get("target_aperture_m"),
                        "arm_target": check.get("target")},
                observed={k: v for k, v in check.items() if k != "command"},
                aperture_target=aperture_target,
                aperture_observed=aperture_observed,
                # 접촉 센서를 붙이지 않았다. 없는 관측을 만들지 않는다.
                contact=None,
                grasp={
                    "object_held": None,
                    "detail": "파지 확인기를 붙이지 않았다. 접촉 관측 없이"
                              " object_held를 만들지 않는다",
                },
                command_accepted=bool(
                    (check.get("command") or {}).get("accepted", passed)),
                target_reached=reached,
                # 목표가 없는 항목(적재·모델·차단 상태)은 도달 개념이 없다.
                # 그때는 성공 여부를 비워 두고 state로만 남긴다 — 도달하지 않은
                # 실행을 성공으로 기록할 수 없다(계약).
                task_succeeded=(passed if reached is not None else None),
                state="verified" if passed else "unverified",
                reason_code=(None if passed else ReasonCode.EXEC_UNVERIFIABLE),
                detail=(check.get("note") or check.get("finding") or "")[:500],
                schema_version=TASK_PLAN_SCHEMA_VERSION,
                moveit_config_version=None,
                kinematics_solver=None,
                kinematics_solver_version=None,
                planning_scene_snapshot_id=None,
                collision_decision=None,
                commanded_joints=(
                    {"gripper": check.get("target_joint_rad")}
                    if check.get("target_joint_rad") is not None else None),
                observed_joints=(
                    {"gripper": observation.get("gripper_joint_rad")}
                    if observation.get("gripper_joint_rad") is not None else None),
                sim_fixture_version=None,
            )
            repository.append_sim_verification(record)
            written.append(record.verification_id)
        rows = repository.sim_verifications(limit=len(written))
    finally:
        repository.close()

    print(json.dumps({
        "db": str(config.db_path),
        "written": len(written),
        "mounting_profile": f"{mounting.mounting_profile_id}"
                            f" {mounting.mounting_profile_version}",
        "composite_profile": composite.composite_profile_version,
        "pick_place_gate": {
            "enabled": gate.enabled,
            "blocking": [item.key for item in gate.blocking],
            "reason_codes": [code.value for code in gate.reason_codes],
        },
        "rows": [
            {"step": r.step, "state": r.state, "arm_only": r.arm_only,
             "is_simulated": r.is_simulated,
             "aperture": [r.aperture_target, r.aperture_observed],
             "grasp": (r.grasp or {}).get("object_held")}
            for r in rows
        ],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
