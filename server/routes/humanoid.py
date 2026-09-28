"""humanoid 라우트 — G1(시뮬레이션) 웹 명령·확인·실행·STOP·3D 화면.

| 경로 | 내용 |
|---|---|
| GET `/v1/humanoid?session_id=` | 제어기·관측 건강 상태, 실행 중·마지막 작업, 세션 출발 위치 |
| POST `/v1/humanoid/command` | 발화 해석 → CONFIRM(확인 카드) · ASK · BLOCK · STOP(즉시) |
| POST `/v1/humanoid/confirm` | 확인 카드 승인/취소 → 실행 |
| POST `/v1/humanoid/stop` | 확인 없이 즉시 정지(목표 취소, 균형 제어 유지) |
| GET `/v1/humanoid/jobs/<id>` | 작업 진행 |
| GET `/v1/humanoid/view/model` · `/view/state` · `/view/mesh/<n>/<파일>` | 3D 화면(읽기 전용) |

FR3 경로(`/v1/sim-demo*`, `/v1/plan`)와 대화 맥락·확인 토큰을 공유하지 않는다.
"""

from __future__ import annotations

from server.api import ApiError
from server.routes.common import Response, RouteContext, json_response

PREFIX = "/v1/humanoid"
_MESH_TYPES = {".stl": "model/stl", ".dae": "model/vnd.collada+xml"}


def _service(ctx: RouteContext):
    return getattr(ctx.runtime, "humanoid_service", None)


def _session(ctx: RouteContext, session_id: str) -> str:
    """서버가 발급했고 아직 쓸 수 있는 세션만. 인증이 아니다(작업 묶음 구분)."""
    if not session_id:
        raise ApiError(400, None, "session_id가 없다")
    api = getattr(ctx, "api", None)
    if api is not None:
        api.require_session(session_id)
    return session_id


async def handle(ctx: RouteContext, method: str, path: str, receive,
                 query: dict[str, str]) -> Response | None:
    if not (path == PREFIX or path.startswith(PREFIX + "/")):
        return None
    service = _service(ctx)
    if service is None:
        return json_response({"enabled": False, "is_simulated": True,
                              "reason": getattr(ctx.runtime, "humanoid_disabled_reason", None)},
                             status=503 if path != PREFIX else 200)
    if method == "GET" and path == PREFIX:
        sid = query.get("session_id") or ""
        return json_response({"enabled": True, **service.status(sid or None)})
    if method == "POST" and path == f"{PREFIX}/command":
        payload = await ctx.read_body(receive)
        sid = _session(ctx, str(payload.get("session_id") or ""))
        return json_response(service.command(sid, str(payload.get("utterance") or ""),
                                             str(payload.get("source") or "text")))
    if method == "POST" and path == f"{PREFIX}/confirm":
        payload = await ctx.read_body(receive)
        sid = _session(ctx, str(payload.get("session_id") or ""))
        result = service.confirm(sid, str(payload.get("token") or ""),
                                 str(payload.get("action") or "cancel"))
        return json_response(result, status=202 if result.get("decision") == "RUN" else 200)
    if method == "POST" and path == f"{PREFIX}/stop":
        payload = await ctx.read_body(receive)
        # STOP은 세션이 없어도 받는다(전체 정지). 확인 카드를 기다리지 않는다.
        return json_response(service.stop(str(payload.get("session_id") or "") or None,
                                          reason=str(payload.get("reason") or "정지 버튼")))
    if method == "GET" and path.startswith(f"{PREFIX}/jobs/"):
        job = service.job(path.rsplit("/", 1)[-1])
        return json_response(job or {"error": "작업이 없다"}, status=200 if job else 404)
    view = getattr(ctx.runtime, "humanoid_view", None)
    if method == "GET" and path.startswith(f"{PREFIX}/view/") and view is not None:
        if path == f"{PREFIX}/view/model":
            model = view.model()
            return json_response(model, status=200 if model.get("available") else 503)
        if path == f"{PREFIX}/view/state":
            return json_response(view.state())
        if path.startswith(f"{PREFIX}/view/mesh/"):
            parts = path[len(f"{PREFIX}/view/mesh/"):].split("/")
            target = view.mesh(parts[0], parts[1]) if len(parts) == 2 else None
            if target is None:
                return json_response({"error": "허용되지 않은 메시"}, status=404)
            kind = _MESH_TYPES.get(target.suffix.lower(), "application/octet-stream")
            return 200, [(b"content-type", kind.encode()),
                         (b"cache-control", b"max-age=3600")], target.read_bytes()
    return None
