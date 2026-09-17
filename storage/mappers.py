"""core 계약 타입 ↔ 저장 레코드 변환 경계.

여기가 유일한 변환 지점이다. PostgreSQL 구현체를 추가해도 이 파일과 core 계약,
API 스키마는 바뀌지 않는다 — 구현체는 레코드만 주고받는다.

호환 원칙 (요구 12):
- ID: 모두 TEXT. 애플리케이션이 생성한다. DB 자동 증가를 쓰지 않는다.
- Boolean: SQLite에 전용 타입이 없으므로 INTEGER 0/1로 저장하고 경계에서 bool로
  되돌린다. PostgreSQL의 BOOLEAN으로 바꿔도 이 경계만 유지하면 된다.
- UTC timestamp: epoch 초(float/REAL). DB의 타임스탬프 타입을 쓰지 않는다 —
  타임존 의미와 정밀도가 DB마다 다르다.
- JSON: TEXT로 저장하고 애플리케이션이 직렬화·역직렬화한다. DB의 JSON 연산자를
  쓰지 않아 SQLite/PostgreSQL 차이가 질의에 새어들지 않는다.
- enum: `value` 문자열로 저장하고 읽을 때 enum으로 되돌린다. 목록에 없는 값은
  거부한다(기본값으로 대체하면 저장된 실패가 성공이 될 수 있다).

원본 보존 (요구 11): TaskPlan과 관측값·근거는 JSON 원본을 그대로 저장하고,
조회에 자주 쓰는 값(ID, 상태, ReasonCode, 시각, Profile·Policy 버전)만 별도
컬럼으로 복제한다. 복제본과 원본이 어긋나면 조회 시 거부한다.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from core.execution_result import ExecutionResult, InvalidExecutionResult
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from core.task_plan import PlanError, TaskPlan, TaskStep
from storage.repository import CorruptedRecord


def to_bool(value: Any) -> bool:
    """INTEGER 0/1 또는 BOOLEAN 모두 받는다."""
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise CorruptedRecord(
        ReasonCode.CONFIG_INVALID, f"boolean으로 해석할 수 없는 값: {value!r}"
    )


def from_bool(value: bool) -> int:
    return 1 if value else 0


def dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def load_json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError) as exc:
        raise CorruptedRecord(
            ReasonCode.CONFIG_INVALID, f"{what}의 JSON을 해석할 수 없다"
        ) from exc


def to_state(value: str) -> ExecutionState:
    try:
        return ExecutionState(value)
    except ValueError as exc:
        raise CorruptedRecord(
            ReasonCode.CONFIG_INVALID, f"저장된 상태를 해석할 수 없다: {value!r}"
        ) from exc


def to_reason(value: str | None) -> ReasonCode | None:
    if value is None:
        return None
    try:
        return ReasonCode(value)
    except ValueError as exc:
        raise CorruptedRecord(
            ReasonCode.CONFIG_INVALID, f"저장된 이유 코드를 해석할 수 없다: {value!r}"
        ) from exc


# ── TaskPlan ────────────────────────────────────────────────────────────


def plan_to_json(plan: TaskPlan) -> str:
    """원본 보존용. plan_hash는 별도 컬럼에 둔다."""
    return dump_json(plan.to_dict())


def plan_from_row(
    *,
    plan_json: str,
    stored_hash: str,
    stored_schema_version: str,
    stored_profile_id: str,
    stored_profile_version: str,
) -> TaskPlan:
    """원본 JSON에서 계획을 복원하고, 검색용 복제 컬럼과 일치하는지 대조한다."""
    raw = load_json(plan_json, "task_plan")
    if not isinstance(raw, dict):
        raise CorruptedRecord(ReasonCode.CONFIG_INVALID, "task_plan JSON이 객체가 아니다")
    try:
        plan = TaskPlan(
            plan_id=raw["plan_id"],
            robot_id=raw["robot_id"],
            profile_id=raw["profile_id"],
            profile_version=raw["profile_version"],
            steps=tuple(TaskStep(s["skill"], s.get("args", {})) for s in raw["steps"]),
            created_at=raw["created_at"],
            ttl_sec=raw["ttl_sec"],
            schema_version=raw["schema_version"],
            utterance=raw.get("utterance", ""),
            terminal_hold=raw.get("terminal_hold"),
        )
    except (KeyError, TypeError) as exc:
        raise CorruptedRecord(
            ReasonCode.CONFIG_INVALID, f"task_plan JSON에 필요한 필드가 없다: {exc}"
        ) from exc
    except PlanError as exc:
        raise CorruptedRecord(exc.reason, f"저장된 계획이 계약을 만족하지 않는다: {exc}") from exc

    if plan.plan_hash() != stored_hash:
        raise CorruptedRecord(
            ReasonCode.PLAN_HASH_MISMATCH,
            f"복원된 계획의 해시가 저장값과 다르다: {plan.plan_id!r}",
        )
    for label, actual, stored in (
        ("schema_version", plan.schema_version, stored_schema_version),
        ("profile_id", plan.profile_id, stored_profile_id),
        ("profile_version", plan.profile_version, stored_profile_version),
    ):
        if actual != stored:
            raise CorruptedRecord(
                ReasonCode.CONFIG_INVALID,
                f"검색용 {label} 컬럼({stored!r})이 원본({actual!r})과 다르다",
            )
    return plan


# ── ExecutionResult ─────────────────────────────────────────────────────

RESULT_COLUMNS = (
    "state",
    "request_accepted",
    "motion_completed",
    "target_reached",
    "task_succeeded",
    "verified",
    "reason",
    "evidence_json",
)


def result_to_columns(result: ExecutionResult) -> tuple[Any, ...]:
    """5축을 각각 컬럼으로 저장한다. 하나의 플래그로 합치지 않는다."""
    return (
        result.state.value,
        from_bool(result.request_accepted),
        from_bool(result.motion_completed),
        from_bool(result.target_reached),
        from_bool(result.task_succeeded),
        from_bool(result.verified),
        result.reason.value if result.reason else None,
        dump_json(dict(result.evidence)),
    )


def result_from_row(row: Mapping[str, Any]) -> ExecutionResult:
    try:
        return ExecutionResult(
            state=to_state(row["state"]),
            request_accepted=to_bool(row["request_accepted"]),
            motion_completed=to_bool(row["motion_completed"]),
            target_reached=to_bool(row["target_reached"]),
            task_succeeded=to_bool(row["task_succeeded"]),
            verified=to_bool(row["verified"]),
            reason=to_reason(row["reason"]),
            evidence=load_json(row["evidence_json"], "evidence"),
        )
    except InvalidExecutionResult as exc:
        # 계약에 어긋나는 행을 정상 데이터로 복원하지 않는다.
        raise CorruptedRecord(
            ReasonCode.CONFIG_INVALID, f"저장된 실행 결과가 계약을 만족하지 않는다: {exc}"
        ) from exc
