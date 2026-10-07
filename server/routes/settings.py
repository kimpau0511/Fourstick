"""settings 라우트(2026-10-07) — 로봇 이름·호출어(`server/robot_names.py`).

| 경로 | 뜻 |
|---|---|
| `GET /v1/settings/robot-name?robot_id=` | 저장된 이름(없으면 기본 '지니')과 호출어 |
| `POST /v1/settings/robot-name` | `{robot_id, name}` 저장 — 한글 2~4글자만 |

로그인 출입 검사는 다른 API와 같다(조회·저장 모두 로그인 필요, 로그인을 끈 환경에서는 열림).
"""

from __future__ import annotations

from core.reason_codes import ReasonCode
from server.api import ApiError
from server.routes.common import RouteContext, Response, json_response

PATH = "/v1/settings/robot-name"


def _robot_id(ctx: RouteContext, given) -> str:
    if isinstance(given, str) and given.strip():
        return given.strip()
    robot = (ctx.runtime.config_payload() or {}).get("robot") or {}
    return str(robot.get("robot_id") or "default")


async def handle(ctx: RouteContext, method: str, path: str, receive, query: dict[str, str]) -> Response | None:
    if path != PATH:
        return None
    from server.robot_names import RobotNameError

    names = getattr(ctx.runtime, "robot_names", None)
    if names is None:
        raise ApiError(503, ReasonCode.CONFIG_MISSING, "로봇 이름 설정을 쓸 수 없다")
    if method == "GET":
        return json_response(names.get(_robot_id(ctx, query.get("robot_id"))))
    if method == "POST":
        payload = await ctx.read_body(receive)
        try:
            return json_response(names.set(_robot_id(ctx, payload.get("robot_id")), payload.get("name")))
        except RobotNameError as exc:
            raise ApiError(400, ReasonCode.PLAN_ARG_UNKNOWN, str(exc)) from exc
    return None
