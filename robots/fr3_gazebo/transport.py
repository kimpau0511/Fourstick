"""FR3 Gazebo 작업 셀 전송 계층 계약 (md/개발플랜.md 8-08 우선순위 6).

Adapter가 ROS·Gazebo에 직접 말하지 않게 한 겹을 둔다. 목적은 두 가지다.

1. **플랫폼 문제와 전송 문제를 분리한다.** Adapter 로직은 ROS 없이 시험할 수
   있어야 한다(테스트는 결정적 스텁을 넣는다).
2. **관측 실패를 성공으로 바꾸지 않는다.** 모든 관측 결과는 "값"과 "관측
   가능했는가"를 따로 담는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol


@dataclass(frozen=True)
class WorldStatus:
    """작업 셀 런타임 상태. 실행 직전 점검이 이 값만 본다."""

    #: world 서비스가 응답하는가.
    world_present: bool
    world_name: str
    gz_partition: str
    ros_domain_id: int
    #: 컨트롤러 이름 -> 상태 문자열("active"/"inactive"/...).
    controllers: Mapping[str, str] = field(default_factory=dict)
    #: Gazebo에 존재하는 모델 이름.
    models: tuple[str, ...] = ()
    detail: str = ""

    def inactive(self, required: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(name for name in required
                     if self.controllers.get(name) != "active")


@dataclass(frozen=True)
class JointObservation:
    """관절 관측 한 장. `valid=False`면 값을 쓰지 않는다."""

    positions: Mapping[str, float]
    velocities: Mapping[str, float]
    observed_at: float
    valid: bool = True
    detail: str = ""


@dataclass(frozen=True)
class GoalOutcome:
    """goal 하나의 결말. 수락·결과·취소 ACK를 따로 담는다."""

    accepted: bool
    #: 결과가 도착했는가. False면 타임아웃이며 성공으로 보지 않는다.
    result_received: bool = False
    error_code: int | None = None
    cancel_ack: bool | None = None
    goals_canceling: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class SceneCheck:
    """planning scene 기반 상태 검사 결과."""

    #: 검사를 수행했는가. False면 판정하지 않는다(안전으로 보지 않는다).
    available: bool
    valid: bool = False
    contacts: tuple[tuple[str, str], ...] = ()
    out_of_bounds: tuple[str, ...] = ()
    snapshot_id: str = ""
    content_hash: str = ""
    detail: str = ""


class WorkcellTransport(Protocol):
    """Adapter가 쓰는 전송 계약. 구현체는 ros_transport.py에 둔다."""

    def connect(self, timeout_sec: float) -> WorldStatus: ...

    def status(self, timeout_sec: float) -> WorldStatus: ...

    def joint_observation(self, timeout_sec: float,
                          *, after: float | None = None) -> JointObservation:
        """`after`를 주면 그보다 새로운 표본을 기다린다."""
        ...

    def send_arm(self, joints: Mapping[str, float], seconds: float,
                 timeout_sec: float) -> GoalOutcome: ...

    def send_gripper(self, value: float, seconds: float,
                     timeout_sec: float) -> GoalOutcome: ...

    def cancel_all(self, timeout_sec: float) -> GoalOutcome: ...

    def check_state(self, joints: Mapping[str, float],
                    timeout_sec: float) -> SceneCheck: ...

    def disconnect(self) -> None: ...
