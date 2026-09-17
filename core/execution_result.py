"""실행 결과 (md/개발플랜.md 1-06).

요청 수락 / 모션 완료 / 목표 도달 / 작업 성공 / 확인 불가를 각각 분리해서
담는다. 하나의 bool로 합치지 않는 이유는, 액션이 완료를 보고했는데 실제로는
작업이 되지 않은 사례가 반복해서 나왔기 때문이다.

강제 규칙(생성 시 검증):
1. 수락되지 않았으면 모션 완료·목표 도달·작업 성공은 모두 False여야 한다.
2. 모션이 완료되지 않았으면 목표 도달은 True일 수 없다.
3. 목표에 도달하지 않았으면 작업 성공은 True일 수 없다.
4. verified=False(확인 불가)이면 작업 성공은 True일 수 없다.
5. 실패(FAILED)·확인 불가(UNKNOWN)이거나 요청이 수락되지 않았으면 이유 코드가
   있어야 한다. 연결·점검·정지 요청처럼 "작업"이 아닌 동작은 작업 성공을
   주장하지 않지만 실패도 아니므로 이유 코드를 요구하지 않는다 — 요구하면
   의미 없는 코드를 채워 넣게 되고, 그러면 이유 코드의 신뢰도가 떨어진다.
6. 작업 성공은 COMPLETED 상태에서만 유효하다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode


class InvalidExecutionResult(Exception):
    """결과 필드 조합이 계약을 위반했을 때."""


@dataclass(frozen=True)
class ExecutionResult:
    state: ExecutionState
    #: 로봇이 요청(goal)을 수락했는가. 전송 성공과는 다르다.
    request_accepted: bool = False
    #: 모션 액션이 종료 상태(성공/취소/실패)로 응답을 돌려줬는가.
    motion_completed: bool = False
    #: 실제 계측값이 목표 허용범위 안에 들어왔는가.
    target_reached: bool = False
    #: 작업 자체가 달성됐는가(예: 물체가 실제로 들렸는가).
    task_succeeded: bool = False
    #: 위 판정을 계측으로 확인할 수 있었는가. False면 작업 성공을 주장할 수 없다.
    verified: bool = True
    reason: ReasonCode | None = None
    #: 판정 근거(측정값·시각 등). 자유 형식이지만 값에 단위를 함께 담는다.
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_accepted and (
            self.motion_completed or self.target_reached or self.task_succeeded
        ):
            raise InvalidExecutionResult(
                "수락되지 않은 요청이 모션 완료/목표 도달/작업 성공일 수 없다"
            )
        if self.target_reached and not self.motion_completed:
            raise InvalidExecutionResult("모션 미완료 상태에서 목표 도달일 수 없다")
        if self.task_succeeded and not self.target_reached:
            raise InvalidExecutionResult("목표 미도달 상태에서 작업 성공일 수 없다")
        if self.task_succeeded and not self.verified:
            raise InvalidExecutionResult("확인 불가(verified=False)를 작업 성공으로 둘 수 없다")
        needs_reason = (
            self.state in (ExecutionState.FAILED, ExecutionState.UNKNOWN)
            or not self.request_accepted
        )
        if needs_reason and self.reason is None:
            raise InvalidExecutionResult(
                "실패·확인 불가이거나 수락되지 않은 결과에는 이유 코드가 있어야 한다"
            )
        if self.task_succeeded and self.state is not ExecutionState.COMPLETED:
            raise InvalidExecutionResult("작업 성공은 COMPLETED 상태에서만 유효하다")

    @property
    def unverifiable(self) -> bool:
        return not self.verified

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "request_accepted": self.request_accepted,
            "motion_completed": self.motion_completed,
            "target_reached": self.target_reached,
            "task_succeeded": self.task_succeeded,
            "verified": self.verified,
            "reason": self.reason.value if self.reason else None,
            "evidence": dict(self.evidence),
        }


def success(evidence: Mapping[str, Any] | None = None) -> ExecutionResult:
    """작업 성공. 계측으로 확인된 경우에만 쓴다."""
    return ExecutionResult(
        state=ExecutionState.COMPLETED,
        request_accepted=True,
        motion_completed=True,
        target_reached=True,
        task_succeeded=True,
        verified=True,
        evidence=evidence or {},
    )


def unverifiable(reason: ReasonCode, evidence: Mapping[str, Any] | None = None) -> ExecutionResult:
    """계측 불가. 성공도 실패도 아니며 UNKNOWN 상태로 남는다."""
    return ExecutionResult(
        state=ExecutionState.UNKNOWN,
        request_accepted=True,
        verified=False,
        reason=reason,
        evidence=evidence or {},
    )


def rejected(reason: ReasonCode, evidence: Mapping[str, Any] | None = None) -> ExecutionResult:
    """요청 자체가 수락되지 않음(허가 거부·미지원 스킬·설정 누락 등)."""
    return ExecutionResult(
        state=ExecutionState.FAILED,
        request_accepted=False,
        reason=reason,
        evidence=evidence or {},
    )
