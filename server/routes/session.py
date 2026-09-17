"""공통 라우트 — 세션 식별과 설정 (md/개발플랜.md 3-04).

모든 라우트가 세션을 명시적으로 요구하므로, 세션 발급·이어받기·종료와 설정·
상태 조회는 공통 경계에 둔다.

- `POST /v1/sessions` — 세션과 클라이언트(탭) 식별자를 **서버가** 만든다.
- `POST /v1/sessions/{id}/clients` — 탭이 세션을 이어받는다. 복제된 탭은 살아
  있는 클라이언트가 있으면 `session.client_conflict`로 걸러진다.
- `DELETE /v1/sessions/{id}` — 세션 종료.
- `GET /health`, `GET /v1/config` — 기능 사용 가능 여부와 설정. **모델 서버
  주소·인증값을 담지 않는다.**
"""

from __future__ import annotations

from server.routes.common import RouteContext, Response, json_response

PATHS: tuple[str, ...] = ("/v1/sessions", "/v1/sessions/", "/health", "/v1/config")


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    api = ctx.api

    if method == "POST" and path == "/v1/sessions":
        payload = await ctx.read_body(receive)
        return json_response(
            api.create_session(origin=str(payload.get("origin", "")))
        )

    if path.startswith("/v1/sessions/"):
        rest = path[len("/v1/sessions/"):]
        if method == "POST" and rest.endswith("/clients"):
            payload = await ctx.read_body(receive)
            return json_response(api.claim_session(
                session_id=rest[: -len("/clients")],
                client_id=payload.get("client_id") or None,
            ))
        if method == "DELETE":
            return json_response(api.end_session(rest))
        return None

    if method == "GET" and path == "/health":
        return json_response(api.health())

    if method == "GET" and path == "/v1/config":
        return json_response(ctx.runtime.config_payload())

    return None
