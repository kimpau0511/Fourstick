"""스트리밍 STT 세션 검증 (md/개발플랜.md 4-03 ~ 4-08).

실제 모델·오디오·시간을 쓰지 않는다. VAD와 전사 백엔드는 결정적 test double을
주입하고, 오디오 시각과 UTC 시각은 모두 값으로 넣는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.policy import SttModelConfig, SttPolicy, SttVerification
from core.reason_codes import ReasonCode
from storage.records import RequestRecord
from storage.repository import IntegrityViolation, Repository, StorageError
from storage.sqlite.repository import SqliteRepository
from stt.protocol import ServerMessage, SttSessionState
from stt.transcription_guard import TranscriptionGuard
from stt.streaming_session import (
    StreamingSTTSession,
    SttSessionError,
    TermCorrection,
)
from stt.whisper_backend import Transcript

SR = 16_000
FRAME = b"\x00\x00" * 160   # 10ms PCM16 mono


def make_policy(**over) -> SttPolicy:
    kw = dict(
        policy_version="fixture", max_audio_sec=5.0, window_sec=2.0,
        window_stride_sec=0.5, silence_end_sec=0.3, speech_start_sec=0.1,
        min_final_confidence=0.6, stop_keywords=("STOPWORD",),
        transcribe_deadline_sec=20.0, max_concurrent_transcriptions=2,
        provenance={
            k: "fixture" for k in (
                "max_audio_sec", "window_sec", "window_stride_sec",
                "silence_end_sec", "speech_start_sec", "min_final_confidence",
                "transcribe_deadline_sec", "max_concurrent_transcriptions",
            )
        },
    )
    kw.update(over)
    return SttPolicy(**kw)


class ScriptedVad:
    """호출 순서대로 음성/무음을 돌려준다."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:
        value = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        return value


