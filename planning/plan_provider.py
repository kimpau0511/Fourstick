"""계획 생성 모델 호출 경계 (md/개발플랜.md 3-03 책임 분리 / 5-01 인터페이스).

이 모듈은 **모델을 호출하지 않는다.** 호출의 입력·출력·오류 모양만 고정한다.
실제 구현체(vLLM OpenAI 호환 클라이언트, Mock)는 5단계에서 이 뒤에 들어온다.

경계를 이렇게 두는 이유:
- 슬롯 추출을 모델 없이 시험할 수 있다(3-03의 목적).
- 모델을 바꿀 때 `core`·`validation`이 영향을 받지 않는다. 모델 이름·엔드포인트·
  template은 구현체와 설정 안에만 있다(계획.md 27장).
- **Mock 결과가 실제 성공으로 보이지 않는다.** `PlanDraft.is_mock`을 계약에
  두어, 기록·API가 이 값을 그대로 전달할 수 있게 한다(5-08 준비).

출력은 완성된 `TaskPlan`이 아니라 **초안(draft)** 이다. plan_id 발급·시각·TTL은
호출자가 정하고, 카탈로그 대조와 안전 검증은 그 뒤에 온다. 모델이 자기 계획을
스스로 승인하는 경로를 만들지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Protocol, Sequence

from core.reason_codes import ReasonCode
from planning.slot_extractor import SlotExtraction


class PlanningMode(str, Enum):
    """모델에게 계획을 받아내는 방식. 어느 경로로 받았는지 기록에 남긴다."""

    FUNCTION_CALLING = "function_calling"
    JSON_SCHEMA = "json_schema"


class PlanProviderError(Exception):
    """모델 호출 실패. 이유 코드를 함께 담는다.

    성공으로 바뀌어 나가면 안 되는 실패다 — 호출자는 이유 코드를 그대로
    기록하고, 빈 계획을 만들어 계속 진행하지 않는다.
    """

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class PlanningContext:
    """모델에게 주는 입력. 발화와 **카탈로그가 허용한 것**만 담는다.

    허용 목록을 함께 넘기는 이유는 모델이 없는 위치·자재를 만들어 내는 것을
    입력 단계에서 줄이기 위해서다. 그래도 만들어 내면 출력 대조에서 걸린다.
    """

    utterance: str
    slots: SlotExtraction
    #: 허용 스킬. `CapabilityProfile.supported_skills`에서 온다.
    allowed_skills: tuple[str, ...]
    #: 허용 위치·자재 resource_id. 카탈로그에서 온다.
    allowed_locations: tuple[str, ...]
    allowed_objects: tuple[str, ...]
    robot_id: str
    profile_id: str
    profile_version: str
    catalog_version: str
    schema_version: str
    #: 참고 문서(RAG). 5-06에서 채운다. 비어 있어도 계약은 성립한다.
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.utterance.strip():
            raise PlanProviderError(
                ReasonCode.PLAN_SLOT_INCOMPLETE, "빈 발화로는 계획을 요청하지 않는다"
            )
        if not self.allowed_skills:
            raise PlanProviderError(
                ReasonCode.CONFIG_MISSING, "허용 스킬이 비어 있다"
            )


@dataclass(frozen=True)
class DraftStep:
    """초안 스텝. 검증 전이므로 `TaskStep`으로 만들지 않는다."""

    skill: str
    args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCall:
    """실제 호출에서 관측한 값. Mock은 채우지 않는다.

    설정값이 아니라 **서버가 알려준 값**을 담는다. 설정과 서버가 다를 수 있고,
    기록에는 실제로 무엇이 답했는지가 남아야 한다.
    """

    #: /v1/models 또는 응답이 알려준 모델 ID.
    served_model_id: str
    server_version: str
    quantization: str
    structured_output: bool
    thinking_mode: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = ""
    system_fingerprint: str = ""


@dataclass(frozen=True)
class PlanDraft:
    """모델이 돌려준 계획 초안과 호출 기록."""

    steps: tuple[DraftStep, ...]
    mode: PlanningMode
    #: 공급자 식별자(예: vllm-openai, mock). 모델 이름과 따로 남긴다.
    provider_id: str
    #: 모델 식별자. 같은 공급자에서 모델만 바뀌는 경우를 구분한다.
    model_name: str
    #: 프롬프트 템플릿 버전. 어떤 지시로 만든 계획인지 추적한다.
    prompt_template_version: str
    #: 출력 스키마 버전.
    output_schema_version: str
    #: 모델 응답까지 걸린 시간(초). 모델별 비교(5-09)의 입력이다.
    latency_sec: float
    #: 목표 종료 상태. 쥔 채 끝나는 계획이면 물체 이름이 들어온다.
    terminal_hold: str | None = None
    #: **실제 모델이 아니라 Mock이 만든 결과인가.** 기록·API가 이 값을 지운 채
    #: 성공으로 표시하면 안 된다(5-08).
    is_mock: bool = False
    #: 모델이 남긴 보조 정보(tool 이름 등). 계약에 넣지 않는다.
    notes: Mapping[str, str] = field(default_factory=dict)
    #: 실제 호출 관측값. Mock·시험용 초안에서는 None이다.
    call: ProviderCall | None = None

    def __post_init__(self) -> None:
        if not self.steps:
            raise PlanProviderError(
                ReasonCode.PLAN_SCHEMA_INVALID, "초안에 스텝이 없다"
            )
        for name in ("provider_id", "model_name", "prompt_template_version",
                     "output_schema_version"):
            if not getattr(self, name):
                raise PlanProviderError(
                    ReasonCode.CONFIG_MISSING, f"{name}이 비어 있다"
                )
        if self.latency_sec < 0:
            raise PlanProviderError(ReasonCode.CONFIG_INVALID, "latency_sec가 음수다")


class PlanProvider(Protocol):
    """계획 생성 모델 하나. 구현체는 5-02~5-04에서 붙인다.

    **공급자 SDK는 구현체 안에만 있다.** 이 프로토콜은 HTTP 클라이언트·토큰·
    엔드포인트를 모른다. 구현체가 바뀌어도 `core`·`validation`은 영향받지 않는다.
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    @property
    def prompt_template_version(self) -> str:
        """이 공급자가 보내는 프롬프트 템플릿 버전.

        초안에도 같은 값이 담기지만, **호출이 실패하면 초안이 없다.** 실패·확인
        요청 기록에도 버전이 남아야 프롬프트 버전별 비교가 성립한다.
        """
        ...

    @property
    def output_schema_version(self) -> str: ...

    @property
    def is_mock(self) -> bool:
        """Mock 구현인가.

        **공급자가 선언한다.** 초안(`PlanDraft.is_mock`)에서 역추정하면 호출이
        실패해 초안이 없을 때 Mock 여부를 알 수 없고, 실패 기록이 실제 호출
        통계에 섞인다(실측에서 Mock 실패 23건이 실제 호출로 잡혔다).
        """
        ...

    def generate(self, context: PlanningContext) -> PlanDraft:
        """계획 초안을 만든다.

        실패는 `PlanProviderError`로 올린다. 빈 초안이나 임의 기본 계획을
        돌려주지 않는다 — 모델 실패가 성공처럼 보이는 경로를 만들지 않기
        위해서다.
        """
        ...


def resource_args(steps: Sequence[DraftStep]) -> tuple[str, ...]:
    """초안이 참조한 리소스 인자 값들. 카탈로그 대조에 넘긴다."""
    out: list[str] = []
    for step in steps:
        for key in ("to", "from", "object"):
            value = step.args.get(key)
            if value is not None:
                out.append(value)
    return tuple(out)
