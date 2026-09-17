"""execution 라우트 (md/개발플랜.md 3-04).

검증 결과 조회·승인·실행·취소·전체 정지·상태 복원·상태 이벤트 WebSocket.

지키는 것(라우트 분리로 바뀌지 않는다):

- 승인은 화면의 `plan_hash`와 로봇·Profile 값을 함께 받아 대조한다(6-06·7-06).
- 실행은 `session_id`·`request_id`·`plan_id`를 명시적으로 받는다.
- 실행 상태는 `execution_id`로 조회한다.
- 전체 정지는 세션을 요구하지 않고(만료된 세션도 가능) 활성 실행 전체에
  적용되며, 이벤트는 전체 범위(건수만)와 세션 범위(자기 실행 목록)로 나뉜다.
- 특정 실행 취소는 소유 세션의 그 실행만 멈춘다.
"""

from __future__ import annotations

import asyncio
import json

from core.reason_codes import ReasonCode
from server.api import ApiError
from server.routes.common import (
    RouteContext,
    Response,
    body_field,
    json_response,
    query_param,
)

PATHS: tuple[str, ...] = (
    "/v1/decision", "/v1/execute", "/v1/executions/", "/v1/stop", "/v1/state",
)


async def handle(
    ctx: RouteContext, method: str, path: str, receive, query: dict[str, str],
) -> Response | None:
    api = ctx.api

    if method == "GET" and path == "/v1/state":
        return json_response(api.state(
            session_id=query_param(query, "session_id")
        ))

    if method == "POST" and path == "/v1/decision":
        payload = await ctx.read_body(receive)
        decision = str(payload.get("decision", ""))
        if decision not in ("approve", "reject"):
            raise ApiError(400, None, "decision은 approve 또는 reject다")
        return json_response(api.decide(
            session_id=body_field(payload, "session_id"),
            request_id=body_field(payload, "request_id"),
            plan_id=body_field(payload, "plan_id"),
            plan_hash=body_field(payload, "plan_hash"),
            approve=(decision == "approve"),
            note=str(payload.get("note", "")),
            # 선택 항목. 보내면 계획의 값과 대조한다(6-06·7-06).
            robot_id=payload.get("robot_id"),
            profile_id=payload.get("profile_id"),
            profile_version=payload.get("profile_version"),
        ))

    if method == "POST" and path == "/v1/execute":
        payload = await ctx.read_body(receive)
        # 실행은 어댑터를 블로킹 호출한다. 이벤트 루프를 막지 않게 스레드로 보낸다.
        result = await asyncio.to_thread(
            api.execute,
            session_id=body_field(payload, "session_id"),
            request_id=body_field(payload, "request_id"),
            plan_id=body_field(payload, "plan_id"),
            approval_id=payload.get("approval_id"),
        )
        return json_response(result, 200 if result.get("ok") else 409)

    if path.startswith("/v1/executions/"):
        rest = path[len("/v1/executions/"):]
        if method == "GET":
            return json_response(api.execution_status(
                session_id=query_param(query, "session_id"),
                execution_id=rest,
            ))
        if method == "POST" and rest.endswith("/cancel"):
            payload = await ctx.read_body(receive)
            return json_response(api.cancel_execution(
                session_id=body_field(payload, "session_id"),
                execution_id=rest[: -len("/cancel")],
            ))
        return None

    if method == "POST" and path == "/v1/stop":
        # 전체 정지. 세션 격리로 막지 않는다 — session_id는 있으면 기록만 한다.
        payload = await ctx.read_body(receive)
        result = await asyncio.to_thread(
            api.stop, session_id=payload.get("session_id")
        )
        return json_response(result)

    return None


# ── 상태 이벤트 WebSocket ───────────────────────────────────────────────
async def events_socket(
    ctx: RouteContext, receive, send, session_id: str, client_id: str = "",
) -> None:
    """`/v1/events?session_id=&client_id=`.

    세션 범위 이벤트는 그 세션에만, 전체 정지(scope=global)는 모든 구독자에게
    간다. `client_id`가 오면 탭 단위 클라이언트로 연결하고, 같은 세션을 이미
    다른 탭이 쓰고 있으면 거부한다(복제 탭 분리).
    """
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    await send({"type": "websocket.accept"})
    if not session_id:
        await _send_json(send, {
            "type": "error", "scope": "session",
            "reason_code": ReasonCode.CONFIG_MISSING.value,
            "detail": "session_id 없이 이벤트를 구독할 수 없다",
        })
        await send({"type": "websocket.close", "code": 4400})
        return
    try:
        ctx.api.require_session(session_id)
        if client_id:
            ctx.api.attach_client(session_id=session_id, client_id=client_id)
    except ApiError as exc:
        await _send_json(send, {
            "type": "error", "scope": "session", **exc.to_dict(),
        })
        await send({"type": "websocket.close", "code": 4403})
        return

    queue = ctx.hub.subscribe(session_id)
    await _send_json(send, {
        "type": "hello", "scope": "session", "session_id": session_id,
        "payload": ctx.api.health(),
    })

    async def pump() -> None:
        while True:
            event = await queue.get()
            await _send_json(send, event)

    pump_task = asyncio.create_task(pump())
    try:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
    finally:
        pump_task.cancel()
        ctx.hub.unsubscribe(queue)
        if client_id:
            # 구독이 끊겼다. **실행을 취소하지 않는다** — 탭을 닫아도 진행 중인
            # 실행은 계속되고, 세션은 만료 정책에 따라 처리된다.
            ctx.api.detach_client(session_id=session_id, client_id=client_id)


async def _send_json(send, payload: dict) -> None:
    await send({
        "type": "websocket.send",
        "text": json.dumps(payload, ensure_ascii=False),
    })
