"""OpenAI 호환 HTTP 경계 (md/개발플랜.md 5-02).

**계획 생성 로직이 없다.** HTTP 요청·응답과 오류 변환만 한다. 프롬프트는
`planning/prompt.py`가 만들고, 출력 검증은 `planning/output_parser.py`가 한다.

의존성을 늘리지 않는다. vLLM의 OpenAI 호환 API는 평범한 HTTP+JSON이므로
표준 라이브러리(`urllib.request`)로 호출한다. SDK를 넣으면 버전 고정·업그레이드
부담이 생기고, 우리가 쓰는 것은 두 엔드포인트뿐이다.

자격정보 취급:
- 설정은 API Key를 담지 않는다. 환경변수 **이름**만 갖는다.
- 헤더는 호출 직전에 만들고 어디에도 기록하지 않는다.
- 예외 메시지에 요청 헤더를 넣지 않는다. 응답 본문도 길이를 제한해 담는다.

구조화 출력은 OpenAI 표준인 `response_format: {"type": "json_schema", ...}`를
쓴다. vLLM 0.28.0에서 동작을 확인했다. 폐기 예정 옵션(`guided_json` 등)을 새
코드에 고정하지 않는다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from core.policy import LlmProviderConfig, StructuredOutputMode, ThinkingMode
from core.reason_codes import ReasonCode

#: 예외·기록에 담는 응답 본문의 최대 길이.
DETAIL_LIMIT = 300


class TransportError(Exception):
    """HTTP 경계 실패. 이유 코드를 함께 담는다.

    메시지에 요청 헤더를 넣지 않는다 — 자격정보가 예외를 타고 로그로 나간다.
    """

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class ServerInfo:
    """서버가 실제로 제공하는 것. 설정과 대조하는 근거다."""

    model_ids: tuple[str, ...]
    #: /v1/models가 알려주는 컨텍스트 길이. 없으면 None이다.
    max_model_len: int | None = None
    #: /version 응답. 없으면 빈 문자열이다.
    server_version: str = ""

    def serves(self, model_id: str) -> bool:
        return model_id in self.model_ids


@dataclass(frozen=True)
class Completion:
    """한 번의 chat 호출 결과."""

    #: 모델이 본문 채널로 보낸 내용. 계획 JSON이 여기 와야 한다.
    content: str
    #: 별도 채널로 온 사고 과정. 본문에 섞이지 않았는지 보는 근거다.
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    latency_sec: float
    #: 응답이 알려주는 모델 ID. 설정과 다르면 호출자가 거부한다.
    served_model_id: str = ""
    #: 서버 지문(vllm-0.28.0-...). 있으면 기록에 남긴다.
    system_fingerprint: str = ""


#: 주입 가능한 전송 함수. (url, payload|None, timeout) -> 응답 dict.
Transport = Callable[[str, "dict[str, Any] | None", float, Mapping[str, str]], dict]


def _http_json(
    url: str, payload: dict[str, Any] | None, timeout_sec: float,
    headers: Mapping[str, str],
) -> dict:
    """표준 라이브러리 HTTP. 실패를 TransportError로 바꾼다."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 본문만, 길이를 제한해 담는다. 헤더는 담지 않는다.
        try:
            detail = exc.read().decode("utf-8")[:DETAIL_LIMIT]
        except Exception:  # noqa: BLE001
            detail = ""
        raise TransportError(
            ReasonCode.PLAN_LLM_UNAVAILABLE,
            f"HTTP {exc.code}: {detail}",
        ) from None
    except TimeoutError as exc:
        raise TransportError(
            ReasonCode.PLAN_LLM_TIMEOUT, f"응답이 {timeout_sec}s를 넘겼다"
        ) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise TransportError(
                ReasonCode.PLAN_LLM_TIMEOUT, f"응답이 {timeout_sec}s를 넘겼다"
            ) from None
        raise TransportError(
            ReasonCode.PLAN_LLM_UNAVAILABLE, f"연결 실패: {reason}"
        ) from None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise TransportError(
            ReasonCode.PLAN_LLM_UNAVAILABLE,
            f"응답이 JSON이 아니다: {body[:DETAIL_LIMIT]}",
        ) from None


