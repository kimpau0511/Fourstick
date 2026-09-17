"""외부 입출력 경계 스키마 (Pydantic).

요구정의서의 사용 기술에 "JSON Schema, Pydantic"이 명시돼 있다. 적용 범위는
**외부 경계**로 한정한다:
  - 들어오는 JSON(API 요청)의 형태 검증
  - 나가는 JSON(API 응답)의 형태 고정
  - JSON Schema 생성(`model_json_schema()`)

core 계약(`core/*.py`)은 Pydantic에 의존하지 않는다. 내부 타입을 특정
라이브러리에 묶지 않고, 순수 단위 테스트가 외부 의존성 없이 끝나도록
유지하기 위해서다.

역할 분담:
  Pydantic  — 형태: 타입, 필수/선택, 미지의 필드 거부, 숫자 범위
  core 계약 — 의미: 지원 스킬, 스킬별 인자, schema version, 좌표 금지,
              plan hash, TTL, Profile 정합성
경계는 Pydantic 실패와 core 실패를 모두 ReasonCode로 바꿔 올린다.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.boundary import BoundaryValidationError, FieldIssue, issues_from_pydantic
from core.constants import SUPPORTED_SCHEMA_VERSIONS, TASK_PLAN_SCHEMA_VERSION
from core.execution_result import ExecutionResult
from core.reason_codes import ReasonCode
from core.task_plan import PlanError, TaskPlan, TaskStep

# strict=True: 외부에서 들어온 값을 조용히 변환하지 않는다. 변환을 허용하면
# 공개한 JSON Schema(타입 거부)와 실제 수용 범위(문자열 "60"을 60.0으로 받음)가
# 어긋나 스키마가 계약을 잘못 설명하게 된다.
STRICT = ConfigDict(extra="forbid", frozen=True, strict=True)


class TaskStepModel(BaseModel):
    """스킬 인자는 문자열만 받는다 — 좌표를 계획에 실어 보내는 경로를 막는다."""

    model_config = STRICT

    skill: str = Field(min_length=1)
    args: dict[str, str] = Field(default_factory=dict)


class TaskPlanModel(BaseModel):
    model_config = STRICT

    schema_version: str = Field(default=TASK_PLAN_SCHEMA_VERSION, min_length=1)
    plan_id: str = Field(min_length=1)
    robot_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    steps: list[TaskStepModel] = Field(min_length=1)
    created_at: float
    ttl_sec: float = Field(gt=0.0)
    utterance: str = ""
    terminal_hold: str | None = None
    #: 클라이언트가 되돌려 보내는 해시. 서버가 재계산해 대조한다.
    plan_hash: str | None = None

    def to_core(self) -> TaskPlan:
        """core 계약으로 변환한다. 의미 검증 실패도 ReasonCode로 올린다."""
        try:
            return TaskPlan(
                plan_id=self.plan_id,
                robot_id=self.robot_id,
                profile_id=self.profile_id,
                profile_version=self.profile_version,
                steps=tuple(TaskStep(s.skill, dict(s.args)) for s in self.steps),
                created_at=self.created_at,
                ttl_sec=self.ttl_sec,
                schema_version=self.schema_version,
                utterance=self.utterance,
                terminal_hold=self.terminal_hold,
            )
        except PlanError as exc:
            raise BoundaryValidationError(exc.reason, [FieldIssue("task_plan", str(exc))]) from exc

    @classmethod
    def from_core(cls, plan: TaskPlan) -> "TaskPlanModel":
        return cls(
            schema_version=plan.schema_version,
            plan_id=plan.plan_id,
            robot_id=plan.robot_id,
            profile_id=plan.profile_id,
            profile_version=plan.profile_version,
            steps=[TaskStepModel(skill=s.skill, args=dict(s.args)) for s in plan.steps],
            created_at=plan.created_at,
            ttl_sec=plan.ttl_sec,
            utterance=plan.utterance,
            terminal_hold=plan.terminal_hold,
            plan_hash=plan.plan_hash(),
        )


class ExecutionResultModel(BaseModel):
    """실행 결과 응답. 5축을 합치지 않고 그대로 노출한다."""

    model_config = STRICT

    state: str
    request_accepted: bool
    motion_completed: bool
    target_reached: bool
    task_succeeded: bool
    verified: bool
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_core(cls, result: ExecutionResult) -> "ExecutionResultModel":
        return cls(**result.to_dict())


class ReasonModel(BaseModel):
    model_config = STRICT

    reason: str
    detail: str


class PermitDecisionModel(BaseModel):
    """실행 허가 응답. 거부 사유를 전부 실어 보낸다 — forstick에서 정지 사유가
    응답에 없어 화면에 "상세 메시지가 없습니다"만 나오던 문제를 막는다."""

    model_config = STRICT

    granted: bool
    reasons: list[ReasonModel] = Field(default_factory=list)


def parse_task_plan(payload: dict[str, Any]) -> TaskPlanModel:
    """외부 JSON -> 형태 검증된 모델. 형태 실패는 PLAN_SCHEMA_INVALID."""
    try:
        return TaskPlanModel.model_validate(payload)
    except ValidationError as exc:
        raise BoundaryValidationError(
            ReasonCode.PLAN_SCHEMA_INVALID, issues_from_pydantic(exc)
        ) from exc


def task_plan_json_schema() -> dict[str, Any]:
    """1-11용 JSON Schema. 계약 타입에서 생성하므로 문서와 코드가 어긋나지 않는다."""
    return TaskPlanModel.model_json_schema()


# ── STT WebSocket 경계 (md/개발플랜.md 1-09) ────────────────────────────


class SttStartModel(BaseModel):
    """세션 시작 파라미터. 샘플레이트·언어를 코드 기본값으로 추측하지 않는다."""

    model_config = STRICT

    type: Literal["start"] = "start"
    sample_rate_hz: int = Field(gt=0)
    language: str = Field(min_length=2)
    #: 신뢰도가 이 값 미만이면 되묻는다. Policy에서 주입되며 기본값을 두지 않는다.
    min_confidence: float = Field(ge=0.0, le=1.0)


class SttAudioModel(BaseModel):
    model_config = STRICT

    type: Literal["audio"] = "audio"
    #: base64 PCM 청크. 바이너리 프레임을 쓰는 구현은 이 메시지를 쓰지 않는다.
    payload_b64: str = Field(min_length=1)
    seq: int = Field(ge=0)


class SttControlModel(BaseModel):
    """flush / stop / abort 처럼 본문이 없는 제어 메시지."""

    model_config = STRICT

    type: Literal["flush", "stop", "abort"]


class SttTranscriptModel(BaseModel):
    """partial / final 전사. partial을 명령으로 실행하지 않도록 타입으로 구분한다."""

    model_config = STRICT

    type: Literal["partial", "final"]
    text: str
    confidence: float = Field(ge=0.0, le=1.0)
    #: 현장 용어 후보정이 적용된 경우 원문을 함께 보낸다.
    raw_text: str | None = None


class SttStateModel(BaseModel):
    model_config = STRICT

    type: Literal["state"] = "state"
    state: str


class SttClarifyModel(BaseModel):
    """되묻기. 무엇이 부족한지 이유 코드로 밝힌다."""

    model_config = STRICT

    type: Literal["clarify"] = "clarify"
    reason: str
    question: str


class SttErrorModel(BaseModel):
    """실패 통지. 소켓을 그냥 끊지 않고 이유 코드를 보낸다."""

    model_config = STRICT

    type: Literal["error"] = "error"
    reason: str
    detail: str = ""


def stt_json_schemas() -> dict[str, dict[str, Any]]:
    return {
        "stt_start": SttStartModel.model_json_schema(),
        "stt_audio": SttAudioModel.model_json_schema(),
        "stt_control": SttControlModel.model_json_schema(),
        "stt_transcript": SttTranscriptModel.model_json_schema(),
        "stt_state": SttStateModel.model_json_schema(),
        "stt_clarify": SttClarifyModel.model_json_schema(),
        "stt_error": SttErrorModel.model_json_schema(),
    }


class VersionRejectionModel(BaseModel):
    """지원하지 않는 schema_version 거부 응답.

    거부만 하고 끝내지 않고 지원 목록을 함께 보낸다 — 클라이언트가 무엇으로
    바꿔야 하는지 알 수 있어야 한다(md/호환범위_1-10.md).
    """

    model_config = STRICT

    reason: str
    received: str
    supported: list[str]
    current: str

    @classmethod
    def build(cls, received: str) -> "VersionRejectionModel":
        return cls(
            reason=str(ReasonCode.PLAN_VERSION_UNSUPPORTED),
            received=received,
            supported=list(SUPPORTED_SCHEMA_VERSIONS),
            current=TASK_PLAN_SCHEMA_VERSION,
        )


# ── LLM 출력 경계 (5-01) ────────────────────────────────────────────────
# 모델 출력은 외부 입력과 같은 취급을 받는다. 구조는 여기서, 의미는 core
# 계약과 카탈로그 대조에서 검사한다. 두 단계를 모두 통과해야 TaskPlan이 된다.


class LlmStepModel(BaseModel):
    model_config = STRICT
    skill: str = Field(min_length=1)
    #: 인자는 문자열만 받는다. 좌표·숫자를 넣을 수 없다.
    args: dict[str, str] = Field(default_factory=dict)


class LlmPlanOutputModel(BaseModel):
    """모델이 돌려준 계획 초안의 **형태**.

    스킬 이름이 계약에 있는지, 리소스가 카탈로그에 있는지는 여기서 보지 않는다
    (의미 검증). 여기서는 필드·타입·빈 값만 본다.

    `result`가 `needs_clarification`이면 계획이 아니라 **확인 요청**이다. 이때
    steps는 비어 있어야 하고 clarification이 있어야 한다 — 확인이 필요하다면서
    계획을 함께 내놓는 응답은 앞뒤가 맞지 않는다.

    문자열 `"null"`을 `terminal_hold`로 받지 않는다. 5-02 실측에서 모델이 JSON
    null 대신 문자열을 보냈고, 그 값이 물체 이름으로 취급돼 거부됐다. 여기서
    None으로 바꿔주지 않는다 — 보정하면 모델의 형태 오류가 보이지 않게 된다.
    """

    model_config = STRICT
    output_schema_version: str = Field(min_length=1)
    result: Literal["plan", "needs_clarification"] = "plan"
    steps: list[LlmStepModel] = Field(default_factory=list)
    terminal_hold: str | None = None
    #: 확인이 필요한 이유. `result`가 needs_clarification이면 필요하다.
    clarification: str | None = None

    @model_validator(mode="after")
    def _check_result_shape(self) -> "LlmPlanOutputModel":
        if self.result == "plan":
            if not self.steps:
                raise ValueError("result가 plan인데 steps가 비어 있다")
            if self.clarification:
                raise ValueError(
                    "result가 plan인데 clarification이 있다 — 계획과 확인 요청을"
                    " 동시에 낼 수 없다"
                )
        else:
            if self.steps:
                raise ValueError(
                    "result가 needs_clarification인데 steps가 있다"
                    " — 확인이 필요하면 계획을 내놓지 않는다"
                )
            if not (self.clarification or "").strip():
                raise ValueError("확인 요청에 이유(clarification)가 없다")
        if self.terminal_hold is not None:
            lowered = self.terminal_hold.strip().lower()
            if lowered in ("null", "none", "nil", ""):
                raise ValueError(
                    f"terminal_hold가 문자열 {self.terminal_hold!r}다"
                    " — 비어 있음은 JSON null로 표기한다"
                )
        return self


def parse_llm_plan_output(payload: dict) -> LlmPlanOutputModel:
    """모델 출력의 구조를 검증한다. 실패는 BoundaryValidationError."""
    try:
        return LlmPlanOutputModel.model_validate(payload)
    except ValidationError as exc:
        raise BoundaryValidationError(
            ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID, issues_from_pydantic(exc)
        ) from exc


def llm_plan_output_json_schema() -> dict:
    return LlmPlanOutputModel.model_json_schema()
