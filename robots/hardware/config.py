"""실기 어댑터 설정 경계 (8-12).

**주소·인증값·엔드포인트·한계값을 이 파일에 적지 않는다.** 전부 실행 환경에서
들어오고, 없으면 어댑터를 만들 수 없다.

## 어디서 오는가

환경변수로 들어온다. 파일에 두면 저장소에 접속 정보가 남기 때문이다.

| 환경변수 | 뜻 |
|---|---|
| `FORSTICK2_HW_ARM_KIND` | 팔 접속 방식 이름(설정이 정한다) |
| `FORSTICK2_HW_ARM_ENDPOINT` | 팔 컨트롤러 접속 대상 |
| `FORSTICK2_HW_GRIPPER_KIND` | 그리퍼 접속 방식 이름 |
| `FORSTICK2_HW_GRIPPER_ENDPOINT` | 그리퍼 접속 대상 |
| `FORSTICK2_HW_LIMITS_FILE` | 관절·힘·속도 한계 파일 경로 |

`endpoint`는 **판정과 기록에 담지 않는다** — 값이 있는지만 남긴다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from core.reason_codes import ReasonCode

#: 환경변수 접두사. 이름을 한 곳에서 정한다.
ENV_PREFIX = "FORSTICK2_HW_"


class HardwareConfigError(Exception):
    """실기 설정이 없거나 불완전할 때. 어댑터를 만들지 못한다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class HardwareLimits:
    """관절·힘·속도 한계. **설정 파일에서만 온다.**

    기본값을 두지 않는다 — 한계를 추정하면 조용히 위험해진다.
    """

    source: str
    joint_limits_rad: Mapping[str, tuple[float, float]]
    joint_velocity_rad_s: Mapping[str, float]
    gripper_force_n: tuple[float, float] | None = None
    gripper_speed_m_s: tuple[float, float] | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.source:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "한계값의 출처가 없다 — 어디서 온 값인지 없이 쓰지 않는다")
        if not self.joint_limits_rad or not self.joint_velocity_rad_s:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                "관절 제한과 속도 한계가 모두 있어야 한다")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "joint_count": len(self.joint_limits_rad),
            "has_gripper_force": self.gripper_force_n is not None,
            "has_gripper_speed": self.gripper_speed_m_s is not None,
            "note": self.note,
        }


@dataclass(frozen=True)
class HardwareConnection:
    """실기 접속 설정. **endpoint 값은 기록에 담지 않는다.**"""

    arm_kind: str
    arm_endpoint: str
    gripper_kind: str
    gripper_endpoint: str
    limits: HardwareLimits | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = [name for name in
                   ("arm_kind", "arm_endpoint", "gripper_kind", "gripper_endpoint")
                   if not getattr(self, name)]
        if missing:
            raise HardwareConfigError(
                ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
                f"실기 접속 설정이 비어 있다: {', '.join(missing)}")

    @property
    def complete(self) -> bool:
        """한계값까지 있는가. 없으면 명령을 보낼 수 없다."""
        return self.limits is not None

    def to_dict(self) -> dict:
        """**접속 대상 값을 담지 않는다.** 있는지만 남긴다."""
        return {
            "arm_kind": self.arm_kind,
            "arm_endpoint_configured": bool(self.arm_endpoint),
            "gripper_kind": self.gripper_kind,
            "gripper_endpoint_configured": bool(self.gripper_endpoint),
            "limits": None if self.limits is None else self.limits.to_dict(),
            "complete": self.complete,
        }


def load_hardware_limits(path: Path | str) -> HardwareLimits:
    """한계값 파일을 읽는다. 없는 값을 채우지 않는다."""
    target = Path(path)
    if not target.is_file():
        raise HardwareConfigError(
            ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
            f"한계값 파일이 없다: {target}")
    payload = json.loads(target.read_text(encoding="utf-8"))
    joints = payload.get("joint_limits_rad") or {}
    velocities = payload.get("joint_velocity_rad_s") or {}
    force = payload.get("gripper_force_n")
    speed = payload.get("gripper_speed_m_s")
    return HardwareLimits(
        source=str(payload.get("source") or ""),
        joint_limits_rad={name: (float(v[0]), float(v[1]))
                          for name, v in joints.items()},
        joint_velocity_rad_s={name: float(v) for name, v in velocities.items()},
        gripper_force_n=(None if not force else (float(force[0]), float(force[1]))),
        gripper_speed_m_s=(None if not speed else (float(speed[0]), float(speed[1]))),
        note=str(payload.get("note") or ""),
    )


def load_hardware_connection(
    env: Mapping[str, str] | None = None,
) -> HardwareConnection:
    """환경에서 실기 접속 설정을 읽는다. **없으면 예외다.**

    기본값을 만들지 않는다 — 접속 대상을 추정하면 잘못된 장비에 명령을 보낼
    수 있다.
    """
    source = os.environ if env is None else env
    values = {
        "arm_kind": source.get(f"{ENV_PREFIX}ARM_KIND", ""),
        "arm_endpoint": source.get(f"{ENV_PREFIX}ARM_ENDPOINT", ""),
        "gripper_kind": source.get(f"{ENV_PREFIX}GRIPPER_KIND", ""),
        "gripper_endpoint": source.get(f"{ENV_PREFIX}GRIPPER_ENDPOINT", ""),
    }
    missing = [f"{ENV_PREFIX}{name.upper()}" for name, value in values.items()
               if not value]
    if missing:
        raise HardwareConfigError(
            ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
            f"실기 접속 설정이 없다 — 필요한 환경변수: {', '.join(missing)}."
            " 주소·인증값을 저장소에 적지 않는다")
    limits_file = source.get(f"{ENV_PREFIX}LIMITS_FILE", "")
    limits = load_hardware_limits(limits_file) if limits_file else None
    return HardwareConnection(**values, limits=limits)


def connection_state(env: Mapping[str, str] | None = None) -> dict:
    """설정이 있는지만 보고한다. 값은 담지 않는다 — 관문·화면이 쓴다."""
    try:
        connection = load_hardware_connection(env)
    except HardwareConfigError as exc:
        return {"configured": False, "detail": str(exc),
                "reason_code": exc.reason.value}
    return {"configured": True, "detail": "실기 접속 설정이 선언됐다",
            "reason_code": None, **connection.to_dict()}
