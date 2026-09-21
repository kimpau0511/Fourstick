"""웹 API 처리 (md/개발플랜.md 7-01 ~ 7-09).

HTTP·WebSocket 처리는 `server/asgi.py`에 있고, 이 모듈은 **요청 하나를 흐름으로
바꾸는 일**만 한다. 계획 생성 → 검증 → 일치 검증 → 승인 → 허가 → 실행의 순서와
저장을 여기서 지킨다.

지키는 것:
- **서버는 '현재 계획'을 추정하지 않는다.** 모든 작업이 명시적 식별자
  (session_id·request_id·plan_id·plan_hash·approval_id·execution_id)로 조회되고,
  소유 관계를 저장소에서 확인한다. 전역 상태를 들고 있지 않으므로 새 요청이
  다른 세션의 계획을 덮어쓰지 않는다.
- **사용자가 누르지 않은 실행은 없다.** 계획 생성은 실행하지 않고, 실행은
  같은 세션·요청·계획에 속한 유효한 승인 기록이 있어야 한다.
- **STOP은 계획·LLM 경로를 거치지 않는다.** 어댑터의 STOP 계약으로 직행하고,
  정지 확인이 안 되면 성공으로 표시하지 않는다.
- **요청↔계획 리소스 일치**를 실행 허가 전에 본다. 모델이 유효한 다른 리소스로
  치환한 계획을 막는다.
- 모든 저장은 Repository를 지난다. 브라우저는 DB에 직접 접근하지 않는다.

**인증이 아니다.** 세션은 작업 묶음을 구분하는 식별자이고, 현재 구성은
localhost 전용이며 사용자 계정이 없다. 세션 id를 아는 클라이언트는 그 세션의
자료에 접근할 수 있다 — 그래서 id를 추측하기 어렵게 만들고(`secrets`) 제한을
문서에 남긴다(`md/웹UI_구조.md`). 인증은 개발플랜이 정한 단계에서 붙인다.
"""

from __future__ import annotations

import inspect
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState
from core.grasp_observation import GraspAvailability
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan
from core.geometry import GeometryDecision, GeometryRequest, GeometryVerdict
from planning.attempt_runner import persist_attempts, run_planning
from planning.slot_extractor import extract_slots
from server.runtime import Runtime
from storage.records import (
    ApprovalDecision,
    ApprovalRecord,
    PermitReasonRecord,
    PermitRecord,
    PlanningExecutionPath,
    RequestRecord,
    RuleResultRecord,
    SessionRecord,
    SessionStatus,
    ValidationDecision,
    ValidationRecord,
    ValidationRunRecord,
)
from storage.repository import (
    CorruptedRecord,
    IntegrityViolation,
    StorageError,
)
from validation.capability_precheck import capability_decision, evaluate_capability
from validation.execution_permit import PermitContext, check_execution_permit
from validation.geometry_check import check_geometry, geometry_rule_result
from validation.place_intent import evaluate_place_intent
from validation.outcome_verifier import (
    StopOutcome,
    finalize_plan_result,
    hold_requirement,
    verify_terminal_hold,
)
from validation.request_plan_consistency import (
    ConsistencyStatus,
    RULE_CODE as CONSISTENCY_RULE,
    as_rule_result,
    check_request_plan_consistency,
)
from validation.safety_validator import (
    RuleStatus,
    SafetyDecision,
    aggregate,
    evaluate,
)

#: 어댑터 호출 제한시간(초). 정책으로 옮기기 전까지 한 곳에 둔다.
ADAPTER_TIMEOUT_SEC = 10.0
#: 클라이언트 유예의 기본값. 실제 값은 `ServerConfig.client_grace_sec`이며
#: 이 상수는 설정을 읽을 수 없는 호출자를 위한 마지막 기본값이다.
DEFAULT_CLIENT_GRACE_SEC = 5.0


class ApiError(Exception):
    """요청을 처리할 수 없다. 이유 코드와 HTTP 상태를 함께 담는다."""

    def __init__(self, status: int, reason: ReasonCode | None, message: str):
        self.status = status
        self.reason = reason
        self.message = message
        super().__init__(message)

    def to_dict(self) -> dict:
        return {
            "error": self.message,
            "reason_code": None if self.reason is None else self.reason.value,
        }


def _rule_dict(result) -> dict:
    return {
        "code": result.code,
        "status": result.status.value,
        "message": result.message,
        "reason_code": None if result.reason is None else result.reason.value,
        "recoverable": None,
    }


def _result_dict(result: ExecutionResult) -> dict:
    """ExecutionResult 5축을 그대로 내려보낸다. 하나로 뭉개지 않는다."""
    return {
        "state": result.state.value,
        "request_accepted": result.request_accepted,
        "motion_completed": result.motion_completed,
        "target_reached": result.target_reached,
        "task_succeeded": result.task_succeeded,
        # 5축 중 하나다. "확인 불가"를 실패와 구별하려면 이 값이 필요하다.
        "verified": result.verified,
        "reason_code": None if result.reason is None else result.reason.value,
        "evidence": dict(result.evidence),
        "terminal": result.state in TERMINAL_HINT,
    }


#: 재시도해도 같은 결과가 나오는 실패. UI가 재시도 버튼을 숨긴다.
NON_RECOVERABLE = {
    ReasonCode.PLAN_RESOURCE_MISMATCH,
    ReasonCode.PLAN_UNKNOWN_RESOURCE,
    ReasonCode.PLAN_UNSUPPORTED_SKILL,
    ReasonCode.ROBOT_SKILL_UNSUPPORTED,
    ReasonCode.PLAN_ARG_MISSING,
    ReasonCode.PLAN_ARG_UNKNOWN,
    ReasonCode.SAFETY_SEQUENCE_INVALID,
    ReasonCode.SAFETY_HOLD_INVALID,
    ReasonCode.SAFETY_LIMIT_EXCEEDED,
    ReasonCode.SAFETY_ARG_OUT_OF_PROFILE,
    ReasonCode.CONFIG_MISSING,
    ReasonCode.CONFIG_INVALID,
    ReasonCode.PLAN_VERSION_UNSUPPORTED,
}
#: 상태가 바뀌면 다시 해볼 수 있는 실패.
RECOVERABLE = {
    ReasonCode.PLAN_LLM_TIMEOUT,
    ReasonCode.PLAN_LLM_UNAVAILABLE,
    ReasonCode.PLAN_LLM_RETRY_EXHAUSTED,
    ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
    ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID,
    ReasonCode.PLAN_CLARIFICATION_REQUIRED,
    ReasonCode.PLAN_SLOT_INCOMPLETE,
    ReasonCode.PLAN_AMBIGUOUS,
    ReasonCode.PLAN_EXPIRED,
    ReasonCode.ROBOT_NOT_CONNECTED,
    ReasonCode.ROBOT_CONNECTION_LOST,
    ReasonCode.ROBOT_STATE_STALE,
    ReasonCode.ROBOT_STATE_UNAVAILABLE,
    ReasonCode.EXEC_SEND_TIMEOUT,
    ReasonCode.EXEC_RESULT_TIMEOUT,
    ReasonCode.STT_TRANSCRIBE_TIMEOUT,
    ReasonCode.STT_CAPACITY_EXCEEDED,
    ReasonCode.STT_NO_SPEECH,
    ReasonCode.STT_LOW_CONFIDENCE,
    ReasonCode.EXEC_ENVIRONMENT_CHANGED,
}
from core.execution_state import TERMINAL_STATES as TERMINAL_HINT


def recoverable(reason: ReasonCode | None) -> bool | None:
    """재시도 가능 여부. 모르면 None이다 — 추측해서 재시도를 권하지 않는다."""
    if reason is None:
        return None
    if reason in RECOVERABLE:
        return True
    if reason in NON_RECOVERABLE:
        return False
    return None


@dataclass
class GateOutcome:
    """실행 전 관문 전체 판정 (안전 + 리소스 일치 + Capability + 기하).

    **서버가 들고 있는 상태가 아니다.** 요청마다 다시 계산한다. 계산은
    저장하지 않고, 저장이 필요한 시점(계획 생성·승인·실행)에만
    `Api._record_gate`가 append-only 행으로 남긴다.
    """

    safety_decision: SafetyDecision
    safety_rules: list[dict]
    consistency: dict
    capability_status: RuleStatus
    capability_rules: list[dict]
    geometry: GeometryVerdict
    decision: ValidationDecision
    reason: ReasonCode | None
    detail: str
    bindings: dict

    @property
    def rules(self) -> list[dict]:
        """UI·저장이 함께 쓰는 규칙 표. 네 검증의 결과를 한 목록으로 보여준다."""
        return [
            *self.safety_rules,
            *self.capability_rules,
            geometry_rule_result(self.geometry),
        ]

    @property
    def allowed(self) -> bool:
        return self.decision is ValidationDecision.ALLOW

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reason_code": None if self.reason is None else self.reason.value,
            "detail": self.detail,
            "capability": {
                "status": self.capability_status.value,
                "rules": self.capability_rules,
            },
            "geometry": self.geometry.to_dict(),
            "bindings": dict(self.bindings),
        }


def _aggregate_gate(
    safety: SafetyDecision, consistency: dict, capability: RuleStatus,
    geometry: GeometryVerdict,
) -> tuple[ValidationDecision, ReasonCode | None, str]:
    """관문 집계. **정보 부족을 통과로 승격하지 않는다.**

    막는 것(BLOCK)이 하나라도 있으면 BLOCK, 없고 정보 부족이 있으면 ASK,
    전부 통과면 ALLOW다. 기하 검사의 ASK도 여기서 ASK가 된다 — 검사하지 않은
    상태를 안전으로 보지 않는다.
    """
    if safety is SafetyDecision.BLOCK:
        return (ValidationDecision.BLOCK, ReasonCode.SAFETY_SEQUENCE_INVALID,
                "안전 검증이 차단했다")
    if not consistency.get("allowed", False):
        reason = consistency.get("reason_code")
        return (
            ValidationDecision.BLOCK,
            ReasonCode(reason) if reason else ReasonCode.PLAN_RESOURCE_MISMATCH,
            consistency.get("detail", "요청과 계획의 리소스가 일치하지 않는다"),
        )
    if capability is RuleStatus.BLOCK:
        return (ValidationDecision.BLOCK, ReasonCode.CAPABILITY_LIMIT_EXCEEDED,
                "Capability 사전 검사가 차단했다")
    if geometry.decision is GeometryDecision.BLOCK:
        codes = geometry.reason_codes()
        return (ValidationDecision.BLOCK, codes[0] if codes else None,
                "기하 검사가 차단했다")
    if safety is SafetyDecision.ASK:
        return (ValidationDecision.ASK, ReasonCode.SAFETY_INSUFFICIENT_DATA,
                "안전 검증에 정보가 부족하다")
    if capability is RuleStatus.INSUFFICIENT_DATA:
        return (ValidationDecision.ASK, ReasonCode.CAPABILITY_PROFILE_INCOMPLETE,
                "Capability 검사에 필요한 Profile 값이 없다")
    if geometry.decision is GeometryDecision.ASK:
        codes = geometry.reason_codes()
        return (ValidationDecision.ASK, codes[0] if codes else None,
                "기하 검사를 완료하지 못했다 — 검사하지 않은 상태를 안전으로 보지 않는다")
    return (ValidationDecision.ALLOW, None, "모든 관문 통과")


