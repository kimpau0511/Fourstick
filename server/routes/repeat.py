"""반복 작업 라우트(2026-10-07) — `server/repeat_runs.py`.

시연 작업과 같은 `/v1/sim-demo/` 아래에 둔다: 로그인 출입 검사·명령 보낸 사람 기록이 다른 작업 셀 명령과
똑같이 적용된다(인증 정책은 바꾸지 않는다).

| 경로 | 뜻 |
|---|---|
| `GET /v1/sim-demo/repeat` | 지금(또는 마지막) 반복 상태. 새로고침 뒤에도 이것으로 다시 본다 |
| `POST /v1/sim-demo/repeat/preview` | `{session_id, materials[], count}` → 작업 내용·횟수·시작 가능 여부(실행 없음) |
| `POST /v1/sim-demo/repeat/start` | `{session_id, token}` → 사용자가 승인한 미리보기 그대로 시작 |
| `POST /v1/sim-demo/repeat/<id>/finish-after-round` | 현재 회차를 마치고 종료 |
| `POST /v1/sim-demo/repeat/<id>/pause` · `/resume` · `/cancel` | 일시정지·재개·(일시정지 상태에서) 취소 |
| `POST /v1/sim-demo/repeat/<id>/verify` | 재시작으로 중단된 반복: 실행 종료·로봇 정지·부착 상태 확인 뒤에만 잠금 해제 |
"""

from __future__ import annotations

import asyncio

from core.reason_codes import ReasonCode
from server.api import ApiError
from server.routes.common import RouteContext, Response, body_field, json_response

PREFIX = "/v1/sim-demo/repeat"


async def handle(ctx: RouteContext, method: str, path: str, receive, query: dict[str, str]) -> Response | None:
    if path != PREFIX and not path.startswith(PREFIX + "/"):
        return None
    from server.repeat_runs import RepeatRunError

    runs = getattr(ctx.runtime, "repeat_runs", None)
    if runs is None:
        raise ApiError(503, ReasonCode.CONFIG_MISSING, "반복 작업을 쓸 수 없다(작업 셀 시연 실행기가 없다)")
    try:
        if method == "GET" and path == PREFIX:
            return json_response({"run": runs.view()})
        if method == "POST" and path == PREFIX + "/preview":
            payload = await ctx.read_body(receive)
            return json_response(runs.preview(session_id=body_field(payload, "session_id"),
                                              materials=payload.get("materials") or [],
                                              count=payload.get("count")))
        if method == "POST" and path == PREFIX + "/start":
            payload = await ctx.read_body(receive)
            return json_response({"run": runs.start(session_id=body_field(payload, "session_id"),
                                                    token=body_field(payload, "token"))}, 202)
        if method == "POST":
            parts = path[len(PREFIX) + 1:].split("/")
            if len(parts) == 2:
                run_id, action = parts
                if action == "verify":
                    # 관측(컨트롤러 status·관절 1초)을 기다린다 — 이벤트 루프 밖에서.
                    return json_response({"run": await asyncio.to_thread(runs.verify, run_id)})
                act = {"finish-after-round": runs.finish_after_round, "pause": runs.pause,
                       "resume": runs.resume, "cancel": runs.cancel}.get(action)
                if act is not None:
                    return json_response({"run": act(run_id)})
    except RepeatRunError as exc:
        reason = ReasonCode.PLAN_ARG_UNKNOWN if exc.status == 400 else ReasonCode.EXEC_PERMIT_DENIED
        raise ApiError(exc.status, reason, exc.detail) from exc
    return None
