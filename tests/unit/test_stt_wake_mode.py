"""STT 호출어 대기 모드(`/v1/stt?mode=wake`, 2026-10-07).

- 발화가 끝나도 소켓을 닫지 않고 다음 발화를 듣는다(여러 final).
- 무음이 길게 이어져도 오디오 상한 오류로 끊기지 않는다(무음을 버린다).
- 전사를 요청으로 저장하지 않는다(저장소를 부르지 않는다).
- 일반 모드는 그대로: 저장소에 남기고 한 발화로 끝난다.
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routes import stt as stt_route  # noqa: E402
from stt.whisper_backend import Transcript  # noqa: E402
from tests.unit.test_stt_session import MODEL_CONFIG, ScriptedTranscriber, make_policy  # noqa: E402

SR = 16_000
SILENCE = b"\x00\x00" * 160            # 10ms
SPEECH = b"\x01\x00" * 160


class EnergyVad:
    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:
        return any(frame)


class Texts:
    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = 0

    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript:
        self.calls += 1
        text = self.texts[0] if self.texts else ""
        return Transcript(text=text, confidence=0.95)


class SpyRepository:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return None
        return record


def drive(frames_then_close, *, mode, transcriber, repository):
    sent = []
    policy = make_policy(max_audio_sec=5.0, window_sec=5.0, window_stride_sec=5.0)   # partial 없이 final만 본다
    runtime = SimpleNamespace(
        stt_factory=lambda: {"vad": EnergyVad(), "transcriber": transcriber,
                             "config": MODEL_CONFIG, "load_sec": 0.0},
        stt_policy=policy, repository=repository, stt=SimpleNamespace(detail=""),
        config_payload=lambda: {"schema_version": "x"})
    ctx = SimpleNamespace(runtime=runtime, api=SimpleNamespace(require_session=lambda s: None))
    incoming = [{"type": "websocket.connect"}]
    incoming += [f if isinstance(f, dict) else {"type": "websocket.receive", "bytes": f} for f in frames_then_close]
    incoming.append({"type": "websocket.disconnect"})

    async def receive():
        return incoming.pop(0)

    async def send(message):
        if message.get("type") == "websocket.send":
            sent.append(json.loads(message["text"]))
        elif message.get("type") == "websocket.close":
            sent.append({"kind": "__closed__"})

    async def go():
        await stt_route.stt_socket(ctx, receive, send, "sess", mode)

    asyncio.run(go())
    return sent


def utterance(sec=0.4):
    return [SPEECH] * int(sec * 100) + [SILENCE] * 50


class WakeModeTest(unittest.TestCase):
    def test_command_final_ignores_queued_audio_and_terminal_controls(self):
        flush = {"type": "websocket.receive", "text": '{"type":"flush"}'}
        abort = {"type": "websocket.receive", "text": '{"type":"abort"}'}
        close = {"type": "websocket.receive", "text": '{"type":"close"}'}
        repo = SpyRepository()
        texts = Texts(["A자재 옮겨줘"])
        sent = drive([SPEECH] * 40 + [flush, SPEECH, flush, abort, close],
                     mode="", transcriber=texts, repository=repo)
        self.assertEqual([e["kind"] for e in sent].count("final"), 1)
        self.assertEqual([e["kind"] for e in sent].count("__closed__"), 1)
        self.assertEqual(texts.calls, 1)
        self.assertEqual(repo.calls.count("save_request_with_stt_inference"), 1)

    def test_command_silence_final_tolerates_remaining_microphone_frames(self):
        texts = Texts(["A자재 옮겨줘"])
        sent = drive(utterance() + [SPEECH] * 20, mode="", transcriber=texts,
                     repository=SpyRepository())
        self.assertEqual([e["kind"] for e in sent].count("final"), 1)
        self.assertEqual(texts.calls, 1)

    def test_command_abort_tolerates_late_audio_without_transcribing(self):
        abort = {"type": "websocket.receive", "text": '{"type":"abort"}'}
        texts = Texts(["실행하면 안 되는 잔여 발화"])
        repo = SpyRepository()
        sent = drive([SPEECH] * 20 + [abort, SPEECH, abort], mode="",
                     transcriber=texts, repository=repo)
        self.assertEqual(texts.calls, 0)
        self.assertEqual(repo.calls, [])
        self.assertEqual([e["kind"] for e in sent].count("error"), 1)
        self.assertFalse(any(e["kind"] == "final" for e in sent))

    def test_closed_wake_flush_does_not_prevent_next_utterance(self):
        flush = {"type": "websocket.receive", "text": '{"type":"flush"}'}
        sent = drive([SPEECH] * 40 + [flush, flush] + utterance(), mode="wake",
                     transcriber=Texts(["지니야"]), repository=SpyRepository())
        self.assertEqual([e["kind"] for e in sent].count("final"), 2)

    def test_command_error_and_clarify_tolerate_queued_audio(self):
        flush = {"type": "websocket.receive", "text": '{"type":"flush"}'}
        cases = (
            (Transcript(text="불명확", confidence=0.1), "clarify"),
            (RuntimeError("fixture backend failure"), "error"),
        )
        for result, terminal_kind in cases:
            with self.subTest(terminal_kind=terminal_kind):
                texts = ScriptedTranscriber([result])
                sent = drive([SPEECH] * 40 + [flush, SPEECH, flush], mode="",
                             transcriber=texts, repository=SpyRepository())
                kinds = [e["kind"] for e in sent]
                self.assertEqual(kinds.count(terminal_kind), 1)
                self.assertNotIn("final", kinds)
                self.assertEqual(texts.calls, 1)

    def test_command_no_speech_error_tolerates_queued_audio(self):
        flush = {"type": "websocket.receive", "text": '{"type":"flush"}'}
        texts = Texts(["전사하면 안 됨"])
        sent = drive([SILENCE, flush, SPEECH, flush], mode="",
                     transcriber=texts, repository=SpyRepository())
        self.assertEqual([e["kind"] for e in sent].count("error"), 1)
        self.assertEqual(texts.calls, 0)

    def test_wake_mode_keeps_listening_and_does_not_persist(self):
        texts = Texts(["지니야"])
        repo = SpyRepository()
        frames = [SILENCE] * 800 + utterance() + [SILENCE] * 200 + utterance()   # 무음 8초(상한 5초 넘음) + 발화 2개
        sent = drive(frames, mode="wake", transcriber=texts, repository=repo)
        kinds = [e["kind"] for e in sent]
        self.assertEqual(sent[0]["kind"], "session")
        self.assertEqual(sent[0]["mode"], "wake")
        self.assertEqual(kinds.count("final"), 2, kinds)
        self.assertNotIn("__closed__", kinds)
        self.assertFalse([e for e in sent if e["kind"] == "error"], sent)       # 무음 상한 오류 없음
        self.assertTrue(all(e["text"] == "지니야" for e in sent if e["kind"] == "final"))
        self.assertEqual(repo.calls, [])                                           # 저장하지 않는다

    def test_wake_mode_keeps_the_start_of_speech_across_a_silence_reset(self):
        # 무음 버리기가 일어나는 프레임 바로 뒤에 발화가 시작돼도 final이 나온다.
        sent = drive([SILENCE] * 301 + utterance(), mode="wake", transcriber=Texts(["지니야"]),
                     repository=SpyRepository())
        self.assertEqual([e["kind"] for e in sent].count("final"), 1)

    def test_wake_mode_abort_drops_the_current_utterance_and_keeps_listening(self):
        abort = {"type": "websocket.receive", "text": json.dumps({"type": "abort"})}
        frames = [SPEECH] * 30 + [abort, abort] + [SILENCE] * 60 + utterance()   # 버린 뒤(두 번째 abort는 무시) 새 발화
        sent = drive(frames, mode="wake", transcriber=Texts(["지니야"]), repository=SpyRepository())
        kinds = [e["kind"] for e in sent]
        self.assertEqual(kinds.count("final"), 1, kinds)                            # 버린 발화의 final은 없다
        self.assertNotIn("__closed__", kinds)
        # abort 두 번 → 세대 2. 그 뒤 새 발화의 이벤트만 세대 2를 단다.
        self.assertEqual([e["epoch"] for e in sent if e["kind"] == "final"], [2])
        self.assertEqual(sent[1]["epoch"], 0)                                      # session 다음 첫 이벤트

    def test_command_mode_is_unchanged(self):
        repo = SpyRepository()
        flush = {"type": "websocket.receive", "text": json.dumps({"type": "flush"})}
        sent = drive([SPEECH] * 40 + [flush], mode="", transcriber=Texts(["A자재 옮겨줘"]), repository=repo)
        kinds = [e["kind"] for e in sent]
        self.assertEqual(kinds.count("final"), 1)
        self.assertEqual(sent[0]["mode"], "command")
        self.assertTrue(repo.calls)                                                # 일반 모드는 저장한다


if __name__ == "__main__":
    unittest.main(verbosity=2)
