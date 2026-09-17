"""요청↔계획 리소스 일치 검증 (7단계 실행 전 게이트).

5-03 실측에서 남은 위험을 막는 규칙이다. 모델이 **없는 리소스를 유효한 다른
리소스로 치환**한 계획은 카탈로그 검증과 안전 검증을 모두 통과한다.

```
"3번 팔레트에서 A자재를 집어줘"  ->  move(loc_pallet_1) / pick(obj_a, loc_pallet_1)
"창고에서 A자재를 가져와"        ->  move(loc_conveyor) / pick(obj_a, loc_conveyor)
```

두 계획 모두 등록된 리소스만 쓰고 순서도 맞다. 그래서 계획 검증으로는 걸리지
않는다. 걸러낼 수 있는 유일한 근거는 **사용자가 실제로 말한 것**이다.

규칙 하나다.

> 계획이 쓰는 리소스는 **요청에서 확인된 리소스 안에** 있어야 한다.

확인된 리소스는 `ResourceCatalog`의 별칭으로 해석된 id다("일번 팔레트" →
`loc_pallet_1`). 요청에 없던 id가 계획에 나오면 차단한다.

이 규칙이 막는 것과 막지 못하는 것:
- 막는다: 없는 위치를 비슷한 위치로 치환, 말하지 않은 물체를 집는 계획
- 막지 못한다: 요청에 있는 리소스만으로 만든 **순서가 틀린** 계획
  (그것은 안전 검증 E-SEQ가 본다)

`home`·`stop`처럼 리소스를 쓰지 않는 계획은 이 검증의 대상이 아니다.

**보정하지 않는다.** 차이를 이유 코드와 함께 돌려주고, UI가 사용자에게 원래
요청과 계획의 차이를 보여준다. 어느 쪽이 맞는지는 사람이 정한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind
from core.task_plan import TaskPlan

#: 리소스를 담는 스킬 인자 이름. `SkillCatalog.arg_kinds`에서 계산한다.
_RESOURCE_ARGS = ("to", "from", "object")

#: 이 검증의 규칙 코드. 안전 검증 결과와 같은 저장 구조에 들어간다.
RULE_CODE = "E-REQ-001"


class ConsistencyStatus(str, Enum):
    #: 계획의 모든 리소스가 요청에서 확인된 범위 안에 있다.
    CONSISTENT = "consistent"
    #: 계획이 요청에 없는 리소스를 쓴다. 실행 전에 차단한다.
    MISMATCH = "mismatch"
    #: 요청에서 확인된 리소스가 없고 계획도 리소스를 쓰지 않는다(home/stop).
    NOT_APPLICABLE = "not_applicable"
    #: 계획은 리소스를 쓰는데 요청에서 확인된 리소스가 하나도 없다.
    #: 무엇을 대상으로 한 계획인지 확인할 근거가 없다.
    UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class ResolvedResource:
    """요청에서 확인된 리소스 하나. UI가 원문과 해석 결과를 함께 보여준다."""

    #: 발화에 실제로 등장한 표현(정규화된 형태).
    surface: str
    resource_id: str
    kind: str
    display_name: str


@dataclass(frozen=True)
class ConsistencyReport:
    status: ConsistencyStatus
    reason: ReasonCode | None
    #: 요청에서 확인된 리소스(별칭 해석 결과 포함).
    request_resources: tuple[ResolvedResource, ...] = ()
    #: 계획이 사용한 리소스 id.
    plan_resources: tuple[str, ...] = ()
    #: 계획에만 있고 요청에 없는 id. 치환이 여기 잡힌다.
    only_in_plan: tuple[str, ...] = ()
    #: 요청에만 있고 계획에 없는 id. 참고용이며 차단 사유가 아니다.
    only_in_request: tuple[str, ...] = ()
    detail: str = ""

    @property
    def allowed(self) -> bool:
        """실행을 진행할 수 있는가. 확인 불가는 통과로 보지 않는다."""
        return self.status in (
            ConsistencyStatus.CONSISTENT, ConsistencyStatus.NOT_APPLICABLE
        )

    def to_dict(self) -> dict:
        return {
            "status": self.status.value,
            "reason_code": None if self.reason is None else self.reason.value,
            "allowed": self.allowed,
            "request_resources": [
                {
                    "surface": r.surface, "resource_id": r.resource_id,
                    "kind": r.kind, "display_name": r.display_name,
                }
                for r in self.request_resources
            ],
            "plan_resources": list(self.plan_resources),
            "only_in_plan": list(self.only_in_plan),
            "only_in_request": list(self.only_in_request),
            "detail": self.detail,
        }


def plan_resource_ids(plan: TaskPlan) -> tuple[str, ...]:
    """계획이 쓰는 리소스 id를 스텝 순서대로. 중복은 첫 등장만 남긴다."""
    out: list[str] = []
    for step in plan.steps:
        for key in _RESOURCE_ARGS:
            value = step.args.get(key)
            if value and value not in out:
                out.append(value)
    if plan.terminal_hold and plan.terminal_hold not in out:
        out.append(plan.terminal_hold)
    return tuple(out)


def resolved_from_slots(slots, catalog: ResourceCatalog) -> tuple[ResolvedResource, ...]:
    """슬롯 추출 결과를 UI가 읽을 수 있는 형태로. 별칭 → id 해석을 드러낸다."""
    if slots is None:
        return ()
    out: list[ResolvedResource] = []
    seen: set[tuple[str, str]] = set()
    for match in slots.matches:
        key = (match.surface, match.resource_id)
        if key in seen:
            continue
        seen.add(key)
        entry = catalog.get(match.resource_id)
        out.append(
            ResolvedResource(
                surface=match.surface, resource_id=match.resource_id,
                kind=match.kind.value, display_name=entry.display_name,
            )
        )
    return tuple(out)


def check_request_plan_consistency(
    *, plan: TaskPlan, slots, catalog: ResourceCatalog
) -> ConsistencyReport:
    """계획이 요청에서 확인된 리소스만 쓰는지 본다."""
    request_resources = resolved_from_slots(slots, catalog)
    confirmed = {r.resource_id for r in request_resources}
    used = plan_resource_ids(plan)

    if not used:
        return ConsistencyReport(
            status=ConsistencyStatus.NOT_APPLICABLE, reason=None,
            request_resources=request_resources,
            detail="계획이 리소스를 쓰지 않는다(복귀·정지 계획)",
        )
    if not confirmed:
        return ConsistencyReport(
            status=ConsistencyStatus.UNVERIFIABLE,
            reason=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
            request_resources=request_resources, plan_resources=used,
            only_in_plan=used,
            detail=(
                "요청에서 확인된 리소스가 없는데 계획이 리소스를 쓴다"
                " — 무엇을 대상으로 한 계획인지 확인할 수 없다"
            ),
        )

    only_in_plan = tuple(r for r in used if r not in confirmed)
    only_in_request = tuple(
        r for r in sorted(confirmed) if r not in used
    )
    if only_in_plan:
        names = ", ".join(
            f"{rid}({catalog.get(rid).display_name})" if catalog.has(rid) else rid
            for rid in only_in_plan
        )
        return ConsistencyReport(
            status=ConsistencyStatus.MISMATCH,
            reason=ReasonCode.PLAN_RESOURCE_MISMATCH,
            request_resources=request_resources, plan_resources=used,
            only_in_plan=only_in_plan, only_in_request=only_in_request,
            detail=(
                f"요청에 없는 리소스를 계획이 쓴다: {names}."
                " 요청에서 확인된 리소스: "
                + (", ".join(sorted(confirmed)) or "없음")
            ),
        )
    return ConsistencyReport(
        status=ConsistencyStatus.CONSISTENT, reason=None,
        request_resources=request_resources, plan_resources=used,
        only_in_request=only_in_request,
        detail="계획의 모든 리소스가 요청에서 확인된 범위 안에 있다",
    )


def as_rule_result(report: ConsistencyReport) -> Mapping[str, object]:
    """안전 검증 결과와 같은 모양으로. 저장과 UI 표시를 한 경로로 모은다."""
    status = {
        ConsistencyStatus.CONSISTENT: "pass",
        ConsistencyStatus.NOT_APPLICABLE: "not_applicable",
        ConsistencyStatus.MISMATCH: "block",
        ConsistencyStatus.UNVERIFIABLE: "insufficient_data",
    }[report.status]
    reason = None if report.reason is None else report.reason.value
    return {
        "code": RULE_CODE,
        "status": status,
        # `reason`은 저장 경로가 쓰던 이름이고, `reason_code`는 안전 검증 규칙
        # 행과 같은 이름이다. 화면이 규칙 표를 한 방식으로 읽게 둘을 함께 둔다.
        "reason": reason,
        "reason_code": reason,
        "message": report.detail,
    }
