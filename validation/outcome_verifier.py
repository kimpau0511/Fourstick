"""실행 결과 검증 — 계획의 목표 상태와 실제 관측 상태를 대조한다.

`TaskPlan.terminal_hold`는 계획이 명시한 **목표** 종료 파지 상태다. 계획
검증(validation/safety_validator.py E-HOLD-003)은 "스텝을 따라가면 그 목표에
도달하는가"만 본다. 실제로 그렇게 끝났는지는 실행 후 관측으로 확인해야 하고,
그것이 이 모듈의 역할이다.

둘을 분리하는 이유: 계획이 논리적으로 맞아도 실행 중 물체를 놓칠 수 있다.
계획 검증 통과를 작업 성공으로 세지 않는다.

"쥔 것이 없다"와 "확인할 수 없다"를 구별한다. 관측이 불가능하면 성공도
실패도 아닌 확인 불가로 남기고, 절대 성공으로 바꾸지 않는다.

## 파지 관측을 요구하는 계획은 어느 것인가 (8-09)

모든 계획에 파지 관측을 요구하면, 파지 센서가 없는 셀에서는 `home`·빈손
`move`·`stop`도 영원히 `exec.unverifiable`이 된다. 모션이 관측으로 확인됐는데
확인 불가로 적는 것은 사실과 다르다.

그래서 **계획의 목표에 물체 보유 상태가 포함되는지**를 먼저 판정한다
(`hold_requirement`). 판정 근거는 두 가지이고, **둘 다 명시적 선언**이다:

1. `TaskPlan.terminal_hold` — 계획이 명시한 목표 종료 파지 상태
2. 스킬 카탈로그가 선언한 **인자 종류**(`arg_kinds`)가 `ResourceKind.OBJECT`인
   인자를 쓰는 스텝이 있는가 — 그 계획은 물체 보유 상태를 바꾸므로 종료
   상태를 관측으로 확인해야 한다

**스킬 이름으로 분기하지 않는다.** "move면 관측 불필요" 같은 규칙을 코드에
두면 카탈로그가 바뀌어도 코드가 옛 가정을 들고 있게 된다. 위 2번은 계약
(`core/constants.py`의 허용 인자)과 카탈로그 선언에서만 나온다.

요구되지 않는 계획에서는 **파지 관측을 아예 하지 않는다** — 호출자가
snapshot을 만들지 않고 `None`을 넘긴다. 없는 관측을 "확인 불가"로 적지도,
성공으로 바꾸지도 않기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from core.execution_result import ExecutionResult, success, unverifiable
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceKind
from core.skill_catalog import SkillCatalog
from core.task_plan import TaskPlan
from robots.base.robot_adapter import RobotStateSnapshot


@dataclass(frozen=True)
class HoldRequirement:
    """이 계획의 목표에 물체 보유 상태가 포함되는가.

    `required=False`면 호출자는 **파지 관측을 하지 않는다**. 근거(`basis`)를
    함께 담아 판정이 왜 그렇게 됐는지 기록에 남긴다.
    """

    required: bool
    #: 판정 근거를 사람이 읽는 한 줄로.
    basis: str
    #: 계획이 명시한 목표 종료 파지 상태.
    terminal_hold: str | None
    #: 물체 인자를 쓰는 스텝(번호, 스킬, 인자 이름, 물체 이름).
    object_steps: tuple[tuple[int, str, str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hold_required": self.required,
            "basis": self.basis,
            "terminal_hold": self.terminal_hold,
            "object_steps": [
                {"step": no, "skill": skill, "arg": arg, "object": name}
                for no, skill, arg, name in self.object_steps
            ],
        }


def hold_requirement(plan: TaskPlan, skill_catalog: SkillCatalog) -> HoldRequirement:
    """계획의 목표에 물체 보유 상태가 포함되는지 **명시적 선언으로** 판정한다.

    스킬 이름으로 분기하지 않는다. 카탈로그가 선언한 인자 종류와 계획이 명시한
    `terminal_hold`만 본다. 카탈로그에 없는 스킬·인자는 **보수적으로 요구**로
    본다 — 모르는 것을 "관측 불필요"로 넘기지 않는다.
    """
    object_steps: list[tuple[int, str, str, str]] = []
    unknown: list[str] = []
    for index, step in enumerate(plan.steps, start=1):
        if not skill_catalog.has(step.skill):
            unknown.append(f"스텝{index}({step.skill})")
            continue
        entry = skill_catalog.get(step.skill)
        for arg, value in step.args.items():
            kind = entry.arg_kinds.get(arg)
            if kind is None:
                unknown.append(f"스텝{index}({step.skill}.{arg})")
                continue
            if kind is ResourceKind.OBJECT:
                object_steps.append((index, step.skill, arg, str(value)))

    if unknown:
        return HoldRequirement(
            required=True,
            basis="카탈로그에 선언되지 않은 스킬·인자가 있다"
                  f" ({', '.join(unknown)}) — 모르는 것을 관측 불필요로 보지 않는다",
            terminal_hold=plan.terminal_hold,
            object_steps=tuple(object_steps),
        )
    if plan.terminal_hold is not None:
        return HoldRequirement(
            required=True,
            basis=f"계획이 목표 종료 파지 상태를 명시했다"
                  f" (terminal_hold={plan.terminal_hold!r})",
            terminal_hold=plan.terminal_hold,
            object_steps=tuple(object_steps),
        )
    if object_steps:
        names = ", ".join(f"스텝{no} {skill}.{arg}={name!r}"
                          for no, skill, arg, name in object_steps)
        return HoldRequirement(
            required=True,
            basis=f"카탈로그가 물체(object)로 선언한 인자를 쓰는 스텝이 있다"
                  f" ({names}) — 보유 상태가 바뀌므로 종료 상태를 확인해야 한다",
            terminal_hold=None,
            object_steps=tuple(object_steps),
        )
    return HoldRequirement(
        required=False,
        basis="계획이 목표 파지 상태를 명시하지 않았고, 카탈로그가 물체로"
              " 선언한 인자를 쓰는 스텝도 없다 — 보유 상태가 목표에 없다",
        terminal_hold=None,
        object_steps=(),
    )


@dataclass(frozen=True)
class StopOutcome:
    """정지 관측 결과. 요청 접수만으로 정지를 주장하지 않는다."""

    #: 정지 요청이 있었는가(계획에 정지가 포함됐거나 실행 중 정지가 걸렸는가).
    requested: bool
    #: 관측으로 정지가 확인됐는가.
    confirmed: bool
    detail: str = ""
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop_requested": self.requested,
            "stop_confirmed": self.confirmed,
            "detail": self.detail,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class HoldOutcome:
    """파지 상태 대조 결과."""

    #: 파지 상태를 관측할 수 있었는가.
    verified: bool
    #: 관측된 상태가 계획의 목표와 일치했는가. verified=False면 의미 없다.
    matched: bool
    expected: str | None
    observed: str | None
    reason: ReasonCode | None = None

    def evidence(self) -> dict[str, Any]:
        return {
            "terminal_hold_expected": self.expected,
            "terminal_hold_observed": self.observed if self.verified else None,
            "hold_observed": self.verified,
        }


def verify_terminal_hold(
    plan: TaskPlan, snapshot: RobotStateSnapshot | None
) -> HoldOutcome:
    """계획의 목표 종료 파지 상태를 실제 관측과 대조한다.

    `snapshot=None`은 "관측하지 않았다"는 뜻이다. 파지 관측이 요구되지 않는
    계획에서 호출자가 관측을 아예 하지 않았을 때 넘어온다 — 그 경우를 확인
    불가로 적지 않으려고 `finalize_plan_result`가 요구 여부를 함께 받는다.
    """
    expected = plan.terminal_hold
    if snapshot is None:
        return HoldOutcome(
            verified=False,
            matched=False,
            expected=expected,
            observed=None,
            reason=None,
        )
    if not snapshot.valid or not snapshot.hold_observed:
        return HoldOutcome(
            verified=False,
            matched=False,
            expected=expected,
            observed=None,
            reason=ReasonCode.EXEC_UNVERIFIABLE,
        )
    observed = snapshot.held_object
    if observed == expected:
        return HoldOutcome(True, True, expected, observed)
    return HoldOutcome(
        verified=True,
        matched=False,
        expected=expected,
        observed=observed,
        reason=ReasonCode.EXEC_TASK_FAILED,
    )


def finalize_plan_result(
    outcome: HoldOutcome,
    *,
    motion_completed: bool,
    target_reached: bool,
    evidence: Mapping[str, Any] | None = None,
    requirement: HoldRequirement | None = None,
    stop: StopOutcome | None = None,
    scene_revalidated: bool | None = None,
) -> ExecutionResult:
    """관측 결과들을 합쳐 최종 ExecutionResult를 만든다.

    판정 순서와 근거:

    1. **정지가 요청된 계획**은 정지 관측이 기준이다. 확인되면 STOPPED로
       끝나고(작업 성공이 아니라 정지가 목표였다), 확인되지 않으면
       `exec.stop_unconfirmed`로 남긴다. 요청 접수만으로 정지를 주장하지 않는다.
    2. **파지 관측이 요구되는 계획**(`requirement.required`)은 파지 대조가
       성공해야 하고 그 대조가 관측으로 확인돼야 한다.
    3. **요구되지 않는 계획**(`home`·빈손 `move` 등)은 파지 관측을 요구하지
       않는다. 모션 완료·목표 도달·planning scene 재검증만 본다.

    `requirement`를 주지 않으면 **예전처럼 파지 관측을 요구한다**(보수적 기본값).
    """
    ev: dict[str, Any] = {**outcome.evidence(), **(dict(evidence) if evidence else {})}
    if requirement is not None:
        ev["hold_requirement"] = requirement.to_dict()
    if stop is not None:
        ev["stop"] = stop.to_dict()
    if scene_revalidated is not None:
        ev["planning_scene_revalidated"] = scene_revalidated

    # 1. 정지가 요청된 계획 — 정지 관측이 기준이다.
    if stop is not None and stop.requested:
        if not stop.confirmed:
            return unverifiable(ReasonCode.EXEC_STOP_UNCONFIRMED, {
                **ev,
                "detail": stop.detail or "정지를 관측으로 확인하지 못했다",
            })
        return ExecutionResult(
            state=ExecutionState.STOPPED,
            request_accepted=True,
            motion_completed=motion_completed,
            target_reached=False,
            task_succeeded=False,
            verified=True,
            reason=ReasonCode.EXEC_STOPPED,
            evidence={**ev, "detail": stop.detail or "정지를 관측으로 확인했다"},
        )

    # planning scene 재검증이 통과하지 않았으면 성공으로 보지 않는다.
    if scene_revalidated is False:
        return ExecutionResult(
            state=ExecutionState.FAILED,
            request_accepted=True,
            motion_completed=motion_completed,
            target_reached=target_reached and motion_completed,
            task_succeeded=False,
            verified=True,
            reason=ReasonCode.GEOMETRY_COLLISION,
            evidence={**ev, "detail": "실행 직전 planning scene 재검증이"
                                      " 통과하지 않았다"},
        )

    # 3. 파지 관측을 요구하지 않는 계획 — 모션 관측만으로 판정한다.
    if requirement is not None and not requirement.required:
        if not (motion_completed and target_reached):
            return ExecutionResult(
                state=ExecutionState.FAILED,
                request_accepted=True,
                motion_completed=motion_completed,
                target_reached=target_reached and motion_completed,
                task_succeeded=False,
                verified=True,
                reason=ReasonCode.EXEC_GOAL_NOT_REACHED,
                evidence=ev,
            )
        return success(ev)

    # 2. 파지 관측을 요구하는 계획.
    if not outcome.verified:
        return unverifiable(outcome.reason or ReasonCode.EXEC_UNVERIFIABLE, ev)
    if not outcome.matched:
        return ExecutionResult(
            state=ExecutionState.FAILED,
            request_accepted=True,
            motion_completed=motion_completed,
            target_reached=target_reached and motion_completed,
            task_succeeded=False,
            verified=True,
            reason=outcome.reason or ReasonCode.EXEC_TASK_FAILED,
            evidence=ev,
        )
    if not (motion_completed and target_reached):
        return ExecutionResult(
            state=ExecutionState.FAILED,
            request_accepted=True,
            motion_completed=motion_completed,
            target_reached=target_reached and motion_completed,
            task_succeeded=False,
            verified=True,
            reason=ReasonCode.EXEC_GOAL_NOT_REACHED,
            evidence=ev,
        )
    return success(ev)
