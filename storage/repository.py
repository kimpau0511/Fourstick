"""저장소 인터페이스.

**SQL이 없다.** DB 방언, 연결 관리, 트랜잭션 처리는 구현체 안에만 둔다
(`storage/sqlite/`). core·validation·API는 이 인터페이스만 본다.

PostgreSQL 구현체를 추가할 때 이 파일과 core 계약, API 스키마는 바뀌지 않는다.

멱등성과 유일성 (요구 9):
- `request_id`는 유일하다. 같은 id로 다른 내용을 저장하면 거부한다.
- `plan_id`는 유일하고, `(request_id, plan_hash)`도 유일하다 — 같은 요청에
  같은 계획을 두 번 저장하지 않는다.
- `(request_id, attempt_no)`는 유일하다.
- `begin_execution()`은 그 요청에 **결과가 확정되지 않은 시도**가 있으면
  거부한다. 같은 요청이 동시에 두 번 실행되는 것을 막는다.
- 재시도는 기존 기록을 지우지 않고 새 `execution_id`와 다음 `attempt_no`를 받는다.

append-only (요구 8): 상태 전이·검증 결과·관측값·실행 결과는 갱신하지 않고
추가한다. 같은 `(execution_id, seq)`를 두 번 쓰면 거부한다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Sequence

from core.execution_result import ExecutionResult
from core.execution_state import ExecutionState
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan
from storage.records import (
    ApprovalRecord,
    ExecutionRecord,
    ExecutionTrace,
    ObservationRecord,
    PermitRecord,
    PlanningAttemptRecord,
    PlanRecord,
    RequestRecord,
    ResultRecord,
    RobotProfileRecord,
    SimVerificationRecord,
    SessionRecord,
    SessionStatus,
    StateTransitionRecord,
    SttExecutionPath,
    SttInferenceRecord,
    ValidationRecord,
    ValidationRunRecord,
)


class StorageError(Exception):
    """저장소 오류. 이유 코드를 함께 담아 상위 계층이 분기할 수 있게 한다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class IntegrityViolation(StorageError):
    """유일성·멱등성 제약 위반."""


class CorruptedRecord(StorageError):
    """저장된 행이 변조됐거나 계약에 어긋난다. 정상 데이터로 복원하지 않는다."""