@dataclass
class PlanBundle:
    """계획 하나에 딸린 판정 묶음.

    **서버가 들고 있는 상태가 아니다.** 요청이 올 때마다 저장소에서 다시
    만든다(`Api.bundle_for`). 전역 '현재 계획'을 두지 않기 위해서다.
    """

    session_id: str | None
    request_id: str
    plan: TaskPlan
    planning_attempt_id: str | None
    safety_decision: SafetyDecision
    safety_rules: list[dict]
    consistency: dict
    #: 관문 전체 판정(안전·리소스 일치·Capability·기하). 실행 가능 여부의 근거다.
    gate: GateOutcome | None = None
    stt_inference_id: str | None = None
    clarification: str | None = None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "plan": {
                "plan_id": self.plan.plan_id,
                "plan_hash": self.plan.plan_hash(),
                "robot_id": self.plan.robot_id,
                "profile_id": self.plan.profile_id,
                "profile_version": self.plan.profile_version,
                "schema_version": self.plan.schema_version,
                "created_at": self.plan.created_at,
                "ttl_sec": self.plan.ttl_sec,
                "terminal_hold": self.plan.terminal_hold,
                "utterance": self.plan.utterance,
                "steps": [
                    {"index": i + 1, "skill": s.skill, "args": dict(s.args)}
                    for i, s in enumerate(self.plan.steps)
                ],
            },
            "planning_attempt_id": self.planning_attempt_id,
            "stt_inference_id": self.stt_inference_id,
            "safety": {
                "decision": self.safety_decision.value,
                "rules": self.safety_rules if self.gate is None else self.gate.rules,
            },
            "consistency": self.consistency,
            "clarification": self.clarification,
            "validation": None if self.gate is None else self.gate.to_dict(),
            # 실행 가능 = 관문 전체가 ALLOW. 기하 검사 ASK도 실행을 막는다.
            "executable": self.gate is not None and self.gate.allowed,
        }