class ScriptedTranscriber:
    """호출 순서대로 전사 결과를 돌려준다. 마지막 값을 반복한다."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript:
        value = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value


class BrokenRepository(Repository):
    """저장이 항상 실패하는 저장소."""

    def __init__(self, error: Exception):
        self._error = error

    def create_session(self, record): raise self._error
    def get_session(self, session_id): raise self._error
    def touch_session(self, session_id, *, at): raise self._error
    def close_session(self, session_id, *, at, status): raise self._error
    def expire_idle_sessions(self, *, now, idle_timeout_sec): raise self._error
    def requests_for_session(self, session_id): raise self._error
    def get_approval(self, approval_id): raise self._error
    def executions_for_session(self, session_id): raise self._error
    def execution_environment_counts(self): raise self._error
    def record_robot_profile(self, record): raise self._error
    def append_sim_verification(self, record): raise self._error
    def sim_verifications(self, *, limit=50, composite_profile_id=None): raise self._error
    def get_robot_profile(self, cid, version): raise self._error
    def robot_profiles(self): raise self._error
    def append_validation_run(self, record): raise self._error
    def get_validation_run(self, validation_run_id): raise self._error
    def validation_runs_for_plan(self, plan_id, *, validator_kind=None): raise self._error
    def validation_runs_for_execution(self, execution_id): raise self._error
    def save_request(self, record): raise self._error
    def get_request(self, request_id): raise self._error
    def save_request_with_stt_inference(self, record, inference): raise self._error
    def stt_inferences_by_path(self, execution_path): raise self._error
    def legacy_stt_metadata(self, request_id): raise self._error
    def append_stt_inference(self, record): raise self._error
    def get_stt_inference(self, stt_inference_id): raise self._error
    def stt_inferences_for_session(self, session_id): raise self._error
    def stt_inferences_for_request(self, request_id): raise self._error
    def next_stt_attempt_no(self, session_id): raise self._error
    def selected_stt_inference(self, request_id): raise self._error
    def append_approval(self, record): raise self._error
    def approvals_for_plan(self, plan_id): raise self._error
    def latest_approval(self, plan_id): raise self._error
    def append_planning_attempt(self, record): raise self._error
    def get_planning_attempt(self, planning_attempt_id): raise self._error
    def planning_attempts_for_request(self, request_id): raise self._error
    def planning_attempt_for_plan(self, plan_id): raise self._error
    def next_planning_attempt_no(self, request_id): raise self._error
    def save_plan(self, *a, **k): raise self._error
    def get_plan(self, plan_id): raise self._error
    def plans_for_request(self, request_id): raise self._error
    def save_validation(self, record): raise self._error
    def validations_for_plan(self, plan_id): raise self._error
    def save_permit(self, record): raise self._error
    def permits_for_plan(self, plan_id): raise self._error
    def begin_execution(self, **k): raise self._error
    def get_execution(self, execution_id): raise self._error
    def executions_for_request(self, request_id): raise self._error
    def append_state_transition(self, execution_id, **k): raise self._error
    def state_transitions(self, execution_id): raise self._error
    def append_observation(self, execution_id, **k): raise self._error
    def observations(self, execution_id): raise self._error
    def append_result(self, execution_id, result, recorded_at): raise self._error
    def results(self, execution_id): raise self._error
    def trace(self, execution_id): raise self._error
    def close(self): pass
    def schema_version(self): return 0


#: 실행 기록에 남길 시험용 구성. 이 환경에서 실제 검증한 저사양 Profile을 흉내낸다.
MODEL_CONFIG = SttModelConfig(
    config_version="test-1.0", profile_id="test-cpu-int8",
    verification=SttVerification.VERIFIED,
    model_name="small", device="cpu", compute_type="int8", language="ko",
    sample_rate_hz=SR, vad_threshold=0.5,
    load_timeout_sec=60.0, transcribe_timeout_sec=60.0,
    provenance={k: "테스트" for k in
                ("model_name", "device", "compute_type", "vad_threshold")},
)


def make_session(
    *, vad_script=(True,), results=None, policy=None, terms=None, repo=None,
    model_config=MODEL_CONFIG, clock=None, guard=None,
):
    policy = policy or make_policy()
    ticks = clock or (lambda: 0.0)
    # 저장소를 붙인 운영 세션은 Guard 없이 만들 수 없다. 테스트도 같은 규칙을
    # 따른다 — 예산 없이 도는 경로를 테스트에서만 허용하지 않는다.
    if repo is not None and guard is None:
        guard = TranscriptionGuard(policy, clock=ticks)
    return StreamingSTTSession(
        request_id="req_1",
        policy=policy,
        vad=ScriptedVad(vad_script),
        transcriber=ScriptedTranscriber(results or [Transcript("옮겨줘", 0.9)]),
        sample_rate_hz=SR,
        schema_version=TASK_PLAN_SCHEMA_VERSION,
        terms=terms,
        repository=repo,
        session_id="sess_1",
        model_config=model_config,
        model_version="test-rev",
        model_load_sec=5.38,
        confidence_metric="exp(avg_logprob) 길이가중평균",
        clock=ticks,
        guard=guard,
    )


def kinds(events):
    return [e.kind for e in events]


class TestSessionLifecycle(unittest.TestCase):
    """4-03 — 연결별 세션."""

    def test_start_reports_ready_then_listening(self):
        s = make_session()
        events = s.start(at_utc=100.0)
        self.assertEqual(kinds(events), [ServerMessage.READY, ServerMessage.STATE])
        self.assertIs(s.state, SttSessionState.LISTENING)

    def test_audio_before_start_is_refused(self):
        s = make_session()
        with self.assertRaises(SttSessionError) as ctx:
            s.feed_audio(FRAME, at_sec=0.0, at_utc=100.0)
        self.assertIs(ctx.exception.reason, ReasonCode.STT_STREAM_ABORTED)

    def test_stop_closes_the_session(self):
        s = make_session()
        s.start(at_utc=100.0)
        events = s.stop(at_utc=101.0)
        self.assertIs(s.state, SttSessionState.CLOSED)
        self.assertIn(ServerMessage.CLOSED, kinds(events))

    def test_every_event_carries_request_id_and_utc(self):
        s = make_session()
        for e in s.start(at_utc=100.0):
            self.assertEqual(e.request_id, "req_1")
            self.assertEqual(e.at_utc, 100.0)

    def test_sample_rate_must_be_positive(self):
        with self.assertRaises(SttSessionError):
            StreamingSTTSession(
                request_id="r", policy=make_policy(), vad=ScriptedVad([True]),
                transcriber=ScriptedTranscriber([Transcript("x", 1.0)]),
                sample_rate_hz=0, schema_version="2.0",
            )


class TestBufferLimit(unittest.TestCase):
    """4-03 — 버퍼 제한. 긴 발화가 정해진 오류로 종료된다."""

    def test_audio_over_the_limit_ends_with_a_defined_error(self):
        s = make_session(policy=make_policy(max_audio_sec=1.0, window_sec=0.5))
        s.start(at_utc=100.0)
        events = s.feed_audio(FRAME, at_sec=1.5, at_utc=101.0)
        self.assertIs(events[0].reason, ReasonCode.STT_STREAM_ABORTED)
        self.assertIs(s.state, SttSessionState.CLOSED)

    def test_buffered_seconds_are_tracked(self):
        s = make_session(vad_script=[False])
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.4, at_utc=100.4)
        self.assertEqual(s.buffered_sec, 0.4)


class TestPartialAndRevision(unittest.TestCase):
    """4-05 — rolling window 전사와 partial revision."""

    def _speak(self, s, texts_at):
        out = []
        for at_sec in texts_at:
            out.extend(s.feed_audio(FRAME, at_sec=at_sec, at_utc=100.0 + at_sec))
        return out

    def test_partial_is_emitted_while_speaking(self):
        s = make_session(
            vad_script=[True], results=[Transcript("옮", 0.8), Transcript("옮겨", 0.85)]
        )
        s.start(at_utc=100.0)
        events = self._speak(s, [0.1, 0.2, 0.8])
        partials = [e for e in events if e.kind is ServerMessage.PARTIAL]
        self.assertTrue(partials)
        self.assertIs(s.state, SttSessionState.SPEAKING)

    def test_unchanged_partial_is_not_re_emitted(self):
        s = make_session(vad_script=[True], results=[Transcript("같은말", 0.8)])
        s.start(at_utc=100.0)
        events = self._speak(s, [0.1, 0.2, 0.8, 1.4, 2.0])
        partials = [e for e in events if e.kind is ServerMessage.PARTIAL]
        self.assertEqual(len(partials), 1, "내용이 같으면 한 번만 내보낸다")

    def test_stride_limits_transcription_frequency(self):
        transcriber = ScriptedTranscriber([Transcript("x", 0.9)])
        s = StreamingSTTSession(
            request_id="req_1", policy=make_policy(window_stride_sec=1.0),
            vad=ScriptedVad([True]), transcriber=transcriber, sample_rate_hz=SR,
            schema_version="2.0",
        )
        s.start(at_utc=100.0)
        for at in (0.1, 0.2, 0.3, 0.4):
            s.feed_audio(FRAME, at_sec=at, at_utc=100.0 + at)
        self.assertLessEqual(transcriber.calls, 1, "stride보다 잦게 전사하지 않는다")

    def test_backend_failure_becomes_an_error_event(self):
        s = make_session(vad_script=[True], results=[RuntimeError("모델 없음")])
        s.start(at_utc=100.0)
        events = self._speak(s, [0.1, 0.2, 0.8])
        errors = [e for e in events if e.kind is ServerMessage.ERROR]
        self.assertTrue(errors)
        self.assertIs(errors[0].reason, ReasonCode.STT_BACKEND_UNAVAILABLE)


class TestFinalOnce(unittest.TestCase):
    """4-06 — 무음 또는 end에서 final 한 번만 확정."""

    def _run_to_silence(self, s):
        s.start(at_utc=100.0)
        events = []
        for at, in_speech in ((0.1, True), (0.2, True), (0.3, False), (0.8, False)):
            s._vad = ScriptedVad([in_speech])
            events.extend(s.feed_audio(FRAME, at_sec=at, at_utc=100.0 + at))
        return events

    def test_silence_produces_exactly_one_final(self):
        s = make_session(results=[Transcript("옮겨줘", 0.9)])
        events = self._run_to_silence(s)
        finals = [e for e in events if e.kind is ServerMessage.FINAL]
        self.assertEqual(len(finals), 1)
        self.assertTrue(s.final_emitted)

    def test_session_is_closed_after_final_so_flush_is_refused(self):
        """final 확정 후 세션은 CLOSED다. CLOSED는 어떤 클라이언트 메시지도
        받지 않으므로(stt/protocol.py) flush는 거부된다 — 두 번째 final이
        나올 경로가 아예 없다."""
        s = make_session(results=[Transcript("옮겨줘", 0.9)])
        self._run_to_silence(s)
        self.assertIs(s.state, SttSessionState.CLOSED)
        self.assertTrue(s.final_emitted)
        with self.assertRaises(SttSessionError):
            s.flush(at_sec=1.0, at_utc=101.0)

    def test_flush_without_audio_reports_no_speech(self):
        s = make_session()
        s.start(at_utc=100.0)
        events = s.flush(at_sec=0.0, at_utc=100.5)
        self.assertIs(events[0].reason, ReasonCode.STT_NO_SPEECH)
        self.assertIs(s.state, SttSessionState.CLOSED)

    def test_final_carries_confidence_and_raw_text(self):
        s = make_session(
            vad_script=[True], results=[Transcript("옮겨줘", 0.91)],
            terms=TermCorrection({"옮겨줘": "이송해줘"}),
        )
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        events = s.flush(at_sec=1.0, at_utc=101.0)
        final = next(e for e in events if e.kind is ServerMessage.FINAL)
        self.assertEqual(final.text, "이송해줘")
        self.assertEqual(final.raw_text, "옮겨줘")
        self.assertEqual(final.confidence, 0.91)


class TestTermCorrectionAndConfidence(unittest.TestCase):
    """4-07 — 승인된 현장 용어 후보정과 신뢰도 기록."""

    def test_low_confidence_asks_instead_of_planning(self):
        s = make_session(results=[Transcript("잘 안들림", 0.3)])
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        events = s.flush(at_sec=0.5, at_utc=100.5)
        clarify = next(e for e in events if e.kind is ServerMessage.CLARIFY)
        self.assertIs(clarify.reason, ReasonCode.STT_LOW_CONFIDENCE)
        self.assertEqual(clarify.confidence, 0.3)
        self.assertNotIn(ServerMessage.FINAL, kinds(events))

    def test_terms_come_from_configuration_not_code(self):
        s = make_session(
            results=[Transcript("에이자재", 0.9)],
            terms=TermCorrection({"에이자재": "A자재"}),
        )
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        final = next(
            e for e in s.flush(at_sec=0.5, at_utc=100.5) if e.kind is ServerMessage.FINAL
        )
        self.assertEqual(final.text, "A자재")

    def test_no_terms_means_no_correction(self):
        s = make_session(results=[Transcript("그대로", 0.9)])
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        final = next(
            e for e in s.flush(at_sec=0.5, at_utc=100.5) if e.kind is ServerMessage.FINAL
        )
        self.assertEqual(final.text, "그대로")


class TestVoiceStopBypass(unittest.TestCase):
    """4-08 — partial STOP 키워드가 계획 생성을 우회한다."""

    def test_stop_keyword_in_partial_bypasses_planning(self):
        s = make_session(
            vad_script=[True], results=[Transcript("STOPWORD 지금", 0.8)]
        )
        s.start(at_utc=100.0)
        events = []
        for at in (0.1, 0.2, 0.8):
            events.extend(s.feed_audio(FRAME, at_sec=at, at_utc=100.0 + at))
        stop_events = [e for e in events if e.reason is ReasonCode.EXEC_STOPPED]
        self.assertTrue(stop_events, "정지 이벤트가 나와야 한다")
        self.assertTrue(s.stop_requested)
        self.assertNotIn(ServerMessage.FINAL, kinds(events))
        self.assertIs(s.state, SttSessionState.CLOSED)

    def test_stop_closes_the_session_so_no_final_can_follow(self):
        s = make_session(vad_script=[True], results=[Transcript("STOPWORD", 0.9)])
        s.start(at_utc=100.0)
        for at in (0.1, 0.2, 0.8):
            s.feed_audio(FRAME, at_sec=at, at_utc=100.0 + at)
        self.assertTrue(s.stop_requested)
        self.assertIs(s.state, SttSessionState.CLOSED)
        self.assertFalse(s.final_emitted)
        with self.assertRaises(SttSessionError):
            s.flush(at_sec=1.0, at_utc=101.0)

    def test_abort_message_bypasses_transcription(self):
        transcriber = ScriptedTranscriber([Transcript("무엇이든", 0.9)])
        s = StreamingSTTSession(
            request_id="req_1", policy=make_policy(), vad=ScriptedVad([True]),
            transcriber=transcriber, sample_rate_hz=SR, schema_version="2.0",
        )
        s.start(at_utc=100.0)
        events = s.abort(at_utc=100.5)
        self.assertIs(events[0].reason, ReasonCode.EXEC_STOPPED)
        self.assertEqual(transcriber.calls, 0, "전사를 기다리지 않는다")
        self.assertTrue(s.stop_requested)

    def test_keywords_come_from_policy(self):
        s = make_session(
            vad_script=[True], results=[Transcript("멈춰", 0.9)],
            policy=make_policy(stop_keywords=("멈춰",)),
        )
        s.start(at_utc=100.0)
        events = []
        for at in (0.1, 0.2, 0.8):
            events.extend(s.feed_audio(FRAME, at_sec=at, at_utc=100.0 + at))
        self.assertTrue(s.stop_requested)


class TestPersistenceLinkage(unittest.TestCase):
    """향후 DB 저장 고려 — 저장은 repository 인터페이스로만, 실패를 숨기지 않는다."""

    def _finalize(self, repo):
        s = make_session(results=[Transcript("옮겨줘", 0.9)], repo=repo)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        return next(
            e for e in s.flush(at_sec=0.5, at_utc=100.5) if e.kind is ServerMessage.FINAL
        )

    def test_final_persists_the_request_with_utc_and_schema_version(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        final = self._finalize(repo)
        self.assertTrue(final.persisted)
        stored = repo.get_request("req_1")
        self.assertEqual(stored.utterance, "옮겨줘")
        self.assertEqual(stored.schema_version, TASK_PLAN_SCHEMA_VERSION)
        self.assertEqual(stored.created_at, 100.5)

    def test_no_repository_means_no_persist_claim(self):
        final = self._finalize(None)
        self.assertIsNone(final.persisted)

    def test_storage_failure_is_reported_not_hidden(self):
        final = self._finalize(
            BrokenRepository(StorageError(ReasonCode.CONFIG_INVALID, "디스크 오류"))
        )
        self.assertFalse(final.persisted)
        self.assertIs(final.persist_reason, ReasonCode.CONFIG_INVALID)
        self.assertTrue(final.persist_recoverable)

    def test_integrity_violation_is_not_recoverable(self):
        final = self._finalize(
            BrokenRepository(IntegrityViolation(ReasonCode.CONFIG_INVALID, "중복 id"))
        )
        self.assertFalse(final.persisted)
        self.assertFalse(final.persist_recoverable)

    def test_unexpected_failure_is_still_reported(self):
        final = self._finalize(BrokenRepository(RuntimeError("예상 못한 오류")))
        self.assertFalse(final.persisted)
        self.assertIsNotNone(final.persist_reason)

    def test_repeated_final_with_same_content_is_idempotent(self):
        """같은 request_id·같은 내용의 재전송은 저장에서 멱등이다.

        요청 내용과 채택된 실행 기록이 모두 같아야 멱등이다 — 신뢰도나 모델
        구성이 달라지면 같은 내용이 아니므로 거부되는 것이 맞다.
        """
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        first = self._finalize(repo)
        self.assertTrue(first.persisted)
        second = self._finalize(repo)   # 같은 세션 구성으로 다시 확정
        self.assertTrue(second.persisted)

    def test_request_keeps_only_the_selected_inference_identifier(self):
        """요청에는 채택한 실행 기록의 식별자만 남는다. 전사 본문·측정값은 없다."""
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        self._finalize(repo)
        request = repo.get_request("req_1")
        self.assertIsNotNone(request.selected_stt_inference_id)
        self.assertFalse(hasattr(request, "stt"))
        columns = {
            r[1] for r in repo._conn.execute("PRAGMA table_info(requests)").fetchall()
        }
        self.assertEqual(
            columns,
            {"request_id", "utterance", "schema_version", "created_at",
             "selected_stt_inference_id", "session_id"},
        )

    def test_selected_inference_carries_the_measurements(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        self._finalize(repo)
        inference = repo.selected_stt_inference("req_1")
        self.assertIsNotNone(inference)
        self.assertEqual(inference.transcript, "옮겨줘")
        self.assertEqual(inference.confidence, 0.9)
        self.assertTrue(inference.final_adopted)
        self.assertEqual(inference.request_id, "req_1")
        self.assertEqual(inference.session_id, "sess_1")
        self.assertEqual(inference.attempt_no, 1)
        self.assertEqual(inference.profile_id, "test-cpu-int8")
        self.assertEqual(inference.verification, "verified")
        self.assertEqual(inference.device, "cpu")
        self.assertEqual(inference.compute_type, "int8")
        self.assertEqual(inference.model_load_duration_ms, 5380)
        self.assertIsNone(inference.reason_code)
        self.assertTrue(inference.vad_speech_detected)

    def test_audio_and_processing_durations_are_separate_fields(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        ticks = iter([0.0, 2.75])   # 처리 2.75초
        s = make_session(results=[Transcript("옮겨줘", 0.9)], repo=repo,
                         clock=lambda: next(ticks))
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=1.0, at_utc=100.1)
        s.flush(at_sec=1.0, at_utc=100.5)
        inference = repo.selected_stt_inference("req_1")
        self.assertEqual(inference.audio_duration_ms, 1000)
        self.assertEqual(inference.processing_duration_ms, 2750)
        # RTF는 원자료에서 파생한다 — 따로 저장해 어긋나게 두지 않는다.
        self.assertAlmostEqual(inference.rtf, 2.75)
        self.assertAlmostEqual(inference.throughput, 1 / 2.75)

    def test_low_confidence_attempt_is_recorded_but_not_adopted(self):
        """되묻기로 끝난 시도도 남는다. 요청은 만들어지지 않는다."""
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        s = make_session(results=[Transcript("어", 0.1)], repo=repo)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        s.flush(at_sec=0.5, at_utc=100.5)
        rows = repo.stt_inferences_for_session("sess_1")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].final_adopted)
        self.assertIs(rows[0].reason_code, ReasonCode.STT_LOW_CONFIDENCE)
        self.assertIsNone(rows[0].request_id)
        with self.assertRaises(StorageError):
            repo.get_request("req_1")

    def test_backend_failure_attempt_is_recorded(self):
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        s = make_session(results=[RuntimeError("모델 없음")], repo=repo)
        s.start(at_utc=100.0)
        s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        s.flush(at_sec=0.5, at_utc=100.5)
        rows = repo.stt_inferences_for_session("sess_1")
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0].reason_code, ReasonCode.STT_BACKEND_UNAVAILABLE)
        self.assertEqual(rows[0].transcript, "")
        self.assertFalse(rows[0].final_adopted)

    def test_retry_appends_a_new_attempt_without_overwriting(self):
        """재전사는 새 기록을 더한다 — 이전 시도를 덮어쓰지 않는다."""
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        first = make_session(results=[Transcript("어", 0.1)], repo=repo)
        first.start(at_utc=100.0)
        first.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
        first.flush(at_sec=0.5, at_utc=100.5)

        second = make_session(results=[Transcript("옮겨줘", 0.95)], repo=repo)
        second.start(at_utc=200.0)
        second.feed_audio(FRAME, at_sec=0.1, at_utc=200.1)
        second.flush(at_sec=0.5, at_utc=200.5)

        rows = repo.stt_inferences_for_session("sess_1")
        self.assertEqual([r.attempt_no for r in rows], [1, 2])
        self.assertEqual([r.transcript for r in rows], ["어", "옮겨줘"])
        self.assertEqual([r.final_adopted for r in rows], [False, True])
        self.assertEqual(
            repo.get_request("req_1").selected_stt_inference_id,
            rows[1].stt_inference_id,
        )

    def test_partial_and_pcm_are_not_stored(self):
        """partial transcript와 PCM 오디오는 DB에 저장하지 않는다."""
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        self._finalize(repo)
        columns = {
            r[1] for r in repo._conn.execute("PRAGMA table_info(requests)").fetchall()
        }
        for banned in ("partial", "audio", "pcm", "waveform"):
            with self.subTest(banned=banned):
                self.assertFalse(
                    [c for c in columns if banned in c], f"{banned} 관련 컬럼이 있다"
                )

    def test_session_does_not_touch_sqlite3(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("streaming_session.py", "vad.py", "whisper_backend.py", "protocol.py"):
            with self.subTest(file=name):
                text = (root / "stt" / name).read_text(encoding="utf-8")
                self.assertNotIn("import sqlite3", text)
                self.assertNotIn("sqlite3.", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
