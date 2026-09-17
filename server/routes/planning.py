"""planning 라우트 (md/개발플랜.md 3-04).

요청·계획 생성·계획 조회. **계획 생성은 실행하지 않는다.**

모든 경로가 `session_id`를 명시적으로 요구하고, 조회는 `request_id`·`plan_id`를
함께 받는다 — 서버가 '현재 계획'을 추정하지 않는다.
"""

from __future__ import annotations

from server.routes.common import (
    RouteContext,
    Response,
    body_field,
    json_response,
    query_param,
)

PATHS: tuple[str, ...] = ("/v1/plan",)


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    """맡은 경로면 응답을, 아니면 None을 돌려준다."""
    if path != "/v1/plan":
        return None

    if method == "GET":
        return json_response(ctx.api.get_plan(
            session_id=query_param(query, "session_id"),
            request_id=query_param(query, "request_id"),
            plan_id=query_param(query, "plan_id"),
        ))

    if method == "POST":
        payload = await ctx.read_body(receive)
        result = ctx.api.create_plan(
            session_id=body_field(payload, "session_id"),
            utterance=str(payload.get("utterance", "")),
            stt_inference_id=payload.get("stt_inference_id"),
        )
        # 계획을 만들지 못한 경우도 이유 코드와 함께 돌려준다(422).
        return json_response(result, 200 if result.get("ok") else 422)

    return None
