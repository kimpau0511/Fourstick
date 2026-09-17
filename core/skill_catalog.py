"""스킬 카탈로그 (md/개발플랜.md 5-01).

계약(`core/constants.py`)은 원자 스킬 5종과 각 스킬의 필수·허용 인자를 고정한다.
이 모듈은 그 계약 위에 **모델에게 보여줄 설명과 인자 종류**를 얹는다.

왜 따로 두는가:
- 프롬프트에 스킬 목록을 문자열로 박지 않는다. 카탈로그에서 렌더링한다.
- 인자가 어떤 종류의 리소스를 받는지 선언한다(`move.to`는 위치, `pick.object`는
  물체). 모델이 물체 이름을 위치 자리에 넣는 것을 잡아낸다.
- 카탈로그에 없는 스킬·인자는 **거부한다.** 비슷한 이름으로 고쳐 주지 않는다.

계약을 넘어서는 것을 만들 수 없다. 카탈로그가 계약에 없는 스킬이나 인자를
선언하면 로딩에서 거부된다 — 카탈로그가 계약을 우회하는 경로가 되면 안 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from core.constants import ATOMIC_SKILLS, SKILL_ALLOWED_ARGS, SKILL_REQUIRED_ARGS
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceKind


class SkillCatalogError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class SkillEntry:
    skill: str
    #: 모델에게 주는 한 줄 설명. 프롬프트에 그대로 들어간다.
    description: str
    #: 인자 이름 -> 받는 리소스 종류. 허용 인자 전부를 선언해야 한다.
    arg_kinds: Mapping[str, ResourceKind]

    def __post_init__(self) -> None:
        if self.skill not in ATOMIC_SKILLS:
            raise SkillCatalogError(
                ReasonCode.PLAN_UNSUPPORTED_SKILL,
                f"계약에 없는 스킬: {self.skill!r} (허용: {list(ATOMIC_SKILLS)})",
            )
        if not self.description:
            raise SkillCatalogError(
                ReasonCode.CONFIG_MISSING, f"{self.skill}의 설명이 없다"
            )
        allowed = set(SKILL_ALLOWED_ARGS[self.skill])
        declared = set(self.arg_kinds)
        unknown = sorted(declared - allowed)
        if unknown:
            raise SkillCatalogError(
                ReasonCode.PLAN_ARG_UNKNOWN,
                f"{self.skill}에 계약이 허용하지 않는 인자: {unknown}",
            )
        missing = sorted(allowed - declared)
        if missing:
            raise SkillCatalogError(
                ReasonCode.CONFIG_MISSING,
                f"{self.skill}의 인자 종류가 선언되지 않았다: {missing}",
            )
        for name, kind in self.arg_kinds.items():
            if not isinstance(kind, ResourceKind):
                raise SkillCatalogError(
                    ReasonCode.CONFIG_INVALID,
                    f"{self.skill}.{name}의 종류가 ResourceKind가 아니다: {kind!r}",
                )

    @property
    def required_args(self) -> tuple[str, ...]:
        """계약이 정한 필수 인자. 카탈로그가 바꿀 수 없다."""
        return SKILL_REQUIRED_ARGS[self.skill]

    @property
    def allowed_args(self) -> tuple[str, ...]:
        return SKILL_ALLOWED_ARGS[self.skill]


@dataclass(frozen=True)
class SkillCatalog:
    catalog_version: str
    entries: tuple[SkillEntry, ...]

    def __post_init__(self) -> None:
        if not self.catalog_version:
            raise SkillCatalogError(ReasonCode.CONFIG_MISSING, "catalog_version 필요")
        if not self.entries:
            raise SkillCatalogError(ReasonCode.CONFIG_MISSING, "entries가 비어 있다")
        seen: set[str] = set()
        for entry in self.entries:
            if entry.skill in seen:
                raise SkillCatalogError(
                    ReasonCode.CONFIG_INVALID, f"중복 스킬: {entry.skill!r}"
                )
            seen.add(entry.skill)

    def names(self) -> tuple[str, ...]:
        return tuple(e.skill for e in self.entries)

    def has(self, skill: str) -> bool:
        return any(e.skill == skill for e in self.entries)

    def get(self, skill: str) -> SkillEntry:
        for entry in self.entries:
            if entry.skill == skill:
                return entry
        raise SkillCatalogError(
            ReasonCode.PLAN_UNSUPPORTED_SKILL, f"카탈로그에 없는 스킬: {skill!r}"
        )

    def arg_kind(self, skill: str, arg: str) -> ResourceKind:
        try:
            return self.get(skill).arg_kinds[arg]
        except KeyError:
            raise SkillCatalogError(
                ReasonCode.PLAN_ARG_UNKNOWN, f"{skill}에 없는 인자: {arg!r}"
            ) from None
