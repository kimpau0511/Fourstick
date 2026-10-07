"""FR3 작업 셀의 로봇별 경로 — 셀 인스턴스(현장)마다 작업 폴더가 다를 수 있다(core/workcell_paths.py)."""

from __future__ import annotations

import os
from pathlib import Path

from core.workcell_paths import log_dir


def moveit_urdf() -> Path:
    """MoveIt용 URDF. 기본 작업 폴더면 지금까지처럼 공유 링크를, 아니면 그 셀 폴더의 파일을 쓴다."""
    if os.environ.get("FORSTICK2_WORKCELL_LOG_DIR"):
        return log_dir() / "workcell/fr3wms_with_2f85.moveit.urdf"
    return Path("/tmp/forstick2_gazebo/workcell/fr3wms_with_2f85.moveit.urdf")
