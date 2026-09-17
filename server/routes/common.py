"""라우트 공통 경계 (md/개발플랜.md 3-04).

라우트 모듈이 함께 쓰는 것만 둔다.

- `RouteContext` — Api·Runtime·설정·Repository 주입 통로. 라우트가 전역 상태를
  보지 않고 이 객체만 받는다.
- 식별자 읽기(`query_param`, `body_field`) — 없으면 `config.missing`으로 거절한다.
  **서버는 현재 세션·현재 계획을 추정하지 않는다.**
- ReasonCode 응답 직렬화(`json_response`, `error_response`).
- `EventHub` — 세션 범위 이벤트 팬아웃. 구독자 목록은 연결 상태이며 기록이
  아니다(저장하지 않는다).

라우트 모듈은 **응답 형태와 ReasonCode를 바꾸지 않는다.** 이 파일의 도우미가
그 형태를 한 곳에 고정한다.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.reason_codes import ReasonCode
from server.api import Api, ApiError
from server.config import ServerConfig
from server.runtime import Runtime

#: (status, headers, body) 하나가 HTTP 응답이다.
Response = tuple[int, list, bytes]

JSON_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"application/json; charset=utf-8"),
    (b"cache-control", b"no-store"),
]


def json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def json_response(payload: Any, status: int = 200) -> Response:
    return status, [], json_bytes(payload)


def error_response(exc: ApiError) -> Response:
    """ApiError를 그대로 돌려준다. 이유 코드를 바꾸거나 감추지 않는다."""
    return exc.status, [], json_bytes(exc.to_dict())


def query_param(query: dict[str, str], name: str) -> str:
    value = query.get(name, "")
    if not value:
        raise ApiError(400, ReasonCode.CONFIG_MISSING, f"{name}이 없다")
    return value


def body_field(payload: dict, name: str) -> str:
    value = str(payload.get(name, "") or "")
    if not value:
        raise ApiError(400, ReasonCode.CONFIG_MISSING, f"{name}이 없다")
    return value


class EventHub:
    """이벤트 구독자 관리와 팬아웃.

    `scope="global"`(전체 정지)은 모든 구독자에게, 그 외에는 해당 세션
    구독자에게만 보낸다. 다른 스레드(실행 루프)에서 호출되므로 루프에 안전하게
    넘긴다. 루프 참조는 lifespan이 아니라 **구독 시점에도** 잡는다 — lifespan을
    돌리지 않는 호출자(테스트 클라이언트)에서도 이벤트가 전달돼야 한다.
    """

    def __init__(self) -> None:
        self._queues: list[tuple[str, asyncio.Queue]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def capture_loop(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def subscribe(self, session_id: str) -> asyncio.Queue:
        self.capture_loop()
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.append((session_id, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues = [row for row in self._queues if row[1] is not queue]

    def fanout(self, event: dict) -> None:
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        scope = event.get("scope", "session")
        target = event.get("session_id")
        for session_id, queue in list(self._queues):
            if scope != "global" and target is not None and session_id != target:
                continue
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                pass


@dataclass
class RouteContext:
    """라우트가 받는 것 전부. 전역 변수를 보지 않는다."""

    api: Api
    runtime: Runtime
    config: ServerConfig
    hub: EventHub
    #: 요청 본문을 읽는 함수. ASGI receive를 라우트가 직접 다루지 않게 한다.
    read_body: Callable[[Any], Awaitable[dict]]

    @property
    def repository(self):
        """Repository는 Runtime을 통해 주입된다. 라우트가 직접 열지 않는다."""
        return self.runtime.repository