class Repository(ABC):
    # ── 세션 ────────────────────────────────────────────────────────────
    # 세션은 작업 묶음을 구분하는 식별자다. **인증이 아니다.**
    # 서버는 '현재 계획'을 추정하지 않고, 모든 조회를 명시적 식별자로 한다.
    @abstractmethod
    def create_session(self, record: SessionRecord) -> None:
        """세션을 만든다. 같은 id에 같은 내용이면 멱등, 다르면 IntegrityViolation."""

    @abstractmethod
    def get_session(self, session_id: str) -> SessionRecord: ...

    @abstractmethod
    def touch_session(self, session_id: str, *, at: float) -> SessionRecord:
        """마지막 활동 시각을 올린다. 활성 세션이 아니면 그대로 돌려준다."""

    @abstractmethod
    def close_session(
        self, session_id: str, *, at: float, status: SessionStatus
    ) -> SessionRecord:
        """세션을 종료(ended)하거나 만료(expired)로 표시한다."""

    @abstractmethod
    def expire_idle_sessions(
        self, *, now: float, idle_timeout_sec: float
    ) -> Sequence[str]:
        """유휴 시간을 넘긴 활성 세션을 만료로 표시하고 그 id를 돌려준다."""

    @abstractmethod
    def requests_for_session(self, session_id: str) -> Sequence[RequestRecord]:
        """세션의 요청을 시간순으로. 다른 세션의 요청은 포함하지 않는다."""

    # ── 요청 ────────────────────────────────────────────────────────────
    @abstractmethod
    def save_request(self, record: RequestRecord) -> None: ...

    @abstractmethod
    def get_request(self, request_id: str) -> RequestRecord: ...

    # ── STT 실행 기록 ───────────────────────────────────────────────────
    # append-only. 갱신·삭제 메서드를 두지 않는다 — 재전사와 모델 교체는 새
    # 기록을 더하고, 어떤 시도가 채택됐는지는 requests가 가리킨다.
    @abstractmethod
    def append_stt_inference(self, record: SttInferenceRecord) -> None:
        """실행 시도 1회를 더한다.

        같은 `stt_inference_id`로 같은 내용이면 멱등이고, 다른 내용이면
        IntegrityViolation이다 — 기존 기록을 덮어쓰지 않는다.
        """

    @abstractmethod
    def save_request_with_stt_inference(
        self, record: RequestRecord, inference: SttInferenceRecord
    ) -> None:
        """확정 요청과 채택된 실행 기록을 원자적으로 함께 저장한다.

        요청은 채택 기록을 가리키고 기록은 요청을 가리키므로, 따로 넣으면 순서가
        순환한다. 구현체는 하나의 트랜잭션으로 처리한다.
        """

    @abstractmethod
    def get_stt_inference(self, stt_inference_id: str) -> SttInferenceRecord: ...

    @abstractmethod
    def stt_inferences_for_session(self, session_id: str) -> Sequence[SttInferenceRecord]:
        """세션의 모든 시도를 attempt_no 순으로. 모델 비교 결과를 함께 읽는다."""

    @abstractmethod
    def stt_inferences_for_request(self, request_id: str) -> Sequence[SttInferenceRecord]:
        """요청에 연결된 모든 시도. 채택되지 않은 시도도 남아 있다."""

    @abstractmethod
    def stt_inferences_by_path(
        self, execution_path: SttExecutionPath
    ) -> Sequence[SttInferenceRecord]:
        """경로별 조회. 운영 지연 통계와 평가 측정을 섞지 않기 위해 둔다.

        평가 실행기는 Transcriber를 직접 호출해 VAD 게이트와 세션 예산을 지나지
        않는다. 두 경로의 처리 시간을 한 평균에 넣으면 아무 것도 설명하지 못한다.
        """

    @abstractmethod
    def legacy_stt_metadata(self, request_id: str) -> Mapping[str, Any] | None:
        """마이그레이션 0002 시절의 STT 메타데이터(legacy).

        **canonical `stt_inferences`와 섞지 않는다.** 옛 스키마에는 전사 본문·
        장치·음성 길이·처리 시간이 없어서 실행 기록으로 변환할 수 없다. 조회
        경로를 따로 두어 통계에 들어가지 않게 하고, 없는 값을 추정하지 않는다.
        반환값은 원본 컬럼 그대로이며 `SttInferenceRecord`가 아니다.
        """

    @abstractmethod
    def next_stt_attempt_no(self, session_id: str) -> int:
        """세션의 다음 시도 번호. 기록이 없으면 1이다."""

    @abstractmethod
    def selected_stt_inference(self, request_id: str) -> SttInferenceRecord | None:
        """요청이 채택한 시도. 텍스트 입력 요청이면 None이다."""

    # ── 계획 생성 시도 (append-only) ────────────────────────────────────
    # 갱신·삭제 메서드를 두지 않는다. 재시도는 새 planning_attempt_id를 받는다.
    @abstractmethod
    def append_planning_attempt(self, record: PlanningAttemptRecord) -> None:
        """계획 생성 시도 1회를 더한다.

        같은 id에 같은 내용이면 멱등이고, 다른 내용이면 IntegrityViolation이다.
        """

    @abstractmethod
    def get_planning_attempt(self, planning_attempt_id: str) -> PlanningAttemptRecord: ...

    @abstractmethod
    def planning_attempts_for_request(
        self, request_id: str
    ) -> Sequence[PlanningAttemptRecord]:
        """요청의 모든 시도를 시도 번호 순으로. 실패한 시도도 남아 있다."""

    @abstractmethod
    def planning_attempt_for_plan(self, plan_id: str) -> PlanningAttemptRecord | None:
        """이 계획을 만든 시도. 최종 채택된 계획에서 생성 시도로 거꾸로 추적한다."""

    @abstractmethod
    def next_planning_attempt_no(self, request_id: str) -> int:
        """요청의 다음 시도 번호. 기록이 없으면 1이다."""

    # ── 작업자 승인 (append-only) ───────────────────────────────────────
    @abstractmethod
    def append_approval(self, record: ApprovalRecord) -> None:
        """승인·거부 1회를 더한다. 같은 id에 다른 내용이면 IntegrityViolation."""

    @abstractmethod
    def get_approval(self, approval_id: str) -> ApprovalRecord: ...

    @abstractmethod
    def approvals_for_plan(self, plan_id: str) -> Sequence[ApprovalRecord]:
        """계획의 승인 이력을 시간순으로. 재승인도 모두 남는다."""

    @abstractmethod
    def latest_approval(self, plan_id: str) -> ApprovalRecord | None:
        """가장 최근 결정. 이력을 지우지 않고 마지막 값만 읽는다."""

    # ── 계획 ────────────────────────────────────────────────────────────
    @abstractmethod
    def save_plan(self, request_id: str, plan: TaskPlan, stored_at: float) -> PlanRecord: ...

    @abstractmethod
    def get_plan(self, plan_id: str) -> PlanRecord: ...

    @abstractmethod
    def plans_for_request(self, request_id: str) -> Sequence[PlanRecord]: ...

    # ── 검증 ────────────────────────────────────────────────────────────
    @abstractmethod
    def save_validation(self, record: ValidationRecord) -> None: ...

    @abstractmethod
    def validations_for_plan(self, plan_id: str) -> Sequence[ValidationRecord]: ...

    @abstractmethod
    def save_permit(self, record: PermitRecord) -> None: ...

    @abstractmethod
    def permits_for_plan(self, plan_id: str) -> Sequence[PermitRecord]: ...

    # ── 실행 ────────────────────────────────────────────────────────────
    @abstractmethod
    def begin_execution(
        self,
        *,
        execution_id: str,
        request_id: str,
        plan_id: str,
        adapter_id: str,
        policy_id: str,
        policy_version: str,
        started_at: float,
        session_id: str | None = None,
        approval_id: str | None = None,
        adapter_kind: str | None = None,
        is_simulated: bool | None = None,
    ) -> ExecutionRecord:
        """실행 시도를 시작한다. `attempt_no`는 저장소가 할당한다(앱 계산, DB
        자동 증가 아님). 미완료 시도가 있으면 거부한다.

        `session_id`·`approval_id`는 어떤 세션의 어떤 승인으로 시작된 실행인지
        남긴다. `adapter_kind`·`is_simulated`는 실행 환경이다 — completed여도
        실제 로봇 실행으로 집계하지 않기 위해 기록에 보존한다.
        """

    @abstractmethod
    def get_execution(self, execution_id: str) -> ExecutionRecord: ...

    @abstractmethod
    def executions_for_request(self, request_id: str) -> Sequence[ExecutionRecord]: ...

    @abstractmethod
    def executions_for_session(self, session_id: str) -> Sequence[ExecutionRecord]:
        """세션의 실행 시도를 시간순으로. 다른 세션의 실행은 포함하지 않는다."""

    # ── 로봇 Profile 기록 (append-only) ─────────────────────────────────
    @abstractmethod
    def record_robot_profile(self, record: RobotProfileRecord) -> None:
        """조합형 Profile 한 버전을 남긴다. 같은 버전에 다른 내용은 거부한다.

        **기존 행을 새 버전 값으로 갱신하지 않는다** — 새 버전은 새 행이다.
        """

    @abstractmethod
    def get_robot_profile(
        self, composite_profile_id: str, composite_profile_version: str
    ) -> RobotProfileRecord: ...

    @abstractmethod
    def robot_profiles(self) -> Sequence[RobotProfileRecord]:
        """기록된 Profile 전체를 기록순으로."""

    # ── 시뮬레이터 검증 기록 (append-only) ──────────────────────────────
    @abstractmethod
    def append_sim_verification(self, record: SimVerificationRecord) -> None:
        """시뮬레이터 검증 한 스텝을 남긴다. 같은 id로 다른 내용은 거부한다."""

    @abstractmethod
    def sim_verifications(
        self, *, limit: int = 50, composite_profile_id: str | None = None
    ) -> Sequence[SimVerificationRecord]:
        """최근 검증 기록. 시간 내림차순으로."""

    # ── 검증 실행 이력 (append-only) ────────────────────────────────────
    @abstractmethod
    def append_validation_run(self, record: ValidationRunRecord) -> None:
        """검증 실행 1회를 남긴다. 같은 id로 다른 내용을 쓸 수 없다.

        환경 데이터 본문이나 기하 모델은 담지 않는다 — snapshot 식별자와
        hash로만 추적한다.
        """

    @abstractmethod
    def get_validation_run(self, validation_run_id: str) -> ValidationRunRecord: ...

    @abstractmethod
    def validation_runs_for_plan(
        self, plan_id: str, *, validator_kind: str | None = None
    ) -> Sequence[ValidationRunRecord]:
        """계획의 검증 이력을 시간순으로. 종류로 걸러 볼 수 있다."""

    @abstractmethod
    def validation_runs_for_execution(
        self, execution_id: str
    ) -> Sequence[ValidationRunRecord]: ...

    @abstractmethod
    def execution_environment_counts(self) -> Mapping[str, int]:
        """실행 수를 실행 환경으로 나눠 센다.

        `{"simulated": n, "real": n, "unknown": n}`. 개발용 Fake 실행이
        completed로 끝나도 실제 로봇 실행과 같은 수에 섞이지 않게, 세는
        지점에서부터 나눈다. 환경 정보가 없는 옛 기록은 unknown이다 —
        실제 실행으로 추정하지 않는다.
        """

    # ── 이력 (append-only) ──────────────────────────────────────────────
    @abstractmethod
    def append_state_transition(
        self,
        execution_id: str,
        *,
        to_state: ExecutionState,
        occurred_at: float,
        reason: ReasonCode | None = None,
    ) -> StateTransitionRecord:
        """상태 전이를 추가한다. `from_state`와 `seq`는 저장소가 마지막 전이에서
        이어붙인다. 허용되지 않은 전이는 거부한다."""

    @abstractmethod
    def state_transitions(self, execution_id: str) -> Sequence[StateTransitionRecord]: ...

    @abstractmethod
    def append_observation(
        self,
        execution_id: str,
        *,
        kind: str,
        observed_at: float,
        payload: Mapping[str, Any],
    ) -> ObservationRecord: ...

    @abstractmethod
    def observations(self, execution_id: str) -> Sequence[ObservationRecord]: ...

    @abstractmethod
    def append_result(
        self, execution_id: str, result: ExecutionResult, recorded_at: float
    ) -> ResultRecord: ...

    @abstractmethod
    def results(self, execution_id: str) -> Sequence[ResultRecord]: ...

    def final_result(self, execution_id: str) -> ResultRecord | None:
        rows = self.results(execution_id)
        return rows[-1] if rows else None

    @abstractmethod
    def trace(self, execution_id: str) -> ExecutionTrace: ...

    # ── 수명 ────────────────────────────────────────────────────────────
    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def schema_version(self) -> int:
        """적용된 저장 스키마 마이그레이션 번호."""
