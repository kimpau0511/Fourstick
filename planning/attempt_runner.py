"""계획 생성 시도 실행기 (md/개발플랜.md 5-01).

`pipeline.plan_from_utterance()`를 **정책에 따라 여러 번** 부르고, 각 시도를
append-only 기록으로 남긴다.

여기 있는 것과 없는 것:
- 있다: 재시도 횟수·제한시간 강제, 이유 코드 구분, 시도 기록 생성, 비밀정보 제거
- 없다: 프롬프트 문구, HTTP·SDK, 모델 이름. 그것들은 Provider 구현체와
  `planning/prompt.py`에 있다.

**같은 호출을 무제한 반복하지 않는다.** `PlanningPolicy.max_attempts`가 상한이고,
`retryable_reasons`에 없는 실패는 한 번에 끝낸다. 형식이 깨진 출력은 다시
물어볼 여지가 있지만, 미등록 리소스는 같은 입력에서 같은 결과가 나오기 쉽다.

**제한시간은 호출자를 실제로 놓아준다.** `with ThreadPoolExecutor(...)`를 쓰면
블록을 벗어날 때 `shutdown(wait=True)`가 걸려서 제한시간이 의미가 없다(STT에서
실측으로 확인했다 — 제한 0.3초가 3.0초 뒤 복귀). 영속 executor를 쓰고 기다리지
않는다. 다만 파이썬에서 실행 중인 스레드를 죽일 수 없으므로 **버려진 호출은
계속 돈다** — 숨기지 않고 기록에 남긴다.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable, Sequence

from core.capability_profile import CapabilityProfile
from core.policy import PlanningPolicy
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog
from core.skill_catalog import SkillCatalog
from planning.pipeline import PlanningFailure, PlanningOutcome, plan_from_utterance
from planning.plan_provider import PlanProvider, PlanProviderError
from planning.redaction import redact, truncate
from storage.records import (
    PlanningAttemptRecord,
    PlanningAttemptStatus,
    PlanningExecutionPath,
    PlanningPayload,
    ValidationStage,
    ValidationStageResult,
)

#: 기록에 남기는 payload 구조의 버전. 모양을 바꾸면 올린다.
PAYLOAD_VERSION = "planning-payload-1.0"


@dataclass
class PlanningRun:
    """한 요청에 대한 시도 전체."""

    outcome: PlanningOutcome
    attempts: tuple[PlanningAttemptRecord, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome.ok

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def used_mock(self) -> bool:
        return self.outcome.used_mock


class _DeadlineGuard:
    """모델 호출에 벽시계 상한을 강제한다. 영속 executor를 쓴다."""

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="planning")
        self.abandoned = 0

    def call(self, fn: Callable[[], PlanningOutcome], timeout_sec: float) -> PlanningOutcome:
        future = self._pool.submit(fn)
        try:
            return future.result(timeout=timeout_sec)
        except FutureTimeout as exc:
            future.cancel()
            self.abandoned += 1
            raise PlanProviderError(
                ReasonCode.PLAN_LLM_TIMEOUT,
                f"계획 생성이 제한시간 {timeout_sec}s를 넘겼다"
                " (호출 스레드는 끝날 때까지 남는다)",
            ) from exc

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


#: 실패 이유 코드 -> 막힌 검증 단계. 어디서 끊겼는지를 기록에 남긴다.
_STAGE_BY_REASON: dict[ReasonCode, ValidationStage] = {
    ReasonCode.PLAN_LLM_UNAVAILABLE: ValidationStage.TRANSPORT,
    ReasonCode.PLAN_LLM_TIMEOUT: ValidationStage.TRANSPORT,
    ReasonCode.PLAN_LLM_MODEL_MISMATCH: ValidationStage.TRANSPORT,
    ReasonCode.CONFIG_MISSING: ValidationStage.TRANSPORT,
    ReasonCode.CONFIG_VERSION_MISMATCH: ValidationStage.TRANSPORT,
    ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE: ValidationStage.JSON_PARSE,
    ReasonCode.PLAN_LLM_REASONING_LEAKED: ValidationStage.JSON_PARSE,
    ReasonCode.PLAN_PROMPT_VERSION_MISMATCH: ValidationStage.SCHEMA_VERSION,
    ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID: ValidationStage.PYDANTIC_STRUCTURE,
}

#: 계획 생성 쪽 단계 순서. 앞 단계는 통과, 뒤 단계는 시도되지 않았다.
_PLANNING_STAGES: tuple[ValidationStage, ...] = (
    ValidationStage.TRANSPORT,
    ValidationStage.JSON_PARSE,
    ValidationStage.SCHEMA_VERSION,
    ValidationStage.PYDANTIC_STRUCTURE,
    ValidationStage.CORE_SEMANTICS,
)


def _stages(outcome: PlanningOutcome) -> tuple[ValidationStageResult, ...]:
    """이번 시도가 어느 단계까지 갔는지.

    통과한 단계와 막힌 단계만 남긴다. 시도하지 않은 뒤 단계는 "통과"로
    적지 않는다 — 통과와 미시도는 다르다.
    """
    if outcome.failure is None:
        return tuple(
            ValidationStageResult(stage=stage, passed=True)
            for stage in _PLANNING_STAGES
        )
    reason = outcome.failure.reason
    blocked = _STAGE_BY_REASON.get(reason, ValidationStage.CORE_SEMANTICS)
    out: list[ValidationStageResult] = []
    for stage in _PLANNING_STAGES:
        if stage is blocked:
            out.append(
                ValidationStageResult(
                    stage=stage, passed=False, reason_code=reason,
                    detail=outcome.failure.detail[:200],
                )
            )
            break
        out.append(ValidationStageResult(stage=stage, passed=True))
    return tuple(out)


def _payload(
    *, utterance: str, prompt: object | None, raw_output: object | None,
    policy: PlanningPolicy,
) -> PlanningPayload | None:
    """원본 payload를 정책에 맞게 만든다. 비밀정보를 지운 뒤 길이를 제한한다."""
    if not policy.store_payloads:
        return None
    body = redact(
        {
            "utterance": utterance,
            "prompt": prompt,
            "raw_output": raw_output,
        }
    )
    text = json.dumps(body, ensure_ascii=False)
    text, truncated = truncate(text, policy.payload_max_chars)
    return PlanningPayload(
        payload_version=PAYLOAD_VERSION,
        body_json=text,
        truncated=truncated,
        retention_days=policy.payload_retention_days,
    )


def run_planning(
    utterance: str,
    *,
    request_id: str,
    catalog: ResourceCatalog,
    skill_catalog: SkillCatalog,
    profile: CapabilityProfile,
    provider: PlanProvider,
    policy: PlanningPolicy,
    robot_id: str,
    plan_id_factory: Callable[[], str],
    attempt_id_factory: Callable[[int], str],
    now_utc: Callable[[], float],
    clock: Callable[[], float],
    ttl_sec: float,
    stop_keywords: tuple[str, ...],
    schema_version: str,
    stt_inference_id: str | None = None,
    references: tuple[str, ...] = (),
    first_attempt_no: int = 1,
    guard: _DeadlineGuard | None = None,
    post_checks: Callable[[object], Sequence[ValidationStageResult]] | None = None,
    execution_path: PlanningExecutionPath | None = None,
    evaluation_run_id: str | None = None,
    evaluation_label: str | None = None,
    evaluation_case_id: str | None = None,
) -> PlanningRun:
    """정책 안에서 계획을 만든다. 시도마다 기록을 남긴다.

    `now_utc`는 기록용 UTC 시각, `clock`은 경과 시간 측정용 단조 시계다. 둘 다
    주입받는다 — 코드가 시계를 직접 읽지 않는다.

    `post_checks`는 계획이 만들어진 뒤 실행되는 검사(SafetyValidator·
    ExecutionPermit)의 결과를 시도 기록에 함께 남기기 위한 주입점이다. 이
    모듈이 `validation`을 import하지 않으면서도 단계별 결과를 한 번의 쓰기로
    남길 수 있다.
    """
    own_guard = guard is None
    guard = guard or _DeadlineGuard()
    attempts: list[PlanningAttemptRecord] = []
    outcome: PlanningOutcome | None = None
    previous_id: str | None = None
    attempt_no = first_attempt_no
    try:
        while True:
            attempt_id = attempt_id_factory(attempt_no)
            started_utc = now_utc()
            started = clock()

            def once() -> PlanningOutcome:
                return plan_from_utterance(
                    utterance, catalog=catalog, skill_catalog=skill_catalog,
                    profile=profile, provider=provider, robot_id=robot_id,
                    plan_id_factory=plan_id_factory, created_at=started_utc,
                    ttl_sec=ttl_sec, stop_keywords=stop_keywords,
                    schema_version=schema_version, references=references,
                )

            try:
                outcome = guard.call(once, policy.request_timeout_sec)
            except PlanProviderError as exc:
                outcome = PlanningOutcome(
                    slots=None, failure=PlanningFailure(exc.reason, str(exc)[:200])
                )

            duration_ms = int(round((clock() - started) * 1000))
            status = (
                PlanningAttemptStatus.SUCCEEDED if outcome.ok
                else PlanningAttemptStatus.FAILED
            )
            if outcome.ok and outcome.bypassed_model:
                status = PlanningAttemptStatus.STOP_BYPASS

            draft = outcome.draft
            # 초안이 없는 결말(확인 요청)도 호출 관측값을 남긴다.
            call = draft.call if draft is not None else outcome.call
            # 공급자가 선언한 값을 쓴다. 초안이 없는 실패에서도 분류가 유지된다.
            is_mock = bool(getattr(provider, "is_mock", False))
            stages = list(_stages(outcome))
            if outcome.plan is not None and post_checks is not None:
                stages.extend(post_checks(outcome.plan))
            attempts.append(
                PlanningAttemptRecord(
                    planning_attempt_id=attempt_id,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    schema_version=schema_version,
                    provider_id=None if draft is None else draft.provider_id,
                    model_id=None if draft is None else draft.model_name,
                    # 초안이 없어도(실패·확인 요청) 공급자에서 버전을 읽는다.
                    # 프롬프트 버전별 비교에서 이 행들이 빠지면 안 된다.
                    prompt_template_version=(
                        draft.prompt_template_version if draft is not None
                        else getattr(provider, "prompt_template_version", None)
                    ),
                    output_schema_version=(
                        draft.output_schema_version if draft is not None
                        else getattr(provider, "output_schema_version", None)
                    ),
                    resource_catalog_version=catalog.catalog_version,
                    skill_catalog_version=skill_catalog.catalog_version,
                    started_at=started_utc,
                    ended_at=started_utc + duration_ms / 1000.0,
                    duration_ms=duration_ms,
                    status=status,
                    reason_code=None if outcome.failure is None else outcome.failure.reason,
                    detail="" if outcome.failure is None else outcome.failure.detail,
                    plan_id=None if outcome.plan is None else outcome.plan.plan_id,
                    plan_hash=None if outcome.plan is None else outcome.plan.plan_hash(),
                    # STOP 우회는 공급자를 호출하지 않는다 — Mock 여부를 남기지 않는다.
                    is_mock=(
                        False if status is PlanningAttemptStatus.STOP_BYPASS
                        else is_mock
                    ),
                    served_model_id=None if call is None else call.served_model_id,
                    server_version=None if call is None else call.server_version,
                    quantization=None if call is None else call.quantization,
                    structured_output=(
                        None if call is None else call.structured_output
                    ),
                    thinking_mode=None if call is None else call.thinking_mode,
                    prompt_tokens=None if call is None else call.prompt_tokens,
                    completion_tokens=None if call is None else call.completion_tokens,
                    validation_stages=tuple(stages),
                    execution_path=execution_path,
                    evaluation_run_id=evaluation_run_id,
                    evaluation_label=evaluation_label,
                    evaluation_case_id=evaluation_case_id,
                    is_retry=previous_id is not None,
                    previous_attempt_id=previous_id,
                    stt_inference_id=stt_inference_id,
                    payload=_payload(
                        utterance=utterance,
                        prompt=None if draft is None else dict(draft.notes),
                        raw_output=(
                            None if draft is None
                            else [
                                {"skill": s.skill, "args": dict(s.args)}
                                for s in draft.steps
                            ]
                        ),
                        policy=policy,
                    ),
                )
            )

            reason = None if outcome.failure is None else outcome.failure.reason
            if not policy.should_retry(reason, attempt_no):
                break
            previous_id = attempt_id
            attempt_no += 1
    finally:
        if own_guard:
            guard.close()

    assert outcome is not None
    if not outcome.ok and len(attempts) > 1:
        # 재시도를 다 쓰고 실패했다. 마지막 실패 사유는 시도 기록에 남아 있다.
        outcome = PlanningOutcome(
            slots=outcome.slots, draft=outcome.draft,
            failure=PlanningFailure(
                ReasonCode.PLAN_LLM_RETRY_EXHAUSTED,
                f"{len(attempts)}회 시도 후 실패. 마지막 사유:"
                f" {attempts[-1].reason_code}",
            ),
        )
    return PlanningRun(outcome=outcome, attempts=tuple(attempts))


def persist_attempts(repository, attempts: Sequence[PlanningAttemptRecord]) -> None:
    """시도 기록을 저장한다. append-only — 기존 기록을 덮어쓰지 않는다."""
    for record in attempts:
        repository.append_planning_attempt(record)