@dataclass
class Api:
    """런타임 위에서 요청을 처리한다. HTTP를 모른다.

    상태를 들고 있지 않는다 — 계획·승인·실행은 모두 저장소에서 식별자로 찾는다.
    유일한 런타임 상태는 **정지·취소 요청 플래그**와 실행 중 execution_id이며,
    이것들은 진행 중인 동작을 끊기 위한 것이라 기록이 아니다.
    """

    runtime: Runtime
    now: Callable[[], float] = time.time
    #: 실행 이벤트 구독자. (event) -> None. 이벤트에 session_id·scope가 담긴다.
    listeners: list[Callable[[dict], None]] = field(default_factory=list)
    #: 지금 돌고 있는 실행. 전체 정지·특정 취소의 대상 판단에 쓴다.
    running_execution_id: str | None = None
    #: 취소 요청된 execution_id. 스텝 사이에서 확인한다.
    _cancel_requested: set[str] = field(default_factory=set)
    #: 전체 정지 요청 여부. 실행 루프가 스텝 사이에서 확인한다.
    _stop_requested: bool = False
    #: 지금 돌고 있는 실행 전체. {execution_id: session_id}.
    #: 전체 정지는 여기 있는 모든 실행에 적용된다.
    _active_executions: dict = field(default_factory=dict)
    #: 세션별 살아 있는 클라이언트(탭). {session_id: {client_id: 상태}}.
    #: 탭 복제로 같은 session_id가 복사돼도 클라이언트로 분리하기 위한 것이며,
    #: **기록이 아니라 연결 상태**다(저장하지 않는다).
    _clients: dict = field(default_factory=dict)
    _flag_lock: Any = field(default_factory=threading.Lock)

    # ── 공통 ────────────────────────────────────────────────────────────
    def _uid(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"

    def emit(
        self, event: dict, *, session_id: str | None = None,
        execution_id: str | None = None, scope: str = "session",
    ) -> None:
        """이벤트를 구독자에게 넘긴다.

        `scope="session"`이면 해당 세션 구독자에게만, `scope="global"`이면
        모두에게 간다. 전체 정지는 로봇 하나를 공유하는 모든 세션에 영향을
        주므로 global이다.
        """
        payload = {
            **event, "session_id": session_id, "execution_id": execution_id,
            "scope": scope,
        }
        for listener in list(self.listeners):
            try:
                listener(payload)
            except Exception:  # noqa: BLE001 — 구독자 실패가 실행을 막지 않는다
                pass

    def health(self) -> dict:
        runtime = self.runtime
        db_ok, db_detail = True, ""
        try:
            db_detail = f"schema {runtime.repository.schema_version()}"
        except Exception as exc:  # noqa: BLE001
            db_ok, db_detail = False, str(exc)[:200]
        return {
            "status": "ok" if db_ok else "degraded",
            "now": self.now(),
            "db": {"available": db_ok, "detail": db_detail},
            "robot": {
                "configured": runtime.robot_configured,
                "robot_id": runtime.robot_id,
                "kind": runtime.adapter_kind,
                "is_simulated": runtime.is_simulated,
            },
            "features": {
                "planning": runtime.planning.to_dict(),
                "stt": runtime.stt.to_dict(),
            },
            # 실행 수는 실행 환경으로 나눠 센다. 개발용 Fake 실행이 completed로
            # 끝나도 실제 로봇 실행 수에 섞이지 않는다.
            "executions": (
                runtime.repository.execution_environment_counts() if db_ok else None
            ),
            "running_execution_id": self.running_execution_id,
        }

    # ── 세션 ────────────────────────────────────────────────────────────
    def create_session(self, *, origin: str = "") -> dict:
        """세션을 만든다. **식별자는 서버가 생성한다.**

        추측하기 어려운 값을 쓴다(`secrets.token_urlsafe`). 인증은 아니다 —
        제한은 `md/웹UI_구조.md`에 적었다.
        """
        now = self.now()
        record = SessionRecord(
            session_id=f"sess_{secrets.token_urlsafe(18)}",
            created_at=now, last_seen_at=now, status=SessionStatus.ACTIVE,
            schema_version=TASK_PLAN_SCHEMA_VERSION, origin=origin[:120],
        )
        self.runtime.repository.create_session(record)
        client_id = self._register_client(record.session_id, now=now)
        return {
            "session_id": record.session_id,
            "client_id": client_id,
            "created_at": record.created_at,
            "status": record.status.value,
            "idle_timeout_sec": self.runtime.config.session_idle_timeout_sec,
            "client_grace_sec": self.client_grace_sec,
        }

    # ── 클라이언트(탭) 분리 ─────────────────────────────────────────────
    @property
    def client_grace_sec(self) -> float:
        """클라이언트 유예. **설정이 유일한 출처다**(브라우저도 이 값을 받는다)."""
        return getattr(
            self.runtime.config, "client_grace_sec", DEFAULT_CLIENT_GRACE_SEC
        )

    def _register_client(self, session_id: str, *, now: float) -> str:
        """이 세션에 새 클라이언트를 등록한다. 식별자는 서버가 만든다."""
        client_id = f"cli_{secrets.token_urlsafe(12)}"
        with self._flag_lock:
            clients = self._clients.setdefault(session_id, {})
            clients[client_id] = {
                "claimed_at": now, "last_seen": now, "connected": False,
            }
        return client_id

    def _live_clients(self, session_id: str, *, now: float,
                      exclude: str | None = None) -> list[str]:
        """살아 있는 클라이언트 목록.

        살아 있음의 근거는 **이벤트 구독 연결**이다. 연결 직전 구간을 위해
        등록 후 짧은 유예(`ServerConfig.client_grace_sec`)를 둔다.
        """
        with self._flag_lock:
            return self._live_locked(session_id, now=now, exclude=exclude)

    def _live_locked(self, session_id: str, *, now: float,
                     exclude: str | None = None) -> list[str]:
        """`_flag_lock`을 이미 잡은 상태에서 쓰는 판정. 잠금을 다시 잡지 않는다."""
        clients = self._clients.get(session_id, {})
        live: list[str] = []
        for client_id, info in clients.items():
            if client_id == exclude:
                continue
            if info["connected"] or now - info["claimed_at"] <= self.client_grace_sec:
                live.append(client_id)
        return live

    def claim_session(
        self, *, session_id: str, client_id: str | None = None
    ) -> dict:
        """탭이 세션을 이어받는다.

        **탭 복제 대응.** 복제된 탭은 sessionStorage를 복사해 같은 session_id를
        보내지만, client_id는 메모리에만 있어 복사되지 않는다. 그래서 이미 살아
        있는 다른 클라이언트가 있으면 `session.client_conflict`로 거부하고,
        브라우저는 새 세션을 발급받는다.

        일반 새로고침은 이전 클라이언트의 연결이 끊긴 뒤라 살아 있는
        클라이언트가 없으므로 **같은 세션을 그대로 이어받는다.**
        """
        record = self.require_session(session_id)
        now = self.now()
        if client_id:
            with self._flag_lock:
                info = self._clients.get(session_id, {}).get(client_id)
                if info is not None:
                    info["last_seen"] = now
                    return {
                        "session_id": session_id, "client_id": client_id,
                        "status": record.status.value, "reused": True,
                    }
            raise ApiError(
                409, ReasonCode.SESSION_CLIENT_UNKNOWN,
                f"등록되지 않은 client_id다: {client_id}",
            )
        # 확인과 등록을 같은 잠금 안에서 끝낸다. 두 탭이 동시에 이어받기를
        # 시도해도 하나만 성공해야 한다(유예 구간 경쟁 조건).
        with self._flag_lock:
            live = self._live_locked(session_id, now=now)
            if live:
                raise ApiError(
                    409, ReasonCode.SESSION_CLIENT_CONFLICT,
                    f"이 세션을 이미 다른 클라이언트 {len(live)}개가 쓰고 있다"
                    " — 복제된 탭은 새 세션을 받아야 한다",
                )
            client_id = f"cli_{secrets.token_urlsafe(12)}"
            self._clients.setdefault(session_id, {})[client_id] = {
                "claimed_at": now, "last_seen": now, "connected": False,
            }
        return {
            "session_id": session_id, "client_id": client_id,
            "status": record.status.value, "reused": False,
        }

    def attach_client(self, *, session_id: str, client_id: str) -> None:
        """이벤트 구독을 클라이언트에 연결한다. 다른 클라이언트가 살아 있으면 거부."""
        self.require_session(session_id)
        now = self.now()
        with self._flag_lock:
            others = self._live_locked(session_id, now=now, exclude=client_id)
            if others:
                raise ApiError(
                    409, ReasonCode.SESSION_CLIENT_CONFLICT,
                    "이 세션을 이미 다른 클라이언트가 쓰고 있다",
                )
            clients = self._clients.setdefault(session_id, {})
            known = clients.get(client_id) or {}
            clients[client_id] = {
                "claimed_at": known.get("claimed_at", now),
                "last_seen": now, "connected": True,
            }

    def detach_client(self, *, session_id: str, client_id: str) -> None:
        """구독이 끊겼다. 등록을 지운다.

        **실행을 취소하지 않는다.** 탭을 닫아도 진행 중인 실행은 계속되고,
        세션은 만료 정책(`session_idle_timeout_sec`)에 따라 처리된다.
        """
        with self._flag_lock:
            clients = self._clients.get(session_id)
            if clients:
                clients.pop(client_id, None)
                if not clients:
                    self._clients.pop(session_id, None)

    def clients_of(self, session_id: str) -> list[str]:
        return self._live_clients(session_id, now=self.now())

    def require_session(self, session_id: str | None) -> SessionRecord:
        """세션을 확인한다. 없거나 종료·만료면 거부한다.

        만료 판정을 먼저 돌린다 — 종료된 세션의 계획이 '현재 계획'으로
        되살아나지 않게 한다.
        """
        if not session_id:
            raise ApiError(
                400, ReasonCode.CONFIG_MISSING,
                "session_id가 없다 — 서버는 현재 세션을 추정하지 않는다",
            )
        repository = self.runtime.repository
        repository.expire_idle_sessions(
            now=self.now(),
            idle_timeout_sec=self.runtime.config.session_idle_timeout_sec,
        )
        try:
            record = repository.get_session(session_id)
        except StorageError:
            raise ApiError(
                404, ReasonCode.CONFIG_MISSING, f"세션을 찾을 수 없다: {session_id}"
            ) from None
        if not record.usable:
            raise ApiError(
                409, ReasonCode.PLAN_EXPIRED,
                f"세션이 {record.status.value} 상태다 — 새 세션을 시작해야 한다",
            )
        return repository.touch_session(session_id, at=self.now())

    def end_session(self, session_id: str) -> dict:
        record = self.runtime.repository.close_session(
            session_id, at=self.now(), status=SessionStatus.ENDED
        )
        return {"session_id": session_id, "status": record.status.value}

    # ── 계획 생성 ───────────────────────────────────────────────────────
    def create_plan(
        self, *, session_id: str, utterance: str,
        stt_inference_id: str | None = None, request_id: str | None = None,
    ) -> dict:
        """발화를 계획으로 만든다. **실행하지 않는다.**

        새 요청은 새 request_id·plan_id를 받는다. 다른 세션이나 같은 세션의
        이전 계획을 덮어쓰지 않는다 — 서버가 '현재 계획'을 들고 있지 않다.
        """
        runtime = self.runtime
        self.require_session(session_id)
        text = (utterance or "").strip()
        if not text:
            raise ApiError(400, ReasonCode.PLAN_SLOT_INCOMPLETE, "발화가 비어 있다")
        if runtime.provider is None:
            raise ApiError(
                503, ReasonCode.PLAN_LLM_UNAVAILABLE,
                f"계획 생성을 사용할 수 없다: {runtime.planning.detail}",
            )
        if runtime.robot_id is None or runtime.profile is None:
            raise ApiError(
                503, ReasonCode.ROBOT_NOT_REGISTERED,
                "등록된 로봇이 없다 — 로봇 미설정 상태다",
            )

        created_at = self.now()
        request_id = request_id or self._uid("req")
        try:
            runtime.repository.save_request(
                RequestRecord(
                    request_id=request_id, utterance=text,
                    schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=created_at,
                    selected_stt_inference_id=stt_inference_id,
                    session_id=session_id,
                )
            )
        except IntegrityViolation as exc:
            raise ApiError(409, exc.reason, str(exc)) from None
        except StorageError as exc:
            raise ApiError(500, exc.reason, str(exc)) from None

        run = run_planning(
            text, request_id=request_id, catalog=runtime.resource_catalog,
            skill_catalog=runtime.skill_catalog, profile=runtime.profile,
            provider=runtime.provider, policy=runtime.planning_policy,
            robot_id=runtime.robot_id,
            plan_id_factory=lambda: self._uid("plan"),
            attempt_id_factory=lambda n: f"pa_{request_id}_{n}",
            now_utc=self.now, clock=time.monotonic,
            ttl_sec=runtime.config.plan_ttl_sec,
            stop_keywords=runtime.stt_policy.stop_keywords,
            schema_version=TASK_PLAN_SCHEMA_VERSION,
            stt_inference_id=stt_inference_id,
            execution_path=PlanningExecutionPath.OPERATIONAL,
        )
        persist_attempts(runtime.repository, run.attempts)
        attempt_id = run.attempts[-1].planning_attempt_id if run.attempts else None

        if not run.ok or run.outcome.plan is None:
            failure = run.outcome.failure
            reason = failure.reason if failure else ReasonCode.PLAN_SCHEMA_INVALID
            payload = {
                "ok": False,
                "session_id": session_id,
                "request_id": request_id,
                "planning_attempt_id": attempt_id,
                "reason_code": reason.value,
                "detail": failure.detail if failure else "",
                "clarification": run.outcome.clarification,
                "recoverable": recoverable(reason),
                "slots": _slots_dict(run.outcome.slots),
                # 막힌 요청도 **무엇을 하려 했는지** 남긴다. 화면이 자원 대조와
                # 차단 이유를 함께 보여줄 수 있어야 한다.
                "draft_steps": [
                    {"no": index, "skill": step.skill, "args": dict(step.args)}
                    for index, step in enumerate(
                        getattr(run.outcome.draft, "steps", ()) or (), start=1)
                ],
                # 부족한 입력을 **구조로** 내려보낸다. 화면이 detail 문자열을
                # 파싱하지 않게 한다.
                "blocked": _blocked_capability(runtime, reason),
                # pick/place 계획 초안의 **사전 검증 결과**(8-10). 실행을 열지
                # 않는다 — 무엇을 검사해서 무엇이 지났는지만 보여준다.
                "plan_validation": _plan_validation(
                    runtime, reason, run.outcome.draft, run.outcome.slots, text),
            }
            self.emit(
                {"type": "plan_failed", "payload": payload}, session_id=session_id
            )
            return payload

        plan = run.outcome.plan
        runtime.repository.save_plan(request_id, plan, created_at)
        # 새 계획 수락 = 정지 래치 해제 지점(`core/stop_contract.reset_for_new_plan`).
        # 진행 중인 실행이 있으면 풀지 않는다 — 이전 동작이 살아 있는데 새 계획을
        # 시작하지 않는다.
        latch_cleared, latch_detail = (False, "진행 중인 실행이 있어 풀지 않았다")
        with self._flag_lock:
            idle = not self._active_executions
        if idle:
            latch_cleared, latch_detail = runtime.reset_stop_latch()
            if latch_cleared:
                with self._flag_lock:
                    self._stop_requested = False
        self._store_validation(plan, run.outcome.slots)
        bundle = self.bundle_for(
            session_id=session_id, request_id=request_id, plan_id=plan.plan_id
        )
        # 계획 시점의 관문 판정을 append-only로 남긴다.
        validation_run_id = None
        if bundle.gate is not None:
            validation_run_id = self._record_gate(
                bundle.gate, session_id=session_id, request_id=request_id, plan=plan
            )
        payload = {
            "ok": True, "validation_run_id": validation_run_id,
            # 정지 래치 해제 결과를 숨기지 않는다. 풀리지 않았으면 실행은 거부된다.
            "stop_latch": {"cleared": latch_cleared, "detail": latch_detail},
            **bundle.to_dict(),
        }
        self.emit({"type": "plan", "payload": payload}, session_id=session_id)
        return payload

    def _confirmed_held_object(self) -> str | None:
        """실제 파지 관측이 **측정값으로 지목한** 물체만 돌려준다.

        시뮬레이션 attachment·개구(aperture)·과거 계획으로 추정하지 않는다.
        관측이 measured가 아니거나(unavailable/simulated) 쥔 물체가 없으면 None이다.
        이 셀의 관측은 unavailable이라 실제로는 항상 None이다.
        """
        try:
            obs = self.runtime.grasp_observation()
        except Exception:  # noqa: BLE001 — 관측 실패는 "확인 안 됨"이다
            return None
        if getattr(obs, "availability", None) is GraspAvailability.MEASURED and obs.held:
            return obs.object_id
        return None

    # ── 관문 (안전 + 리소스 일치 + Capability + 기하) ──────────────────
    def gate_for(self, plan: TaskPlan, slots) -> GateOutcome:
        """네 검증을 같은 입력으로 다시 계산한다. **저장하지 않는다.**

        저장된 판정을 읽지 않고 매번 계산하는 이유: 판정의 근거(정책·카탈로그·
        Profile·환경 snapshot)가 시간이 지나며 바뀔 수 있고, 실행 판단은 지금
        기준이어야 한다. 기록은 감사용이며 `_record_gate`가 남긴다.
        """
        runtime = self.runtime
        rule_results = evaluate(plan, runtime.safety_policy, runtime.resource_catalog)
        safety_decision = aggregate(rule_results)
        consistency = check_request_plan_consistency(
            plan=plan, slots=slots, catalog=runtime.resource_catalog
        )
        safety_rules = [_rule_dict(r) for r in rule_results]
        safety_rules.append(dict(as_rule_result(consistency), recoverable=False))

        motion = runtime.resolved_motion(plan)
        capability_results = evaluate_capability(
            plan, runtime.profile, runtime.skill_catalog, runtime.resource_catalog,
            motion,
        )
        capability_status = capability_decision(capability_results)
        capability_rules = [_rule_dict(r) for r in capability_results]

        snapshot = runtime.environment_snapshot()
        checked_at = self.now()
        try:
            request = GeometryRequest(
                plan=plan, plan_hash=plan.plan_hash(),
                robot_id=plan.robot_id, profile_id=plan.profile_id,
                profile_version=plan.profile_version,
                frame_id=runtime.frame_id, checked_at=checked_at,
                snapshot=snapshot, timeout_sec=runtime.geometry_timeout_sec,
                # 해석된 수치 관절값. 없으면 기하 검사가 ASK로 남긴다.
                resolved_motion=runtime.geometry_motion(plan),
            )
        except Exception as exc:  # noqa: BLE001 — 입력을 만들 수 없으면 ASK다
            geometry = _geometry_input_error(checked_at, str(exc))
        else:
            geometry = check_geometry(runtime.validator(), request)

        decision, reason, detail = _aggregate_gate(
            safety_decision, consistency.to_dict(), capability_status, geometry
        )
        # 놓기(place) 요청인데 놓을 물체가 확인되지 않았으면 되묻는다.
        #
        # 발화가 "…에 내려놔"처럼 놓기 의도인데 모델이 물체를 빼고 단순 이동
        # `[move, home]`으로 만들면 관문은 안전한 이동으로 보고 통과시킨다(8-15
        # false-PASS). 발화의 놓기 의도를 결정론적으로 읽어, 물체·목적지가 확인되지
        # 않은 놓기 요청은 실행 가능(ALLOW)에서 ASK로 되돌린다. **계획을 고치거나
        # 기본 위치를 배정하지 않는다** — 무엇을 놓을지 물어볼 뿐이다. 이미 BLOCK/ASK인
        # 판정은 낮추지 않는다(더 강한 차단을 유지한다).
        if decision is ValidationDecision.ALLOW:
            place = evaluate_place_intent(
                utterance=plan.utterance or "", slots=slots,
                catalog=runtime.resource_catalog,
                held_object_id=self._confirmed_held_object(),
            )
            if place.underspecified:
                decision = ValidationDecision.ASK
                reason = ReasonCode.PLAN_CLARIFICATION_REQUIRED
                detail = (
                    "놓기 요청인데 무엇을 놓을지"
                    + ("" if place.destination_confirmed else "·어디에 놓을지")
                    + " 확인되지 않았다 — 놓을 물체를 말해 달라"
                )
        # 계획이 만들어질 때의 Profile과 지금 Profile이 다르면 통과시키지 않는다.
        if runtime.profile is not None and (
            plan.profile_id != runtime.profile.profile_id
            or plan.profile_version != runtime.profile.profile_version
        ):
            decision = ValidationDecision.BLOCK
            reason = ReasonCode.ROBOT_PROFILE_MISMATCH
            detail = (
                f"계획의 Profile({plan.profile_id} {plan.profile_version})과 현재"
                f" Profile({runtime.profile.profile_id}"
                f" {runtime.profile.profile_version})이 다르다"
            )
        policy_id, policy_version = runtime.policy_binding
        profile = runtime.profile
        bindings = {
            "plan_hash": plan.plan_hash(),
            # 계획에 적힌 값이 아니라 **지금 실행에 쓰일 값**을 묶는다.
            # 그래야 승인 후 Profile이 바뀐 것을 실행 직전에 잡을 수 있다.
            "robot_id": runtime.robot_id or plan.robot_id,
            "capability_profile_id": (
                plan.profile_id if profile is None else profile.profile_id
            ),
            "capability_profile_version": (
                plan.profile_version if profile is None else profile.profile_version
            ),
            "policy_id": policy_id,
            "policy_version": policy_version,
            "snapshot_id": None if snapshot is None else snapshot.snapshot_id,
            "snapshot_version": None if snapshot is None else snapshot.snapshot_version,
            "snapshot_hash": None if snapshot is None else snapshot.content_hash,
            "frame_id": runtime.frame_id,
        }
        return GateOutcome(
            safety_decision=safety_decision, safety_rules=safety_rules,
            consistency=consistency.to_dict(),
            capability_status=capability_status, capability_rules=capability_rules,
            geometry=geometry, decision=decision, reason=reason, detail=detail,
            bindings=bindings,
        )

    def _record_gate(
        self, gate: GateOutcome, *, session_id: str | None, request_id: str,
        plan: TaskPlan, execution_id: str | None = None,
    ) -> str:
        """관문 판정을 append-only 검증 이력으로 남긴다.

        검증기 종류마다 한 행, 마지막에 집계 한 행(`gate`)을 남기고 그 id를
        돌려준다. 승인은 이 id에 묶인다. 환경 데이터 본문이나 기하 모델은
        넣지 않는다 — snapshot 식별자와 hash만 남는다.
        """
        runtime = self.runtime
        now = self.now()
        bindings = gate.bindings
        common = dict(
            session_id=session_id, request_id=request_id, plan_id=plan.plan_id,
            plan_hash=plan.plan_hash(), execution_id=execution_id,
            robot_id=bindings["robot_id"],
            profile_id=bindings["capability_profile_id"],
            profile_version=bindings["capability_profile_version"],
            policy_id=bindings["policy_id"], policy_version=bindings["policy_version"],
            schema_version=TASK_PLAN_SCHEMA_VERSION,
        )
        geometry = gate.geometry
        rows: list[tuple[str, str, str, bool, ValidationDecision, ReasonCode | None,
                         str, float, float, dict]] = []

        safety_reason = next(
            (ReasonCode(r["reason_code"]) for r in gate.safety_rules
             if r.get("reason_code") and r.get("status") in ("block", "insufficient_data")),
            None,
        )
        rows.append((
            "safety", "rule-engine", runtime.safety_policy.policy_version, True,
            _decision_of_safety(gate.safety_decision), safety_reason,
            f"규칙 {len(gate.safety_rules)}건", now, now, {},
        ))
        consistency_allowed = gate.consistency.get("allowed", False)
        rows.append((
            "consistency", "request-plan", "E-REQ-001",
            gate.consistency.get("status") != ConsistencyStatus.UNVERIFIABLE.value,
            ValidationDecision.ALLOW if consistency_allowed else ValidationDecision.BLOCK,
            (ReasonCode(gate.consistency["reason_code"])
             if gate.consistency.get("reason_code") else None),
            gate.consistency.get("detail", "")[:400], now, now, {},
        ))
        rows.append((
            "capability", "capability-precheck", runtime.skill_catalog.catalog_version,
            gate.capability_status is not RuleStatus.INSUFFICIENT_DATA,
            _decision_of_status(gate.capability_status),
            next((ReasonCode(r["reason_code"]) for r in gate.capability_rules
                  if r.get("reason_code")
                  and r.get("status") in ("block", "insufficient_data")), None),
            f"규칙 {len(gate.capability_rules)}건", now, now, {},
        ))
        geometry_codes = geometry.reason_codes()
        rows.append((
            "geometry", geometry.validator_id, geometry.validator_version,
            geometry.input_complete, _decision_of_geometry(geometry.decision),
            geometry_codes[0] if geometry_codes else None,
            "; ".join(r.detail for r in geometry.reasons)[:400]
            or f"검사한 스텝 {geometry.evidence.get('checked_steps', 0)}개",
            geometry.started_at, geometry.finished_at, {},
        ))
        gate_run_id = self._uid("vr")
        for kind, vid, vver, complete, decision, reason, detail, start, end, _ in rows:
            self._append_validation_run(ValidationRunRecord(
                validation_run_id=self._uid("vr"), validator_kind=kind,
                validator_id=vid or "unknown", validator_version=vver or "unknown",
                input_complete=complete, decision=decision, reason_code=reason,
                detail=detail, started_at=start, finished_at=end,
                snapshot_id=bindings["snapshot_id"] if kind == "geometry" else None,
                snapshot_version=(
                    bindings["snapshot_version"] if kind == "geometry" else None
                ),
                snapshot_hash=bindings["snapshot_hash"] if kind == "geometry" else None,
                **common,
            ))
        self._append_validation_run(ValidationRunRecord(
            validation_run_id=gate_run_id, validator_kind="gate",
            validator_id="execution-gate", validator_version=TASK_PLAN_SCHEMA_VERSION,
            input_complete=gate.decision is not ValidationDecision.ASK,
            decision=gate.decision, reason_code=gate.reason, detail=gate.detail[:400],
            started_at=now, finished_at=self.now(),
            snapshot_id=bindings["snapshot_id"],
            snapshot_version=bindings["snapshot_version"],
            snapshot_hash=bindings["snapshot_hash"],
            **common,
        ))
        return gate_run_id

    def _append_validation_run(self, record: ValidationRunRecord) -> None:
        try:
            self.runtime.repository.append_validation_run(record)
        except StorageError as exc:
            # 검증 기록을 남기지 못하면 통과시키지 않는다 — 근거 없는 승인·실행을
            # 만들지 않기 위해서다.
            raise ApiError(500, exc.reason, f"검증 기록을 남길 수 없다: {exc}") from None

    def _store_validation(self, plan: TaskPlan, slots) -> None:
        """안전 검증 + 요청↔계획 리소스 일치 검증을 한 묶음으로 저장한다."""
        runtime = self.runtime
        rule_results = evaluate(plan, runtime.safety_policy, runtime.resource_catalog)
        decision = aggregate(rule_results)
        consistency = check_request_plan_consistency(
            plan=plan, slots=slots, catalog=runtime.resource_catalog
        )
        records = [
            RuleResultRecord(
                rule_code=r.code, status=r.status.value, reason=r.reason,
                message=r.message,
            )
            for r in rule_results
        ]
        records.append(
            RuleResultRecord(
                rule_code=CONSISTENCY_RULE,
                status=as_rule_result(consistency)["status"],
                reason=consistency.reason, message=consistency.detail,
            )
        )
        runtime.repository.save_validation(
            ValidationRecord(
                validation_id=self._uid("val"), plan_id=plan.plan_id,
                plan_hash=plan.plan_hash(),
                decision=(
                    decision.value if consistency.allowed
                    else SafetyDecision.BLOCK.value
                ),
                policy_id=runtime.safety_policy.policy_version,
                policy_version=runtime.safety_policy.policy_version,
                evaluated_at=self.now(), rule_results=tuple(records),
            )
        )

    # ── 조회 (모든 작업의 입구) ──────────────────────────────────────────
    def bundle_for(
        self, *, session_id: str, request_id: str, plan_id: str
    ) -> PlanBundle:
        """저장소에서 계획 묶음을 다시 만든다. **소유 관계를 확인한다.**

        확인하는 것:
        - 요청이 이 세션의 것인가(다른 세션의 계획에 접근할 수 없다)
        - 계획이 그 요청의 것인가
        - 저장된 plan_hash가 계획 내용에서 다시 계산한 값과 같은가
          (내용이 달라졌으면 기존 판정·승인을 쓸 수 없다)
        """
        repository = self.runtime.repository
        try:
            request = repository.get_request(request_id)
        except StorageError:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"요청을 찾을 수 없다: {request_id}",
            ) from None
        if request.session_id != session_id:
            # 다른 세션의 자료에 접근하려는 요청이다. 존재 여부를 알려주지 않는다.
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"이 세션의 요청이 아니다: {request_id}",
            )
        try:
            plan_record = repository.get_plan(plan_id)
        except CorruptedRecord as exc:
            # 저장된 행이 변조됐다. "없다"고 하지 않고 그대로 알린다 —
            # 이 상태에서는 기존 판정·승인을 쓸 수 없다.
            raise ApiError(409, exc.reason, str(exc)[:300]) from None
        except StorageError:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"계획을 찾을 수 없다: {plan_id}",
            ) from None
        if plan_record.request_id != request_id:
            raise ApiError(
                409, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"계획 {plan_id}는 요청 {request_id}의 것이 아니다",
            )
        plan = plan_record.plan
        recomputed = plan.plan_hash()
        if recomputed != plan_record.plan_hash:
            raise ApiError(
                409, ReasonCode.PLAN_HASH_MISMATCH,
                f"저장된 plan_hash({plan_record.plan_hash[:12]})와 계획 내용에서"
                f" 계산한 값({recomputed[:12]})이 다르다",
            )

        # 판정은 저장된 검증 기록이 아니라 같은 입력으로 다시 계산한다.
        # 기록은 감사용이고, 실행 판단은 현재 정책·카탈로그·Profile·환경 기준이어야
        # 한다. 관문에는 안전 검증·리소스 일치·Capability 사전 검사·기하 검사가
        # 모두 들어간다(6-04·6-05).
        slots = extract_slots(
            request.utterance, self.runtime.resource_catalog,
            stop_keywords=self.runtime.stt_policy.stop_keywords,
        )
        gate = self.gate_for(plan, slots)
        attempt = repository.planning_attempt_for_plan(plan_id)
        return PlanBundle(
            session_id=session_id, request_id=request_id, plan=plan,
            planning_attempt_id=None if attempt is None else attempt.planning_attempt_id,
            safety_decision=gate.safety_decision, safety_rules=gate.safety_rules,
            consistency=gate.consistency, gate=gate,
            stt_inference_id=request.selected_stt_inference_id,
        )

    def get_plan(self, *, session_id: str, request_id: str, plan_id: str) -> dict:
        self.require_session(session_id)
        bundle = self.bundle_for(
            session_id=session_id, request_id=request_id, plan_id=plan_id
        )
        approvals = self._approval_history(plan_id, session_id)
        return {"ok": True, **bundle.to_dict(), "approvals": approvals}

    def _approval_history(self, plan_id: str, session_id: str) -> list[dict]:
        rows = self.runtime.repository.approvals_for_plan(plan_id)
        return [
            {
                "approval_id": a.approval_id, "decision": a.decision.value,
                "decided_at": a.decided_at, "note": a.note,
                "consistency_status": a.consistency_status,
                "plan_hash": a.plan_hash,
                "robot_id": a.robot_id,
                "profile_id": a.profile_id,
                "profile_version": a.profile_version,
                "policy_version": a.policy_version,
                "snapshot_id": a.snapshot_id,
                "snapshot_version": a.snapshot_version,
                "validation_run_id": a.validation_run_id,
                "own_session": a.session_id == session_id,
            }
            for a in rows
        ]

    # ── 승인 ────────────────────────────────────────────────────────────
    def decide(
        self, *, session_id: str, request_id: str, plan_id: str,
        plan_hash: str, approve: bool, note: str = "",
        robot_id: str | None = None, profile_id: str | None = None,
        profile_version: str | None = None,
    ) -> dict:
        """승인·거부를 append-only로 남긴다.

        화면에 표시된 `plan_hash`를 함께 받아 저장된 값과 대조한다. 사용자가 본
        계획과 저장된 계획이 다르면 승인이 성립하지 않는다.

        로봇·Profile 버전도 보내면 같은 방식으로 대조하고 기록에 남긴다
        (개발플랜 6-06·7-06). 보내지 않으면 계획의 값을 그대로 기록한다 —
        추정이 아니라 "화면 값 대조 없이 기록했다"는 뜻이다.
        """
        runtime = self.runtime
        self.require_session(session_id)
        bundle = self.bundle_for(
            session_id=session_id, request_id=request_id, plan_id=plan_id
        )
        stored_hash = bundle.plan.plan_hash()
        if not plan_hash:
            raise ApiError(
                400, ReasonCode.CONFIG_MISSING,
                "plan_hash가 없다 — 화면에 표시된 계획과 대조할 수 없다",
            )
        if plan_hash != stored_hash:
            raise ApiError(
                409, ReasonCode.PLAN_HASH_MISMATCH,
                f"화면의 plan_hash({plan_hash[:12]})가 저장된 값"
                f"({stored_hash[:12]})과 다르다 — 계획을 다시 확인해야 한다",
            )
        for label, seen, stored in (
            ("robot_id", robot_id, bundle.plan.robot_id),
            ("profile_id", profile_id, bundle.plan.profile_id),
            ("profile_version", profile_version, bundle.plan.profile_version),
        ):
            if seen is not None and seen != stored:
                raise ApiError(
                    409, ReasonCode.ROBOT_PROFILE_MISMATCH,
                    f"화면의 {label}({seen!r})가 계획의 값({stored!r})과 다르다"
                    " — 다른 로봇·Profile 기준으로 승인할 수 없다",
                )
        # 승인 시점에 관문을 다시 돌리고, 그 결과에 승인을 묶는다.
        # "무엇을 보고 승인했는가"가 기록에 남아야 재사용 조건을 판정할 수 있다.
        gate = bundle.gate
        if gate is None:
            raise ApiError(
                500, ReasonCode.CONFIG_INVALID, "관문 판정을 계산하지 못했다"
            )
        validation_run_id = self._record_gate(
            gate, session_id=session_id, request_id=request_id, plan=bundle.plan
        )
        bindings = gate.bindings
        record = ApprovalRecord(
            approval_id=self._uid("apv"), plan_id=plan_id, plan_hash=stored_hash,
            request_id=request_id,
            decision=ApprovalDecision.APPROVED if approve else ApprovalDecision.REJECTED,
            decided_at=self.now(), schema_version=TASK_PLAN_SCHEMA_VERSION,
            planning_attempt_id=bundle.planning_attempt_id,
            session_id=session_id,
            consistency_status=bundle.consistency.get("status"),
            consistency_reason_code=(
                ReasonCode(bundle.consistency["reason_code"])
                if bundle.consistency.get("reason_code") else None
            ),
            robot_id=bindings["robot_id"],
            profile_id=bindings["capability_profile_id"],
            profile_version=bindings["capability_profile_version"],
            policy_id=bindings["policy_id"],
            policy_version=bindings["policy_version"],
            snapshot_id=bindings["snapshot_id"],
            snapshot_version=bindings["snapshot_version"],
            snapshot_hash=bindings["snapshot_hash"],
            validation_run_id=validation_run_id,
            note=note[:500],
        )
        runtime.repository.append_approval(record)
        payload = {
            "ok": True, "session_id": session_id, "request_id": request_id,
            "plan_id": plan_id, "plan_hash": stored_hash,
            "decision": record.decision.value,
            "approval_id": record.approval_id,
            "decided_at": record.decided_at,
            "robot_id": record.robot_id,
            "profile_id": record.profile_id,
            "profile_version": record.profile_version,
            "policy_id": record.policy_id,
            "policy_version": record.policy_version,
            "snapshot_id": record.snapshot_id,
            "snapshot_version": record.snapshot_version,
            "validation_run_id": record.validation_run_id,
            "validation_decision": gate.decision.value,
            # 화면 값을 실제로 대조했는지. 대조 없이 기록한 승인과 구분한다.
            "verified_against_view": {
                "plan_hash": True,
                "robot_id": robot_id is not None,
                "profile_id": profile_id is not None,
                "profile_version": profile_version is not None,
            },
            "executable": bundle.to_dict()["executable"],
            "history": self._approval_history(plan_id, session_id),
        }
        self.emit({"type": "approval", "payload": payload}, session_id=session_id)
        return payload

    def _usable_approval(
        self, *, session_id: str, request_id: str, plan_id: str,
        plan_hash: str, approval_id: str | None,
    ) -> ApprovalRecord:
        """실행에 쓸 수 있는 승인을 찾는다.

        조건 네 가지를 모두 본다.
        - 같은 세션의 승인인가 (다른 탭·세션의 승인으로 실행할 수 없다)
        - 같은 요청·계획의 승인인가
        - 승인 당시의 plan_hash가 지금 계획의 hash와 같은가
          (승인 이후 계획 내용이 달라지면 기존 승인을 쓸 수 없다)
        - 마지막 결정이 승인인가 (거부 후 실행할 수 없다)
        """
        repository = self.runtime.repository
        history = repository.approvals_for_plan(plan_id)
        mine = [a for a in history if a.session_id == session_id]
        if not mine:
            others = len(history) - len(mine)
            raise ApiError(
                409, ReasonCode.SAFETY_APPROVAL_REQUIRED,
                "이 세션의 승인 기록이 없다"
                + (f" (다른 세션의 결정 {others}건은 쓸 수 없다)" if others else ""),
            )
        if approval_id is not None:
            selected = [a for a in mine if a.approval_id == approval_id]
            if not selected:
                raise ApiError(
                    404, ReasonCode.SAFETY_APPROVAL_REQUIRED,
                    f"이 세션의 승인이 아니다: {approval_id}",
                )
            approval = selected[-1]
        else:
            approval = mine[-1]
        if approval.decision is not ApprovalDecision.APPROVED:
            raise ApiError(
                409, ReasonCode.SAFETY_APPROVAL_REQUIRED,
                f"마지막 결정이 {approval.decision.value}다 — 승인 없이 실행하지 않는다",
            )
        if approval.request_id != request_id:
            raise ApiError(
                409, ReasonCode.SAFETY_APPROVAL_REQUIRED,
                "승인이 다른 요청의 것이다",
            )
        if approval.plan_hash != plan_hash:
            raise ApiError(
                409, ReasonCode.PLAN_HASH_MISMATCH,
                f"승인 당시 plan_hash({approval.plan_hash[:12]})와 현재"
                f" 계획의 hash({plan_hash[:12]})가 다르다 — 다시 승인해야 한다",
            )
        return approval

    # ── 실행 ────────────────────────────────────────────────────────────
    def execute(
        self, *, session_id: str, request_id: str, plan_id: str,
        approval_id: str | None = None,
    ) -> dict:
        """승인된 계획을 실행한다. 허가 전 검사를 모두 지난다.

        로봇은 하나이므로 실행을 직렬화한다. 다른 실행이 진행 중이면 거부하고
        진행 중인 execution_id를 알려준다.
        """
        runtime = self.runtime
        self.require_session(session_id)
        bundle = self.bundle_for(
            session_id=session_id, request_id=request_id, plan_id=plan_id
        )
        plan = bundle.plan
        plan_hash = plan.plan_hash()
        approval = self._usable_approval(
            session_id=session_id, request_id=request_id, plan_id=plan_id,
            plan_hash=plan_hash, approval_id=approval_id,
        )
        gate = bundle.gate
        if gate is None:
            raise ApiError(500, ReasonCode.CONFIG_INVALID, "관문 판정을 계산하지 못했다")

        # 1) 승인이 검증 당시 조건에 묶여 있고, 그 조건이 지금도 같은가.
        self._require_matching_bindings(approval, gate)

        # 2) 관문이 지금도 ALLOW인가. **ASK는 실행 허가가 아니다.**
        if not gate.allowed:
            self._record_gate(
                gate, session_id=session_id, request_id=request_id, plan=plan
            )
            status = 409
            detail = gate.detail
            if gate.decision is ValidationDecision.ASK:
                detail = (
                    f"{gate.detail} — 검증이 확인되지 않은 상태로 실행하지 않는다"
                )
            raise ApiError(
                status, gate.reason or ReasonCode.EXEC_PERMIT_DENIED, detail
            )
        if runtime.robot_id is None:
            raise ApiError(503, ReasonCode.ROBOT_NOT_REGISTERED, "등록된 로봇이 없다")

        # 전체 정지 래치. 새 계획을 수락할 때까지 실행을 받지 않는다.
        with self._flag_lock:
            latched = self._stop_requested
        if latched:
            raise ApiError(
                409, ReasonCode.EXEC_STOPPED,
                "전체 정지가 걸려 있다 — 새 계획을 요청하면 정지 래치가 풀린다",
            )

        if not runtime.execution_lock.acquire(blocking=False):
            raise ApiError(
                409, ReasonCode.EXEC_GOAL_REJECTED,
                f"다른 실행이 진행 중이다 (execution_id={self.running_execution_id})"
                " — 로봇은 하나이므로 동시에 실행하지 않는다",
            )
        try:
            return self._run_execution(
                session_id=session_id, bundle=bundle, approval=approval
            )
        finally:
            finished = self.running_execution_id
            with self._flag_lock:
                if finished is not None:
                    self._active_executions.pop(finished, None)
            self.running_execution_id = None
            runtime.execution_lock.release()

    def _require_matching_bindings(
        self, approval: ApprovalRecord, gate: GateOutcome
    ) -> None:
        """승인이 묶인 조건이 지금과 같은지 확인한다.

        plan_hash·robot_id·Capability Profile·Policy·환경 snapshot·검증 결과 중
        하나라도 다르면 기존 승인을 재사용하지 않는다. 각각 다른 ReasonCode로
        돌려줘 사용자가 무엇이 바뀌었는지 알 수 있게 한다.
        """
        if not approval.validation_run_id:
            raise ApiError(
                409, ReasonCode.SAFETY_APPROVAL_REQUIRED,
                "이 승인에는 검증 근거(validation_run_id)가 없다 — 다시 검증하고"
                " 승인해야 한다",
            )
        bindings = gate.bindings
        for label, stored, current, reason in (
            ("robot_id", approval.robot_id, bindings["robot_id"],
             ReasonCode.ROBOT_PROFILE_MISMATCH),
            ("capability_profile_id", approval.profile_id,
             bindings["capability_profile_id"], ReasonCode.ROBOT_PROFILE_MISMATCH),
            ("capability_profile_version", approval.profile_version,
             bindings["capability_profile_version"], ReasonCode.ROBOT_PROFILE_MISMATCH),
            ("policy_id", approval.policy_id, bindings["policy_id"],
             ReasonCode.CONFIG_VERSION_MISMATCH),
            ("policy_version", approval.policy_version, bindings["policy_version"],
             ReasonCode.CONFIG_VERSION_MISMATCH),
            ("snapshot_id", approval.snapshot_id, bindings["snapshot_id"],
             ReasonCode.EXEC_ENVIRONMENT_CHANGED),
            ("snapshot_version", approval.snapshot_version,
             bindings["snapshot_version"], ReasonCode.EXEC_ENVIRONMENT_CHANGED),
            ("snapshot_hash", approval.snapshot_hash, bindings["snapshot_hash"],
             ReasonCode.EXEC_ENVIRONMENT_CHANGED),
        ):
            if stored != current:
                raise ApiError(
                    409, reason,
                    f"승인 당시 {label}({stored!r})와 지금 값({current!r})이 다르다"
                    " — 다시 검증하고 승인해야 한다",
                )

    def _run_execution(
        self, *, session_id: str, bundle: PlanBundle, approval: ApprovalRecord
    ) -> dict:
        runtime = self.runtime
        plan = bundle.plan
        adapter = runtime.adapter()
        if adapter is None:
            raise ApiError(503, ReasonCode.ROBOT_NOT_REGISTERED, "어댑터가 없다")
        connect = adapter.connect(ADAPTER_TIMEOUT_SEC)
        if not connect.request_accepted:
            raise ApiError(
                503, connect.reason or ReasonCode.ROBOT_NOT_CONNECTED,
                "로봇에 연결할 수 없다",
            )

        rule_results = evaluate(plan, runtime.safety_policy, runtime.resource_catalog)
        gate_bindings = {} if bundle.gate is None else dict(bundle.gate.bindings)
        state = adapter.state()
        now = self.now()
        environment_marker = f"{runtime.robot_id}:{record_session_id(adapter)}"
        permit = check_execution_permit(
            plan, rule_results,
            PermitContext(
                now=now, robot_ready=state.valid,
                robot_state_observed_at=state.observed_at,
                robot_state_valid=state.valid,
                environment_observed_at=state.observed_at,
                recorded_environment_version=1, current_environment_version=1,
                recorded_environment_session=environment_marker,
                current_environment_session=environment_marker,
                recorded_plan_hash=plan.plan_hash(),
                profile_id=plan.profile_id, profile_version=plan.profile_version,
                supported_skills=adapter.profile.supported_skills,
                # 승인 당시 Policy·환경 snapshot을 실행 직전에 다시 대조한다(6-07).
                recorded_policy_id=approval.policy_id,
                current_policy_id=gate_bindings.get("policy_id"),
                recorded_policy_version=approval.policy_version,
                current_policy_version=gate_bindings.get("policy_version"),
                recorded_snapshot_id=approval.snapshot_id,
                current_snapshot_id=gate_bindings.get("snapshot_id"),
                recorded_snapshot_hash=approval.snapshot_hash,
                current_snapshot_hash=gate_bindings.get("snapshot_hash"),
            ),
            runtime.freshness_policy,
        )
        permit_record_reasons = [
            {
                "reason_code": r.reason.value, "detail": r.detail,
                "recoverable": recoverable(r.reason),
            }
            for r in permit.reasons
        ]
        permit_id = self._uid("pmt")
        try:
            runtime.repository.save_permit(PermitRecord(
                permit_id=permit_id, plan_id=plan.plan_id,
                plan_hash=plan.plan_hash(), granted=permit.granted,
                policy_id=runtime.freshness_policy.policy_version,
                policy_version=runtime.freshness_policy.policy_version,
                decided_at=now,
                reasons=tuple(
                    PermitReasonRecord(reason=r.reason, detail=r.detail)
                    for r in permit.reasons
                ),
            ))
        except StorageError as exc:
            # 허가 기록을 남기지 못하면 실행하지 않는다.
            raise ApiError(500, exc.reason, f"허가 기록을 남길 수 없다: {exc}") from None

        if not permit.granted:
            payload = {
                "ok": False, "session_id": session_id,
                "request_id": bundle.request_id, "plan_id": plan.plan_id,
                "granted": False, "permit_id": permit_id,
                "reasons": permit_record_reasons,
            }
            self.emit(
                {"type": "permit_denied", "payload": payload}, session_id=session_id
            )
            return payload

        execution_id = self._uid("exec")
        # 실행 환경은 연결 확인 결과와 함께 정한다. Fake는 항상 simulated이고,
        # 실제 어댑터는 연결이 확인된 경우에만 real로 기록된다.
        adapter_kind, is_simulated, confirmed_at = runtime.execution_environment(
            connected=connect.request_accepted, now=now
        )
        record = runtime.repository.begin_execution(
            execution_id=execution_id, request_id=bundle.request_id,
            plan_id=plan.plan_id, adapter_id=runtime.robot_id,
            policy_id=runtime.freshness_policy.policy_version,
            policy_version=runtime.freshness_policy.policy_version,
            started_at=now, session_id=session_id,
            approval_id=approval.approval_id,
            adapter_kind=adapter_kind,
            is_simulated=is_simulated,
            environment_confirmed_at=confirmed_at,
        )
        self.running_execution_id = execution_id
        with self._flag_lock:
            # 전체 정지가 적용될 대상 목록. 소유 세션을 함께 둬, 정지 결과를
            # 해당 세션에만 상세히 전달한다.
            self._active_executions[execution_id] = session_id
        # 실행 직전 재검증을 이 실행 식별자로 남긴다(승인에 쓰인 것과 별개 행).
        if bundle.gate is not None:
            self._record_gate(
                bundle.gate, session_id=session_id, request_id=bundle.request_id,
                plan=plan, execution_id=execution_id,
            )
        with self._flag_lock:
            # **전체 정지 래치를 여기서 풀지 않는다.** 계약(`core/stop_contract`의
            # `reset_for_new_plan`)은 새 계획을 수락할 때 풀도록 정한다 —
            # 실행마다 풀면 STOP 이후 첫 실행이 이유 없이 진행된다.
            self._cancel_requested.discard(execution_id)

        started_payload = {
            "execution_id": execution_id, "plan_id": plan.plan_id,
            "request_id": bundle.request_id, "attempt_no": record.attempt_no,
            "approval_id": approval.approval_id,
            "adapter_kind": record.adapter_kind,
            "is_simulated": record.is_simulated,
        }
        self.emit(
            {"type": "execution_started", "payload": started_payload},
            session_id=session_id, execution_id=execution_id,
        )

        transitions: list[dict] = []

        def go(state_value: ExecutionState, reason: ReasonCode | None = None) -> None:
            runtime.repository.append_state_transition(
                execution_id, to_state=state_value, occurred_at=self.now(),
                reason=reason,
            )
            row = {
                "to_state": state_value.value,
                "reason_code": None if reason is None else reason.value,
                "at": self.now(),
            }
            transitions.append(row)
            self.emit(
                {"type": "state", "payload": {"execution_id": execution_id, **row}},
                session_id=session_id, execution_id=execution_id,
            )

        go(ExecutionState.ACCEPTED)
        go(ExecutionState.EXECUTING)

        step_results: list[dict] = []
        motion_completed = True
        target_reached = True
        interrupted: ReasonCode | None = None
        interrupted_step = 0
        step_ran = False

        def interruption() -> ReasonCode | None:
            """정지·취소 요청을 확인한다. 전체 정지가 특정 취소보다 앞선다."""
            with self._flag_lock:
                if self._stop_requested:
                    return ReasonCode.EXEC_STOPPED
                if execution_id in self._cancel_requested:
                    return ReasonCode.EXEC_CANCELED
            return None

        for index, step in enumerate(plan.steps, start=1):
            interrupted = interruption()
            if interrupted is not None:
                interrupted_step, step_ran = index, False
                motion_completed = False
                target_reached = False
                break
            result = _run_step(adapter, step)
            runtime.repository.append_result(execution_id, result, self.now())
            row = {
                "index": index, "skill": step.skill, "args": dict(step.args),
                **_result_dict(result),
                "recoverable": recoverable(result.reason),
            }
            step_results.append(row)
            self.emit(
                {"type": "step", "payload": {"execution_id": execution_id, **row}},
                session_id=session_id, execution_id=execution_id,
            )
            if not result.motion_completed:
                motion_completed = False
            if not result.target_reached:
                target_reached = False
            # 스텝을 보내는 동안 정지·취소가 들어오면 그 스텝은 실패한다.
            # 그 실패를 일반 실패로 기록하면 원인이 사라진다 — 정지로 기록한다.
            interrupted = interruption()
            if interrupted is not None:
                interrupted_step, step_ran = index, True
                motion_completed = False
                target_reached = False
                break
            if result.state in (ExecutionState.FAILED, ExecutionState.UNKNOWN,
                                ExecutionState.STOPPED):
                break

        if interrupted is not None:
            go(ExecutionState.STOPPING, interrupted)
            final = ExecutionResult(
                state=ExecutionState.STOPPED, request_accepted=True,
                motion_completed=False, target_reached=False, task_succeeded=False,
                reason=interrupted,
                evidence={
                    "interrupted_at_step": interrupted_step,
                    "step_was_sent": step_ran,
                    "steps_recorded": len(step_results),
                    "total_steps": len(plan.steps),
                },
            )
            outcome = None
        else:
            # **파지 관측을 요구하는 계획인지 먼저 판정한다.**
            # 요구되지 않으면 `adapter.state()`를 호출하지 않는다 — 없는 관측을
            # 확인 불가로 적지도, 성공으로 바꾸지도 않기 위해서다(8-09).
            requirement = hold_requirement(plan, runtime.skill_catalog)
            snapshot = adapter.state() if requirement.required else None
            hold_observation_performed = snapshot is not None
            outcome = verify_terminal_hold(plan, snapshot)
            # 정지 관측: 스텝 결과에서 모은다(요청 접수가 아니라 관측 기준).
            stop_rows = [row for row in step_results
                         if (row.get("evidence") or {}).get("stop_request")
                         is not None]
            stop = None
            if stop_rows:
                confirmed = all(
                    (row.get("evidence") or {}).get("stop_confirmed") is True
                    for row in stop_rows)
                stop = StopOutcome(
                    requested=True, confirmed=confirmed,
                    detail=("정지를 관측으로 확인했다" if confirmed
                            else "정지를 관측으로 확인하지 못했다"),
                    evidence={"stop_steps": [row["index"] for row in stop_rows]},
                )
            # planning scene 재검증: 스텝 근거에 기록된 검사 결과를 모은다.
            scene_rows = [(row.get("evidence") or {}).get("planning_scene")
                          for row in step_results]
            scene_checked = [s for s in scene_rows if isinstance(s, dict)]
            scene_revalidated = (
                all(s.get("checked") and s.get("valid") for s in scene_checked)
                if scene_checked else None)
            final = finalize_plan_result(
                outcome, motion_completed=motion_completed,
                target_reached=target_reached,
                requirement=requirement, stop=stop,
                scene_revalidated=scene_revalidated,
                evidence={
                    # 관측을 **했는지** 자체를 기록한다. 하지 않은 것과
                    # 해 봤지만 확인 못 한 것은 다른 사실이다.
                    "hold_observation_performed": hold_observation_performed,
                },
            )
        runtime.repository.append_result(execution_id, final, self.now())
        go(final.state, final.reason)

        payload = {
            # 성공만 ok다. 중단·실패를 ok로 표시하지 않는다.
            "ok": interrupted is None and final.task_succeeded,
            "interrupted": None if interrupted is None else interrupted.value,
            "session_id": session_id,
            "request_id": bundle.request_id, "plan_id": plan.plan_id,
            "granted": True, "permit_id": permit_id,
            "execution_id": execution_id,
            "attempt_no": record.attempt_no,
            "approval_id": approval.approval_id,
            "adapter_kind": record.adapter_kind,
            "is_simulated": record.is_simulated,
            "transitions": transitions, "steps": step_results,
            "final": {
                **_result_dict(final),
                "recoverable": recoverable(final.reason),
                "hold": None if outcome is None else {
                    "verified": outcome.verified, "matched": outcome.matched,
                    "expected": outcome.expected, "observed": outcome.observed,
                    # 파지 관측을 **요구했는지**와 그 근거. 요구하지 않았으면
                    # 관측하지 않았다는 뜻이다(확인 불가와 구별한다).
                    "required": (None if outcome is None
                                 else final.evidence.get("hold_requirement", {})
                                 .get("hold_required")),
                    "basis": (final.evidence.get("hold_requirement", {})
                              .get("basis")),
                },
            },
        }
        self.emit(
            {"type": "execution_final", "payload": payload},
            session_id=session_id, execution_id=execution_id,
        )
        return payload

    def execution_status(self, *, session_id: str, execution_id: str) -> dict:
        """실행 상태를 **execution_id 기준으로** 조회한다."""
        self.require_session(session_id)
        repository = self.runtime.repository
        try:
            record = repository.get_execution(execution_id)
        except StorageError:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"실행을 찾을 수 없다: {execution_id}",
            ) from None
        if record.session_id != session_id:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"이 세션의 실행이 아니다: {execution_id}",
            )
        return {"ok": True, **_execution_dict(record, repository)}

    # ── 정지와 취소 (계약상 구분) ───────────────────────────────────────
    def stop(self, *, session_id: str | None = None) -> dict:
        """**전체 정지.** 로봇 전체를 멈춘다.

        - 세션 격리로 막지 않는다 — 세션이 없거나 만료됐어도 동작한다.
        - **지금 돌고 있는 모든 실행에 적용된다**(로봇이 하나여도 계약은
          '전체'다). 각 실행은 스텝 사이에서 정지 요청을 확인하고 멈춘다.
        - 결과는 영향받은 **각 세션에 따로** 전달한다. 전체 범위 이벤트에는
          다른 세션의 계획·승인·실행 상세를 담지 않는다(건수만 담는다).
        - 정지 확인이 안 되면 성공으로 표시하지 않는다.
        """
        runtime = self.runtime
        with self._flag_lock:
            self._stop_requested = True
            affected = dict(self._active_executions)   # {execution_id: session_id}

        by_session: dict[str, list[str]] = {}
        for execution_id, owner in affected.items():
            if owner:
                by_session.setdefault(owner, []).append(execution_id)
        mine = sorted(by_session.get(session_id or "", []))

        def finish(payload: dict) -> dict:
            """전체 범위 이벤트 + 세션별 이벤트. 상세는 소유 세션에만 간다."""
            self.emit({"type": "stop", "payload": payload}, scope="global")
            for owner, ids in by_session.items():
                self.emit(
                    {
                        "type": "stop",
                        "payload": {**payload, "your_execution_ids": sorted(ids)},
                    },
                    session_id=owner,
                )
            return {**payload, "your_execution_ids": mine}

        base = {
            "scope": "global",
            # 다른 세션의 execution_id를 노출하지 않는다. 건수만 알린다.
            "affected_execution_count": len(affected),
            "at": self.now(),
        }
        if runtime.robot_id is None:
            return finish({
                **base, "ok": False, "requested": False, "confirmed": False,
                "state": "unconfirmed",
                "reason_code": ReasonCode.ROBOT_NOT_REGISTERED.value,
                "detail": "등록된 로봇이 없어 정지 명령을 보낼 대상이 없다",
            })
        try:
            adapter = runtime.adapter()
            # 진행 중인 실행이 있으면 그 실행이 이미 연결을 확인한 어댑터다
            # (`_run_execution`은 connect가 수락된 뒤에만 실행을 등록한다).
            # 연결 재확인(world·컨트롤러·모델 조회)을 취소보다 앞에 두지 않는다 —
            # 그 사이 로봇이 계속 움직인다. 실행이 없을 때만 먼저 연결을 확인한다.
            if not affected:
                adapter.connect(ADAPTER_TIMEOUT_SEC)
            # 정지 래치에 멈춘 실행을 남긴다. 대상이 정확히 하나일 때만 그 id를
            # 넘기고, 없거나 여럿이면 None이다 — 임의로 하나를 고르지 않는다.
            # execution_id를 받지 않는 어댑터에는 이전과 같이 호출한다.
            if _accepts_execution_id(adapter.stop):
                stop_result = adapter.stop(
                    ADAPTER_TIMEOUT_SEC,
                    execution_id=next(iter(affected)) if len(affected) == 1 else None,
                )
            else:
                stop_result = adapter.stop(ADAPTER_TIMEOUT_SEC)
            confirm = adapter.confirm_stopped(ADAPTER_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 — 연결 끊김도 미확인이다
            return finish({
                **base, "ok": False, "requested": False, "confirmed": False,
                "state": "unconfirmed",
                "reason_code": ReasonCode.EXEC_STOP_UNCONFIRMED.value,
                "detail": f"정지 요청을 보내지 못했다: {exc}"[:200],
            })

        # 상태만 보지 않는다. **계측으로 확인됐는가**(verified)도 본다.
        confirmed = (confirm.state is ExecutionState.STOPPED
                     and confirm.verified)
        return finish({
            **base,
            "ok": confirmed,
            "requested": stop_result.request_accepted,
            "confirmed": confirmed,
            "state": "confirmed" if confirmed else "unconfirmed",
            "reason_code": (
                None if confirmed
                else (confirm.reason or ReasonCode.EXEC_STOP_UNCONFIRMED).value
            ),
            "detail": (
                "정지 확인됨(관측 기준)" if confirmed
                else "정지 요청은 보냈지만 실제 정지를 확인하지 못했다"
            ),
            "stop_result": _result_dict(stop_result),
            "confirm_result": _result_dict(confirm),
            "adapter_kind": runtime.adapter_kind,
            "is_simulated": runtime.is_simulated,
        })

    def cancel_execution(self, *, session_id: str, execution_id: str) -> dict:
        """**특정 실행 취소.** 전체 정지와 다른 계약이다.

        이 세션의 실행만 취소할 수 있다. 로봇 전체를 멈추지 않는다 — 진행 중인
        그 실행의 남은 스텝을 보내지 않고, 어댑터에 취소를 요청한다.
        """
        self.require_session(session_id)
        repository = self.runtime.repository
        try:
            record = repository.get_execution(execution_id)
        except StorageError:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"실행을 찾을 수 없다: {execution_id}",
            ) from None
        if record.session_id != session_id:
            raise ApiError(
                404, ReasonCode.PLAN_UNKNOWN_RESOURCE,
                f"이 세션의 실행이 아니다: {execution_id}",
            )
        with self._flag_lock:
            self._cancel_requested.add(execution_id)
        cancel_result = None
        if self.running_execution_id == execution_id:
            adapter = self.runtime.adapter()
            if adapter is not None:
                try:
                    cancel_result = adapter.cancel(ADAPTER_TIMEOUT_SEC)
                except Exception as exc:  # noqa: BLE001
                    cancel_result = None
        payload = {
            "ok": True, "scope": "execution", "session_id": session_id,
            "execution_id": execution_id,
            "was_running": self.running_execution_id == execution_id,
            "cancel_result": None if cancel_result is None else _result_dict(cancel_result),
            "detail": (
                "취소를 요청했다. 남은 스텝은 보내지 않는다"
                if self.running_execution_id == execution_id
                else "이미 끝난 실행이다 — 취소 요청만 기록한다"
            ),
            "at": self.now(),
        }
        self.emit(
            {"type": "cancel", "payload": payload},
            session_id=session_id, execution_id=execution_id,
        )
        return payload

    # ── 복원 ────────────────────────────────────────────────────────────
    def state(self, *, session_id: str) -> dict:
        """새로고침 복원. **이 세션의 진행 상태만** 저장소에서 읽는다.

        종료·만료된 세션은 여기까지 오지 않는다(`require_session`이 막는다).
        """
        self.require_session(session_id)
        repository = self.runtime.repository
        requests = repository.requests_for_session(session_id)
        if not requests:
            return {"session_id": session_id, "plan": None, "executions": []}

        latest = requests[-1]
        plans = repository.plans_for_request(latest.request_id)
        if not plans:
            return {
                "session_id": session_id, "plan": None, "executions": [],
                "request_id": latest.request_id, "utterance": latest.utterance,
            }
        plan_record = plans[-1]
        bundle = self.bundle_for(
            session_id=session_id, request_id=latest.request_id,
            plan_id=plan_record.plan_id,
        )
        executions = [
            _execution_dict(record, repository)
            for record in repository.executions_for_session(session_id)
        ]
        return {
            **bundle.to_dict(),
            "approvals": self._approval_history(plan_record.plan_id, session_id),
            "executions": executions,
            "running_execution_id": self.running_execution_id,
        }