@dataclass
class OpenAiCompatClient:
    """OpenAI 호환 서버 하나. 계획을 모른다."""

    config: LlmProviderConfig
    #: 테스트가 전송을 바꿔 끼울 수 있게 주입 가능하게 둔다.
    transport: Transport = _http_json
    #: 환경변수 조회. 테스트가 바꿔 끼운다.
    env: Callable[[str], str | None] = os.environ.get
    clock: Callable[[], float] = time.monotonic

    # ── 자격정보 ────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        """호출 직전에 만든다. 돌려준 값을 기록하지 않는다."""
        name = self.config.api_key_env
        if not name:
            return {}
        key = self.env(name)
        if not key:
            raise TransportError(
                ReasonCode.CONFIG_MISSING,
                f"환경변수 {name}에 API Key가 없다",   # 이름만, 값은 담지 않는다
            )
        return {"Authorization": f"Bearer {key}"}

    # ── 호출 ────────────────────────────────────────────────────────────
    def _invoke(self, url: str, payload: dict[str, Any] | None) -> dict:
        """전송을 부르고 오류를 이유 코드로 바꾼다.

        변환이 **클라이언트에** 있어야 한다. 전송 함수에만 두면 전송을 바꿔
        끼울 때마다 매핑을 다시 구현해야 하고, 구현마다 이유 코드가 달라진다.
        """
        try:
            return self.transport(
                url, payload, self.config.request_timeout_sec, self._headers()
            )
        except TransportError:
            raise
        except TimeoutError:
            raise TransportError(
                ReasonCode.PLAN_LLM_TIMEOUT,
                f"응답이 {self.config.request_timeout_sec}s를 넘겼다",
            ) from None
        except urllib.error.HTTPError as exc:
            raise TransportError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, f"HTTP {exc.code}"
            ) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise TransportError(
                    ReasonCode.PLAN_LLM_TIMEOUT,
                    f"응답이 {self.config.request_timeout_sec}s를 넘겼다",
                ) from None
            raise TransportError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, f"연결 실패: {reason}"
            ) from None
        except Exception as exc:  # noqa: BLE001 — 전송 구현의 예상 못한 실패
            raise TransportError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, f"전송 실패: {type(exc).__name__}"
            ) from None

    # ── 조회 ────────────────────────────────────────────────────────────
    def server_info(self) -> ServerInfo:
        """서버가 제공하는 모델을 읽는다. 호출 전 대조의 근거다."""
        body = self._invoke(self.config.models_url, None)
        rows = body.get("data") or []
        if not isinstance(rows, list):
            raise TransportError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, "/models 응답에 data 배열이 없다"
            )
        ids = tuple(str(r.get("id", "")) for r in rows if isinstance(r, dict))
        lengths = [
            r.get("max_model_len") for r in rows
            if isinstance(r, dict) and isinstance(r.get("max_model_len"), int)
        ]
        version = ""
        try:
            version_body = self._invoke(
                f"{self.config.base_url.rstrip('/').rsplit('/v1', 1)[0]}/version",
                None,
            )
            version = str(version_body.get("version", ""))
        except TransportError:
            version = ""   # /version이 없는 서버도 있다. 없으면 빈 문자열이다.
        return ServerInfo(
            model_ids=ids,
            max_model_len=lengths[0] if lengths else None,
            server_version=version,
        )

    # ── 생성 ────────────────────────────────────────────────────────────
    def chat(
        self, *, system: str, user: str, json_schema: dict | None, schema_name: str,
    ) -> Completion:
        """chat/completions 한 번. 구조화 출력은 설정이 정한다."""
        payload: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
        }
        if self.config.seed is not None:
            payload["seed"] = self.config.seed
        if (
            self.config.structured_output
            is StructuredOutputMode.RESPONSE_FORMAT_JSON_SCHEMA
            and json_schema is not None
        ):
            # OpenAI 표준 형식. 폐기 예정 옵션을 쓰지 않는다.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": True,
                },
            }
        if self.config.thinking_mode is ThinkingMode.OFF:
            # Qwen3 계열의 non-thinking 전환. 템플릿이 무시하면 응답의
            # reasoning 필드와 본문 검사로 한 번 더 걸러낸다.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        started = self.clock()
        body = self._invoke(self.config.chat_url, payload)
        latency = self.clock() - started

        choices = body.get("choices") or []
        if not choices:
            raise TransportError(
                ReasonCode.PLAN_LLM_UNAVAILABLE, "응답에 choices가 없다"
            )
        message = choices[0].get("message") or {}
        usage = body.get("usage") or {}
        return Completion(
            content=str(message.get("content") or ""),
            reasoning=str(
                message.get("reasoning") or message.get("reasoning_content") or ""
            ),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            finish_reason=str(choices[0].get("finish_reason") or ""),
            latency_sec=latency,
            served_model_id=str(body.get("model") or ""),
            system_fingerprint=str(body.get("system_fingerprint") or ""),
        )
