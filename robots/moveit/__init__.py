"""MoveIt2 기반 기하 검사 (md/개발플랜.md 8-07).

이 패키지는 **rclpy를 모듈 import 시점에 불러오지 않는다.** 웹 서버(.venv)에는
ROS가 없기 때문이다. ROS 접근은 `scene.RosPlanningSceneClient`가 담당하고,
`validator.MoveItGeometryValidator`는 그 client 계약만 본다 — 그래서 ROS 없이
계약을 테스트할 수 있다.
"""

from robots.moveit.scene import (
    PlanningSceneClient,
    SceneSnapshot,
    StateValidity,
    scene_snapshot_from_parts,
)
from robots.moveit.validator import MoveItGeometryValidator

__all__ = [
    "MoveItGeometryValidator",
    "PlanningSceneClient",
    "SceneSnapshot",
    "StateValidity",
    "scene_snapshot_from_parts",
]
