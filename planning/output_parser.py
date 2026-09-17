"""모델 출력 → 계획 초안 (md/개발플랜.md 5-01).

**모델 출력을 믿지 않는다.** 세 단계를 모두 통과해야 초안이 된다.

| 단계 | 검사 | 실패 이유 코드 |
|---|---|---|
| 1 | JSON으로 읽히는가 | `plan.llm_output_unparseable` |
| 2 | 출력 스키마 버전이 기대와 같은가 | `plan.prompt_version_mismatch` |
| 3 | Pydantic 구조(필드·타입·빈 값·result 정합성) | `plan.llm_output_schema_invalid` |
| 4 | 확인 요청인가 | `plan.clarification_required` (실패가 아니라 결말) |

스킬이 카탈로그에 있는지, 리소스가 존재하는지, 인자 종류가 맞는지는 그 다음
단계(`planning/pipeline.py`)에서 본다. 여기서 보정하지 않는다 — 비슷한 이름으로
고쳐 주면 사용자가 말한 것과 다른 계획이 실행된다.

코드펜스(```json ... ```)만 벗겨낸다. 그 외 형태 교정은 하지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

from core.boundary import BoundaryValidationError
from core.reason_codes import ReasonCode
from planning.plan_provider import (
    DraftStep,
    PlanDraft,
    PlanningMode,
    PlanProviderError,
    ProviderCall,
)
from server.schemas import parse_llm_plan_output


#: 사고 과정이 본문에 섞였는지 보는 표지.
REASONING_MARKERS: tuple[str, ...] = ("<think>", "</think>", "<|thinking|>")


def check_no_reasoning(content: str) -> None:
    """본문에 사고 과정이 섞였으면 거부한다.

    벗겨내지 않는다. 벗겨내기 시작하면 모델 출력을 코드가 보정하는 것이고,
    어디까지가 사고이고 어디부터가 계획인지 코드가 추측해야 한다. non-thinking
    으로 요청했는데 섞여 온 것은 설정이 먹지 않았다는 신호다.
    """
    lowered = content.lower()
    for marker in REASONING_MARKERS:
        if marker in lowered:
            raise PlanProviderError(
                ReasonCode.PLAN_LLM_REASONING_LEAKED,
                f"응답 본문에 사고 과정 표지 {marker!r}가 있다"
                " — non-thinking 설정이 적용되지 않았다",
            )


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped[3:]
    if body.lower().startswith("json"):
        body = body[4:]
    end = body.rfind("```")
    return (body[:end] if end != -1 else body).strip()


class ClarificationNeeded(PlanProviderError):
    """모델이 계획 대신 확인을 요청했다.

    실패가 아니라 **결말**이다. 모호하거나 정보가 부족한 요청에 그럴듯한 계획을
    만드는 것보다 되묻는 것이 맞다. 호출자는 이 결과를 사용자에게 전달한다.
    """

    def __init__(self, question: str, call: ProviderCall | None = None):
        self.question = question
        #: 이 응답의 호출 관측값(토큰·지연·모델). 초안이 없어도 기록에 남긴다.
        self.call = call
        super().__init__(
            ReasonCode.PLAN_CLARIFICATION_REQUIRED,
            f"확인 필요: {question[:200]}",
        )


def parse_output(raw: str | dict[str, Any], *, expected_schema_version: str) -> dict:
    """모델 출력을 검증된 dict로 만든다. 실패는 PlanProviderError."""
    if isinstance(raw, str):
        try:
            payload = json.loads(_strip_code_fence(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise PlanProviderError(
                ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
                f"JSON으로 읽히지 않는다: {exc}",
            ) from exc
    else:
        payload = raw
    if not isinstance(payload, dict):
        raise PlanProviderError(
            ReasonCode.PLAN_LLM_OUTPUT_UNPARSEABLE,
            f"객체가 아니다: {type(payload).__name__}",
        )

    declared = payload.get("output_schema_version")
    if declared != expected_schema_version:
        raise PlanProviderError(
            ReasonCode.PLAN_PROMPT_VERSION_MISMATCH,
            f"출력 스키마 버전이 {declared!r}다 (기대 {expected_schema_version!r})",
        )
    try:
        model = parse_llm_plan_output(payload)
    except BoundaryValidationError as exc:
        raise PlanProviderError(
            ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID,
            f"출력 스키마 위반: {exc}"[:200],
        ) from exc
    if model.result == "needs_clarification":
        raise ClarificationNeeded(model.clarification or "이유 없음")
    return {
        "steps": tuple(
            DraftStep(skill=s.skill, args=dict(s.args)) for s in model.steps
        ),
        "terminal_hold": model.terminal_hold,
    }


def draft_from_output(
    raw: str | dict[str, Any],
    *,
    provider_id: str,
    model_id: str,
    prompt_template_version: str,
    output_schema_version: str,
    mode: PlanningMode,
    latency_sec: float,
    is_mock: bool = False,
    notes: dict[str, str] | None = None,
    call: ProviderCall | None = None,
) -> PlanDraft:
    """공급자 구현체가 쓰는 유일한 초안 생성 경로.

    파싱·검증이 공급자 안에 들어가지 않게 한다. 공급자는 전송만 하고, 검증은
    모든 공급자에게 같은 코드로 적용된다.
    """
    try:
        parsed = parse_output(raw, expected_schema_version=output_schema_version)
    except ClarificationNeeded as exc:
        # 확인 요청에도 호출 관측값을 붙여 올린다 — 토큰·지연 기록이 빠지면
        # 프롬프트 버전별 비용 비교에서 이 응답들이 사라진다.
        raise ClarificationNeeded(exc.question, call=call) from None
    return PlanDraft(
        steps=parsed["steps"], mode=mode, provider_id=provider_id, model_name=model_id,
        prompt_template_version=prompt_template_version,
        output_schema_version=output_schema_version,
        latency_sec=latency_sec, terminal_hold=parsed["terminal_hold"],
        is_mock=is_mock, notes=notes or {}, call=call,
    )
