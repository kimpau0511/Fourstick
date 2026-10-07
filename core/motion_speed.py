"""시뮬레이션 관절 이동량·속도·가속도 제한으로 정지→정지 quintic 시간을 계산한다.

경로는 관절 공간 직선 그대로다. q=q0+Δq(10u³−15u⁴+6u⁵), u=t/T.
최대 속도=15/8·|Δq|/T, 최대 가속도=10√3/3·|Δq|/T².
100% 팔 속도는 URDF 속도 한계의 설정 비율(0.8), 가속도는 시뮬레이션 전용
파생 한계(URDF 속도×0.5/s)의 같은 비율이다. 공식 가속도 규격으로 주장하지 않는다.
0%는 실행을 거부한다. 관측·한계가 없으면 고정 시간으로 대체하지 않는다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from core.policy import PolicyError
from core.reason_codes import ReasonCode


@dataclass(frozen=True)
class MotionSpeedPolicy:
    default_percent: int
    min_percent: int
    max_percent: int
    step_percent: int
    max_fraction_of_joint_limit: float
    min_arm_seconds: float
    min_gripper_seconds: float
    peak_factor: float
    gripper_velocity_rad_s: float
    acceleration_factor_per_sec: float

    def __post_init__(self) -> None:
        if not 0 <= self.min_percent <= self.default_percent <= self.max_percent <= 100:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "speed_percent 범위가 맞지 않는다(0 ≤ min ≤ default ≤ max ≤ 100)")
        if self.step_percent <= 0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "speed_percent.step은 양수여야 한다")
        if not 0 < self.max_fraction_of_joint_limit <= 1:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "max_fraction_of_joint_limit은 (0, 1]이어야 한다")
        for name in ("min_arm_seconds", "min_gripper_seconds", "peak_factor",
                     "gripper_velocity_rad_s", "acceleration_factor_per_sec"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}는 양수여야 한다")

    @staticmethod
    def from_config(data: Mapping[str, Any]) -> "MotionSpeedPolicy":
        try:
            sp = data["speed_percent"]
            policy = MotionSpeedPolicy(
                default_percent=int(sp["default"]), min_percent=int(sp["min"]),
                max_percent=int(sp["max"]), step_percent=int(sp["step"]),
                max_fraction_of_joint_limit=float(data["max_fraction_of_joint_limit"]),
                min_arm_seconds=float(data["min_arm_seconds"]),
                min_gripper_seconds=float(data["min_gripper_seconds"]),
                peak_factor=float(data["interpolation_peak_factor"]),
                gripper_velocity_rad_s=float(data["gripper_velocity_rad_s"]),
                acceleration_factor_per_sec=float(data["acceleration_factor_per_sec"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError(ReasonCode.CONFIG_MISSING, f"이동 속도 설정을 읽을 수 없다: {exc}") from None
        if policy.peak_factor != 15 / 8:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "quintic 속도 계수는 15/8이어야 합니다")
        if not data.get("provenance"):
            raise PolicyError(ReasonCode.CONFIG_MISSING, "이동 속도 설정에 근거(provenance)가 없다")
        return policy

    def check_percent(self, percent: Any) -> int:
        """웹에서 받은 값. 정수·범위·간격이 맞아야 한다. 고쳐 주지 않는다."""
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise PolicyError(ReasonCode.CONFIG_INVALID, f"속도는 정수 %여야 한다: {percent!r}")
        if not self.min_percent <= percent <= self.max_percent:
            raise PolicyError(ReasonCode.CONFIG_INVALID, f"속도는 {self.min_percent}~{self.max_percent}%다: {percent}")
        if (percent - self.min_percent) % self.step_percent:
            raise PolicyError(ReasonCode.CONFIG_INVALID, f"속도는 {self.step_percent}% 간격이다: {percent}")
        return percent

    def execution_percent(self, percent: Any) -> int:
        percent = self.check_percent(percent)
        if percent == 0:
            raise PolicyError(ReasonCode.EXEC_SPEED_ZERO,
                              "이동 속도가 0%입니다 — 다음 작업 실행이 차단됩니다")
        return percent

    def arm_seconds(self, start: Mapping[str, float], target: Mapping[str, float],
                    limits_rad_s: Mapping[str, float], percent: int) -> float:
        """시작→목표 관절 이동 시간. 목표 관절마다 한계가 있어야 한다(없으면 PolicyError)."""
        percent = self.execution_percent(percent)
        scale = self.max_fraction_of_joint_limit * percent / 100.0
        needed = self.min_arm_seconds
        for name, goal in target.items():
            if name not in limits_rad_s:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"관절 한계 속도가 없다: {name}")
            if name not in start:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"시작 관절값이 없다: {name}")
            allowed = float(limits_rad_s[name]) * scale
            acceleration = float(limits_rad_s[name]) * self.acceleration_factor_per_sec * scale
            distance = abs(float(goal) - float(start[name]))
            if not all(math.isfinite(v) for v in (allowed, acceleration, distance)) or allowed <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"관절 이동량 또는 한계가 유효하지 않습니다: {name}")
            needed = max(needed, self.peak_factor * distance / allowed,
                         math.sqrt((10 * math.sqrt(3) / 3) * distance / acceleration))
        return needed

    def gripper_seconds(self, start: float, target: float, percent: int) -> float:
        percent = self.execution_percent(percent)
        allowed = self.gripper_velocity_rad_s * percent / 100.0
        distance = abs(target - start)
        if not math.isfinite(distance):
            raise PolicyError(ReasonCode.ROBOT_STATE_UNAVAILABLE, "그리퍼 이동량이 유효하지 않습니다")
        acceleration = allowed * self.acceleration_factor_per_sec
        return max(self.min_gripper_seconds, self.peak_factor * distance / allowed,
                   math.sqrt((10 * math.sqrt(3) / 3) * distance / acceleration))


def quintic_samples(start: Mapping[str, float], target: Mapping[str, float], seconds: float) -> list[dict]:
    """5차 곡선의 끝점·속도/가속도 극값 지점. 모든 구간은 동일한 다항식이다."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise PolicyError(ReasonCode.CONFIG_INVALID, "궤적 시간이 유효하지 않습니다")
    if not target or any(name not in start or not math.isfinite(start[name])
                         or not math.isfinite(value) for name, value in target.items()):
        raise PolicyError(ReasonCode.ROBOT_STATE_UNAVAILABLE, "궤적 관절값이 유효하지 않습니다")
    samples = []
    for u in (0.0, (3-math.sqrt(3))/6, .5, (3+math.sqrt(3))/6, 1.0):
        position = 10*u**3 - 15*u**4 + 6*u**5
        velocity = (30*u**2 - 60*u**3 + 30*u**4) / seconds
        acceleration = (60*u - 180*u**2 + 120*u**3) / seconds**2
        samples.append({"seconds": u*seconds,
                        "positions": {n: start[n]+(v-start[n])*position for n,v in target.items()},
                        "velocities": {n: (v-start[n])*velocity for n,v in target.items()},
                        "accelerations": {n: (v-start[n])*acceleration for n,v in target.items()}})
    return samples
