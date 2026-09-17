"""ASGI 진입점 (md/개발플랜.md 7단계 · 3-04).

**앱 조립과 오류 응답만** 여기 있다. 경로 처리는 `server/routes/`의 모듈이
맡는다(공통 · planning · stt · execution · robot).

프레임워크를 넣지 않는다. 경로가 10여 개고 WebSocket이 둘이라, uvicorn이 주는
ASGI 인터페이스를 직접 쓰는 것이 의존성을 늘리지 않는 가장 단순한 방법이다.
(vLLM 호출을 표준 라이브러리 HTTP로 한 것과 같은 판단이다.)

경계 검증은 `server/schemas.py`의 Pydantic 모델이 한다 — 이 파일은 요청 본문을
읽어 넘기고 결과를 JSON으로 돌려주는 일만 한다.

WebSocket 둘. **둘 다 session_id를 요구한다.**
- `/v1/events?session_id=…&client_id=…` — 그 세션의 이벤트만 보낸다. 전체
  정지(scope=global)는 로봇 하나를 공유하는 모든 구독자에게 간다.
- `/v1/stt?session_id=…` — PCM16 프레임을 받아 partial/final을 돌려준다.

브라우저는 DB에 직접 접근하지 않는다. 모든 저장은 Repository를 지난다.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.parse

from server.api import Api, ApiError
from server.config import ServerConfig
from server.routes import (
    HTTP_ROUTES,
    execution as execution_routes,
    scene as scene_routes,
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
        self.api.listeners.append(self.hub.fanout)
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
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ── HTTP ────────────────────────────────────────────────────────────
    async def _http(self, scope, receive, send) -> None:
        path = scope["path"]
        method = scope["method"]
        query = _query_dict(scope.get("query_string", b""))
        try:
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
        if path == "/v1/events":
            await execution_routes.events_socket(
                self.ctx, receive, send, session_id, client_id
            )
            return
        if path == "/v1/stt":
            await stt_routes.stt_socket(self.ctx, receive, send, session_id)
            return
        if path == SCENE_STREAM_PATH:
            await scene_routes.scene_socket(self.ctx, receive, send)
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
