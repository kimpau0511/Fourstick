"""로그인 라우트 — `/v1/auth/*` (2026-10-06 결정, 화면 약속은 `dashboard2/src/auth.js` 맨 위).

| 경로 | 하는 일 |
|---|---|
| `GET /v1/auth/me` | 로그인 세션이면 200 {user}, 아니면 401(`session.unauthenticated`·`session.expired`) |
| `POST /v1/auth/google` | {code} → 구글 교환·등록 계정 확인 → 세션 쿠키 + 200 {user}. 거절은 403/502/503 |
| `POST /v1/auth/logout` | 세션 삭제 + 쿠키 지움. 세션이 없어도 200 |

다른 라우트와 달리 **쿠키 토큰과 AuthService를 받는다** — 그래서 `HTTP_ROUTES` 순회가 아니라 `server/asgi.py`가
경로를 보고 직접 부른다. 판정은 `server/auth.py`가 하고 여기는 응답 모양만 만든다.
"""

from __future__ import annotations

import asyncio

from core.reason_codes import ReasonCode
from server.auth import AuthService, LoginError, cookie_token, header, origin_ok, public_user
from server.routes.common import JSON_HEADERS, Response, RouteContext, json_bytes

PREFIX = "/v1/auth/"


async def handle(
    auth: AuthService, ctx: RouteContext, method: str, path: str, receive, headers: list,
) -> Response | None:
    token = cookie_token(headers)
    if method == "GET" and path == "/v1/auth/me":
        account, reason = auth.resolve(token)
        if account is None:
            return 401, [], json_bytes({"error": "로그인이 필요하다", "reason_code": reason.value})
        return 200, [], json_bytes({"user": public_user(account)})

    if method == "POST" and path == "/v1/auth/google":
        if not header(headers, b"x-requested-with") or not origin_ok(headers, auth.allowed_origins):
            return 403, [], json_bytes({"error": "로그인 요청의 출처를 확인하지 못했다",
                                        "reason_code": ReasonCode.SESSION_LOGIN_FAILED.value})
        payload = await ctx.read_body(receive)
        try:
            account, new_token = await asyncio.to_thread(
                auth.login, str(payload.get("code") or ""), origin=header(headers, b"origin"),
            )
        except LoginError as exc:
            return exc.status, [], json_bytes(exc.to_dict())
        if token:
            auth.logout(token)  # 같은 브라우저의 이전 세션은 남기지 않는다
        return 200, [*JSON_HEADERS, auth.set_cookie(new_token, origin=header(headers, b"origin"))], json_bytes({"user": public_user(account)})

    if method == "POST" and path == "/v1/auth/logout":
        auth.logout(token)
        return 200, [*JSON_HEADERS, auth.clear_cookie(origin=header(headers, b"origin"))], json_bytes({"ok": True})

    return None
