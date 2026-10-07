"""stt 라우트 (md/개발플랜.md 3-04).

STT 스트림 WebSocket과 partial/final 전달.

- 브라우저 세션(`session_id`) 안에서 **스트림마다 별도 `stream_id`**를 발급한다.
  재연결하면 다른 스트림이 되고, 끊긴 스트림의 partial을 새 스트림에 이어
  붙이지 않는다(4단계 계약).
- 확정된 요청은 브라우저 세션에 귀속된다(`owner_session_id`).
- STT를 쓸 수 없으면 **사용 불가로 알리고 닫는다.** Mock 성공을 만들지 않는다.
- 일반 모드의 발화가 종료된 뒤 큐에 남은 오디오·flush·abort는 소비만 한다.
  종료된 세션을 다시 열거나 final·저장을 반복하지 않는다. 세션 자체의 상태 계약은 유지한다.
- PCM과 partial transcript를 DB에 직접 저장하지 않는다 — 저장은
  `StreamingSTTSession`이 Repository 계약으로만 한다.
- **호출어 대기(`mode=wake`, 2026-10-07)**: 대시보드 음성 명령이 켜져 있는 동안 계속 듣는다. 발화 하나가
  끝나도(final·clarify·오류) 소켓을 닫지 않고 새 발화를 기다린다. 이 모드의 전사는 **요청으로 저장하지
  않는다**(주변 말소리는 명령이 아니다) — 호출어 뒤 명령은 화면이 일반 모드 소켓으로 따로 받는다.
  발화가 없는 동안 쌓인 무음은 `WAKE_IDLE_RESET_SEC`마다 버리고, 발화 시작이 잘리지 않게 마지막
  `WAKE_PREROLL_SEC` 프레임을 새 발화 세션에 다시 넣는다.
  화면이 `abort`를 보내면 지금 발화를 버리고 **세대(epoch)**를 하나 올린다. 이후 이벤트에는 새 세대 번호가
  붙는다 — 화면은 안내 음성(TTS)이 끝난 뒤 abort를 보내, 그 뒤의 새 발화만 승인 응답으로 쓴다.
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
WAKE_IDLE_RESET_SEC = 3.0
WAKE_PREROLL_SEC = 0.6


async def stt_socket(ctx: RouteContext, receive, send, session_id: str, mode: str = "") -> None:
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
    from stt.protocol import SttSessionState
    from stt.streaming_session import StreamingSTTSession
    from stt.transcription_guard import TranscriptionGuard

    wake = mode == "wake"
    parts = runtime.stt_factory()
    stream_id = f"stt_{uuid.uuid4().hex[:12]}"
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    guard = TranscriptionGuard(runtime.stt_policy)
    rate = parts["config"].sample_rate_hz

    def new_session(req_id: str) -> StreamingSTTSession:
        return StreamingSTTSession(
            request_id=req_id, policy=runtime.stt_policy, vad=parts["vad"],
            transcriber=parts["transcriber"],
            sample_rate_hz=rate,
            schema_version=ctx.runtime.config_payload()["schema_version"],
            # 호출어 대기의 전사는 요청으로 남기지 않는다(저장소 없음 — 전사 가드는 그대로).
            repository=None if wake else runtime.repository, guard=guard, session_id=stream_id,
            owner_session_id=session_id,
            model_config=parts["config"], model_version="faster-whisper-1.2.1",
            model_load_sec=parts["load_sec"],
            confidence_metric="exp(avg_logprob) 길이가중평균",
        )

    session = new_session(request_id)
    started = time.monotonic()
    await send({"type": "websocket.send", "text": json.dumps({
        "kind": "session",
        "session_id": session_id,
        "stream_id": stream_id,
        "request_id": request_id,
        "sample_rate_hz": parts["config"].sample_rate_hz,
        "max_audio_sec": runtime.stt_policy.max_audio_sec,
        "mode": "wake" if wake else "command",
    }, ensure_ascii=False)})

    epoch = 0                         # 호출어 대기: abort마다 하나씩 오르는 발화 세대

    async def emit(events) -> None:
        for event in events:
            payload = _stt_event(event, session_id, stream_id)
            if wake:
                payload["epoch"] = epoch
            await send({"type": "websocket.send", "text": json.dumps(payload, ensure_ascii=False)})

    await emit(session.start(at_utc=time.time()))
    audio_sec = 0.0
    recent: list[bytes] = []          # 호출어 대기: 새 발화 세션에 다시 넣을 마지막 프레임들

    async def restart(preroll: list[bytes]) -> float:
        """호출어 대기: 새 발화 세션을 시작하고 직전 프레임을 다시 넣는다. 새 오디오 시각을 돌려준다."""
        nonlocal session
        session = new_session(f"req_{uuid.uuid4().hex[:12]}")
        session.start(at_utc=time.time())
        at = 0.0
        for frame in preroll:
            if session.state is SttSessionState.CLOSED:
                break
            at += len(frame) / 2 / rate
            await emit(await asyncio.to_thread(session.feed_audio, frame, at_sec=at, at_utc=time.time()))
        return at

    try:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                return
            if message["type"] != "websocket.receive":
                continue
            if message.get("bytes"):
                chunk = message["bytes"]
                if not wake and session.state is SttSessionState.CLOSED:
                    # final을 보내는 동안에도 마이크 프레임은 수신 큐에 쌓일 수 있다.
                    # 한 명령은 이미 끝났다. 잔여 오디오를 새 명령으로 해석하지 않는다.
                    continue
                if wake:
                    recent.append(chunk)
                    while sum(len(c) for c in recent) / 2 / rate > WAKE_PREROLL_SEC:
                        recent.pop(0)
                    if session.state is SttSessionState.CLOSED:
                        # 앞 발화가 끝났다(final·clarify·오류) — 소켓은 그대로, 새 발화를 듣는다.
                        # 직전 프레임은 이미 전사한 발화의 끝이다 — 다시 넣지 않는다(같은 말 두 번 방지).
                        del recent[:-1]
                        audio_sec = await restart([])
                    elif (session.state in (SttSessionState.IDLE, SttSessionState.LISTENING)
                          and audio_sec > WAKE_IDLE_RESET_SEC):
                        audio_sec = await restart(list(recent[:-1]))     # 무음만 쌓였다 — 버린다
                        if session.state is SttSessionState.CLOSED:
                            audio_sec = await restart([])
                audio_sec += len(chunk) / 2 / rate
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
                if session.state is SttSessionState.CLOSED:
                    # 무음 final과 클라이언트 flush가 겹쳐도 한 번만 확정한다.
                    continue
                events = await asyncio.to_thread(
                    session.flush, at_sec=audio_sec, at_utc=time.time()
                )
                await emit(events)
            elif kind == "abort":
                if wake:
                    epoch += 1
                    del recent[:]           # 버린 발화의 끝을 새 발화에 다시 넣지 않는다
                if session.state is SttSessionState.CLOSED:
                    continue                # 종료된 발화에 대한 중복 abort. wake의 세대는 위에서 갱신한다.
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
