"""실행 상태와 상태 전이 (md/개발플랜.md 1-05).

IDLE에서 UNKNOWN까지의 전이를 한 곳에서 정의한다. 임의 전이를 허용하지 않고
허용 표에 없는 전이는 거부한다 — 상태가 조용히 뒤바뀌면 "정지했는지"를
사후에 신뢰할 수 없기 때문이다.

UNKNOWN의 의미: 계측이 불가능해 현재 상태를 말할 수 없는 상태다. UNKNOWN에서
COMPLETED로 가는 전이는 없다. 확인되지 않은 것을 성공으로 바꾸지 않는다.
"""

from __future__ import annotations

from enum import Enum


class ExecutionState(str, Enum):
    IDLE = "idle"                # 실행 중인 계획 없음
    ACCEPTED = "accepted"        # 요청을 수락했고 아직 동작 전
    EXECUTING = "executing"      # 스텝 실행 중
    STOPPING = "stopping"        # STOP 접수, 실제 정지 확인 전
    STOPPED = "stopped"          # 실제 정지를 계측으로 확인함
    COMPLETED = "completed"      # 모든 스텝이 작업 성공으로 끝남
    FAILED = "failed"            # 원인이 확인된 실패
    UNKNOWN = "unknown"          # 계측 불가 — 성공으로 변환 금지

    def __str__(self) -> str:
        return self.value


#: 허용 전이표. 키에서 값들로만 이동할 수 있다.
ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.IDLE: frozenset({ExecutionState.ACCEPTED, ExecutionState.UNKNOWN}),
    ExecutionState.ACCEPTED: frozenset({
        ExecutionState.EXECUTING,
        ExecutionState.STOPPING,
        ExecutionState.FAILED,
        ExecutionState.UNKNOWN,
    }),
    ExecutionState.EXECUTING: frozenset({
        ExecutionState.EXECUTING,   # 다음 스텝으로 진행
        ExecutionState.STOPPING,
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
        ExecutionState.UNKNOWN,
    }),
    ExecutionState.STOPPING: frozenset({
        ExecutionState.STOPPED,     # 실측으로 정지 확인됨
        ExecutionState.UNKNOWN,     # 정지를 확인할 수 없음
    }),
    # 종료 상태에서는 새 계획 수락(IDLE 복귀)만 가능하다.
    ExecutionState.STOPPED: frozenset({ExecutionState.IDLE}),
    ExecutionState.COMPLETED: frozenset({ExecutionState.IDLE}),
    ExecutionState.FAILED: frozenset({ExecutionState.IDLE}),
    # UNKNOWN에서 COMPLETED로 가는 경로는 의도적으로 없다.
    ExecutionState.UNKNOWN: frozenset({ExecutionState.IDLE, ExecutionState.STOPPING}),
}

#: 더 이상 진행하지 않는 상태(새 계획 수락 전까지).
TERMINAL_STATES: frozenset[ExecutionState] = frozenset({
    ExecutionState.STOPPED,
    ExecutionState.COMPLETED,
    ExecutionState.FAILED,
    ExecutionState.UNKNOWN,
})


class InvalidStateTransition(Exception):
    """허용 표에 없는 전이를 시도했을 때."""

    def __init__(self, src: ExecutionState, dst: ExecutionState):
        self.src, self.dst = src, dst
        super().__init__(f"허용되지 않은 상태 전이: {src} -> {dst}")


def can_transition(src: ExecutionState, dst: ExecutionState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


def transition(src: ExecutionState, dst: ExecutionState) -> ExecutionState:
    """전이를 검증하고 목표 상태를 돌려준다. 허용되지 않으면 예외."""
    if not can_transition(src, dst):
        raise InvalidStateTransition(src, dst)
    return dst
