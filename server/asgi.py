"""ASGI 진입점 (md/개발플랜.md 7단계 · 3-04).

**앱 조립과 오류 응답만** 여기 있다. 경로 처리는 `server/routes/`의 모듈이
맡는다(공통 · planning · stt · execution · robot).

프레임워크를 넣지 않는다. 경로가 10여 개고 WebSocket이 둘이라, uvicorn이 주는
ASGI 인터페이스를 직접 쓰는 것이 의존성을 늘리지 않는 가장 단순한 방법이다.
(vLLM 호출을 표준 라이브러리 HTTP로 한 것과 같은 판단이다.)

경계 검증은 `server/schemas.py`의 Pydantic 모델이 한다 — 이 파일은 요청 본문을
읽어 넘기고 결과를 JSON으로 돌려주는 일만 한다.

로그인(`server/auth.py`): `config.require_login`이면 로그인·정지·정적 파일 말고는 로그인 세션 쿠키가
있어야 한다. 검사는 여기 한 곳에서 한다 — 라우트마다 넣으면 새 라우트가 조용히 열린다.

WebSocket 둘. **둘 다 session_id를 요구한다.**
- `/v1/events?session_id=…&client_id=…` — 그 세션의 이벤트만 보낸다. 전체
  정지(scope=global)는 로봇 하나를 공유하는 모든 구독자에게 간다.
- `/v1/stt?session_id=…` — PCM16 프레임을 받아 partial/final을 돌려준다.

브라우저는 DB에 직접 접근하지 않는다. 모든 저장은 Repository를 지난다.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import sys
import urllib.parse

from server.api import Api, ApiError
from server.auth import AuthService, cookie_token
from server.config import ServerConfig
from server.exit_watchdog import arm_exit_watchdog
from server.robot_names import RobotNames
from server.routes import (
    HTTP_ROUTES,
    auth as auth_routes,
    execution as execution_routes,
    scene as scene_routes,
    sim_view as sim_view_routes,
    stt as stt_routes,
)
from server.routes.common import (
    EventHub,
    JSON_HEADERS,
    Response,
    RouteContext,
    error_response,
    json_bytes,
)
from server.runtime import Runtime, build_runtime

#: 장면 스트림 WebSocket 경로. 라우트 모듈이 처리한다.
SCENE_STREAM_PATH = "/v1/scene/stream"

#: 정적 파일 허용 확장자. 디렉터리 탈출과 예상 못한 파일 노출을 막는다.
STATIC_SUFFIXES = {".html", ".css", ".js", ".mjs", ".svg", ".png", ".ico", ".webmanifest"}


class Application:
    """ASGI 앱. **조립·정적 파일·오류 응답만 담당한다.**"""

    def __init__(self, runtime: Runtime | None = None, config: ServerConfig | None = None):
        self.config = config or ServerConfig.from_env()
        self.runtime = runtime or build_runtime(self.config)
        self.api = Api(runtime=self.runtime)
        self.hub = EventHub()
        self.auth = AuthService(repository=self.runtime.repository, config=self.config)
        self.api.listeners.append(self.hub.fanout)
        # 반복 작업(2026-10-07): 작업 셀 시연 실행기가 있을 때만. 상태 파일은 시연 상태 파일 옆(셀마다 따로).
        if getattr(self.runtime, "sim_demo_jobs", None) is not None:
            from pathlib import Path

            from server.repeat_runs import RepeatRuns
            from validation.simulation_demo_state import DEFAULT_PATH as _STATE

            state_path = Path(getattr(self.runtime, "simulation_demo_state_path", None) or _STATE)
            self.runtime.repeat_runs = RepeatRuns(api=self.api, runtime=self.runtime,
                                                  path=state_path.with_name("repeat_runs.json"))
        # 로봇 이름·호출어(2026-10-07): DB 옆 파일 — 새로고침·다른 브라우저에서도 같은 이름.
        self.runtime.robot_names = RobotNames(self.config.db_path.with_name("robot_settings.json"))
        self.ctx = RouteContext(
            api=self.api, runtime=self.runtime, config=self.config,
            hub=self.hub, read_body=self._body,
        )

    # ── ASGI ────────────────────────────────────────────────────────────
    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] == "http":
            await self._http(scope, receive, send)
            return

    async def _lifespan(self, scope, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self.hub.capture_loop()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                self.runtime.repository.close()
                # gz 구독을 끊고 끝낸다(인터프리터 정리 중 콜백이 들어오면 죽는다).
                view = getattr(self.runtime, "sim_view", None)
                if view is not None:
                    view.close()
                bridge = getattr(self.runtime, "humanoid_bridge", None)
                if bridge is not None:
                    bridge.close()
                arm_exit_watchdog()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ── HTTP ────────────────────────────────────────────────────────────
    async def _http(self, scope, receive, send) -> None:
        path = scope["path"]
        method = scope["method"]
        query = _query_dict(scope.get("query_string", b""))
        token = cookie_token(scope.get("headers", []))
        actor = self.auth.actor_of(method, path, token)
        try:
            if path.startswith(auth_routes.PREFIX):
                result = await auth_routes.handle(self.auth, self.ctx, method, path, receive, scope.get("headers", []))
                status, headers, body = result or (404, [], json_bytes({"error": f"경로가 없다: {path}", "reason_code": None}))
            elif (refused := self.auth.refusal(method, path, token)) is not None:
                status, headers, body = refused
            else:
                status, headers, body = await self._route(method, path, receive, query)
        except ApiError as exc:
            status, headers, body = error_response(exc)
        except Exception as exc:  # noqa: BLE001 — 서버 오류를 성공으로 숨기지 않는다
            status = 500
            headers = []
            body = json_bytes({
                "error": f"서버 오류: {type(exc).__name__}: {exc}"[:300],
                "reason_code": None,
            })
        merged = headers or list(JSON_HEADERS)
        await send({"type": "http.response.start", "status": status,
                    "headers": merged})
        await send({"type": "http.response.body", "body": body})
        # 명령을 보낸 사람(결정 Q10). 응답을 보낸 **뒤에** 남긴다 — 기록 때문에 정지 응답이 늦어지지 않게.
        # 기록 실패가 이미 처리된 명령을 실패로 바꾸지 않게 하고, 실패는 숨기지 않고 서버 로그에 남긴다.
        try:
            await asyncio.to_thread(self.auth.record_command, method, path, actor, status, body)
        except Exception as exc:  # noqa: BLE001
            print(f"[audit] 명령 보낸 사람 기록 실패 {method} {path}: {type(exc).__name__}: {exc}", file=sys.stderr)

    async def _body(self, receive) -> dict:
        chunks = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise ApiError(400, None, "요청이 끊겼다")
            chunks += message.get("body", b"")
            if not message.get("more_body"):
                break
        if not chunks:
            return {}
        try:
            payload = json.loads(chunks)
        except json.JSONDecodeError as exc:
            raise ApiError(400, None, f"JSON이 아니다: {exc}") from None
        if not isinstance(payload, dict):
            raise ApiError(400, None, "객체가 아니다")
        return payload

    async def _route(
        self, method: str, path: str, receive, query: dict[str, str],
    ) -> Response:
        """정적 파일을 먼저 보고, 나머지는 라우트 모듈에 순서대로 묻는다."""
        if method == "GET" and path in ("/", "/index.html"):
            return self._static("index.html")
        if method == "GET" and path.startswith("/static/"):
            return self._static(f"static/{path[len('/static/'):]}")

        for module in HTTP_ROUTES:
            result = await module.handle(self.ctx, method, path, receive, query)
            if result is not None:
                return result

        return 404, [], json_bytes({
            "error": f"경로가 없다: {path}", "reason_code": None,
        })

    def _static(self, relative: str) -> Response:
        root = self.config.web_dir.resolve()
        target = (root / relative).resolve()
        if root not in target.parents and target != root:
            return 403, [], json_bytes({"error": "허용되지 않은 경로",
                                        "reason_code": None})
        if target.suffix not in STATIC_SUFFIXES or not target.is_file():
            return 404, [], json_bytes({"error": f"파일이 없다: {relative}",
                                        "reason_code": None})
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        headers = [
            (b"content-type", f"{content_type}; charset=utf-8".encode()),
            (b"cache-control", b"no-store"),
        ]
        return 200, headers, target.read_bytes()

    # ── WebSocket ───────────────────────────────────────────────────────
    async def _websocket(self, scope, receive, send) -> None:
        path = scope["path"]
        query = _query_dict(scope.get("query_string", b""))
        session_id = query.get("session_id", "")
        client_id = query.get("client_id", "")
        if self.auth.ws_refused(path, scope.get("headers", [])):
            await receive()
            await send({"type": "websocket.close", "code": 4401})  # accept 전 close → 핸드셰이크 거절
            return
        if path == "/v1/events":
            await execution_routes.events_socket(
                self.ctx, receive, send, session_id, client_id
            )
            return
        if path == "/v1/stt":
            await stt_routes.stt_socket(self.ctx, receive, send, session_id, query.get("mode", ""))
            return
        if path == SCENE_STREAM_PATH:
            await scene_routes.scene_socket(self.ctx, receive, send)
            return
        if path == sim_view_routes.STREAM_PATH:
            await sim_view_routes.stream_socket(self.ctx, receive, send)
            return
        await receive()
        await send({"type": "websocket.close", "code": 4404})


def _query_dict(raw: bytes) -> dict[str, str]:
    return {
        key: values[0]
        for key, values in urllib.parse.parse_qs(raw.decode("utf-8")).items()
    }


def create_app() -> Application:
    """uvicorn 팩토리. `uvicorn --factory server.asgi:create_app`으로 뜬다.

    모듈을 import하는 것만으로 모델을 올리지 않기 위해 팩토리로 둔다 —
    테스트가 이 모듈을 import할 때 STT 모델 로딩이 일어나면 안 된다.
    """
    return Application()
