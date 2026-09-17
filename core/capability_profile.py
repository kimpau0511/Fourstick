"""Capability Profile (md/개발플랜.md 1-03).

로봇의 물리·제어 정보를 담는 버전 있는 설정이다. 계획.md 27장에 따라 관절명,
프레임명, TCP 오프셋, 그리퍼 열림/닫힘 값, 속도·tolerance 같은 수치는 제품
코드가 아니라 이 Profile에 둔다. 공통 로직은 Profile을 의존성으로 받는다.

값이 없으면 임의 기본값으로 진행하지 않고 ProfileError로 차단한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from core.constants import ATOMIC_SKILLS
from core.frames import ANGLE_UNIT, FrameKind, LENGTH_UNIT, Vector3
from core.reason_codes import ReasonCode


class ProfileError(Exception):
    """Profile이 계약을 만족하지 못할 때. 이유 코드를 함께 담는다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class JointLimit:
    name: str                 # 로봇 고유 관절명. 공통 코드는 이 값을 해석하지 않는다.
    lower: float              # rad (revolute) / m (prismatic)
    upper: float
    max_velocity: float
    unit: str                 # ANGLE_UNIT 또는 LENGTH_UNIT
    kind: str                 # "revolute" | "prismatic"
    #: 가속도 한계. **없으면 None이고, 기본값을 만들지 않는다** — 가속도 값을
    #: 검사해야 하는데 이 값이 없으면 Capability 사전 검사가 통과시키지 않고
    #: `capability.profile_incomplete`로 돌린다(개발플랜 6-04).
    max_acceleration: float | None = None

    def __post_init__(self) -> None:
        if self.unit not in (ANGLE_UNIT, LENGTH_UNIT):
            raise ProfileError(
                ReasonCode.CONFIG_UNIT_MISMATCH,
                f"관절 {self.name}의 단위 {self.unit!r}는 SI가 아니다",
            )
        if self.lower >= self.upper:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID, f"관절 {self.name}의 lower >= upper"
            )
        if self.max_velocity <= 0:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID, f"관절 {self.name}의 max_velocity가 0 이하"
            )
        if self.max_acceleration is not None and self.max_acceleration <= 0:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID,
                f"관절 {self.name}의 max_acceleration이 0 이하 — 값이 없으면"
                " 0이 아니라 None으로 둔다",
            )


@dataclass(frozen=True)
class GripperSpec:
    """그리퍼 값. 열림/닫힘의 크기 관계는 로봇마다 반대일 수 있으므로
    방향을 코드가 추측하지 않고 여기서 명시한다."""

    joint_name: str
    open_position: float
    close_position: float
    unit: str                        # ANGLE_UNIT 또는 LENGTH_UNIT
    #: 명령값과 실제 파지면 사이 기하. 근거를 함께 적는다.
    grasp_aperture_m: float          # close_position에서의 실제 패드 간격(m)
    max_effort: float                # N 또는 Nm
    #: "닫기 명령을 보냈는데 아직 열린 상태 그대로인가"를 판정할 때 쓰는 여유값.
    #: open_position에서 이 값 이내에 머물러 있으면 "닫기 시도 자체가 없었다"로
    #: 본다. 단위는 open/close와 같다. 닫힘 구간(|close-open|)에 비례해 로봇마다
    #: 다르므로 다른 로봇의 값을 재사용하지 않는다 — 단위가 아예 다를 수 있다.
    fully_open_margin: float
    mimic_joint_name: str = ""       # 반대쪽 관절(있으면)

    def __post_init__(self) -> None:
        if self.unit not in (ANGLE_UNIT, LENGTH_UNIT):
            raise ProfileError(
                ReasonCode.CONFIG_UNIT_MISMATCH, f"그리퍼 단위 {self.unit!r}는 SI가 아니다"
            )
        if self.open_position == self.close_position:
            raise ProfileError(ReasonCode.CONFIG_INVALID, "그리퍼 open == close")
        if self.grasp_aperture_m <= 0:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID, "grasp_aperture_m가 0 이하 — 파지면 간격 근거 필요"
            )
        if self.fully_open_margin <= 0:
            raise ProfileError(ReasonCode.CONFIG_INVALID, "fully_open_margin이 0 이하")
        travel = abs(self.close_position - self.open_position)
        if self.fully_open_margin >= travel:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID,
                f"fully_open_margin({self.fully_open_margin})이 닫힘 구간({travel})보다 크거나 같다 "
                "— 정상 닫힘까지 '완전개방'으로 오판한다",
            )

    @property
    def closes_by_increasing(self) -> bool:
        """닫을 때 값이 커지는가. 로봇별 방향을 코드가 추측하지 않게 한다."""
        return self.close_position > self.open_position

    def still_fully_open(self, position: float) -> bool:
        """닫기 명령 후 실제 위치가 여전히 완전개방 상태인가.

        방향을 추측하지 않고 open_position에서 닫는 쪽으로 얼마나 움직였는지를
        본다. 값의 크기 비교로 구현하면 닫는 방향이 반대인 로봇에서 정상 닫힘을
        완전개방으로 오판한다.

        판정이 True라는 것은 "닫기 시도가 없었다"는 뜻이고, False는 "완전개방은
        아니다"라는 뜻일 뿐 파지 성공의 증거가 아니다.
        """
        moved = (position - self.open_position) if self.closes_by_increasing else (
            self.open_position - position
        )
        return moved < self.fully_open_margin