def _execution_dict(record, repository) -> dict:
    """실행 하나를 이력과 함께. 실행 환경 정보를 항상 포함한다."""
    trace = repository.trace(record.execution_id)
    return {
        "execution_id": record.execution_id,
        "session_id": record.session_id,
        "request_id": record.request_id,
        "plan_id": record.plan_id,
        "plan_hash": record.plan_hash,
        "attempt_no": record.attempt_no,
        "approval_id": record.approval_id,
        "adapter_id": record.adapter_id,
        "adapter_kind": record.adapter_kind,
        "is_simulated": record.is_simulated,
        "started_at": record.started_at,
        "transitions": [
            {
                "seq": t.seq,
                "from_state": None if t.from_state is None else t.from_state.value,
                "to_state": t.to_state.value,
                "reason_code": None if t.reason is None else t.reason.value,
                "at": t.occurred_at,
            }
            for t in trace.transitions
        ],
        "results": [
            {
                **_result_dict(r.result), "recorded_at": r.recorded_at,
                "recoverable": recoverable(r.result.reason),
            }
            for r in trace.results
        ],
    }


def _accepts_execution_id(method) -> bool:
    """`adapter.stop`이 `execution_id` 인자를 받는가. 받지 않으면 넘기지 않는다."""
    try:
        return "execution_id" in inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False


