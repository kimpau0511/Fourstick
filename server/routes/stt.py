"""stt 라우트 (md/개발플랜.md 3-04).

STT 스트림 WebSocket과 partial/final 전달.

- 브라우저 세션(`session_id`) 안에서 **스트림마다 별도 `stream_id`**를 발급한다.
  재연결하면 다른 스트림이 되고, 끊긴 스트림의 partial을 새 스트림에 이어
  붙이지 않는다(4단계 계약).
- 확정된 요청은 브라우저 세션에 귀속된다(`owner_session_id`).
- STT를 쓸 수 없으면 **사용 불가로 알리고 닫는다.** Mock 성공을 만들지 않는다.
- PCM과 partial transcript를 DB에 직접 저장하지 않는다 — 저장은
  `StreamingSTTSession`이 Repository 계약으로만 한다.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from core.reason_codes import ReasonCode
from server.api import ApiError
from server.routes.common import RouteContext

PATHS: tuple[str, ...] = ()


async def stt_socket(ctx: RouteContext, receive, send, session_id: str) -> None:
    """PCM16 프레임을 받아 partial/final을 돌려준다.

    브라우저 세션(`session_id`) 안에서 **STT 스트림마다 별도 stream_id**를
    발급한다. 재연결하면 다른 스트림이 되고, 끊긴 스트림의 partial을 새
    스트림에 이어 붙이지 않는다. 확정된 요청은 브라우저 세션에 귀속된다.
    """
    message = await receive()
    if message["type"] != "websocket.connect":
        return
    runtime = ctx.runtime
    if not session_id:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": json.dumps({
            "kind": "error", "reason_code": ReasonCode.CONFIG_MISSING.value,
            "detail": "session_id 없이 STT를 시작할 수 없다",
        }, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 4400})
        return
    try:
        ctx.api.require_session(session_id)
    except ApiError as exc:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": json.dumps({
            "kind": "error", **exc.to_dict(),
        }, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 4403})
        return
    if runtime.stt_factory is None:
        await send({"type": "websocket.accept"})
        await send({"type": "websocket.send", "text": json.dumps({
            "kind": "error",
            "reason_code": ReasonCode.STT_BACKEND_UNAVAILABLE.value,
            "detail": f"STT를 사용할 수 없다: {runtime.stt.detail}",
        }, ensure_ascii=False)})
        await send({"type": "websocket.close", "code": 1011})
        return

    await send({"type": "websocket.accept"})
    from stt.streaming_session import StreamingSTTSession
    from stt.transcription_guard import TranscriptionGuard

    parts = runtime.stt_factory()
    stream_id = f"stt_{uuid.uuid4().hex[:12]}"
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    guard = TranscriptionGuard(runtime.stt_policy)
    session = StreamingSTTSession(
        request_id=request_id, policy=runtime.stt_policy, vad=parts["vad"],
        transcriber=parts["transcriber"],
        sample_rate_hz=parts["config"].sample_rate_hz,
        schema_version=ctx.runtime.config_payload()["schema_version"],
        repository=runtime.repository, guard=guard, session_id=stream_id,
        owner_session_id=session_id,
        model_config=parts["config"], model_version="faster-whisper-1.2.1",
        model_load_sec=parts["load_sec"],
        confidence_metric="exp(avg_logprob) 길이가중평균",
    )
    started = time.monotonic()
    await send({"type": "websocket.send", "text": json.dumps({
        "kind": "session",
        "session_id": session_id,
        "stream_id": stream_id,
        "request_id": request_id,
        "sample_rate_hz": parts["config"].sample_rate_hz,
        "max_audio_sec": runtime.stt_policy.max_audio_sec,
    }, ensure_ascii=False)})

    async def emit(events) -> None:
        for event in events:
            await send({"type": "websocket.send", "text": json.dumps(
                _stt_event(event, session_id, stream_id), ensure_ascii=False
            )})

    await emit(session.start(at_utc=time.time()))
    audio_sec = 0.0
    try:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            if message["type"] != "websocket.receive":
                continue
            if message.get("bytes"):
                chunk = message["bytes"]
                audio_sec += len(chunk) / 2 / parts["config"].sample_rate_hz
                events = await asyncio.to_thread(
                    session.feed_audio, chunk,
                    at_sec=audio_sec, at_utc=time.time(),
                )
                await emit(events)
                continue
            text = message.get("text") or ""
            try:
                command = json.loads(text)
            except json.JSONDecodeError:
                continue
            kind = command.get("type")
            if kind == "flush":
                events = await asyncio.to_thread(
                    session.flush, at_sec=audio_sec, at_utc=time.time()
                )
                await emit(events)
            elif kind == "abort":
                events = session.abort(at_utc=time.time())
                await emit(events)
            elif kind == "close":
                await send({"type": "websocket.close", "code": 1000})
                return
    finally:
        guard.close()


def _stt_event(event, session_id: str, stream_id: str) -> dict:
    """세션 이벤트를 브라우저용 JSON으로. 세션·스트림 식별자를 함께 보낸다."""
    return {
        "kind": event.kind.value,
        "session_id": session_id,
        "stream_id": stream_id,
        "request_id": event.request_id,
        "at_utc": event.at_utc,
        "state": None if event.state is None else event.state.value,
        "text": event.text,
        "raw_text": event.raw_text,
        "confidence": event.confidence,
        "reason_code": None if event.reason is None else event.reason.value,
        "detail": event.detail,
        "persisted": event.persisted,
        "persist_reason": (
            None if event.persist_reason is None else event.persist_reason.value
        ),
        "persist_recoverable": event.persist_recoverable,
    }
