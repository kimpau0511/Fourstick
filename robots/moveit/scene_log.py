"""MoveIt 장면 조회 진단 로그(2026-10-07).

장면 조회(`/get_planning_scene` 등)의 준비·전송·응답·타임아웃·예외를 한 줄 JSON으로 남긴다.
`scene_query {...}` 형식으로 표준 오류에 쓴다 — 웹 서버는 이것을 `web.log`로 받는다(uvicorn 기본 설정은
INFO를 버리므로 이 로거는 자기 처리기를 갖는다). **rclpy를 import하지 않는다.**

- `SCENE_CONTEXT`: 지금 조회를 부른 요청(request_id·plan_id·용도). 서버가 계획 판정 전에 넣는다.
- `recent()`: 최근 기록(메모리). 조회 실패를 None으로 바꾸는 곳도 원인을 여기 남긴다.
"""

from __future__ import annotations

import collections
import contextvars
import itertools
import json
import logging
import os
import sys
import threading
import time

SCENE_CONTEXT: contextvars.ContextVar[dict] = contextvars.ContextVar("scene_context", default={})
_RECENT: collections.deque = collections.deque(maxlen=200)
_IDS = itertools.count(1)
_LOCK = threading.Lock()

logger = logging.getLogger("forstick2.scene")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def next_id() -> str:
    return f"sq{next(_IDS)}"


def event(kind: str, *, level: int = logging.INFO, **fields) -> dict:
    """기록 한 줄. 요청 맥락·시각·부하를 함께 담는다."""
    row = {"kind": kind, "at": round(time.time(), 3), **SCENE_CONTEXT.get(), **fields,
           "thread": threading.current_thread().name}
    try:
        row["loadavg_1m"] = round(os.getloadavg()[0], 2)
    except OSError:
        pass
    with _LOCK:
        _RECENT.append(row)
    logger.log(level, "scene_query %s", json.dumps(row, ensure_ascii=False, default=str))
    return row


def recent(n: int = 50) -> list[dict]:
    with _LOCK:
        return list(_RECENT)[-n:]
