"""HTTP 요청 크기 제한(2026-10-08 리뷰 8번) — 본문을 다 받은 뒤가 아니라 읽는 도중에 끊는다."""

from __future__ import annotations

import json

from core.reason_codes import ReasonCode
from server.api import ApiError


#: HTTP 요청 본문 상한(바이트). 모든 API 본문은 작은 JSON이다 — 가장 큰 것(명령 200자 + 음성 기록)도 1 KB 남짓이다.
#: 본문을 다 받은 뒤가 아니라 **읽는 도중에** 넘으면 끊는다(2026-10-08 리뷰 8번).
MAX_BODY_BYTES = 64 * 1024


def limited_receive(receive, headers):
    """Content-Length가 상한을 넘으면 읽기 전에, 조각을 받는 중 누적이 넘으면 그 자리에서 413으로 끊는다."""
    declared = next((v for k, v in headers if k.lower() == b"content-length"), None)
    try:
        declared_len = int(declared) if declared is not None else None
    except ValueError:
        declared_len = None
    total = 0

    async def limited():
        nonlocal total
        if declared_len is not None and declared_len > MAX_BODY_BYTES:
            raise ApiError(413, ReasonCode.SESSION_REQUEST_TOO_LARGE,
                           f"요청 본문이 너무 큽니다({declared_len}바이트, 상한 {MAX_BODY_BYTES}바이트)")
        message = await receive()
        if message.get("type") == "http.request":
            total += len(message.get("body", b""))
            if total > MAX_BODY_BYTES:
                raise ApiError(413, ReasonCode.SESSION_REQUEST_TOO_LARGE,
                               f"요청 본문이 너무 큽니다(상한 {MAX_BODY_BYTES}바이트)")
        return message

    return limited


async def read_json_body(receive) -> dict:
    """본문을 읽어 JSON 객체로. `limited_receive`로 감싼 receive를 받는다."""
    parts: list[bytes] = []
    while True:
        message = await receive()          # `_limited_receive`가 상한을 넘으면 읽는 도중에 413으로 끊는다
        if message["type"] == "http.disconnect":
            raise ApiError(400, None, "요청이 끊겼다")
        parts.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    chunks = b"".join(parts)
    if not chunks:
        return {}
    try:
        payload = json.loads(chunks)
    except json.JSONDecodeError as exc:
        raise ApiError(400, None, f"JSON이 아니다: {exc}") from None
    if not isinstance(payload, dict):
        raise ApiError(400, None, "객체가 아니다")
    return payload
