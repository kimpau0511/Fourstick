"""vLLM OpenAI 호환 PlanProvider (md/개발플랜.md 5-02).

`PlanProvider` 구현체다. 세 가지만 한다.

1. 호출 전 **서버가 실제로 제공하는 모델**과 설정된 모델 ID를 대조한다.
2. 프롬프트를 렌더링해 HTTP 경계로 보낸다.
3. 응답을 검증 경로(`output_parser`)에 넘긴다.

여기에 계획 규칙도, 프롬프트 문구도, 검증 로직도 없다. HTTP는
`planning/openai_compat.py`, 프롬프트는 `planning/prompt.py`, 검증은
`planning/output_parser.py`와 `planning/pipeline.py`가 맡는다.

**구조화 출력을 쓴다고 검증을 건너뛰지 않는다.** 서버가 스키마를 강제해도
파싱·버전 대조·Pydantic 구조 검증·core 의미 검증을 그대로 통과시킨다. 문법
제약은 필드 모양만 보장하고, 리소스가 카탈로그에 있는지·스킬이 Profile에
있는지는 보장하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.policy import LlmProviderConfig, StructuredOutputMode, ThinkingMode
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog
from core.skill_catalog import SkillCatalog
from core.termination import ApproachRequirement, TerminationRequirement
from planning.openai_compat import OpenAiCompatClient, ServerInfo, TransportError
from planning.output_parser import check_no_reasoning, draft_from_output
from planning.plan_provider import (
    PlanDraft,
    PlanningContext,
    PlanningMode,
    PlanProviderError,
    ProviderCall,
)
from planning.prompt import (
    OUTPUT_SCHEMA_VERSION,
    PROMPT_TEMPLATE_VERSION,
    output_json_schema,
    render_prompt,
)

PROVIDER_ID = "vllm-openai"
#: 구조화 출력 스키마에 붙이는 이름. 서버 로그에서 구분된다.
SCHEMA_NAME = "task_plan_draft"


@dataclass
class VllmPlanProvider:
    """로컬 vLLM 서버 하나를 쓰는 계획 생성 공급자."""

    config: LlmProviderConfig
    client: OpenAiCompatClient
    resource_catalog: ResourceCatalog
    skill_catalog: SkillCatalog
    #: 종료 조건. SafetyPolicy·CapabilityProfile에서 계산해 주입한다.
    #: None이면 종료 조건을 프롬프트에 넣지 않는다(제약 없음으로 렌더링).
    termination: TerminationRequirement | None = None
    #: 접근(이동) 선행 요구. SkillCatalog·SafetyPolicy에서 계산해 주입한다.
    approach: ApproachRequirement | None = None
    #: 확인한 서버 정보. 첫 호출에서 채우고 재사용한다.
    server_info: ServerInfo | None = field(default=None, repr=False)

    @property
    def provider_id(self) -> str:
        return PROVIDER_ID

    @property
    def model_name(self) -> str:
        return self.config.model_id

    @property
    def prompt_template_version(self) -> str:
        return PROMPT_TEMPLATE_VERSION

    @property
    def output_schema_version(self) -> str:
        return OUTPUT_SCHEMA_VERSION

    @property
    def is_mock(self) -> bool:
        return False

    # ── 사전 대조 ───────────────────────────────────────────────────────
    def verify_server(self, *, force: bool = False) -> ServerInfo:
        """서버가 설정된 모델을 제공하는지 확인한다.

        다르면 **호출하지 않는다.** 다른 모델의 응답을 설정된 모델의 결과로
        기록하면 측정 자체가 무의미해진다.
        """
        if self.server_info is not None and not force:
            return self.server_info
        try:
            info = self.client.server_info()
        except TransportError as exc:
            raise PlanProviderError(exc.reason, str(exc)[:200]) from None
        if not info.serves(self.config.model_id):
            raise PlanProviderError(
                ReasonCode.PLAN_LLM_MODEL_MISMATCH,
                f"서버가 제공하는 모델은 {list(info.model_ids)}인데 설정은"
                f" {self.config.model_id!r}다",
            )
        if (
            info.max_model_len is not None
            and info.max_model_len != self.config.max_model_len
        ):
            raise PlanProviderError(
                ReasonCode.CONFIG_VERSION_MISMATCH,
                f"서버 max_model_len({info.max_model_len})이 설정"
                f"({self.config.max_model_len})과 다르다",
            )
        self.server_info = info
        return info

    # ── 생성 ────────────────────────────────────────────────────────────
    def generate(self, context: PlanningContext) -> PlanDraft:
        info = (
            self.verify_server() if self.config.verify_model_id
            else ServerInfo(model_ids=(self.config.model_id,))
        )
        termination = self.termination or TerminationRequirement(
            final_skill=None, max_steps=0, hold_possible=False,
            sources=("종료 조건 미주입",),
        )
        rendering = render_prompt(
            context,
            resource_catalog=self.resource_catalog,
            skill_catalog=self.skill_catalog,
            termination=termination,
            approach=self.approach or ApproachRequirement(approach_skill=None),
        )
        if rendering.output_schema_version != OUTPUT_SCHEMA_VERSION:
            raise PlanProviderError(
                ReasonCode.PLAN_PROMPT_VERSION_MISMATCH,
                "렌더링된 출력 스키마 버전이 모듈 상수와 다르다",
            )
        structured = (
            self.config.structured_output
            is StructuredOutputMode.RESPONSE_FORMAT_JSON_SCHEMA
        )
        try:
            completion = self.client.chat(
                system=rendering.system,
                user=rendering.user,
                json_schema=(
                    output_json_schema(self.skill_catalog, self.resource_catalog)
                    if structured else None
                ),
                schema_name=SCHEMA_NAME,
            )
        except TransportError as exc:
            raise PlanProviderError(exc.reason, str(exc)[:200]) from None

        # non-thinking으로 요청했는데 본문에 사고 과정이 섞였으면 거부한다.
        check_no_reasoning(completion.content)
        if completion.finish_reason == "length":
            raise PlanProviderError(
                ReasonCode.PLAN_LLM_OUTPUT_SCHEMA_INVALID,
                f"응답이 max_tokens({self.config.max_tokens})에서 잘렸다",
            )

        call = ProviderCall(
            served_model_id=completion.served_model_id or self.config.model_id,
            server_version=info.server_version,
            quantization=self.config.quantization,
            structured_output=structured,
            thinking_mode=self.config.thinking_mode.value,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            finish_reason=completion.finish_reason,
            system_fingerprint=completion.system_fingerprint,
        )
        # 사고 과정은 본문이 아니라 별도 채널에서만 다룬다. 길이만 남긴다 —
        # 내용을 기록하면 원본 보존 정책 대상이 되고 계획과 섞일 위험이 있다.
        notes = {"reasoning_chars": str(len(completion.reasoning))}
        return draft_from_output(
            completion.content,
            provider_id=self.provider_id,
            model_id=self.config.model_id,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            mode=(
                PlanningMode.JSON_SCHEMA if structured
                else PlanningMode.FUNCTION_CALLING
            ),
            latency_sec=completion.latency_sec,
            is_mock=False,
            notes=notes,
            call=call,
        )