def record_session_id(adapter) -> str:
    """이 연결의 환경 세션 표식. 어댑터가 제공하지 않으면 객체 식별자를 쓴다."""
    return getattr(adapter, "session_id", None) or f"conn{id(adapter):x}"


def _slots_dict(slots) -> dict:
    if slots is None:
        return {"matches": []}
    return {
        "matches": [
            {
                "surface": m.surface, "resource_id": m.resource_id,
                "kind": m.kind.value,
            }
            for m in slots.matches
        ],
        "stop_keyword_hit": slots.stop_keyword_hit,
    }


def _decision_of_safety(decision: SafetyDecision) -> ValidationDecision:
    return {
        SafetyDecision.ALLOW: ValidationDecision.ALLOW,
        SafetyDecision.ASK: ValidationDecision.ASK,
        SafetyDecision.BLOCK: ValidationDecision.BLOCK,
    }[decision]


def _decision_of_status(status: RuleStatus) -> ValidationDecision:
    if status is RuleStatus.BLOCK:
        return ValidationDecision.BLOCK
    if status is RuleStatus.INSUFFICIENT_DATA:
        return ValidationDecision.ASK
    return ValidationDecision.ALLOW


def _decision_of_geometry(decision: GeometryDecision) -> ValidationDecision:
    return {
        GeometryDecision.ALLOW: ValidationDecision.ALLOW,
        GeometryDecision.BLOCK: ValidationDecision.BLOCK,
        GeometryDecision.ASK: ValidationDecision.ASK,
    }[decision]


