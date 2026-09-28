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


#: 공개 응답에서 **가리는** 실기(실제 하드웨어) 상태 키.
#:
#: 지우는 것이 아니라 **내보내지 않는 것**이다. 실기 어댑터·설정·기준 데이터와
#: 내부 모델(`validation/hardware_readiness.py`, `SimulationDemoState.status()`,
#: 어댑터의 `stop_diagnostics()` 등)은 그대로 두고 계속 계산한다 — 내부 불변식
#: (시뮬레이션 결과를 실기로 승격하지 않는다)이 그 값에 기대고 있기 때문이다.
#:
#: 지금 서비스는 Gazebo 시뮬레이션 전용이라, 사용자에게 실기 준비도·검증 상태를
#: 보여 주면 실제 로봇을 고르거나 돌릴 수 있는 것처럼 읽힌다. 그래서 화면과
#: 공개 JSON에서만 뺀다.
HIDDEN_HARDWARE_KEYS = frozenset({
    "real_hardware",
    "real_hardware_ready",
    "real_hardware_verified",
    "real_hardware_connected",
    "real_hardware_ready_at",
    "hardware_readiness",
})

#: 리스트 안에서 **정확히 일치할 때만** 빼는 문자열 값.
#:
#: 키가 아니라 값이다. 그리퍼 프로필의 `unverified_items`는 "아직 확인하지 못한
#: 항목" 목록인데, 그중 한 줄이 실기 활성화를 가리킨다. 목록 자체와 나머지 항목은
#: 그대로 두고 이 한 줄만 뺀다.
#:
#: **부분 일치·정규식을 쓰지 않는다.** 값 안에 이 낱말이 들어 있는 다른 항목을
#: 같이 지우면 기준 데이터가 조용히 깎인다.
HIDDEN_LIST_VALUES = frozenset({
    "real_hardware_activation",
})


def public_payload(value: Any) -> Any:
    """응답 본문에서 실기 상태를 재귀적으로 뺀다.

    두 가지를 **구분해서** 처리한다.

    - dict의 **키**가 `HIDDEN_HARDWARE_KEYS`에 있으면 그 항목을 통째로 뺀다.
    - list의 **문자열 값**이 `HIDDEN_LIST_VALUES`와 정확히 같으면 그 값만 뺀다.
      리스트 구조와 나머지 값은 그대로 둔다.

    **모든 공개 응답이 이 한 곳을 지난다**(`json_response`). 라우트마다 지우면
    새 라우트가 조용히 새어 나간다. 원본은 건드리지 않고 사본만 깎는다 —
    내부 설정·기준 데이터 파일은 그대로다.
    """
    if isinstance(value, dict):
        return {key: public_payload(item) for key, item in value.items()
                if key not in HIDDEN_HARDWARE_KEYS}
    if isinstance(value, (list, tuple)):
        return [public_payload(item) for item in value
                if not (isinstance(item, str) and item in HIDDEN_LIST_VALUES)]
    return value


def json_response(payload: Any, status: int = 200) -> Response:
    return status, [], json_bytes(public_payload(payload))


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
