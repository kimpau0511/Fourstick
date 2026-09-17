"""작업 대상 리소스 카탈로그 (md/개발플랜.md 3-03).

슬롯 추출이 쓰는 어휘의 **유일한 출처**다. 위치 이름·자재 이름·별칭을 제품
코드에 두지 않는다(계획.md 27장). 셀이 바뀌면 이 설정만 바꾼다.

두 가지를 계약으로 막는다.
- **별칭 충돌**: 같은 표현이 두 리소스를 가리키면 실행 시점에 추측하게 된다.
  설정을 읽을 때 거부한다.
- **모르는 리소스**: 카탈로그에 없는 이름은 매칭되지 않는다. LLM이 없는 위치를
  만들어 내도 여기서 걸린다.

`CapabilityProfile`과 역할이 다르다. Profile은 **로봇이 무엇을 할 수 있는가**
(관절·페이로드·그리퍼)이고, 카탈로그는 **셀에 무엇이 있는가**(위치·자재)다.
로봇을 바꿔도 셀이 같으면 카탈로그는 그대로다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from core.reason_codes import ReasonCode


class CatalogError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class ResourceKind(str, Enum):
    """리소스 종류. 스킬 인자가 어떤 종류를 받는지 판단할 때 쓴다."""

    #: 로봇이 이동·집기·놓기의 대상으로 삼는 장소.
    LOCATION = "location"
    #: 집어 옮기는 물체.
    OBJECT = "object"


def normalize(text: str) -> str:
    """별칭 비교용 정규화.

    한국어 발화에는 공백이 들쑥날쑥 들어온다("1번 팔레트" / "1번팔레트").
    전각·반각과 자모 조합도 섞이므로 NFKC로 모은 뒤 공백을 지우고 소문자로
    맞춘다. **조사나 어미는 건드리지 않는다** — 규칙을 늘리면 의도를 추측하게
    되므로, 필요한 변형은 설정의 별칭으로 명시한다.
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(folded.split()).lower()


@dataclass(frozen=True)
class ResourceEntry:
    resource_id: str
    kind: ResourceKind
    #: 사용자에게 보여줄 이름.
    display_name: str
    #: 발화에서 이 리소스를 가리키는 표현들. display_name도 포함해야 한다.
    aliases: tuple[str, ...]
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise CatalogError(ReasonCode.CONFIG_MISSING, "resource_id가 비어 있다")
        if not isinstance(self.kind, ResourceKind):
            raise CatalogError(
                ReasonCode.CONFIG_INVALID, f"kind가 ResourceKind가 아니다: {self.kind!r}"
            )
        if not self.display_name:
            raise CatalogError(
                ReasonCode.CONFIG_MISSING, f"{self.resource_id}의 display_name이 없다"
            )
        if not self.aliases:
            raise CatalogError(
                ReasonCode.CONFIG_MISSING, f"{self.resource_id}의 별칭이 없다"
            )
        if any(not normalize(a) for a in self.aliases):
            raise CatalogError(
                ReasonCode.CONFIG_INVALID, f"{self.resource_id}에 빈 별칭이 있다"
            )
        if normalize(self.display_name) not in {normalize(a) for a in self.aliases}:
            raise CatalogError(
                ReasonCode.CONFIG_INVALID,
                f"{self.resource_id}의 별칭에 display_name이 없다"
                " — 화면에 보이는 이름으로 말해도 인식돼야 한다",
            )

    def normalized_aliases(self) -> tuple[str, ...]:
        return tuple(normalize(a) for a in self.aliases)


@dataclass(frozen=True)
class ResourceCatalog:
    catalog_version: str
    entries: tuple[ResourceEntry, ...]
    #: 정규화된 별칭 -> resource_id. __post_init__이 만든다.
    _index: Mapping[str, str] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.catalog_version:
            raise CatalogError(ReasonCode.CONFIG_MISSING, "catalog_version 필요")
        if not self.entries:
            raise CatalogError(ReasonCode.CONFIG_MISSING, "entries가 비어 있다")
        index: dict[str, str] = {}
        seen_ids: set[str] = set()
        for entry in self.entries:
            if entry.resource_id in seen_ids:
                raise CatalogError(
                    ReasonCode.CONFIG_INVALID, f"중복 resource_id: {entry.resource_id!r}"
                )
            seen_ids.add(entry.resource_id)
            for alias in entry.normalized_aliases():
                owner = index.get(alias)
                if owner is not None and owner != entry.resource_id:
                    raise CatalogError(
                        ReasonCode.CONFIG_INVALID,
                        f"별칭 {alias!r}이 {owner!r}와 {entry.resource_id!r}를 함께 가리킨다"
                        " — 실행 시점에 추측하지 않도록 설정에서 막는다",
                    )
                index[alias] = entry.resource_id
        object.__setattr__(self, "_index", index)

    # ── 조회 ────────────────────────────────────────────────────────────
    def get(self, resource_id: str) -> ResourceEntry:
        for entry in self.entries:
            if entry.resource_id == resource_id:
                return entry
        raise CatalogError(
            ReasonCode.PLAN_UNKNOWN_RESOURCE, f"카탈로그에 없는 리소스: {resource_id!r}"
        )

    def has(self, resource_id: str) -> bool:
        return any(e.resource_id == resource_id for e in self.entries)

    def kind_of(self, resource_id: str) -> ResourceKind:
        return self.get(resource_id).kind

    def ids_of_kind(self, kind: ResourceKind) -> tuple[str, ...]:
        return tuple(e.resource_id for e in self.entries if e.kind is kind)

    def resolve_alias(self, alias: str) -> str | None:
        """정규화한 표현이 가리키는 resource_id. 없으면 None이다."""
        return self._index.get(normalize(alias))

    def alias_index(self) -> Mapping[str, str]:
        return dict(self._index)

    @property
    def locations(self) -> frozenset[str]:
        """위치 resource_id 집합. 안전 검증의 인자 대조가 쓴다."""
        return frozenset(self.ids_of_kind(ResourceKind.LOCATION))

    @property
    def objects(self) -> frozenset[str]:
        return frozenset(self.ids_of_kind(ResourceKind.OBJECT))
