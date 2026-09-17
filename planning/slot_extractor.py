"""발화 → 리소스 슬롯 추출 (md/개발플랜.md 3-03).

**모델을 호출하지 않는다.** 카탈로그의 별칭과 문자열 일치만 쓴다. 의도 분류와
스킬 순서 결정은 계획 생성(LLM)의 일이고, 여기서는 "이 발화에 셀의 어떤
리소스가 등장했는가"만 답한다.

추측하지 않는 것:
- 카탈로그에 없는 표현을 비슷한 리소스로 바꾸지 않는다.
- 조사·어미를 규칙으로 벗기지 않는다. 필요한 변형은 카탈로그의 별칭에 적는다.
- 리소스가 하나도 없다고 해서 실패로 만들지 않는다 — "홈으로", "정지" 같은
  발화에는 원래 리소스가 없다. 판단은 호출자가 한다.

겹치는 별칭은 **긴 것이 이긴다**. "1번 팔레트"와 "팔레트"가 모두 등록된 경우
"1번 팔레트로"에서 짧은 쪽을 고르면 뜻이 달라진다.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind, normalize


@dataclass(frozen=True)
class SlotMatch:
    """발화 안에서 찾은 리소스 하나."""

    resource_id: str
    kind: ResourceKind
    #: 실제로 발화에 등장한 표현(정규화 전).
    surface: str
    #: 정규화한 발화 문자열 기준 위치. 등장 순서를 비교할 때 쓴다.
    start: int
    end: int


@dataclass(frozen=True)
class SlotExtraction:
    """추출 결과. 판정하지 않고 사실만 담는다."""

    utterance: str
    matches: tuple[SlotMatch, ...]
    #: 정지 키워드가 발화에 있는가. 계획 생성을 건너뛸지는 호출자가 정한다.
    stop_keyword_hit: bool = False

    @property
    def locations(self) -> tuple[str, ...]:
        """등장 순서대로의 위치 resource_id. 중복은 그대로 둔다."""
        return tuple(m.resource_id for m in self.matches if m.kind is ResourceKind.LOCATION)

    @property
    def objects(self) -> tuple[str, ...]:
        return tuple(m.resource_id for m in self.matches if m.kind is ResourceKind.OBJECT)

    @property
    def resource_ids(self) -> tuple[str, ...]:
        return tuple(m.resource_id for m in self.matches)

    def distinct(self, kind: ResourceKind) -> tuple[str, ...]:
        """종류별 고유 resource_id를 첫 등장 순서로."""
        out: list[str] = []
        for m in self.matches:
            if m.kind is kind and m.resource_id not in out:
                out.append(m.resource_id)
        return tuple(out)


def extract_slots(
    utterance: str, catalog: ResourceCatalog, *, stop_keywords: tuple[str, ...] = ()
) -> SlotExtraction:
    """발화에서 카탈로그 리소스를 찾는다.

    빈 발화는 예외로 만들지 않고 빈 결과로 돌려준다 — 빈 발화의 처리는
    호출자(요청 경계)의 몫이다.
    """
    text = normalize(utterance)
    index = catalog.alias_index()
    # 긴 별칭을 먼저 시도해 짧은 별칭이 먼저 먹는 것을 막는다.
    aliases = sorted(index, key=len, reverse=True)

    matches: list[SlotMatch] = []
    taken = [False] * len(text)
    for alias in aliases:
        start = text.find(alias)
        while start != -1:
            end = start + len(alias)
            if not any(taken[start:end]):
                resource_id = index[alias]
                matches.append(
                    SlotMatch(
                        resource_id=resource_id,
                        kind=catalog.kind_of(resource_id),
                        surface=alias,
                        start=start,
                        end=end,
                    )
                )
                for i in range(start, end):
                    taken[i] = True
            start = text.find(alias, start + 1)

    matches.sort(key=lambda m: m.start)
    return SlotExtraction(
        utterance=utterance,
        matches=tuple(matches),
        stop_keyword_hit=any(normalize(k) in text for k in stop_keywords),
    )


@dataclass(frozen=True)
class SlotIssue:
    reason: ReasonCode
    detail: str


def check_plan_resources(
    resource_args: tuple[str, ...], catalog: ResourceCatalog
) -> tuple[SlotIssue, ...]:
    """계획이 참조한 리소스가 카탈로그에 있는지 본다.

    계획 생성 **뒤에** 쓰는 함수다. 모델이 없는 위치를 만들어 내도 실행까지
    가지 않게 막는다. 비슷한 이름으로 바꿔주지 않는다 — 고쳐 주면 사용자가
    말한 것과 다른 곳으로 움직인다.
    """
    issues: list[SlotIssue] = []
    for value in resource_args:
        if not value:
            issues.append(
                SlotIssue(ReasonCode.PLAN_SLOT_INCOMPLETE, "리소스 인자가 비어 있다")
            )
        elif not catalog.has(value):
            issues.append(
                SlotIssue(
                    ReasonCode.PLAN_UNKNOWN_RESOURCE,
                    f"카탈로그에 없는 리소스: {value!r}",
                )
            )
    return tuple(issues)


def check_arg_kinds(steps, catalog: ResourceCatalog, skill_catalog) -> tuple[SlotIssue, ...]:
    """스킬 인자가 받아야 하는 리소스 **종류**까지 본다.

    `move.to`에 물체가 들어오면 이름은 카탈로그에 있어도 뜻이 틀렸다. 존재
    검사만으로는 걸리지 않으므로 종류를 따로 대조한다. 여기서도 보정하지 않는다.
    """
    issues: list[SlotIssue] = []
    for index, step in enumerate(steps, start=1):
        if not skill_catalog.has(step.skill):
            issues.append(
                SlotIssue(
                    ReasonCode.PLAN_UNSUPPORTED_SKILL,
                    f"스텝{index}: 카탈로그에 없는 스킬 {step.skill!r}",
                )
            )
            continue
        entry = skill_catalog.get(step.skill)
        for name, value in step.args.items():
            if name not in entry.arg_kinds:
                issues.append(
                    SlotIssue(
                        ReasonCode.PLAN_ARG_UNKNOWN,
                        f"스텝{index}: {step.skill}에 없는 인자 {name!r}",
                    )
                )
                continue
            if not catalog.has(value):
                continue   # 존재 검사는 check_plan_resources가 이미 보고한다
            expected = entry.arg_kinds[name]
            actual = catalog.kind_of(value)
            if actual is not expected:
                issues.append(
                    SlotIssue(
                        ReasonCode.PLAN_UNKNOWN_RESOURCE,
                        f"스텝{index}: {step.skill}.{name}에 {expected.value}가"
                        f" 와야 하는데 {value!r}는 {actual.value}다",
                    )
                )
    return tuple(issues)
