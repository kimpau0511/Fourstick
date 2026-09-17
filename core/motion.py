"""해석된 모션 값 계약 (md/개발플랜.md 6-04).

`TaskPlan`의 스텝은 **기호**다(`move.to = loc_pallet_1`). 계약이 허용하는 인자에
관절값·속도 같은 숫자는 없다(`core/constants.py`의 `SKILL_ALLOWED_ARGS`).
숫자는 기호를 실제 목표로 바꾸는 쪽(Adapter, teaching 자료, 기하 제공자)이 만든다.

이 모듈은 그렇게 **해석된 값**을 담는 그릇이다. Capability 사전 검사가 이 값을
Profile 한계와 대조한다. 값이 어디서 왔는지(`source`)와 단위를 함께 요구한다 —
단위를 추정해 비교하면 조용히 틀린다.

여기에 로봇별 수치를 넣지 않는다. 관절 이름도 Profile이 주는 값을 그대로 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from core.reason_codes import ReasonCode


class MotionError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class Quantity(str, Enum):
    """단위 선언 대상. 각 양마다 단위를 따로 받는다."""

    JOINT_POSITION = "joint_position"
    JOINT_VELOCITY = "joint_velocity"
    JOINT_ACCELERATION = "joint_acceleration"
    GRIPPER_POSITION = "gripper_position"
    GRIPPER_EFFORT = "gripper_effort"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class MotionRequest:
    """스텝 하나를 실제로 보내기 위해 해석된 값.

    빈 MotionRequest는 "해석된 수치가 없다"는 뜻이고, 그 자체로는 위반이 아니다
    (기호 계획은 Adapter가 내부에서 해석한다). 다만 값이 있으면 반드시 Profile
    한계와 대조하고, 대조에 필요한 Profile 값이 없으면 통과시키지 않는다.
    """

    #: 관절명 -> 목표값. 이름은 Profile의 joint_limits와 같아야 한다.
    joint_targets: Mapping[str, float] = field(default_factory=dict)
    joint_velocities: Mapping[str, float] = field(default_factory=dict)
    joint_accelerations: Mapping[str, float] = field(default_factory=dict)
    gripper_position: float | None = None
    gripper_effort: float | None = None
    #: 양 -> 단위 문자열. 값이 있는 양은 단위도 있어야 한다.
    units: Mapping[Quantity, str] = field(default_factory=dict)
    #: 값의 출처(teaching 파일, IK 해, 시뮬레이터 등). 감사용이다.
    source: str = ""

    @property
    def is_empty(self) -> bool:
        return not (
            self.joint_targets or self.joint_velocities or self.joint_accelerations
            or self.gripper_position is not None or self.gripper_effort is not None
        )

    def unit_for(self, quantity: Quantity) -> str | None:
        return self.units.get(quantity)

    def to_dict(self) -> dict:
        return {
            "joint_targets": dict(self.joint_targets),
            "joint_velocities": dict(self.joint_velocities),
            "joint_accelerations": dict(self.joint_accelerations),
            "gripper_position": self.gripper_position,
            "gripper_effort": self.gripper_effort,
            "units": {str(k): v for k, v in self.units.items()},
            "source": self.source,
        }


#: 스텝 순번(1부터) -> 해석된 값. 없는 스텝은 기호 그대로 실행된다.
ResolvedMotion = Mapping[int, MotionRequest]