def _geometry_input_error(checked_at: float, detail: str) -> GeometryVerdict:
    """기하 검사 입력을 만들 수 없는 경우. 통과로 바꾸지 않고 ASK로 남긴다."""
    from core.geometry import ask as _ask_verdict

    return _ask_verdict(
        ReasonCode.GEOMETRY_VALIDATOR_ERROR,
        f"기하 검사 입력을 만들 수 없다: {detail}"[:300],
        validator_id="execution-gate", validator_version=TASK_PLAN_SCHEMA_VERSION,
        started_at=checked_at, finished_at=checked_at,
    )


def _blocked_capability(runtime, reason: ReasonCode) -> dict | None:
    """관문에 막힌 스킬의 구조화된 근거. Profile 선언에서만 온다.

    `capability.profile_incomplete`로 막힌 요청에 대해 "무엇이 부족한가"를
    화면이 읽을 수 있는 형태로 돌려준다. 화면에 목록을 두지 않기 위해서다.
    """
    if reason is not ReasonCode.CAPABILITY_PROFILE_INCOMPLETE:
        return None
    profile = runtime.profile
    gated = dict(getattr(profile, "extras", {}).get("gated_skills") or {}) \
        if profile is not None else {}
    if not gated:
        return None
    return {
        "skills": list(gated.get("skills") or ()),
        "reason_code": gated.get("reason_code") or reason.value,
        "message": gated.get("message") or "",
        "unmet": list(gated.get("unmet") or ()),
        # 무엇이 이미 확인됐는지도 함께 보여준다. 남은 것만 보이면 진행 상태를
        # 알 수 없고, 반대로 통과 항목만 보이면 열린 것으로 오해한다.
        "met": list(gated.get("met") or ()),
        "note": gated.get("note") or "",
        "gate": "validation/pick_place_gate.py",
    }


