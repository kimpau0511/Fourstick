"""기록에 남기기 전 비밀정보 제거 (md/개발플랜.md 5-01).

API Key·토큰·인증 헤더·환경변수를 로그나 DB에 남기지 않는다. 프롬프트 원본을
보존하면 이런 값이 섞여 들어올 수 있으므로, **저장 경로에 들어가기 직전 한
곳에서** 지운다.

방침:
- 키 이름이 의심스러우면 값을 통째로 `[REDACTED]`로 바꾼다. 일부만 가리지
  않는다 — 앞 네 자리만 남겨도 식별에 쓸 수 있다.
- 값 안에 있는 `Bearer <토큰>`, `sk-...` 같은 패턴도 지운다.
- 환경변수 전체(`os.environ`)를 payload에 넣는 경로를 아예 만들지 않는다.
  이 모듈은 넘어온 것만 지울 수 있으므로, 호출자가 환경변수를 담지 않는 것이
  1차 방어다. 그래서 `environ`·`env` 키도 의심 목록에 넣었다.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

#: 이 조각이 키 이름에 들어 있으면 값을 지운다(대소문자 무시).
SECRET_KEY_HINTS: tuple[str, ...] = (
    "api_key", "apikey", "secret", "token", "password", "passwd", "credential",
    "authorization", "auth", "cookie", "session_key", "private_key",
    "bearer", "environ", "env_vars", "envvars",
)

#: 값 안에서 지울 패턴.
_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"sk-[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+"),
)


def looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in SECRET_KEY_HINTS)


def redact_text(text: str) -> str:
    for pattern in _VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any) -> Any:
    """dict·list·문자열을 재귀적으로 훑어 비밀정보를 지운다."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if looks_secret(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """길이를 제한한다. 잘랐는지 함께 돌려준다 — 조용히 자르지 않는다."""
    if limit <= 0 or len(text) <= limit:
        return text, False
    return text[:limit], True
