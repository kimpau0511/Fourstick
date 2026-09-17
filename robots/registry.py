"""Robot Registry (md/개발플랜.md 2-01).

어떤 로봇이 존재하고 무엇을 할 수 있는지의 유일한 출처다. 공통 코드와 UI는
제조사·로봇명으로 분기하지 않고(계획.md 27장) 이 Registry에 물어본다.
UI의 로봇 목록도 이 Registry 응답에서 나온다.

미등록·미지원은 공통 이유 코드로 거부한다. 없는 로봇을 그럴듯한 기본값으로
대체하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Mapping

from core.capability_profile import CapabilityProfile
from core.reason_codes import ReasonCode
from robots.base.robot_adapter import RobotAdapter


class RegistryError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class RobotEntry:
    """Registry 한 칸. UI가 목록을 만들 때 쓰는 정보도 여기서 나온다."""

    robot_id: str
    profile: CapabilityProfile
    #: Adapter를 만드는 함수. Registry가 인스턴스를 붙들고 있지 않아도 되게 한다.
    factory: Callable[[str, CapabilityProfile], RobotAdapter]

    @property
    def supported_skills(self) -> tuple[str, ...]:
        return self.profile.supported_skills


class RobotRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, RobotEntry] = {}

    def register(
        self,
        robot_id: str,
        profile: CapabilityProfile,
        factory: Callable[[str, CapabilityProfile], RobotAdapter],
    ) -> None:
        if not robot_id:
            raise RegistryError(ReasonCode.CONFIG_MISSING, "robot_id가 비어 있다")
        if robot_id in self._entries:
            raise RegistryError(
                ReasonCode.CONFIG_INVALID, f"이미 등록된 robot_id: {robot_id!r}"
            )
        self._entries[robot_id] = RobotEntry(robot_id, profile, factory)

    # ── 조회 ────────────────────────────────────────────────────────────
    def robot_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def entries(self) -> tuple[RobotEntry, ...]:
        return tuple(self._entries[r] for r in self.robot_ids())

    def catalog(self) -> Mapping[str, Mapping[str, object]]:
        """UI·API가 그대로 내보낼 수 있는 목록. 코드에 고정 목록을 두지 않기 위함."""
        return {
            e.robot_id: {
                "profile_id": e.profile.profile_id,
                "profile_version": e.profile.profile_version,
                "dof": e.profile.dof,
                "supported_skills": list(e.supported_skills),
                "work_radius_m": e.profile.work_radius_m,
                "payload_kg": e.profile.payload_kg,
            }
            for e in self.entries()
        }

    def entry(self, robot_id: str) -> RobotEntry:
        if robot_id not in self._entries:
            raise RegistryError(
                ReasonCode.ROBOT_NOT_REGISTERED,
                f"등록되지 않은 로봇: {robot_id!r} (등록됨: {list(self.robot_ids())})",
            )
        return self._entries[robot_id]

    def profile(self, robot_id: str) -> CapabilityProfile:
        return self.entry(robot_id).profile

    def create(self, robot_id: str) -> RobotAdapter:
        e = self.entry(robot_id)
        return e.factory(e.robot_id, e.profile)

    # ── 검증 ────────────────────────────────────────────────────────────
    def require_skills(self, robot_id: str, skills: Iterable[str]) -> None:
        """이 로봇이 요청한 스킬을 전부 지원하는지. 아니면 공통 코드로 거부한다."""
        profile = self.profile(robot_id)
        missing = sorted({s for s in skills if not profile.supports(s)})
        if missing:
            raise RegistryError(
                ReasonCode.ROBOT_SKILL_UNSUPPORTED,
                f"{robot_id!r}가 지원하지 않는 스킬: {missing} "
                f"(지원: {list(profile.supported_skills)})",
            )

    def require_profile(self, robot_id: str, profile_id: str, profile_version: str) -> None:
        """계획이 검증된 Profile과 지금 등록된 Profile이 같은지."""
        profile = self.profile(robot_id)
        if (profile.profile_id, profile.profile_version) != (profile_id, profile_version):
            raise RegistryError(
                ReasonCode.ROBOT_PROFILE_MISMATCH,
                f"{robot_id!r}의 Profile은 "
                f"{profile.profile_id}@{profile.profile_version}인데 "
                f"{profile_id}@{profile_version}로 실행하려 한다",
            )
