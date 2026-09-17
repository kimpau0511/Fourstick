"""STT 장시간 처리 위험 — 시간·동시성 예산 검증.

실측 근거: 220Hz 순음 2초를 small/cpu/int8로 전사하면 42~65초가 걸린다
(RTF 21~32). 침묵·잡음도 3.5~3.8초를 쓴다. 발화가 아닌 신호가 세션을 이만큼
붙잡으면 후속 요청이 밀린다. 여기서 확인하는 것:

1. VAD가 발화로 보지 않은 오디오는 전사에 넘어가지 않는다.
2. 최대 음성 길이·처리 제한시간·동시 처리 수가 Policy에서 온다.
3. 제한시간 초과가 이유 코드와 복구 가능 여부로 돌아온다.
4. 긴 추론이 세션과 후속 요청을 무기한 점유하지 않는다.
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.policy import PolicyError, SttPolicy
from core.reason_codes import ReasonCode
from storage.records import SttExecutionPath
from storage.sqlite.repository import SqliteRepository
from stt.protocol import ServerMessage
from stt.transcription_guard import TranscriptionGuard
from stt.whisper_backend import Transcript

sys.path.insert(0, str(ROOT / "tests"))
from unit.test_stt_session import (  # noqa: E402
    FRAME,
    MODEL_CONFIG,
    ScriptedTranscriber,
    make_policy,
    make_session,
)


class SlowTranscriber:
    """정해진 시간 동안 도는 전사기. 순음처럼 오래 걸리는 추론을 흉내낸다."""

    def __init__(self, seconds: float, text: str = "옮겨줘", confidence: float = 0.9):
        self.seconds, self.text, self.confidence = seconds, text, confidence
        self.started = threading.Event()
        self.calls = 0

    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript:
        self.calls += 1
        self.started.set()
        time.sleep(self.seconds)
        return Transcript(text=self.text, confidence=self.confidence)


class CountingTranscriber:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript:
        self.calls += 1
        return Transcript(text="옮겨줘", confidence=0.9)


class TestPolicyCarriesTheBudget(unittest.TestCase):
    """2 — 최대 음성 길이·제한시간·동시 처리 수는 Policy에서 온다."""

    def test_policy_holds_all_three_values(self):
        p = make_policy()
        self.assertEqual(p.max_audio_sec, 5.0)
        self.assertEqual(p.transcribe_deadline_sec, 20.0)
        self.assertEqual(p.max_concurrent_transcriptions, 2)

    def test_missing_deadline_is_refused(self):
        with self.assertRaises(TypeError):
            SttPolicy(
                policy_version="x", max_audio_sec=5.0, window_sec=2.0,
                window_stride_sec=0.5, silence_end_sec=0.3, speech_start_sec=0.1,
                min_final_confidence=0.6, stop_keywords=("정지",),
                max_concurrent_transcriptions=1, provenance={},
            )

    def test_non_positive_deadline_is_refused(self):
        with self.assertRaises(PolicyError) as ctx:
            make_policy(transcribe_deadline_sec=0.0)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)

    def test_zero_concurrency_is_refused(self):
        with self.assertRaises(PolicyError):
            make_policy(max_concurrent_transcriptions=0)

    def test_budget_values_need_provenance(self):
        prov = {
            k: "fixture" for k in (
                "max_audio_sec", "window_sec", "window_stride_sec",
                "silence_end_sec", "speech_start_sec", "min_final_confidence",
            )
        }
        with self.assertRaises(PolicyError) as ctx:
            make_policy(provenance=prov)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)


class TestVadGate(unittest.TestCase):
    """1 — VAD가 발화로 판정하지 않은 오디오는 전사되지 않는다."""

    def test_non_speech_audio_is_never_transcribed_on_flush(self):
        t = CountingTranscriber()
        s = make_session(vad_script=[False], results=[Transcript("x", 0.9)])
        s._transcriber = t
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.5, at_utc=100.5)
        events = s.flush(at_sec=1.0, at_utc=101.0)
        self.assertEqual(t.calls, 0, "발화가 없는데 전사를 호출했다")
        errors = [e for e in events if e.kind is ServerMessage.ERROR]
        self.assertTrue(errors)
        self.assertIs(errors[0].reason, ReasonCode.STT_NO_SPEECH)
        self.assertTrue(errors[0].persist_recoverable)

    def test_speech_audio_is_transcribed(self):
        t = CountingTranscriber()
        s = make_session(vad_script=[True])
        s._transcriber = t
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.5, at_utc=100.5)
        s.flush(at_sec=1.0, at_utc=101.0)
        self.assertGreater(t.calls, 0)

    def test_non_speech_flush_is_recorded_without_transcription(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        s = make_session(vad_script=[False], repo=repo)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.5, at_utc=100.5)
        s.flush(at_sec=1.0, at_utc=101.0)
        rows = repo.stt_inferences_for_session("sess_1")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].reason_code, ReasonCode.STT_NO_SPEECH)
        self.assertEqual(rows[0].processing_duration_ms, 0)
        self.assertFalse(rows[0].vad_speech_detected)
        self.assertFalse(rows[0].final_adopted)


class TestGuardDeadline(unittest.TestCase):
    """3·4 — 제한시간 초과가 세션을 즉시 놓아준다."""

    def test_timeout_returns_a_reason_code_not_an_exception(self):
        guard = TranscriptionGuard(make_policy(transcribe_deadline_sec=0.2))
        self.addCleanup(guard.close)
        slow = SlowTranscriber(2.0)
        started = time.monotonic()
        outcome = guard.run(slow, b"\x00\x00" * 100, 16_000)
        waited = time.monotonic() - started

        self.assertFalse(outcome.ok)
        self.assertIs(outcome.reason, ReasonCode.STT_TRANSCRIBE_TIMEOUT)
        self.assertTrue(outcome.recoverable, "재시도 가능한 실패다")
        self.assertTrue(outcome.abandoned)
        # 예산 0.2초에 2초짜리 작업을 넣었다 — 호출자는 0.2초 근처에서 풀려야 한다.
        self.assertLess(waited, 1.0, f"제한시간이 호출자를 놓아주지 않았다({waited:.2f}s)")

    def test_abandoned_work_keeps_running_and_is_reported(self):
        """스레드를 죽일 수 없다. 숨기지 않고 abandoned로 드러낸다."""
        guard = TranscriptionGuard(make_policy(transcribe_deadline_sec=0.2))
        self.addCleanup(guard.close)
        slow = SlowTranscriber(1.0)
        outcome = guard.run(slow, b"\x00\x00" * 100, 16_000)
        self.assertTrue(outcome.abandoned)
        self.assertTrue(slow.started.is_set())
        self.assertEqual(guard.inflight, 1, "버려진 작업이 슬롯을 점유한다")

    def test_backend_failure_is_not_marked_recoverable(self):
        class Broken:
            def transcribe(self, audio, sample_rate_hz):
                raise RuntimeError("모델 없음")

        guard = TranscriptionGuard(make_policy())
        self.addCleanup(guard.close)
        outcome = guard.run(Broken(), b"\x00\x00", 16_000)
        self.assertIs(outcome.reason, ReasonCode.STT_BACKEND_UNAVAILABLE)
        self.assertFalse(outcome.recoverable)
        self.assertFalse(outcome.abandoned)

    def test_backend_reason_code_is_preserved(self):
        class Refusing:
            def transcribe(self, audio, sample_rate_hz):
                exc = RuntimeError("샘플레이트 불일치")
                exc.reason = ReasonCode.CONFIG_UNIT_MISMATCH
                raise exc

        guard = TranscriptionGuard(make_policy())
        self.addCleanup(guard.close)
        self.assertIs(
            guard.run(Refusing(), b"\x00\x00", 16_000).reason,
            ReasonCode.CONFIG_UNIT_MISMATCH,
        )

    def test_success_reports_the_wait(self):
        guard = TranscriptionGuard(make_policy())
        self.addCleanup(guard.close)
        outcome = guard.run(CountingTranscriber(), b"\x00\x00", 16_000)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.transcript.text, "옮겨줘")
        self.assertIsNone(outcome.reason)
        self.assertFalse(outcome.abandoned)


class TestGuardConcurrency(unittest.TestCase):
    """4 — 후속 요청이 대기열에서 무기한 기다리지 않는다."""

    def test_full_capacity_is_refused_immediately(self):
        guard = TranscriptionGuard(
            make_policy(max_concurrent_transcriptions=1, transcribe_deadline_sec=0.2)
        )
        self.addCleanup(guard.close)
        # 1번 요청이 제한시간을 넘겨 버려진다 — 슬롯은 아직 점유 중이다.
        first = guard.run(SlowTranscriber(1.5), b"\x00\x00", 16_000)
        self.assertTrue(first.abandoned)
        self.assertEqual(guard.free_slots, 0)

        started = time.monotonic()
        second = guard.run(CountingTranscriber(), b"\x00\x00", 16_000)
        waited = time.monotonic() - started
        self.assertIs(second.reason, ReasonCode.STT_CAPACITY_EXCEEDED)
        self.assertTrue(second.recoverable)
        self.assertLess(waited, 0.1, "거절이 아니라 대기했다")

    def test_slot_is_returned_when_the_abandoned_work_finishes(self):
        guard = TranscriptionGuard(
            make_policy(max_concurrent_transcriptions=1, transcribe_deadline_sec=0.1)
        )
        self.addCleanup(guard.close)
        guard.run(SlowTranscriber(0.4), b"\x00\x00", 16_000)
        self.assertEqual(guard.free_slots, 0)
        deadline = time.monotonic() + 3.0
        while guard.free_slots == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(guard.free_slots, 1, "작업이 끝났는데 슬롯이 돌아오지 않았다")
        self.assertTrue(guard.run(CountingTranscriber(), b"\x00\x00", 16_000).ok)

    def test_capacity_comes_from_policy(self):
        guard = TranscriptionGuard(make_policy(max_concurrent_transcriptions=3))
        self.addCleanup(guard.close)
        self.assertEqual(guard.free_slots, 3)


class TestSessionUsesTheGuard(unittest.TestCase):
    def test_session_timeout_closes_without_a_final(self):
        guard = TranscriptionGuard(make_policy(transcribe_deadline_sec=0.2))
        self.addCleanup(guard.close)
        s = make_session(vad_script=[True], guard=guard)
        s._transcriber = SlowTranscriber(1.5)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.5, at_utc=100.5)
        started = time.monotonic()
        events = s.flush(at_sec=1.0, at_utc=101.0)
        waited = time.monotonic() - started

        errors = [e for e in events if e.kind is ServerMessage.ERROR]
        self.assertTrue(errors)
        self.assertIs(errors[0].reason, ReasonCode.STT_TRANSCRIBE_TIMEOUT)
        self.assertTrue(errors[0].persist_recoverable)
        self.assertNotIn(ServerMessage.FINAL, [e.kind for e in events])
        self.assertLess(waited, 1.0, "세션이 추론이 끝날 때까지 붙잡혔다")

    def test_timeout_is_recorded_as_an_attempt(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        guard = TranscriptionGuard(make_policy(transcribe_deadline_sec=0.2))
        self.addCleanup(guard.close)
        s = make_session(vad_script=[True], repo=repo, guard=guard)
        s._transcriber = SlowTranscriber(1.0)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.5, at_utc=100.5)
        s.flush(at_sec=1.0, at_utc=101.0)
        rows = repo.stt_inferences_for_session("sess_1")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].reason_code, ReasonCode.STT_TRANSCRIBE_TIMEOUT)
        self.assertFalse(rows[0].final_adopted)

    def test_operational_session_with_storage_needs_a_guard(self):
        """예산 없이 도는 운영 세션을 만들 수 없다."""
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        from core.constants import TASK_PLAN_SCHEMA_VERSION
        from stt.streaming_session import StreamingSTTSession, SttSessionError
        from unit.test_stt_session import ScriptedVad

        with self.assertRaises(SttSessionError) as ctx:
            StreamingSTTSession(
                request_id="req_1", policy=make_policy(), vad=ScriptedVad([True]),
                transcriber=ScriptedTranscriber([Transcript("x", 0.9)]),
                sample_rate_hz=16_000, schema_version=TASK_PLAN_SCHEMA_VERSION,
                repository=repo, session_id="s", model_config=MODEL_CONFIG,
            )
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)

    def test_session_records_the_operational_path(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        s = make_session(vad_script=[True], repo=repo)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        s.flush(at_sec=0.5, at_utc=100.5)
        row = repo.selected_stt_inference("req_1")
        self.assertIs(row.execution_path, SttExecutionPath.OPERATIONAL)


class TestEvaluationPathIsSeparate(unittest.TestCase):
    """5 — 직접 호출하는 평가는 운영 기록과 섞이지 않는다."""

    def test_evaluation_rows_are_labeled_and_never_adopted(self):
        from storage.records import SttInferenceRecord

        with self.assertRaises(ValueError):
            SttInferenceRecord(
                stt_inference_id="e1", session_id="eval", attempt_no=1,
                schema_version="2.0", created_at=1.0, model_name="small",
                model_version="rev", profile_id="p", profile_version="1.0",
                verification="verified", device="cpu", compute_type="int8",
                language="ko", audio_duration_ms=2000, processing_duration_ms=42220,
                transcript="", confidence=0.0, confidence_metric="m",
                final_adopted=True, request_id="req_1",
                execution_path=SttExecutionPath.EVALUATION,
            )

    def test_paths_can_be_counted_separately(self):
        from storage.records import SttInferenceRecord

        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)

        def row(rid, path, attempt):
            return SttInferenceRecord(
                stt_inference_id=rid, session_id="sess_mixed", attempt_no=attempt,
                schema_version="2.0", created_at=1.0, model_name="small",
                model_version="rev", profile_id="p", profile_version="1.0",
                verification="verified", device="cpu", compute_type="int8",
                language="ko", audio_duration_ms=2000, processing_duration_ms=3600,
                transcript="", confidence=0.0, confidence_metric="m",
                final_adopted=False, execution_path=path,
            )

        repo.append_stt_inference(row("o1", SttExecutionPath.OPERATIONAL, 1))
        repo.append_stt_inference(row("e1", SttExecutionPath.EVALUATION, 2))
        operational = repo.stt_inferences_by_path(SttExecutionPath.OPERATIONAL)
        evaluation = repo.stt_inferences_by_path(SttExecutionPath.EVALUATION)
        self.assertEqual([r.stt_inference_id for r in operational], ["o1"])
        self.assertEqual([r.stt_inference_id for r in evaluation], ["e1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