def _plan_validation(runtime, reason: ReasonCode, draft, slots, text: str) -> dict | None:
    """막힌 pick/place 초안을 단계로 펼쳐 사전 검증한다(8-10).

    **계획 검증 통과는 실행 가능이 아니다.** 결과의 `execution_allowed`는
    항상 False이고, 실행 차단 이유는 `blocked`가 따로 담는다. 검증을 돌릴 수
    없으면(선언·scene 없음) 그 사실을 이유 코드로 남긴다.
    """
    if reason is not ReasonCode.CAPABILITY_PROFILE_INCOMPLETE:
        return None
    steps = getattr(draft, "steps", ()) or ()
    if not steps:
        return None
    try:
        result = runtime.pick_place_validation(steps, slots=slots, utterance=text)
    except Exception as exc:  # noqa: BLE001 — 검증 실패를 통과로 쓰지 않는다
        return {
            "available": False,
            "detail": f"계획 검증을 돌리지 못했다: {type(exc).__name__}: {exc}"[:200],
            "reason_code": ReasonCode.GEOMETRY_VALIDATOR_ERROR.value,
        }
    if result is None:
        return {
            "available": False,
            "detail": "이 로봇에는 pick/place 계획 검증에 쓸 셀 선언이 없다",
            "reason_code": ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value,
        }
    return {"available": True, **result.to_dict()}


