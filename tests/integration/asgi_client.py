"""테스트용 ASGI 클라이언트.

의존성을 늘리지 않기 위해 직접 만든다. `httpx`는 시스템 인터프리터에 없고,
WebSocket도 다루지 못한다 — 우리는 둘 다 필요하다(이벤트 스트림과 STT 세션).

ASGI는 dict를 주고받는 인터페이스라 테스트에서 직접 몰기 쉽다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def split_target(target: str) -> tuple[str, bytes]:
    """ASGI 서버처럼 경로와 query_string을 분리한다.

    uvicorn이 실제로 하는 일이다. 테스트 클라이언트가 이를 하지 않으면
    `?session_id=` 같은 질의가 경로에 붙어 라우팅이 어긋난다.
    """
    path, _, query = target.partition("?")
    return path, query.encode("utf-8")


class HttpClient:
    """ASGI 앱에 HTTP 요청 하나를 보낸다."""

    def __init__(self, app):
        self.app = app

    async def request(self, method: str, path: str, payload: Any = None) -> Response:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        path, query = split_target(path)
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path, "raw_path": path.encode(),
            "query_string": query, "root_path": "", "scheme": "http",
            "headers": [(b"host", b"test"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345), "server": ("test", 80),
        }
        sent: list[dict] = []
        incoming = [
            {"type": "http.request", "body": body, "more_body": False}
        ]

        async def receive() -> dict:
            if incoming:
                return incoming.pop(0)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        await self.app(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        chunks = b"".join(
            m.get("body", b"") for m in sent if m["type"] == "http.response.body"
        )
        headers = {
            key.decode().lower(): value.decode() for key, value in start["headers"]
        }
        return Response(status=start["status"], headers=headers, body=chunks)

    async def get(self, path: str) -> Response:
        return await self.request("GET", path)

    async def post(self, path: str, payload: Any = None) -> Response:
        return await self.request("POST", path, payload)


@dataclass
class WebSocketSession:
    """ASGI WebSocket 엔드포인트를 테스트에서 몰기 위한 최소 구현."""

    app: Any
    path: str
    incoming: asyncio.Queue = field(default_factory=asyncio.Queue)
    outgoing: list[dict] = field(default_factory=list)
    accepted: bool = False
    closed: bool = False
    close_code: int | None = None
    _task: asyncio.Task | None = None

    async def __aenter__(self) -> "WebSocketSession":
        path, query = split_target(self.path)
        scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "path": path,
            "raw_path": path.encode(), "query_string": query, "root_path": "",
            "scheme": "ws", "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 12345), "server": ("test", 80),
            "subprotocols": [],
        }
        await self.incoming.put({"type": "websocket.connect"})

        async def receive() -> dict:
            return await self.incoming.get()

        async def send(message: dict) -> None:
            if message["type"] == "websocket.accept":
                self.accepted = True
            elif message["type"] == "websocket.close":
                self.closed = True
                self.close_code = message.get("code")
            self.outgoing.append(message)

        self._task = asyncio.create_task(self.app(scope, receive, send))
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *exc) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def send_json(self, payload: Any) -> None:
        await self.incoming.put({
            "type": "websocket.receive",
            "text": json.dumps(payload, ensure_ascii=False),
        })
        await asyncio.sleep(0)

    async def send_bytes(self, data: bytes) -> None:
        await self.incoming.put({"type": "websocket.receive", "bytes": data})
        await asyncio.sleep(0)

    async def messages(self, *, timeout: float = 2.0) -> list[dict]:
        """지금까지 서버가 보낸 텍스트 메시지를 JSON으로."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.outgoing and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        out = []
        for message in self.outgoing:
            if message["type"] == "websocket.send" and "text" in message:
                out.append(json.loads(message["text"]))
        return out

    async def wait_for(self, predicate, *, timeout: float = 3.0) -> dict:
        """조건에 맞는 메시지를 기다린다. 없으면 AssertionError."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for message in await self.messages(timeout=0.05):
                if predicate(message):
                    return message
            await asyncio.sleep(0.02)
        raise AssertionError(f"조건에 맞는 메시지를 받지 못했다: {self.outgoing}")
