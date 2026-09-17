"""운영·안전 정책 (md/계획.md 27장 "안전 한계와 운영 정책").

계획.md 27장은 timeout·tolerance·재시도 횟수·스텝 상한 같은 수치를 제품 코드에
쓰지 못하게 한다. 그런 값의 유일한 집이 이 모듈이다. 기본값을 두지 않는다 —
호출자가 명시하지 않으면 PolicyError로 차단한다.

각 값은 단위와 근거(provenance)를 함께 요구한다. "왜 이 숫자인가"를 답할 수
없는 값이 설정에 들어가는 것을 막기 위해서다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from core.constants import ATOMIC_SKILLS
from core.reason_codes import ReasonCode


class PolicyError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class SafetyPolicy:
    """계획 단계 안전 한계. 로봇과 무관한 운영 정책만 담는다."""

    policy_version: str
    #: 한 계획의 최대 스텝 수. 폭주·루프성 계획 차단용.
    max_steps: int
    #: 값의 근거. 키는 필드명.
    provenance: Mapping[str, str]
    #: 위치 인자를 받는 작업 스텝 직전에 있어야 하는 이동 스킬.
    #: None이면 접근 제약을 프롬프트에 넣지 않는다.
    #:
    #: 검증기의 E-SEQ-003/004가 같은 요구를 한다. 그 규칙은 아직 코드 상수로
    #: 스킬 이름을 갖고 있어서, 두 값이 어긋나지 않는지 테스트로 확인한다
    #: (`test_prompt_generation.py`). 규칙 자체를 설정으로 옮기는 것은 6단계
    #: 안전 규칙 설정화 과제다.
    approach_skill: str | None = None
    #: 계획이 반드시 이 스킬로 끝나야 한다. None이면 종료 스킬 제약이 없다.
    #:
    #: 이 값이 정책에 있는 이유: 같은 요구를 안전 검증(E-SEQ-002)과 계획 생성
    #: 프롬프트가 함께 봐야 한다. 검증기에 상수로 박아 두면 프롬프트는 그 규칙을
    #: 알 수 없고, 프롬프트에 문장으로 박아 두면 두 곳이 어긋난다.
    required_final_skill: str | None = None

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "policy_version 필요")
        if self.max_steps <= 0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "max_steps가 0 이하")
        missing = [f for f in ("max_steps",) if f not in self.provenance]
        if missing:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING, f"근거가 없는 값: {missing}"
            )
        for name in ("required_final_skill", "approach_skill"):
            value = getattr(self, name)
            if value is None:
                continue
            if value not in ATOMIC_SKILLS:
                raise PolicyError(
                    ReasonCode.PLAN_UNSUPPORTED_SKILL,
                    f"계약에 없는 스킬: {value!r} ({name})",
                )
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")


@dataclass(frozen=True)
class FreshnessPolicy:
    """관측값을 신뢰할 수 있는 최대 나이(초). 낡은 값을 성공 판정에 쓰지 않기 위함."""

    policy_version: str
    robot_state_max_age_sec: float
    environment_max_age_sec: float
    provenance: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "policy_version 필요")
        for name in ("robot_state_max_age_sec", "environment_max_age_sec"):
            if getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}이 0 이하")
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")


@dataclass(frozen=True)
class StopPolicy:
    """정지 확인 정책. 채널(팔/그리퍼 등)마다 단위와 허용치가 다르므로
    채널별로 받는다 — 한 채널의 허용치를 다른 채널에 재사용하지 않는다."""

    policy_version: str
    #: 채널 이름 -> 정지 판정 위치 허용치(해당 채널의 SI 단위).
    position_tolerance: Mapping[str, float]
    #: 허용치 안에 연속으로 머물러야 하는 시간(초).
    hold_sec: float
    #: 취소 ACK 대기 상한(초).
    cancel_ack_timeout_sec: float
    #: 표본 사이 최대 공백(초). 넘으면 연속 정지로 누적하지 않는다.
    max_sample_gap_sec: float
    provenance: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "policy_version 필요")
        if not self.position_tolerance:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "채널별 position_tolerance 필요")
        for ch, tol in self.position_tolerance.items():
            if tol <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"채널 {ch}의 허용치가 0 이하")
        for name in ("hold_sec", "cancel_ack_timeout_sec", "max_sample_gap_sec"):
            if getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}이 0 이하")
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")

    def tolerance_for(self, channel: str) -> float:
        """채널 허용치 조회. 없으면 다른 채널 값을 빌려오지 않고 차단한다."""
        if channel not in self.position_tolerance:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING,
                f"채널 {channel!r}의 정지 허용치가 없다 — 다른 채널 값을 재사용하지 않는다",
            )
        return self.position_tolerance[channel]


@dataclass(frozen=True)
class SttPolicy:
    """실시간 STT 운영 정책 (md/개발플랜.md 4단계).

    버퍼 제한(4-03), VAD 판정 시간(4-04), rolling window(4-05), final 확정
    조건(4-06), 신뢰도 기준(4-07), STOP 키워드(4-08)의 수치가 여기 모인다.
    코드에 기본값을 두지 않는다 — 없으면 PolicyError로 차단한다.
    """

    policy_version: str
    #: 세션이 보관하는 오디오 상한(초). 넘으면 긴 발화 오류로 종료한다.
    max_audio_sec: float
    #: 한 번 전사할 rolling window 길이(초).
    window_sec: float
    #: window를 다시 전사하는 간격(초, 오디오 시간 기준).
    window_stride_sec: float
    #: 이 시간만큼 무음이면 발화가 끝난 것으로 본다.
    silence_end_sec: float
    #: 이 시간만큼 음성이 이어지면 발화가 시작된 것으로 본다.
    speech_start_sec: float
    #: final 전사의 신뢰도가 이 값 미만이면 되묻는다.
    min_final_confidence: float
    #: partial에서 이 키워드가 보이면 계획 생성을 거치지 않고 정지로 직행한다.
    stop_keywords: tuple[str, ...]
    #: 운영 경로에서 한 번의 전사에 허용하는 벽시계 상한(초).
    #:
    #: `SttModelConfig.transcribe_timeout_sec`과 역할이 다르다. 그쪽은 백엔드가
    #: 자기 호출에 두는 한도(모델·장치에 따라 달라진다)이고, 이 값은 **세션이
    #: 기다려 주는 예산**이다. 실효 한도는 둘 중 작은 값이다.
    #:
    #: 이 값이 필요한 이유는 실측으로 확인했다. 220Hz 순음 2초를 small/cpu/int8로
    #: 전사하면 42~65초가 걸린다(RTF 21~32). 발화가 아닌 신호 때문에 세션이
    #: 그만큼 붙잡히면 안 된다.
    transcribe_deadline_sec: float
    #: 동시에 돌릴 수 있는 전사 수. 넘치면 대기열에 넣지 않고 바로 거절한다.
    max_concurrent_transcriptions: int
    provenance: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "policy_version 필요")
        for name in (
            "max_audio_sec", "window_sec", "window_stride_sec",
            "silence_end_sec", "speech_start_sec",
        ):
            if getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}이 0 이하")
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")
        if not 0.0 <= self.min_final_confidence <= 1.0:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "min_final_confidence가 0~1 범위를 벗어난다"
            )
        if "min_final_confidence" not in self.provenance:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "min_final_confidence의 근거가 없다")
        if self.window_stride_sec > self.window_sec:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                "window_stride_sec가 window_sec보다 크다 — 구간이 끊긴다",
            )
        if self.window_sec > self.max_audio_sec:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "window_sec가 max_audio_sec보다 크다"
            )
        if self.transcribe_deadline_sec <= 0:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "transcribe_deadline_sec가 0 이하"
            )
        if "transcribe_deadline_sec" not in self.provenance:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING, "transcribe_deadline_sec의 근거가 없다"
            )
        if self.max_concurrent_transcriptions < 1:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "max_concurrent_transcriptions가 1 미만"
            )
        if "max_concurrent_transcriptions" not in self.provenance:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING,
                "max_concurrent_transcriptions의 근거가 없다",
            )
        if not self.stop_keywords:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "stop_keywords가 비어 있다")
        if any(not k for k in self.stop_keywords):
            raise PolicyError(ReasonCode.CONFIG_INVALID, "빈 stop_keyword가 있다")


class SttVerification(str, Enum):
    """STT 구성의 검증 상태.

    측정하지 않은 구성을 "쓸 수 있는 구성"으로 적지 않기 위해 상태를 값으로
    남긴다. large-v3-turbo는 이 환경에서 아직 로딩조차 못 했으므로
    `CANDIDATE`다 — 보고서와 저장 기록에 그대로 따라간다.
    """

    #: 이 환경에서 실제로 로딩·전사까지 확인했다.
    VERIFIED = "verified"
    #: 아직 실행하지 않은 후보. 기본 Profile로 지정할 수 없다.
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class SttModelConfig:
    """STT 모델 실행 구성 (md/STT_모델_구성.md).

    모델 이름·장치·compute_type·언어·VAD 임계값을 코드에 두지 않는다
    (계획.md 27장). 전부 이 설정으로 주입받고, 없으면 PolicyError로 차단한다.

    `config_version`은 측정 결과를 어떤 구성에서 얻었는지 기록하기 위한 값이다.
    구성을 바꾸면 올려서, 저장된 transcript가 어떤 구성의 산출물인지 추적한다.
    """

    config_version: str
    #: Profile 식별자. 어떤 구성을 골랐는지 기록·비교의 키가 된다.
    profile_id: str
    #: 이 구성을 실제로 실행해 봤는지. 미검증 구성이 기본으로 쓰이는 것을 막는다.
    verification: SttVerification
    #: Whisper 모델 이름 또는 경로. 코드에 기본값을 두지 않는다.
    model_name: str
    #: "cpu" | "cuda" | "auto". 실행 환경이 정한다.
    device: str
    #: "int8" | "float16" | "float32" 등 백엔드가 받는 값.
    compute_type: str
    #: 전사 언어 코드. 자동 감지에 맡기지 않는다 — 현장 언어가 고정이다.
    language: str
    #: 입력 PCM 샘플레이트(Hz). 모델이 기대하는 값과 다르면 거부한다.
    sample_rate_hz: int
    #: Silero VAD 음성 판정 임계값(0~1).
    vad_threshold: float
    #: 모델 로딩 상한(초). 넘기면 실패로 반환한다.
    load_timeout_sec: float
    #: 한 번의 전사 상한(초). 넘기면 실패로 반환한다.
    transcribe_timeout_sec: float
    #: 모델 캐시 경로. 프로젝트 밖이어야 한다(None이면 백엔드 기본값).
    cache_dir: str | None = None
    #: 평가용 원본 음성을 남길지. 일반 실행 기록과 분리해서 보관한다.
    retain_eval_audio: bool = False
    #: 평가용 음성을 남길 경로. `retain_eval_audio`가 True면 필요하다.
    eval_audio_dir: str | None = None
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "config_version", "profile_id", "model_name", "device",
            "compute_type", "language",
        ):
            if not getattr(self, name):
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name} 필요")
        if not isinstance(self.verification, SttVerification):
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                f"verification이 SttVerification이 아니다: {self.verification!r}",
            )
        if self.device not in ("cpu", "cuda", "auto"):
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, f"지원하지 않는 device: {self.device!r}"
            )
        if self.sample_rate_hz <= 0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "sample_rate_hz가 0 이하")
        if not 0.0 < self.vad_threshold < 1.0:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "vad_threshold가 0~1 사이가 아니다"
            )
        for name in ("load_timeout_sec", "transcribe_timeout_sec"):
            if getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}이 0 이하")
        if self.cache_dir is not None and not self.cache_dir:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "cache_dir이 빈 문자열이다")
        if self.retain_eval_audio and not self.eval_audio_dir:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING,
                "retain_eval_audio가 켜져 있으면 eval_audio_dir이 필요하다",
            )
        for name in ("model_name", "device", "compute_type", "vad_threshold"):
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")


@dataclass(frozen=True)
class SttProfileCatalog:
    """선택 가능한 STT 구성 Profile 모음 (md/STT_모델_구성.md).

    모델·장치·compute_type을 Profile로 골라 쓴다. 코드가 특정 모델 이름을 갖지
    않고, 실행 환경이 `default_profile_id`로 자기 환경에서 실제 돌아가는 구성을
    가리킨다.

    **불변식: 기본 Profile은 VERIFIED여야 한다.** 실행해 본 적 없는 구성이
    기본값이 되면, 문서와 실제 동작이 갈라진다. 이 환경이 그 사례다 —
    large-v3-turbo는 가용 RAM 2.4GiB / GPU 677MiB에서 로딩을 시도조차 못 했다.
    그런 구성을 기본으로 적는 것을 계약으로 막는다.
    """

    catalog_version: str
    default_profile_id: str
    profiles: Mapping[str, SttModelConfig]

    def __post_init__(self) -> None:
        if not self.catalog_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "catalog_version 필요")
        if not self.profiles:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "profiles가 비어 있다")
        for profile_id, config in self.profiles.items():
            if profile_id != config.profile_id:
                raise PolicyError(
                    ReasonCode.CONFIG_INVALID,
                    f"Profile 키 {profile_id!r}와 profile_id {config.profile_id!r}가 다르다",
                )
        if self.default_profile_id not in self.profiles:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING,
                f"기본 Profile {self.default_profile_id!r}가 목록에 없다",
            )
        default = self.profiles[self.default_profile_id]
        if default.verification is not SttVerification.VERIFIED:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                f"기본 Profile {self.default_profile_id!r}가 미검증({default.verification.value})"
                " — 실행을 확인한 구성만 기본이 될 수 있다",
            )

    def default(self) -> SttModelConfig:
        return self.profiles[self.default_profile_id]

    def get(self, profile_id: str) -> SttModelConfig:
        try:
            return self.profiles[profile_id]
        except KeyError:
            raise PolicyError(
                ReasonCode.CONFIG_MISSING, f"알 수 없는 Profile: {profile_id!r}"
            ) from None

    def verified_ids(self) -> tuple[str, ...]:
        return tuple(
            pid for pid, c in self.profiles.items()
            if c.verification is SttVerification.VERIFIED
        )

    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            pid for pid, c in self.profiles.items()
            if c.verification is SttVerification.CANDIDATE
        )


@dataclass(frozen=True)
class PlanningPolicy:
    """계획 생성(LLM) 호출 정책 (md/개발플랜.md 5-01).

    재시도 횟수·제한시간·원본 보존을 코드에 두지 않는다. 같은 호출을 무제한
    반복하지 않기 위해 `max_attempts`가 필수다.

    `retryable_reasons`에 없는 실패는 재시도하지 않는다. 형식이 깨진 출력은
    다시 물어볼 여지가 있지만, 미등록 리소스나 미지원 스킬은 같은 입력에서
    같은 결과가 나오기 쉬우므로 정책이 정하게 한다.
    """

    policy_version: str
    #: 한 요청에 허용하는 총 시도 수. 1이면 재시도하지 않는다.
    max_attempts: int
    #: 모델 호출 1회의 벽시계 상한(초).
    request_timeout_sec: float
    #: 이 이유 코드일 때만 재시도한다.
    retryable_reasons: tuple[ReasonCode, ...]
    #: 프롬프트·출력 원본을 기록에 남길지. 비밀정보 제거 후에만 남긴다.
    store_payloads: bool
    #: 남길 원본의 최대 길이(문자). 넘으면 잘라내고 잘렸음을 표시한다.
    payload_max_chars: int
    #: 원본 보존 기간(일). 0이면 보존하지 않는다.
    payload_retention_days: int
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise PolicyError(ReasonCode.CONFIG_MISSING, "policy_version 필요")
        if self.max_attempts < 1:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "max_attempts가 1 미만")
        if self.request_timeout_sec <= 0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "request_timeout_sec가 0 이하")
        for reason in self.retryable_reasons:
            if not isinstance(reason, ReasonCode):
                raise PolicyError(
                    ReasonCode.CONFIG_INVALID,
                    f"retryable_reasons에 ReasonCode가 아닌 값: {reason!r}",
                )
        if self.store_payloads:
            if self.payload_max_chars <= 0:
                raise PolicyError(
                    ReasonCode.CONFIG_INVALID,
                    "store_payloads가 켜져 있으면 payload_max_chars가 필요하다",
                )
            if self.payload_retention_days <= 0:
                raise PolicyError(
                    ReasonCode.CONFIG_INVALID,
                    "store_payloads가 켜져 있으면 보존 기간이 필요하다",
                )
        for name in ("max_attempts", "request_timeout_sec", "retryable_reasons"):
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")

    def should_retry(self, reason: ReasonCode | None, attempt_no: int) -> bool:
        """이 실패로 한 번 더 시도할지. 성공(None)이면 재시도하지 않는다."""
        if reason is None:
            return False
        if attempt_no >= self.max_attempts:
            return False
        return reason in self.retryable_reasons


class ThinkingMode(str, Enum):
    """모델의 사고 과정 노출 방식.

    Qwen3처럼 thinking을 켜고 끌 수 있는 모델에서, 계획 생성은 `OFF`로 쓴다.
    사고 과정이 응답 본문에 섞이면 JSON 파싱이 깨지고, 섞인 것을 코드가
    벗겨내기 시작하면 모델 출력 보정이 된다.
    """

    #: thinking을 끈다. 계획 생성의 기본.
    OFF = "off"
    #: thinking을 켜고, 사고 과정은 별도 채널로만 받는다.
    SEPARATE_CHANNEL = "separate_channel"


class StructuredOutputMode(str, Enum):
    """구조화 출력 요청 방식."""

    #: OpenAI 호환 `response_format: {"type": "json_schema", ...}`.
    RESPONSE_FORMAT_JSON_SCHEMA = "response_format_json_schema"
    #: 구조화 출력을 쓰지 않고 프롬프트 지시에만 의존한다.
    NONE = "none"


@dataclass(frozen=True)
class LlmProviderConfig:
    """LLM 공급자 접속 설정 (md/개발플랜.md 5-02).

    서버 주소·모델 ID·timeout·max_tokens·sampling을 코드에 두지 않는다.

    **API Key를 담지 않는다.** 대신 키가 들어 있는 환경변수 **이름**만 갖는다
    (`api_key_env`). 설정 객체가 로그·기록·예외에 실려도 키가 새지 않는다.
    Adapter가 호출 시점에 환경변수를 읽어 헤더를 만들고, 그 헤더는 어디에도
    기록하지 않는다.
    """

    config_version: str
    #: OpenAI 호환 API의 기준 주소. 형태는 `http(s)://호스트:포트/v1`이다.
    #: 주소를 코드에 두지 않으므로 여기에 구체적 예시도 쓰지 않는다.
    base_url: str
    #: 설정된 모델 ID. 서버의 /v1/models 응답과 다르면 호출을 거부한다.
    model_id: str
    #: 양자화 방식. 서버 기동 인자에서 온다 — 기록에 남긴다.
    quantization: str
    #: 서버가 허용하는 컨텍스트 길이. /v1/models 응답과 대조한다.
    max_model_len: int
    #: 한 호출의 최대 생성 토큰.
    max_tokens: int
    #: HTTP 호출 제한시간(초).
    request_timeout_sec: float
    #: sampling. 계획 생성은 재현성이 중요하므로 낮게 둔다.
    temperature: float
    top_p: float
    seed: int | None
    thinking_mode: ThinkingMode
    structured_output: StructuredOutputMode
    #: API Key가 들어 있는 환경변수 이름. 키 자체를 담지 않는다.
    api_key_env: str | None = None
    #: 호출 전 /v1/models로 모델 ID를 확인할지.
    verify_model_id: bool = True
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("config_version", "base_url", "model_id", "quantization"):
            if not getattr(self, name):
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name} 필요")
        if not self.base_url.startswith(("http://", "https://")):
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, f"base_url이 http(s)가 아니다: {self.base_url!r}"
            )
        for name in ("max_model_len", "max_tokens"):
            if getattr(self, name) <= 0:
                raise PolicyError(ReasonCode.CONFIG_INVALID, f"{name}이 0 이하")
        if self.max_tokens > self.max_model_len:
            raise PolicyError(
                ReasonCode.CONFIG_INVALID,
                f"max_tokens({self.max_tokens})가 max_model_len"
                f"({self.max_model_len})보다 크다",
            )
        if self.request_timeout_sec <= 0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "request_timeout_sec가 0 이하")
        if not 0.0 <= self.temperature <= 2.0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "temperature가 0~2를 벗어난다")
        if not 0.0 < self.top_p <= 1.0:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "top_p가 0~1 범위를 벗어난다")
        if not isinstance(self.thinking_mode, ThinkingMode):
            raise PolicyError(ReasonCode.CONFIG_INVALID, "thinking_mode가 열거형이 아니다")
        if not isinstance(self.structured_output, StructuredOutputMode):
            raise PolicyError(
                ReasonCode.CONFIG_INVALID, "structured_output이 열거형이 아니다"
            )
        if self.api_key_env is not None and not self.api_key_env:
            raise PolicyError(ReasonCode.CONFIG_INVALID, "api_key_env가 빈 문자열이다")
        # 키를 설정에 직접 넣는 실수를 막는다.
        for name in ("base_url", "model_id", "quantization"):
            value = getattr(self, name).lower()
            for banned in ("sk-", "bearer ", "api_key="):
                if banned in value:
                    raise PolicyError(
                        ReasonCode.CONFIG_INVALID,
                        f"{name}에 자격정보로 보이는 값이 있다 — 키는 환경변수로 준다",
                    )
        for name in ("model_id", "max_tokens", "request_timeout_sec", "temperature",
                     "thinking_mode", "structured_output"):
            if name not in self.provenance:
                raise PolicyError(ReasonCode.CONFIG_MISSING, f"{name}의 근거가 없다")

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/models"

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"
