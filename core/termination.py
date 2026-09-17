"""계획 종료 조건 생성 (md/개발플랜.md 5-03).

계획이 어떻게 끝나야 하는가는 **계약·정책·Profile에서 계산**한다. 프롬프트에
"항상 home을 추가하라"처럼 문장으로 박아 두지 않는다. 박아 두면 두 가지가
깨진다.

- 정책이 종료 스킬을 바꿔도 프롬프트는 옛 규칙을 말한다.
- 그 스킬을 지원하지 않는 로봇에게도 같은 문장을 보낸다.

출처는 셋이다.
- `SafetyPolicy.required_final_skill` — 어떤 스킬로 끝나야 하는가
- `SafetyPolicy.max_steps` — 스텝 상한
- `CapabilityProfile.supported_skills` / `gripper` — 그 로봇이 할 수 있는가,
  쥔 채 종료가 물리적으로 가능한가
- `TaskPlan` 계약(`terminal_hold`) — 목표 종료 상태를 어떻게 표기하는가

정책과 Profile이 어긋나면(정책은 `home` 종료를 요구하는데 로봇이 home을
지원하지 않으면) 그 사실을 숨기지 않고 `conflicts`에 남긴다. 프롬프트가 지킬
수 없는 요구를 하지 않도록, 그리고 설정 오류가 조용히 넘어가지 않도록.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.capability_profile import CapabilityProfile
from core.policy import SafetyPolicy
from core.resource_catalog import ResourceKind
from core.skill_catalog import SkillCatalog


@dataclass(frozen=True)
class ApproachRequirement:
    """위치 인자를 받는 작업 스텝 앞에 무엇이 있어야 하는가.

    스킬 이름과 인자 이름을 **카탈로그에서** 읽는다. 프롬프트가 "pick 또는
    place 직전에는"이라고 말하려면 그 이름이 카탈로그에서 와야 한다 — 스킬이
    늘거나 이름이 바뀌면 문장도 따라 바뀌어야 한다.
    """

    #: 직전에 있어야 하는 이동 스킬. None이면 제약이 없다.
    approach_skill: str | None
    #: (스킬, 위치 인자 이름) 쌍. 카탈로그의 arg_kinds에서 계산한다.
    location_args: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TerminationRequirement:
    """이번 계획이 만족해야 하는 종료 조건. 계산 결과이며 상수가 아니다."""

    #: 계획의 마지막 스텝이어야 하는 스킬. None이면 제약이 없다.
    final_skill: str | None
    #: 스텝 상한.
    max_steps: int
    #: 물체를 쥔 채 종료할 수 있는가(그리퍼와 pick 지원 여부에서 계산).
    hold_possible: bool
    #: 근거. 어떤 설정에서 나온 값인지 프롬프트와 기록에 함께 남긴다.
    sources: tuple[str, ...] = ()
    #: 정책과 Profile이 어긋난 지점.
    conflicts: tuple[str, ...] = ()

    @property
    def satisfiable(self) -> bool:
        return not self.conflicts


def termination_requirement(
    *, profile: CapabilityProfile, safety_policy: SafetyPolicy
) -> TerminationRequirement:
    """정책과 Profile에서 종료 조건을 계산한다."""
    sources: list[str] = [
        f"SafetyPolicy {safety_policy.policy_version}",
        f"CapabilityProfile {profile.profile_id} {profile.profile_version}",
    ]
    conflicts: list[str] = []

    final_skill = safety_policy.required_final_skill
    if final_skill is not None and not profile.supports(final_skill):
        conflicts.append(
            f"정책은 {final_skill!r}로 종료하라고 요구하는데 이 로봇의"
            f" supported_skills에 {final_skill!r}가 없다"
        )
        final_skill = None

    # 쥔 채 종료는 그리퍼가 있고 pick을 지원할 때만 가능하다.
    hold_possible = profile.gripper is not None and profile.supports("pick")

    return TerminationRequirement(
        final_skill=final_skill,
        max_steps=safety_policy.max_steps,
        hold_possible=hold_possible,
        sources=tuple(sources),
        conflicts=tuple(conflicts),
    )


def approach_requirement(
    *, skill_catalog: SkillCatalog, safety_policy: SafetyPolicy,
    profile: CapabilityProfile,
) -> ApproachRequirement:
    """위치 인자를 받는 스킬과 그 인자 이름을 카탈로그에서 계산한다.

    이동 스킬 자신은 제외한다 — 이동 앞에 이동을 요구하지 않는다.
    """
    approach = safety_policy.approach_skill
    if approach is not None and not profile.supports(approach):
        approach = None
    pairs: list[tuple[str, str]] = []
    for entry in skill_catalog.entries:
        if entry.skill == approach or not profile.supports(entry.skill):
            continue
        for arg, kind in entry.arg_kinds.items():
            if kind is ResourceKind.LOCATION:
                pairs.append((entry.skill, arg))
    return ApproachRequirement(
        approach_skill=approach, location_args=tuple(pairs)
    )