def _run_step(adapter, step) -> ExecutionResult:
    """스텝 하나를 어댑터 계약으로 보낸다. 스킬을 명시 값으로 분기한다."""
    if step.skill == "home":
        return adapter.home(ADAPTER_TIMEOUT_SEC)
    if step.skill == "move":
        return adapter.move(step.args["to"], ADAPTER_TIMEOUT_SEC)
    if step.skill == "pick":
        return adapter.pick(step.args["object"], step.args["from"], ADAPTER_TIMEOUT_SEC)
    if step.skill == "place":
        return adapter.place(step.args["object"], step.args["to"], ADAPTER_TIMEOUT_SEC)
    if step.skill == "stop":
        return _run_stop(adapter)
    raise ApiError(
        400, ReasonCode.PLAN_UNSUPPORTED_SKILL, f"어댑터가 모르는 스킬: {step.skill}"
    )


def _run_stop(adapter) -> ExecutionResult:
    """정지를 요청하고 **관측으로 확인한다.**

    `stop()`의 반환만으로 정지를 주장하지 않는다(어댑터 계약 5번).
    `confirm_stopped()`가 계측으로 확인한 뒤에만 STOPPED로 돌려준다.
    확인되지 않으면 `exec.stop_unconfirmed`로 남긴다 — 대기를 늘려
    통과시키지 않는다.
    """
    requested = adapter.stop(ADAPTER_TIMEOUT_SEC)
    if not requested.request_accepted:
        return requested
    confirmed = adapter.confirm_stopped(ADAPTER_TIMEOUT_SEC)
    evidence = {
        "stop_request": dict(requested.evidence),
        "stop_confirmation": dict(confirmed.evidence),
        "stop_confirmed": confirmed.state is ExecutionState.STOPPED
                          and confirmed.verified,
    }
    if not evidence["stop_confirmed"]:
        return ExecutionResult(
            state=ExecutionState.UNKNOWN, request_accepted=True,
            motion_completed=False, target_reached=False, task_succeeded=False,
            verified=False,
            reason=confirmed.reason or ReasonCode.EXEC_STOP_UNCONFIRMED,
            evidence=evidence,
        )
    return ExecutionResult(
        state=ExecutionState.STOPPED, request_accepted=True,
        # 정지는 모션을 끝낸 것이지 목표에 도달한 것이 아니다.
        motion_completed=True, target_reached=False, task_succeeded=False,
        verified=True, reason=ReasonCode.EXEC_STOPPED, evidence=evidence,
    )
