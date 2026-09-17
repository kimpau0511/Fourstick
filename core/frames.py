"""좌표·단위·프레임과 변환 책임 (md/개발플랜.md 1-02).

단위는 SI로 고정한다 — 길이 m, 각도 rad, 시간 s, 질량 kg, 힘 N.
계약 경계를 넘는 값은 모두 SI여야 하며, 로봇 고유 단위(도, mm, 관절 tick)는
해당 Robot Adapter 내부에서만 쓰고 경계에서 변환한다.

프레임명은 하드코딩하지 않는다(계획.md 27장). 아래 FrameKind는 "역할"이고,
실제 프레임 이름 문자열은 Capability Profile의 frames가 제공한다.

변환 책임:
- WORLD <-> BASE : Robot Adapter (로봇 설치 위치를 아는 유일한 곳)
- BASE  <-> TOOL : Robot Adapter (FK/IK를 가진 유일한 곳)
- TOOL  <-> GRASP: Capability Profile의 tcp (도구 오프셋)
- WORLD <-> OBJECT: World/Resource Catalog (셀 배치를 아는 유일한 곳)
공통 로직은 어떤 변환도 직접 계산하지 않고 위 소유자에게 요청한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

LENGTH_UNIT: Final[str] = "m"
ANGLE_UNIT: Final[str] = "rad"
TIME_UNIT: Final[str] = "s"
MASS_UNIT: Final[str] = "kg"
FORCE_UNIT: Final[str] = "N"


class FrameKind(str, Enum):
    """프레임의 역할. 실제 이름은 Capability Profile이 정한다."""

    WORLD = "world"    # 셀 고정 기준
    BASE = "base"      # 로봇 설치 기준
    TOOL = "tool"      # 로봇이 IK로 제어하는 말단 프레임(flange 등)
    GRASP = "grasp"    # 실제 파지점(TCP). TOOL에서 tcp 오프셋만큼 떨어져 있다
    OBJECT = "object"  # 대상 물체 기준

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Vector3:
    """SI 단위(m) 위치."""

    x: float
    y: float
    z: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


@dataclass(frozen=True)
class Quaternion:
    """단위 쿼터니언. 회전을 오일러각으로 주고받지 않는다(축 순서 혼동 방지)."""

    x: float
    y: float
    z: float
    w: float

    def norm(self) -> float:
        return (self.x**2 + self.y**2 + self.z**2 + self.w**2) ** 0.5


@dataclass(frozen=True)
class Pose:
    """프레임이 명시된 pose. frame 없이 좌표만 주고받는 것을 금지한다."""

    frame: FrameKind
    position: Vector3
    orientation: Quaternion