@dataclass(frozen=True)
class CapabilityProfile:
    profile_id: str
    profile_version: str
    dof: int
    joint_limits: tuple[JointLimit, ...]
    #: FrameKind -> 실제 프레임 이름. 프레임명을 코드에 쓰지 않기 위한 유일한 출처.
    frames: Mapping[FrameKind, str]
    #: TOOL -> GRASP 오프셋(m). 근거를 evidence에 남긴다.
    tcp_offset: Vector3
    work_radius_m: float
    #: 정격 페이로드(kg). **없으면 None이다** — 데이터시트가 없을 때 값을 만들지
    #: 않는다. None이면 물체를 드는 스킬(pick·place)을 지원할 수 없고, 자유
    #: 이동(home·move·stop)은 페이로드와 무관하게 판정한다.
    payload_kg: float | None
    supported_skills: tuple[str, ...]
    gripper: GripperSpec | None = None
    #: 값의 출처·측정 방법. 숫자마다 근거를 요구하는 27장 기준을 위해 둔다.
    provenance: Mapping[str, str] = field(default_factory=dict)
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id or not self.profile_version:
            raise ProfileError(ReasonCode.CONFIG_MISSING, "profile_id/profile_version 필요")
        if self.dof <= 0:
            raise ProfileError(ReasonCode.CONFIG_INVALID, "dof가 0 이하")
        if len(self.joint_limits) != self.dof:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID,
                f"joint_limits 개수({len(self.joint_limits)})가 dof({self.dof})와 다르다",
            )
        for kind in (FrameKind.BASE, FrameKind.TOOL):
            if kind not in self.frames or not self.frames[kind]:
                raise ProfileError(ReasonCode.CONFIG_MISSING, f"{kind} 프레임 이름 필요")
        if self.work_radius_m <= 0:
            raise ProfileError(ReasonCode.CONFIG_INVALID, "work_radius_m가 0 이하")
        if self.payload_kg is not None and self.payload_kg <= 0:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID,
                "payload_kg가 0 이하 — 값이 없으면 0이 아니라 None으로 둔다",
            )
        if not self.supported_skills:
            raise ProfileError(ReasonCode.CONFIG_MISSING, "supported_skills가 비어 있다")
        unknown = set(self.supported_skills) - set(ATOMIC_SKILLS)
        if unknown:
            raise ProfileError(
                ReasonCode.CONFIG_INVALID, f"계약에 없는 스킬: {sorted(unknown)}"
            )
        needs_gripper = {"pick", "place"} & set(self.supported_skills)
        if needs_gripper and self.gripper is None:
            raise ProfileError(
                ReasonCode.CONFIG_MISSING,
                f"{sorted(needs_gripper)}를 지원하면 gripper 설정이 필요하다",
            )
        if needs_gripper and self.payload_kg is None:
            raise ProfileError(
                ReasonCode.CONFIG_MISSING,
                f"{sorted(needs_gripper)}를 지원하면 정격 페이로드가 필요하다"
                " — 근거 없이 물체를 드는 스킬을 허용하지 않는다",
            )

    def supports(self, skill: str) -> bool:
        return skill in self.supported_skills

    @property
    def can_lift(self) -> bool:
        """물체를 들 수 있는 근거가 있는가(페이로드 + 그리퍼)."""
        return self.payload_kg is not None and self.gripper is not None

    def joint(self, name: str) -> JointLimit | None:
        """관절 한계 조회. 없으면 None — 비슷한 이름으로 고쳐 주지 않는다."""
        for limit in self.joint_limits:
            if limit.name == name:
                return limit
        return None

    def frame_name(self, kind: FrameKind) -> str:
        """프레임 이름 조회. 없으면 추측하지 않고 차단한다."""
        name = self.frames.get(kind)
        if not name:
            raise ProfileError(ReasonCode.CONFIG_MISSING, f"{kind} 프레임 이름이 없다")
        return name
