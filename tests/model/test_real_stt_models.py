"""실제 모델 통합 테스트 (md/개발플랜.md 4-04·4-05·4-11).

**일반 단위 테스트와 분리된 경로다.** 모델 다운로드와 최초 로딩 시간이 60초
제한에 섞이지 않도록, 이 파일은 `FORSTICK_STT_MODEL_TESTS=1`일 때만 돌고
`scripts/run_stt_model_tests.sh`가 별도 제한으로 실행한다.

test double 기반 테스트(`tests/unit/test_stt_session.py`)는 그대로 유지한다 —
여기서는 실제 백엔드 배선과 실패 경로만 확인한다.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np

from core.policy import SttModelConfig, SttPolicy, SttVerification
from core.reason_codes import ReasonCode
from storage.sqlite.repository import SqliteRepository
from stt.evaluation import (
    EvalReport,
    evaluate_non_speech,
    evaluate_speech,
    load_cases,
    synthesize,
)
from stt.faster_whisper_backend import FasterWhisperBackend, TranscriberUnavailable
from stt.protocol import ServerMessage, SttSessionState
from stt.silero_vad_backend import (
    FRAME_SAMPLES,
    SileroVadBackend,
    VadUnavailable,
    bundled_model_path,
)
from stt.streaming_session import StreamingSTTSession
from stt.transcription_guard import TranscriptionGuard
from stt.whisper_backend import Transcript

ENABLED = os.environ.get("FORSTICK_STT_MODEL_TESTS") == "1"
SKIP_REASON = "실제 모델 테스트는 FORSTICK_STT_MODEL_TESTS=1 에서만 돈다"
SR = 16_000

PROV = {k: "테스트 fixture" for k in ("model_name", "device", "compute_type", "vad_threshold")}


def model_config(**over) -> SttModelConfig:
    kw = dict(
        config_version="test-1.0", profile_id="test-cpu-int8",
        verification=SttVerification.VERIFIED,
        model_name="small", device="cpu", compute_type="int8",
        language="ko", sample_rate_hz=SR, vad_threshold=0.5,
        load_timeout_sec=600.0, transcribe_timeout_sec=120.0, provenance=PROV,
    )
    kw.update(over)
    return SttModelConfig(**kw)


def stt_policy() -> SttPolicy:
    return SttPolicy(
        policy_version="test-1.0", max_audio_sec=10.0, window_sec=2.0,
        window_stride_sec=1.0, silence_end_sec=0.3, speech_start_sec=0.1,
        min_final_confidence=0.6, stop_keywords=("정지",),
        transcribe_deadline_sec=20.0, max_concurrent_transcriptions=2,
        provenance={
            k: "fixture" for k in (
                "max_audio_sec", "window_sec", "window_stride_sec",
                "silence_end_sec", "speech_start_sec", "min_final_confidence",
                "transcribe_deadline_sec", "max_concurrent_transcriptions",
            )
        },
    )


def pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestRealSileroVad(unittest.TestCase):
    """4-04 — 실제 Silero VAD 연결."""

    def test_model_file_is_outside_the_project_tree(self):
        path = Path(bundled_model_path())
        self.assertTrue(path.exists())
        # 설치된 패키지 안에 있고, 소스 트리(fixtures/·stt/)에 복사되지 않았다.
        self.assertIn("site-packages", str(path))

    def test_session_loads(self):
        backend = SileroVadBackend(model_config())
        backend.load()
        self.assertIsNotNone(backend._session)

    def test_rejects_synthetic_non_speech(self):
        """Silero는 에너지 검출기가 아니다 — 잡음·순음을 음성으로 보지 않는다.

        그래서 합성 신호로는 발화 시작·종료를 만들 수 없고, 그 검증에는 실제
        녹음이 필요하다(fixtures/stt/README.md).
        """
        t = np.arange(SR) / SR
        rng = np.random.default_rng(1)
        for label, signal in (
            ("침묵", np.zeros(SR)),
            ("백색잡음", rng.standard_normal(SR) * 0.3),
            ("순음", np.sin(2 * np.pi * 220 * t) * 0.3),
        ):
            with self.subTest(signal=label):
                backend = SileroVadBackend(model_config())
                self.assertFalse(backend.is_speech(pcm(signal), SR))
                self.assertLess(backend.last_probability, 0.5)

    def test_streaming_state_is_kept_between_calls(self):
        """프레임을 나눠 넣어도 상태가 이어져 판정이 흔들리지 않는다."""
        backend = SileroVadBackend(model_config())
        signal = np.zeros(FRAME_SAMPLES * 8)
        probs = []
        for i in range(8):
            chunk = signal[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES]
            backend.is_speech(pcm(chunk), SR)
            probs.append(backend.last_probability)
        self.assertEqual(len(probs), 8)
        self.assertLess(max(probs), 0.5)

    def test_partial_frame_keeps_previous_decision(self):
        """512 샘플이 안 모이면 판정을 못 한 것이다 — 무음으로 단정하지 않는다."""
        backend = SileroVadBackend(model_config())
        self.assertFalse(backend.is_speech(pcm(np.zeros(100)), SR))

    def test_sample_rate_mismatch_is_refused(self):
        backend = SileroVadBackend(model_config())
        with self.assertRaises(VadUnavailable) as ctx:
            backend.is_speech(pcm(np.zeros(FRAME_SAMPLES)), 8_000)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_unsupported_sample_rate_config_is_refused(self):
        with self.assertRaises(VadUnavailable):
            SileroVadBackend(model_config(sample_rate_hz=8_000))

    def test_odd_byte_frame_is_refused(self):
        backend = SileroVadBackend(model_config())
        with self.assertRaises(VadUnavailable) as ctx:
            backend.is_speech(b"\x00", SR)
        self.assertIs(ctx.exception.reason, ReasonCode.STT_STREAM_ABORTED)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestRealFasterWhisper(unittest.TestCase):
    """4-05 — 실제 faster-whisper 연결."""

    @classmethod
    def setUpClass(cls):
        cls.backend = FasterWhisperBackend(model_config())
        cls.load_report = cls.backend.load()

    def test_load_time_is_reported_separately(self):
        self.assertGreater(self.load_report.load_sec, 0.0)
        self.assertEqual(self.load_report.model_name, "small")
        self.assertEqual(self.load_report.device, "cpu")

    def test_silence_transcribes_to_empty_text(self):
        result = self.backend.transcribe(synthesize("generated:silence:1.0", SR), SR)
        self.assertIsInstance(result, Transcript)
        self.assertEqual(result.text, "")
        self.assertEqual(result.confidence, 0.0)

    def test_noise_returns_a_valid_transcript_object(self):
        """잡음에서 환각 문자열이 나올 수 있다. 여기서는 내용을 단정하지 않고
        계약(Transcript 범위)만 확인한다. 실측 결과는 보고서에 남긴다."""
        result = self.backend.transcribe(synthesize("generated:noise:1.0:0.1", SR), SR)
        self.assertIsInstance(result.text, str)
        self.assertGreaterEqual(result.confidence, 0.0)
        self.assertLessEqual(result.confidence, 1.0)

    def test_latency_and_realtime_factor_are_recorded(self):
        before = len(self.backend.reports)
        self.backend.transcribe(synthesize("generated:silence:1.0", SR), SR)
        report = self.backend.reports[-1]
        self.assertEqual(len(self.backend.reports), before + 1)
        self.assertGreater(report.process_sec, 0.0)
        self.assertAlmostEqual(report.audio_sec, 1.0, places=3)
        self.assertGreater(report.realtime_factor, 0.0)

    def test_empty_audio_is_refused(self):
        with self.assertRaises(TranscriberUnavailable) as ctx:
            self.backend.transcribe(b"", SR)
        self.assertIs(ctx.exception.reason, ReasonCode.STT_NO_SPEECH)

    def test_corrupted_audio_is_refused(self):
        with self.assertRaises(TranscriberUnavailable) as ctx:
            self.backend.transcribe(b"\x00", SR)
        self.assertIs(ctx.exception.reason, ReasonCode.STT_STREAM_ABORTED)

    def test_sample_rate_mismatch_is_refused(self):
        with self.assertRaises(TranscriberUnavailable) as ctx:
            self.backend.transcribe(synthesize("generated:silence:0.5", SR), 8_000)
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_UNIT_MISMATCH)

    def test_missing_model_is_reported_as_backend_unavailable(self):
        backend = FasterWhisperBackend(model_config(model_name="no_such_model_xyz"))
        with self.assertRaises(TranscriberUnavailable) as ctx:
            backend.load()
        self.assertIs(ctx.exception.reason, ReasonCode.STT_BACKEND_UNAVAILABLE)

    def test_transcribe_timeout_is_reported(self):
        backend = FasterWhisperBackend(model_config(transcribe_timeout_sec=0.001))
        backend._model = self.backend._model   # 로딩은 재사용하고 전사만 제한한다
        backend.load_report = self.load_report
        with self.assertRaises(TranscriberUnavailable) as ctx:
            backend.transcribe(synthesize("generated:noise:2.0:0.1", SR), SR)
        self.assertIs(ctx.exception.reason, ReasonCode.STT_TRANSCRIBE_TIMEOUT)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestSessionWithRealBackends(unittest.TestCase):
    """4-11 — 저장(합성) 음성으로 세션을 실제 백엔드와 함께 돌린다."""

    @classmethod
    def setUpClass(cls):
        cls.transcriber = FasterWhisperBackend(model_config())
        cls.transcriber.load()
        cls.backend = cls.transcriber

    def setUp(self):
        #: VAD 게이트가 전사를 막았는지 보기 위한 기준선.
        self._reports_before = len(self.backend.reports)

    def _session(self, repo=None) -> StreamingSTTSession:
        return StreamingSTTSession(
            request_id="req_model", policy=stt_policy(),
            vad=SileroVadBackend(model_config()), transcriber=self.transcriber,
            sample_rate_hz=SR, schema_version="2.0", repository=repo,
            guard=TranscriptionGuard(stt_policy()),
            session_id="sess_model", model_config=model_config(),
            model_version="faster-whisper-1.2.1",
            confidence_metric="exp(avg_logprob) 길이가중평균",
        )

    def test_non_speech_audio_never_enters_speaking(self):
        """실제 Silero가 음성이 아니라고 판정하므로 partial이 나오지 않는다."""
        session = self._session()
        session.start(at_utc=100.0)
        events = []
        noise = synthesize("generated:noise:0.1:0.2", SR)
        for i in range(10):
            events.extend(session.feed_audio(noise, at_sec=0.1 * (i + 1), at_utc=100.0 + i))
        self.assertIs(session.state, SttSessionState.LISTENING)
        self.assertNotIn(ServerMessage.PARTIAL, [e.kind for e in events])

    def test_silence_flush_ends_without_transcription(self):
        """침묵은 **전사에 가지 않는다.**

        이전에는 침묵도 전사해서 신뢰도 0을 받고 되묻기로 끝났다. 실제 모델에서
        침묵 2초에 3.6초, 순음 2초에 42~65초가 걸리는 것을 확인한 뒤로는 VAD가
        발화로 보지 않은 오디오를 전사에 넘기지 않는다. 결과는 같은 "요청 없음"
        이지만 추론을 낭비하지 않고 세션이 즉시 닫힌다.
        """
        session = self._session()
        session.start(at_utc=100.0)
        session.feed_audio(synthesize("generated:silence:0.5", SR), at_sec=0.5, at_utc=100.5)
        events = session.flush(at_sec=1.0, at_utc=101.0)
        kinds = [e.kind for e in events]
        self.assertTrue(session.final_emitted, "발화당 한 번만 확정된다")
        self.assertNotIn(ServerMessage.FINAL, kinds)
        errors = [e for e in events if e.kind is ServerMessage.ERROR]
        self.assertTrue(errors)
        self.assertIs(errors[0].reason, ReasonCode.STT_NO_SPEECH)
        self.assertIs(session.state, SttSessionState.CLOSED)

    def test_real_vad_refuses_synthetic_silence_before_transcription(self):
        """실제 Silero가 침묵을 발화로 보지 않아 전사 단계로 넘어가지 않는다."""
        session = self._session()
        session.start(at_utc=100.0)
        session.feed_audio(synthesize("generated:silence:0.5", SR), at_sec=0.5, at_utc=100.5)
        session.flush(at_sec=1.0, at_utc=101.0)
        self.assertEqual(
            len(self.backend.reports), self._reports_before,
            "전사 백엔드가 호출됐다 — VAD 게이트가 열려 있다",
        )

    def test_non_speech_attempt_is_recorded_without_a_request(self):
        """침묵은 요청이 생기지 않는다. **시도 기록은 남는다.**

        어떤 구성에서 어떤 이유로 끝났는지가 재전사·모델 비교의 자료이므로,
        채택되지 않은 시도도 append-only로 저장된다. 전사를 돌리지 않았으므로
        처리 시간은 0이다.
        """
        repo = SqliteRepository(now=0.0)
        self.addCleanup(repo.close)
        session = self._session(repo)
        session.start(at_utc=100.0)
        session.feed_audio(synthesize("generated:silence:0.5", SR), at_sec=0.5, at_utc=100.5)
        session.flush(at_sec=1.0, at_utc=101.0)
        from storage.repository import StorageError

        with self.assertRaises(StorageError):
            repo.get_request("req_model")

        rows = repo.stt_inferences_for_session("sess_model")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertFalse(row.final_adopted)
        self.assertIs(row.reason_code, ReasonCode.STT_NO_SPEECH)
        self.assertEqual(row.processing_duration_ms, 0, "전사를 돌리지 않았다")
        self.assertIsNone(row.request_id)
        # 실제 모델 구성이 기록에 그대로 남는다.
        self.assertEqual(row.model_name, "small")
        self.assertEqual(row.device, "cpu")
        self.assertEqual(row.compute_type, "int8")
        self.assertEqual(row.verification, "verified")
        self.assertEqual(row.profile_id, "test-cpu-int8")
        # 침묵을 음성으로 보지 않았다.
        self.assertFalse(row.vad_speech_detected)
        # 음성 길이는 남고, 실행 경로는 운영이다.
        self.assertEqual(row.audio_duration_ms, 500)
        from storage.records import SttExecutionPath
        self.assertIs(row.execution_path, SttExecutionPath.OPERATIONAL)


@unittest.skipUnless(ENABLED, SKIP_REASON)
class TestEvaluationPipeline(unittest.TestCase):
    """측정이 사례 종류에 맞는 지표로 돌아가는지 확인한다.

    발화 사례에만 WER·CER을 계산하고, 침묵·잡음·순음은 오탐·환각으로 본다.
    이전 판은 침묵에 WER 0.0을 기록해 "정확도 완벽"처럼 보였는데, 정답이 빈
    사례의 편집거리는 정확도가 아니다.
    """

    def fixture(self):
        return ROOT / "fixtures" / "stt" / "eval_ko_robot_commands.jsonl"

    def test_speech_cases_without_recordings_are_skipped_not_scored(self):
        """녹음이 없으면 미측정이다. 0점도, 만점도 아니다."""
        backend = FasterWhisperBackend(model_config())
        backend.load()
        f = self.fixture()
        rows = evaluate_speech(
            load_cases(f), backend, sample_rate_hz=SR, fixture_dir=f.parent
        )
        self.assertTrue(rows, "fixture에 발화 사례가 있어야 한다")
        for r in rows:
            with self.subTest(case=r.case_id):
                self.assertFalse(r.measured, "실제 녹음이 없으므로 측정될 수 없다")
                self.assertIsNotNone(r.skip)
                self.assertIsNone(r.wer)
                self.assertIsNone(r.cer)

    def test_non_speech_cases_are_scored_by_false_positive_and_hallucination(self):
        backend = FasterWhisperBackend(model_config())
        backend.load()
        vad = SileroVadBackend(model_config())
        vad.load()
        f = self.fixture()
        rows = evaluate_non_speech(
            load_cases(f), backend, vad,
            sample_rate_hz=SR, fixture_dir=f.parent, min_final_confidence=0.6,
        )
        self.assertTrue(rows)
        for r in rows:
            with self.subTest(case=r.case_id):
                self.assertTrue(r.measured, r.skip)
                # 합성 비발화를 음성으로 보면 오탐이다.
                self.assertFalse(r.vad_false_positive, "합성 비발화에 VAD가 오탐했다")
                # VAD가 막으므로 final 요청까지 가지 않는다.
                self.assertFalse(r.final_request_created)
                self.assertIsNotNone(r.transcript_emitted)

    def test_report_separates_speech_and_non_speech_summaries(self):
        backend = FasterWhisperBackend(model_config())
        backend.load()
        vad = SileroVadBackend(model_config())
        vad.load()
        f = self.fixture()
        cases = load_cases(f)
        report = EvalReport(
            model_load_sec=1.0,
            speech=evaluate_speech(
                cases, backend, sample_rate_hz=SR, fixture_dir=f.parent),
            non_speech=evaluate_non_speech(
                cases, backend, vad, sample_rate_hz=SR,
                fixture_dir=f.parent, min_final_confidence=0.6),
        )
        d = report.to_dict()
        # 발화 녹음이 없으므로 WER·CER 평균은 "없음"이어야 한다 — 0이 아니다.
        self.assertIsNone(d["speech"]["mean_wer"])
        self.assertIsNone(d["speech"]["mean_cer"])
        self.assertEqual(d["non_speech"]["vad_false_positive_rate"], 0.0)
        self.assertIsNotNone(d["non_speech"]["hallucination_rate"])
        self.assertIsNotNone(d["model_load_sec"])

    def test_speed_is_recorded_as_both_rtf_and_throughput(self):
        backend = FasterWhisperBackend(model_config())
        backend.load()
        f = self.fixture()
        rows = evaluate_non_speech(
            load_cases(f), backend, None,
            sample_rate_hz=SR, fixture_dir=f.parent, min_final_confidence=0.6,
        )
        measured = [r for r in rows if r.measured]
        self.assertTrue(measured)
        for r in measured:
            with self.subTest(case=r.case_id):
                s = r.speed
                self.assertGreater(s.audio_sec, 0.0)
                self.assertGreater(s.process_sec, 0.0)
                self.assertAlmostEqual(s.rtf * s.throughput, 1.0, places=6)
            # VAD를 돌리지 않았으면 오탐 여부는 "측정 안 함"이다.
            self.assertIsNone(r.vad_false_positive)


if __name__ == "__main__":
    unittest.main(verbosity=2)
