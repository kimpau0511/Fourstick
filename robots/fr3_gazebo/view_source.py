"""3D 작업 셀 화면(읽기 전용)의 이 로봇 몫 — 조립 URDF 위치 · 메시 경로 · 관절 이름.

공통 서버 코드(`server/sim_view.py`)는 로봇·제조사 이름을 모른다. 이 모듈이
Gazebo 기동 스크립트(`scripts/run_gazebo_fr3_2f85_workcell_gui.sh`)와 **같은 기본
경로**로 URDF와 메시를 찾아 준다(환경 변수로 바꿀 수 있다).
"""

from __future__ import annotations

import os
from pathlib import Path

ARM_JOINTS = ("j1", "j2", "j3", "j4", "j5", "j6")
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
ROBOT_MODEL = "fr3wms_2f85_workcell"


def _roots() -> tuple[Path, Path]:
    from robots.fr3_gazebo.paths import fr3_repo, robotiq_repo
    fr3, robotiq = fr3_repo(), robotiq_repo()
    return fr3 / "fairino_description" / "urdf", robotiq / "grippers" / "robotiq_description"


def resolve_mesh(filename: str) -> Path | None:
    """URDF 메시 경로 → 파일. 모르는 형식이면 None."""
    fr3_urdf_dir, robotiq = _roots()
    package = "package://robotiq_description/"
    if filename.startswith(package):
        return robotiq / filename[len(package):]
    if filename.startswith("../"):
        return (fr3_urdf_dir / filename).resolve()
    return None


def build_view_source() -> dict:
    """공통 화면 코드가 쓰는 묶음."""
    from core.paths import workcell_log_dir
    log_dir = workcell_log_dir()
    return {"urdf_path": log_dir / "workcell" / "fr3wms_with_2f85.urdf",
            "resolve_mesh": resolve_mesh, "arm_joints": ARM_JOINTS,
            "gripper_joint": GRIPPER_JOINT, "robot_model": ROBOT_MODEL}
