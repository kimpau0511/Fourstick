"""조합형 로봇 Profile (md/개발플랜.md 8-02).

Profile을 세 부분으로 나눈다. 팔과 그리퍼와 장착을 따로 두면, 다음 로봇에서 팔만 바꾸고 **같은 그리퍼
Profile을 재사용**할 수 있다. 모델 이름은 설정(JSON)에만 있고 이 모듈에는 없다.

```
CompositeRobotProfile
  ├ ArmCapabilityProfile      팔 (모델별 설정으로 주입)
  ├ GripperCapabilityProfile  그리퍼 (교체 없이 재사용)
  └ MountingProfile           팔 flange ↔ 그리퍼 base 고정 변환
```

지키는 것:

- 모든 수치는 `core/provenance.Measured`로 값·단위·출처·버전·상태·시각을 갖는다.
- **근거 없는 값은 None(unavailable)로 남긴다.** 기본값·추정값·다른 로봇 값을
  넣지 않는다.
- 장착 변환을 확인할 공식 자료가 없으면 identity transform을 쓰지 않는다.
  MountingProfile을 미완료로 두고, 그 상태에서는 **pick·place를 지원 스킬에서
  제외**한다.
- 제조사 이름으로 분기하지 않는다. 이 모듈에는 모델별 if가 없다.
- 실기·시뮬레이터 검증 전에는 `environment`가 `simulation`이고 `verified`가
  False다. Fake·시뮬레이션 결과를 real로 승격하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from core.capability_profile import (
    CapabilityProfile,
    GripperSpec,
    JointLimit,
    ProfileError,
)
from core.constants import ATOMIC_SKILLS, SKILL_PICK, SKILL_PLACE
from core.frames import ANGLE_UNIT, FrameKind, LENGTH_UNIT, Vector3
from core.provenance import (
    Measured,
    VerificationStatus,
    missing_items,
    weakest_status,
)
from core.reason_codes import ReasonCode


class RobotProfileError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class Environment(str, Enum):
    """이 Profile이 어디에서 검증됐는가."""

    SIMULATION = "simulation"
    REAL = "real"

    def __str__(self) -> str:
        return self.value


class JointKind(str, Enum):
    REVOLUTE = "revolute"
    PRISMATIC = "prismatic"
    CONTINUOUS = "continuous"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ArmJointSpec:
    """관절 하나. 이름·순서·종류와 제한값을 근거와 함께 담는다."""

    name: str
    kind: JointKind
    #: 위치 하한·상한, 최대 속도, 최대 가속도. 없으면 unavailable.
    lower: Measured
    upper: Measured
    max_velocity: Measured
    max_acceleration: Measured

    def __post_init__(self) -> None:
        if not self.name:
            raise RobotProfileError(ReasonCode.CONFIG_MISSING, "관절 이름이 없다")
        if not isinstance(self.kind, JointKind):
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID, f"관절 종류가 낯설다: {self.kind!r}"
            )
        expected = LENGTH_UNIT if self.kind is JointKind.PRISMATIC else ANGLE_UNIT
        for label, item, unit in (
            ("lower", self.lower, expected),
            ("upper", self.upper, expected),
            ("max_velocity", self.max_velocity, f"{expected}/s"),
            ("max_acceleration", self.max_acceleration, f"{expected}/s^2"),
        ):
            if item.unit and item.unit != unit:
                raise RobotProfileError(
                    ReasonCode.CONFIG_UNIT_MISMATCH,
                    f"{self.name}.{label}의 단위가 {item.unit!r}인데"
                    f" {self.kind}는 {unit!r}를 쓴다",
                )
        if self.lower.available and self.upper.available:
            if self.lower.value >= self.upper.value:
                raise RobotProfileError(
                    ReasonCode.CONFIG_INVALID, f"{self.name}의 lower >= upper"
                )

    @property
    def values(self) -> Mapping[str, Measured]:
        return {
            "lower": self.lower, "upper": self.upper,
            "max_velocity": self.max_velocity,
            "max_acceleration": self.max_acceleration,
        }

    @property
    def required_values(self) -> Mapping[str, Measured]:
        """완성 판정에 필요한 값.

        가속도 한계는 **선택 항목**이다 — URDF에 없는 경우가 많고, 없으면
        Capability 사전 검사(6-04)가 가속도 값을 만났을 때 ASK로 돌린다.
        완성 판정에서 빼되 `optional_missing`으로 추적한다.
        """
        return {
            "lower": self.lower, "upper": self.upper,
            "max_velocity": self.max_velocity,
        }

    @property
    def optional_missing(self) -> tuple[str, ...]:
        return () if self.max_acceleration.available else ("max_acceleration",)

    @property
    def complete(self) -> bool:
        return all(m.available for m in self.required_values.values())

    def to_joint_limit(self) -> JointLimit:
        """기존 `CapabilityProfile` 계약으로 변환. 값이 없으면 막는다."""
        unit = LENGTH_UNIT if self.kind is JointKind.PRISMATIC else ANGLE_UNIT
        return JointLimit(
            name=self.name,
            lower=self.lower.require(f"{self.name}.lower"),
            upper=self.upper.require(f"{self.name}.upper"),
            max_velocity=self.max_velocity.require(f"{self.name}.max_velocity"),
            unit=unit,
            kind=("prismatic" if self.kind is JointKind.PRISMATIC else "revolute"),
            max_acceleration=self.max_acceleration.value,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name, "kind": self.kind.value,
            **{k: v.to_dict() for k, v in self.values.items()},
        }


@dataclass(frozen=True)
class ArmCapabilityProfile:
    """팔 하나. 모델명을 코드가 해석하지 않는다."""

    arm_profile_id: str
    arm_profile_version: str
    #: 사람이 읽는 모델 표기. 분기에 쓰지 않는다.
    display_name: str
    #: 관절 **순서가 의미를 갖는다.** base부터 flange까지.
    joints: tuple[ArmJointSpec, ...]
    payload: Measured
    reach: Measured
    #: FrameKind -> 프레임 이름. base·flange(tool)는 필수 확인 대상이다.
    frames: Mapping[FrameKind, str]
    #: 컨트롤러 요구 버전(문자열 비교용). 없으면 빈 값.
    controller_requirement: str
    #: 이 팔이 제공할 수 있는 스킬(그리퍼·장착 조건은 Composite가 본다).
    declared_skills: tuple[str, ...]
    #: 상태 신선도·연결 판정 조건. Policy가 아니라 **로봇 특성**으로 둔다.
    state_max_age: Measured
    connect_timeout: Measured
    environment: Environment = Environment.SIMULATION
    verified: bool = False
    unverified_items: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("arm_profile_id", "arm_profile_version", "display_name"):
            if not getattr(self, name):
                raise RobotProfileError(ReasonCode.CONFIG_MISSING, f"{name}이 없다")
        if not self.joints:
            raise RobotProfileError(
                ReasonCode.CONFIG_MISSING, "관절 목록이 비어 있다"
            )
        names = [j.name for j in self.joints]
        if len(set(names)) != len(names):
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID, f"중복 관절 이름: {names}"
            )
        unknown = set(self.declared_skills) - set(ATOMIC_SKILLS)
        if unknown:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID, f"계약에 없는 스킬: {sorted(unknown)}"
            )
        if self.verified and self.missing:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                f"verified인데 미확보 항목이 있다: {self.missing}",
            )

    @property
    def dof(self) -> int:
        return len(self.joints)

    @property
    def joint_names(self) -> tuple[str, ...]:
        return tuple(j.name for j in self.joints)

    @property
    def scalar_values(self) -> Mapping[str, Measured]:
        return {
            "payload": self.payload, "reach": self.reach,
            "state_max_age": self.state_max_age,
            "connect_timeout": self.connect_timeout,
        }

    @property
    def motion_values(self) -> Mapping[str, Measured]:
        """자유 이동(home·move·stop) 판정에 필요한 값.

        **정격 페이로드는 여기 없다** — 물체를 들지 않는 이동은 페이로드와
        무관하다. 페이로드가 없으면 `lift_missing`으로 잡히고, 조합 Profile이
        pick·place를 제외한다.
        """
        return {
            "reach": self.reach,
            "state_max_age": self.state_max_age,
            "connect_timeout": self.connect_timeout,
        }

    @property
    def lift_missing(self) -> tuple[str, ...]:
        """물체를 드는 스킬에만 필요한데 없는 값."""
        return () if self.payload.available else ("payload",)

    @property
    def missing(self) -> tuple[str, ...]:
        out = list(missing_items(self.motion_values))
        for joint in self.joints:
            out += [
                f"{joint.name}.{k}" for k in missing_items(joint.required_values)
            ]
        for kind in (FrameKind.BASE, FrameKind.TOOL):
            if not self.frames.get(kind):
                out.append(f"frame.{kind.value}")
        if not self.controller_requirement:
            out.append("controller_requirement")
        return tuple(out)

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def optional_missing(self) -> tuple[str, ...]:
        """없어도 이동은 가능하지만, 검사에서 ASK가 되거나 스킬이 줄어드는 값들."""
        out: list[str] = list(self.lift_missing)
        for joint in self.joints:
            out += [f"{joint.name}.{k}" for k in joint.optional_missing]
        return tuple(out)

    @property
    def status(self) -> VerificationStatus:
        if not self.complete:
            return VerificationStatus.UNAVAILABLE
        return weakest_status(self.scalar_values)

    def to_dict(self) -> dict:
        return {
            "arm_profile_id": self.arm_profile_id,
            "arm_profile_version": self.arm_profile_version,
            "display_name": self.display_name,
            "dof": self.dof,
            "joints": [j.to_dict() for j in self.joints],
            "frames": {k.value: v for k, v in self.frames.items()},
            "controller_requirement": self.controller_requirement,
            "declared_skills": list(self.declared_skills),
            "environment": self.environment.value,
            "verified": self.verified,
            "complete": self.complete,
            "missing": list(self.missing),
            "optional_missing": list(self.optional_missing),
            "unverified_items": list(self.unverified_items),
            "notes": self.notes,
            **{k: v.to_dict() for k, v in self.scalar_values.items()},
        }


@dataclass(frozen=True)
class GripperCapabilityProfile:
    """2지 평행 그리퍼 하나. 팔과 독립이다(9단계에서 재사용한다)."""

    gripper_profile_id: str
    gripper_profile_version: str
    display_name: str
    #: 2지 평행인가. 다른 형식이면 이 Profile을 쓰지 않는다.
    two_finger_parallel: bool
    #: 명령을 받는 관절과, 그것을 따라가는 관절들(배수 포함).
    command_joint: str
    mimic_joints: Mapping[str, float]
    #: 명령 관절의 열림·닫힘 위치(각도 또는 길이). 방향을 추측하지 않는다.
    open_position: Measured
    closed_position: Measured
    #: 명령 관절 한계.
    joint_lower: Measured
    joint_upper: Measured
    max_velocity: Measured
    max_effort: Measured
    #: 실제 패드 간격(m). URDF에 없으면 unavailable로 둔다.
    pad_aperture_open: Measured
    pad_aperture_closed: Measured
    #: "닫기 명령을 보냈는데 여전히 열린 상태인가" 판정 여유값.
    fully_open_margin: Measured
    #: ros2_control 인터페이스 이름.
    command_interfaces: tuple[str, ...] = ()
    state_interfaces: tuple[str, ...] = ()
    #: 실제 하드웨어가 물체 감지 상태를 주는가 / 시뮬레이터가 주는가.
    object_detection_real: bool = False
    object_detection_simulation: bool = False
    #: 선택 상태: "selected"(확정) / "candidate"(후보) / "alternative"(대안 기록).
    selection_status: str = "candidate"
    #: 손가락 선속도 한계(m/s). 관절 각속도와 다른 양이다.
    max_finger_speed: Measured | None = None
    #: **파지력 한계(N).** `max_effort`(관절 토크, N·m)와 **다른 값이다.**
    #: 둘을 같은 값으로 취급하지 않는다. 모터 전류로 환산하지도 않는다.
    max_grasp_force: Measured | None = None
    #: 개방 행정(패드가 벌어지는 거리, m). 기구학 유도값.
    aperture_travel: Measured | None = None
    #: 양쪽 손가락 끝 프레임 사이 거리(열림/닫힘). 패드 표면 간격이 아니다.
    tip_separation_open: Measured | None = None
    tip_separation_closed: Measured | None = None
    #: 질량(kg).
    mass: Measured | None = None
    #: 파지력·속도의 하한(공식 명세 범위의 반대쪽 끝).
    min_grasp_force: Measured | None = None
    min_finger_speed: Measured | None = None
    #: 정격 페이로드(kg). 그리퍼가 들 수 있다고 제조사가 명시한 값.
    rated_payload: Measured | None = None
    #: **물리 제품 명세와 시뮬레이션 모델 값의 차이**를 기록한다.
    #: 어느 값을 API 계약에 쓰고 어느 값을 충돌·관측에 쓰는지 함께 적는다.
    model_tolerance: Mapping[str, Any] = field(default_factory=dict)
    #: 필드버스 상태·명령 레지스터의 의미(이름 -> 설명).
    bus_semantics: Mapping[str, str] = field(default_factory=dict)
    #: 통신 기본값(이름 -> "값 단위").
    bus_defaults: Mapping[str, str] = field(default_factory=dict)
    #: 물체 감지 상태 값의 의미(실제 하드웨어 기준).
    object_detection_states: tuple[str, ...] = ()
    #: 활성화·오류·제한시간 관련 인터페이스·상태 이름.
    activation_interfaces: tuple[str, ...] = ()
    fault_interfaces: tuple[str, ...] = ()
    command_timeout: Measured | None = None
    base_frame: str = ""
    environment: Environment = Environment.SIMULATION
    verified: bool = False
    unverified_items: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("gripper_profile_id", "gripper_profile_version",
                     "display_name", "command_joint"):
            if not getattr(self, name):
                raise RobotProfileError(ReasonCode.CONFIG_MISSING, f"{name}이 없다")
        if not self.two_finger_parallel:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                "이 Profile은 2지 평행 그리퍼 계약이다 — 다른 형식은 별도 계약이 필요하다",
            )
        if self.command_joint in self.mimic_joints:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                f"명령 관절 {self.command_joint!r}이 mimic 목록에도 있다",
            )
        for joint, multiplier in self.mimic_joints.items():
            if multiplier == 0:
                raise RobotProfileError(
                    ReasonCode.CONFIG_INVALID, f"{joint}의 mimic 배수가 0이다"
                )
        if self.open_position.available and self.closed_position.available:
            if self.open_position.value == self.closed_position.value:
                raise RobotProfileError(
                    ReasonCode.CONFIG_INVALID, "열림 위치와 닫힘 위치가 같다"
                )
        if self.object_detection_simulation and not self.object_detection_real:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                "시뮬레이터가 실제 하드웨어에 없는 물체 감지 상태를 제공한다고"
                " 선언할 수 없다 — 시뮬레이션 파지 확인은 별도 확인기로 분리한다",
            )
        if self.verified and self.missing:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                f"verified인데 미확보 항목이 있다: {self.missing}",
            )

    @property
    def closes_by_increasing(self) -> bool | None:
        """닫을 때 명령값이 커지는가. 근거가 없으면 None — 추측하지 않는다."""
        if not (self.open_position.available and self.closed_position.available):
            return None
        return self.closed_position.value > self.open_position.value

    @property
    def scalar_values(self) -> Mapping[str, Measured]:
        values = {
            "open_position": self.open_position,
            "closed_position": self.closed_position,
            "joint_lower": self.joint_lower,
            "joint_upper": self.joint_upper,
            "max_velocity": self.max_velocity,
            "max_effort": self.max_effort,
            "pad_aperture_open": self.pad_aperture_open,
            "pad_aperture_closed": self.pad_aperture_closed,
            "fully_open_margin": self.fully_open_margin,
        }
        for name in ("command_timeout", "max_finger_speed", "max_grasp_force",
                     "aperture_travel", "tip_separation_open",
                     "tip_separation_closed", "mass", "min_grasp_force",
                     "min_finger_speed", "rated_payload"):
            item = getattr(self, name)
            if item is not None:
                values[name] = item
        return values

    @property
    def missing(self) -> tuple[str, ...]:
        out = list(missing_items(self.scalar_values))
        if not self.base_frame:
            out.append("base_frame")
        if self.selection_status not in ("selected", "candidate", "alternative"):
            out.append("selection_status")
        if not self.command_interfaces:
            out.append("command_interfaces")
        if not self.state_interfaces:
            out.append("state_interfaces")
        return tuple(out)

    @property
    def complete(self) -> bool:
        return not self.missing

    def to_gripper_spec(self, joint_name: str | None = None) -> GripperSpec:
        """기존 `GripperSpec` 계약으로 변환. 값이 없으면 막는다."""
        open_position = self.open_position.require("open_position")
        closed_position = self.closed_position.require("closed_position")
        aperture = self.pad_aperture_closed.require("pad_aperture_closed")
        if aperture <= 0:
            # 닫힘 패드 간격이 0이면 파지면 근거로 쓸 수 없다.
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID, "pad_aperture_closed가 0 이하"
            )
        mimic = sorted(self.mimic_joints)
        return GripperSpec(
            joint_name=joint_name or self.command_joint,
            open_position=open_position,
            close_position=closed_position,
            unit=self.open_position.unit,
            grasp_aperture_m=aperture,
            max_effort=self.max_effort.require("max_effort"),
            fully_open_margin=self.fully_open_margin.require("fully_open_margin"),
            mimic_joint_name=mimic[0] if mimic else "",
        )

    def to_dict(self) -> dict:
        return {
            "gripper_profile_id": self.gripper_profile_id,
            "gripper_profile_version": self.gripper_profile_version,
            "display_name": self.display_name,
            "two_finger_parallel": self.two_finger_parallel,
            "command_joint": self.command_joint,
            "mimic_joints": dict(self.mimic_joints),
            "closes_by_increasing": self.closes_by_increasing,
            "command_interfaces": list(self.command_interfaces),
            "state_interfaces": list(self.state_interfaces),
            "object_detection": {
                "real_hardware": self.object_detection_real,
                "simulation": self.object_detection_simulation,
                "states": list(self.object_detection_states),
            },
            "selection_status": self.selection_status,
            "model_tolerance": dict(self.model_tolerance),
            "bus_semantics": dict(self.bus_semantics),
            "bus_defaults": dict(self.bus_defaults),
            "activation_interfaces": list(self.activation_interfaces),
            "fault_interfaces": list(self.fault_interfaces),
            "base_frame": self.base_frame,
            "environment": self.environment.value,
            "verified": self.verified,
            "complete": self.complete,
            "missing": list(self.missing),
            "unverified_items": list(self.unverified_items),
            "notes": self.notes,
            **{k: v.to_dict() for k, v in self.scalar_values.items()},
        }


@dataclass(frozen=True)
class MountingProfile:
    """팔 flange ↔ 그리퍼 base 고정 변환.

    **공식 CAD·도면·URDF가 없으면 identity transform을 쓰지 않는다.** 값이 없는
    상태로 두고, Composite가 pick·place를 비활성화한다.
    """

    mounting_profile_id: str
    mounting_profile_version: str
    #: 팔 쪽 프레임 이름과 그리퍼 쪽 프레임 이름.
    arm_flange_frame: str
    gripper_base_frame: str
    #: 고정 조인트 변환. 6개 값 각각에 근거가 붙는다.
    xyz: tuple[Measured, Measured, Measured]
    rpy: tuple[Measured, Measured, Measured]
    #: 어댑터 플레이트 식별자(부품 번호·도면 번호). 없으면 빈 값.
    adapter_plate_id: str = ""
    #: 커플링 부품 정보(모델·키트·질량·관성·높이·출처). 값이 없으면 None으로 둔다.
    coupling: Mapping[str, Any] = field(default_factory=dict)
    #: 그리퍼 쪽 공식값(명령 조인트·mimic·질량·패드면·개구). 장착 판단의 근거다.
    gripper_official: Mapping[str, Any] = field(default_factory=dict)
    #: 공식 도면 항목별 대조 결과. 직접 장착 판단의 근거다.
    comparison: Mapping[str, Any] = field(default_factory=dict)
    #: 장착 변환 계산 과정(가능해진 뒤 채운다).
    transform_derivation: Mapping[str, Any] = field(default_factory=dict)
    #: TCP(파지점) 프레임 이름과 그리퍼 base로부터의 오프셋.
    tcp_frame: str = ""
    tcp_xyz: tuple[Measured, Measured, Measured] | None = None
    environment: Environment = Environment.SIMULATION
    verified: bool = False
    unverified_items: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("mounting_profile_id", "mounting_profile_version"):
            if not getattr(self, name):
                raise RobotProfileError(ReasonCode.CONFIG_MISSING, f"{name}이 없다")
        for label, triple in (("xyz", self.xyz), ("rpy", self.rpy)):
            if len(triple) != 3:
                raise RobotProfileError(
                    ReasonCode.CONFIG_INVALID, f"{label}는 3개 값이어야 한다"
                )
        if self.verified and self.missing:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                f"verified인데 미확보 항목이 있다: {self.missing}",
            )

    @property
    def scalar_values(self) -> Mapping[str, Measured]:
        values: dict[str, Measured] = {}
        for axis, item in zip("xyz", self.xyz):
            values[f"xyz.{axis}"] = item
        for axis, item in zip(("roll", "pitch", "yaw"), self.rpy):
            values[f"rpy.{axis}"] = item
        if self.tcp_xyz is not None:
            for axis, item in zip("xyz", self.tcp_xyz):
                values[f"tcp.{axis}"] = item
        return values

    @property
    def missing(self) -> tuple[str, ...]:
        out = list(missing_items(self.scalar_values))
        if not self.arm_flange_frame:
            out.append("arm_flange_frame")
        if not self.gripper_base_frame:
            out.append("gripper_base_frame")
        if not self.tcp_frame:
            out.append("tcp_frame")
        if self.tcp_xyz is None:
            out.append("tcp_xyz")
        return tuple(out)

    @property
    def complete(self) -> bool:
        return not self.missing

    def tcp_offset(self) -> Vector3:
        """TCP 오프셋. 값이 없으면 막는다 — 0으로 대체하지 않는다."""
        if self.tcp_xyz is None:
            raise RobotProfileError(
                ReasonCode.CONFIG_MISSING, "tcp_xyz가 없다(장착 변환 미확보)"
            )
        return Vector3(
            x=self.tcp_xyz[0].require("tcp.x"),
            y=self.tcp_xyz[1].require("tcp.y"),
            z=self.tcp_xyz[2].require("tcp.z"),
        )

    def to_dict(self) -> dict:
        return {
            "mounting_profile_id": self.mounting_profile_id,
            "mounting_profile_version": self.mounting_profile_version,
            "arm_flange_frame": self.arm_flange_frame,
            "gripper_base_frame": self.gripper_base_frame,
            "adapter_plate_id": self.adapter_plate_id,
            "coupling": dict(self.coupling),
            "gripper_official": dict(self.gripper_official),
            "comparison": dict(self.comparison),
            "transform_derivation": dict(self.transform_derivation),
            "tcp_frame": self.tcp_frame,
            "environment": self.environment.value,
            "verified": self.verified,
            "complete": self.complete,
            "missing": list(self.missing),
            "unverified_items": list(self.unverified_items),
            "notes": self.notes,
            **{k: v.to_dict() for k, v in self.scalar_values.items()},
        }


@dataclass(frozen=True)
class CompositeRobotProfile:
    """팔 + 그리퍼 + 장착을 묶은 하나의 로봇 구성."""

    composite_profile_id: str
    composite_profile_version: str
    arm: ArmCapabilityProfile
    gripper: GripperCapabilityProfile | None
    mounting: MountingProfile | None
    #: 이 구성이 참조하는 제3자 자산 manifest 버전.
    asset_manifest_version: str = ""
    environment: Environment = Environment.SIMULATION
    #: 실기/시뮬레이터 검증이 끝났는가. **Fake 결과로 올리지 않는다.**
    verified: bool = False
    notes: str = ""

    def __post_init__(self) -> None:
        for name in ("composite_profile_id", "composite_profile_version"):
            if not getattr(self, name):
                raise RobotProfileError(ReasonCode.CONFIG_MISSING, f"{name}이 없다")
        if self.verified and self.blocking_items:
            raise RobotProfileError(
                ReasonCode.CONFIG_INVALID,
                f"verified인데 미확보 항목이 있다: {self.blocking_items}",
            )

    # ── 상태 ────────────────────────────────────────────────────────────
    @property
    def blocking_items(self) -> tuple[str, ...]:
        """실행 가능해지기까지 남은 항목 전체.

        자유 이동에 필요한 값과, 물체를 드는 스킬에만 필요한 값을 함께 담는다.
        전자가 비면 이동조차 못 하고, 후자만 비면 pick·place만 제외된다.
        """
        out = [f"arm.{item}" for item in self.arm.missing]
        out += [f"arm.{item}" for item in self.arm.lift_missing]
        if self.gripper is None:
            out.append("gripper.missing_profile")
        else:
            out += [f"gripper.{item}" for item in self.gripper.missing]
        if self.mounting is None:
            out.append("mounting.missing_profile")
        else:
            out += [f"mounting.{item}" for item in self.mounting.missing]
        if not self.asset_manifest_version:
            out.append("asset_manifest_version")
        return tuple(out)

    @property
    def complete(self) -> bool:
        """모든 항목이 채워졌는가(pick·place까지 가능한 상태)."""
        return not self.blocking_items

    @property
    def motion_ready(self) -> bool:
        """자유 이동(home·move·stop)을 실행할 수 있는가.

        팔의 이동 관련 값이 모두 있으면 참이다. 그리퍼·장착·페이로드가 없어도
        이동은 가능하다 — 그 경우 pick·place만 제외된다.
        """
        return self.arm.complete and bool(self.asset_manifest_version)

    @property
    def supported_skills(self) -> tuple[str, ...]:
        """지금 쓸 수 있는 스킬.

        **그리퍼나 장착 변환이 미완료면 pick·place를 제외한다.** 값이 없는 채로
        파지·배치를 허용하지 않는다.
        """
        skills = [s for s in self.arm.declared_skills]
        gripper_ready = self.gripper is not None and self.gripper.complete
        mounting_ready = self.mounting is not None and self.mounting.complete
        payload_known = not self.arm.lift_missing
        if not (gripper_ready and mounting_ready and payload_known):
            skills = [s for s in skills if s not in (SKILL_PICK, SKILL_PLACE)]
        return tuple(skills)

    @property
    def excluded_skills(self) -> tuple[str, ...]:
        return tuple(
            s for s in self.arm.declared_skills if s not in self.supported_skills
        )

    def version_key(self) -> tuple[str, ...]:
        """구성 버전 지문. 하나라도 바뀌면 기존 승인을 재사용하지 않는다."""
        return (
            self.composite_profile_id, self.composite_profile_version,
            self.arm.arm_profile_id, self.arm.arm_profile_version,
            "" if self.gripper is None else self.gripper.gripper_profile_id,
            "" if self.gripper is None else self.gripper.gripper_profile_version,
            "" if self.mounting is None else self.mounting.mounting_profile_id,
            "" if self.mounting is None else self.mounting.mounting_profile_version,
            self.asset_manifest_version,
        )

    # ── 기존 계약으로의 변환 ────────────────────────────────────────────
    def capability_profile(self) -> CapabilityProfile:
        """검증·Registry가 쓰는 `CapabilityProfile`로 변환한다.

        미확보 값이 있으면 **변환하지 않고 막는다.** 0이나 기본값으로 채워
        실행 가능한 것처럼 보이게 하지 않는다.
        """
        if not self.motion_ready:
            raise RobotProfileError(
                ReasonCode.CONFIG_MISSING,
                f"이동에 필요한 값이 없어 Profile을 만들 수 없다:"
                f" {list(self.arm.missing)}",
            )
        mounting_ready = self.mounting is not None and self.mounting.complete
        gripper_ready = self.gripper is not None and self.gripper.complete
        frames = {
            FrameKind.BASE: self.arm.frames[FrameKind.BASE],
            FrameKind.TOOL: self.arm.frames[FrameKind.TOOL],
        }
        if mounting_ready:
            frames[FrameKind.GRASP] = self.mounting.tcp_frame
        try:
            return CapabilityProfile(
                profile_id=self.composite_profile_id,
                profile_version=self.composite_profile_version,
                dof=self.arm.dof,
                joint_limits=tuple(j.to_joint_limit() for j in self.arm.joints),
                frames=frames,
                # 장착 변환이 없으면 TCP 오프셋도 없다. 0으로 채우지 않고
                # 도구가 없는 상태(플랜지 기준)로 둔다.
                tcp_offset=(
                    self.mounting.tcp_offset() if mounting_ready
                    else Vector3(0.0, 0.0, 0.0)
                ),
                work_radius_m=self.arm.reach.require("reach"),
                payload_kg=self.arm.payload.value,
                supported_skills=self.supported_skills,
                gripper=(
                    self.gripper.to_gripper_spec() if gripper_ready and mounting_ready
                    else None
                ),
                provenance=self.provenance_summary(),
            )
        except ProfileError as exc:
            raise RobotProfileError(exc.reason, str(exc)) from None

    def provenance_summary(self) -> dict[str, str]:
        """수치의 출처 요약(Profile.provenance에 들어간다)."""
        out: dict[str, str] = {}
        for name, item in self.arm.scalar_values.items():
            if item.available:
                out[f"arm.{name}"] = (
                    f"{item.provenance.source} {item.provenance.source_version}"
                ).strip()
        if self.gripper is not None:
            for name, item in self.gripper.scalar_values.items():
                if item.available:
                    out[f"gripper.{name}"] = (
                        f"{item.provenance.source} {item.provenance.source_version}"
                    ).strip()
        if self.mounting is not None:
            for name, item in self.mounting.scalar_values.items():
                if item.available:
                    out[f"mounting.{name}"] = (
                        f"{item.provenance.source} {item.provenance.source_version}"
                    ).strip()
        return out

    def to_dict(self) -> dict:
        return {
            "composite_profile_id": self.composite_profile_id,
            "composite_profile_version": self.composite_profile_version,
            "display_name": self.display_name,
            "asset_manifest_version": self.asset_manifest_version,
            "environment": self.environment.value,
            "verified": self.verified,
            "complete": self.complete,
            "motion_ready": self.motion_ready,
            "blocking_items": list(self.blocking_items),
            "supported_skills": list(self.supported_skills),
            "excluded_skills": list(self.excluded_skills),
            "arm": self.arm.to_dict(),
            "gripper": None if self.gripper is None else self.gripper.to_dict(),
            "mounting": None if self.mounting is None else self.mounting.to_dict(),
            "notes": self.notes,
        }

    @property
    def display_name(self) -> str:
        parts = [self.arm.display_name]
        if self.gripper is not None:
            parts.append(self.gripper.display_name)
        return " + ".join(parts)


def swap_arm(
    composite: CompositeRobotProfile, arm: ArmCapabilityProfile,
    mounting: MountingProfile | None, *, composite_profile_id: str,
    composite_profile_version: str,
) -> CompositeRobotProfile:
    """팔과 장착만 바꾸고 **그리퍼 Profile을 재사용**한다(회귀 로봇 경로).

    그리퍼 Profile을 복사하지 않고 같은 객체를 그대로 참조한다 — 두 구성이
    같은 그리퍼 값을 쓰는 것이 기록에서도 보여야 한다.
    """
    return CompositeRobotProfile(
        composite_profile_id=composite_profile_id,
        composite_profile_version=composite_profile_version,
        arm=arm, gripper=composite.gripper, mounting=mounting,
        asset_manifest_version=composite.asset_manifest_version,
        environment=composite.environment, verified=False,
        notes=f"{composite.composite_profile_id}에서 팔·장착만 교체",
    )
