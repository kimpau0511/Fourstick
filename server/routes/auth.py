"""로그인 라우트 — `/v1/auth/*` (2026-10-06 결정, 화면 약속은 `dashboard2/src/auth.js` 맨 위).

| 경로 | 하는 일 |
|---|---|
| `GET /v1/auth/me` | 로그인 세션이면 200 {user}, 아니면 401(`session.unauthenticated`·`session.expired`) |
| `POST /v1/auth/google` | {code} → 구글 교환·등록 계정 확인 → 세션 쿠키 + 200 {user}. 거절은 403/502/503 |
| `POST /v1/auth/logout` | 세션 삭제 + 쿠키 지움. 세션이 없어도 200 |
| `GET /v1/auth/accounts` | 최고 관리자만. 사용 중 계정 목록 {accounts} |
| `POST /v1/auth/accounts` | 최고 관리자만. {emails:[...]} 여러 개 등록 → {results, accounts} (항목별 성공·거절) |
| `POST /v1/auth/accounts/delete` | 최고 관리자만. {emails:[...]} 여러 개 삭제(사용 중지) → {results, accounts} |

다른 라우트와 달리 **쿠키 토큰과 AuthService를 받는다** — 그래서 `HTTP_ROUTES` 순회가 아니라 `server/asgi.py`가
경로를 보고 직접 부른다. 판정은 `server/auth.py`가 하고 여기는 응답 모양만 만든다.
"""

from __future__ import annotations

import asyncio

from core.reason_codes import ReasonCode
from server.auth import AuthService, LoginError, account_row, cookie_token, header, origin_ok, public_user
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

    if path in ("/v1/auth/accounts", "/v1/auth/accounts/delete"):
        return await _accounts(auth, ctx, method, path, receive, headers, token)

    return None


async def _accounts(auth: AuthService, ctx: RouteContext, method: str, path: str, receive, headers: list, token) -> Response | None:
    """계정 관리. 이 경로는 출입 검사 전에 불리므로 로그인·최고 관리자 확인을 여기서 한다."""
    payload: dict = {}
    if method == "POST":
        # 계정을 바꾸는 요청은 출처를 확인한다 — 다른 사이트가 관리자 브라우저로 계정을 넣지 못하게.
        # 로그인과 달리 Origin이 없으면 거절한다(브라우저 fetch POST는 항상 Origin을 붙인다).
        if not header(headers, b"x-requested-with") or not header(headers, b"origin") or not origin_ok(headers, auth.allowed_origins):
            return 403, [], json_bytes({"error": "요청의 출처를 확인하지 못했다",
                                        "reason_code": ReasonCode.SESSION_LOGIN_FAILED.value})
        payload = await ctx.read_body(receive)
    # 본문을 다 받은 **뒤에** 권한을 본다 — 받는 사이 세션 만료·관리자 해제가 생겨도 변경이 통과하지 않게.
    admin, status, reason = auth.admin_of(token)
    if admin is None:
        message = "최고 관리자만 할 수 있다" if status == 403 else "로그인이 필요하다"
        return status, [], json_bytes({"error": message, "reason_code": reason.value})
    if method == "GET" and path == "/v1/auth/accounts":
        return 200, [], json_bytes({"accounts": [account_row(a) for a in auth.active_accounts()]})
    if method != "POST":
        return None
    emails = payload.get("emails")
    if not isinstance(emails, list) or not emails or not all(isinstance(e, str) for e in emails):
        return 400, [], json_bytes({"error": "emails는 이메일 문자열 목록이어야 한다",
                                    "reason_code": ReasonCode.SESSION_ACCOUNT_EMAIL_INVALID.value})
    change = auth.add_accounts if path == "/v1/auth/accounts" else auth.delete_accounts
    # 계정 수만큼 DB를 쓰므로 이벤트 루프 밖에서 한다 — 많이 붙여 넣어도 정지 요청이 늦어지면 안 된다.
    results = await asyncio.to_thread(change, emails)
    return 200, [], json_bytes({"results": results, "accounts": [account_row(a) for a in auth.active_accounts()]})
