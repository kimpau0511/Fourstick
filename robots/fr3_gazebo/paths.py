"""FR3 작업 셀 자산·생성물 경로(환경변수 → config/local.env → 프로젝트 상대 기본값). 규칙은 `core/paths.py`."""

from __future__ import annotations

from pathlib import Path

from core.paths import setting, third_party_dir, workcell_log_dir


def fr3_repo() -> Path:
    """FAIR-INNOVATION/frcobot_ros2 복제 위치(재배포 조건 미확인 자산 — 저장소에 넣지 않는다)."""
    return setting("FORSTICK2_FR3_REPO", third_party_dir() / "frcobot_ros2")


def robotiq_repo() -> Path:
    """robotiq/ros v1.1.0 복제 위치."""
    return setting("FORSTICK2_ROBOTIQ_REPO", third_party_dir() / "robotiq_ros")


def workcell_moveit_urdf() -> Path:
    """Gazebo 기동 스크립트가 만드는 MoveIt용 URDF(절대 메시 경로)."""
    return workcell_log_dir() / "workcell" / "fr3wms_with_2f85.moveit.urdf"


def gz_partition(workcell: dict) -> str:
    """Gazebo 파티션: 환경변수 GZ_PARTITION이 있으면 그 값, 없으면 작업 셀 설정 값.

    한 PC에서 작업 셀을 둘 띄울 때 설정 파일을 고치지 않고 나눌 수 있게 한다. 기본 실행 스크립트는
    설정과 같은 값을 export하므로 평소 동작은 같다.
    """
    import os
    return os.environ.get("GZ_PARTITION") or str(workcell.get("gz_partition") or "")


def ros_domain_id(workcell: dict) -> int:
    """ROS 도메인: 환경변수 ROS_DOMAIN_ID가 있으면 그 값, 없으면 작업 셀 설정 값."""
    import os
    value = os.environ.get("ROS_DOMAIN_ID")
    return int(value) if value not in (None, "") else int(workcell["ros_domain_id"])
