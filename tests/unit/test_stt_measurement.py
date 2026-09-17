"""STT 측정 정의와 Profile 검증 상태 (md/STT_모델_구성.md).

실제 모델 없이 도는 경로만 담는다. 확인 대상:
- WER·CER은 정답 문장이 있는 사례에만 계산한다.
- 침묵·잡음·순음은 오탐·환각·final 생성으로 평가한다.
- RTF와 처리 배수를 서로의 역수로 함께 기록한다.
- 미검증 Profile을 기본으로 지정할 수 없다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.policy import (
    PolicyError,
    SttModelConfig,
    SttProfileCatalog,
    SttVerification,
)
from core.reason_codes import ReasonCode
from stt.evaluation import (
    EmptyReference,
    EvalCase,
    EvalReport,
    NonSpeechCaseResult,
    SpeechCaseResult,
    SpeedMetrics,
    character_error_rate,
    evaluate_non_speech,
    evaluate_speech,
    load_cases,
    synthesize,
    word_error_rate,
)
from stt.whisper_backend import Transcript

FIXTURE = ROOT / "fixtures" / "stt" / "eval_ko_robot_commands.jsonl"
SR = 16_000
PROV = {k: "테스트" for k in ("model_name", "device", "compute_type", "vad_threshold")}


def config(profile_id="p1", verification=SttVerification.VERIFIED, **over) -> SttModelConfig:
    kw = dict(
        config_version="test-1.0", profile_id=profile_id, verification=verification,
        model_name="small", device="cpu", compute_type="int8", language="ko",
        sample_rate_hz=SR, vad_threshold=0.5,
        load_timeout_sec=60.0, transcribe_timeout_sec=60.0, provenance=PROV,
    )
    kw.update(over)
    return SttModelConfig(**kw)


class FakeTranscriber:
    """정해진 텍스트를 돌려준다. 호출 수를 세어 전사까지 갔는지 본다."""

    def __init__(self, text: str, confidence: float = 0.9):
        self.text, self.confidence, self.calls = text, confidence, 0

    def transcribe(self, pcm: bytes, sample_rate_hz: int) -> Transcript:
        self.calls += 1
        return Transcript(text=self.text, confidence=self.confidence)


class FakeVad:
    def __init__(self, speech: bool, probability: float = 0.0):
        self.speech, self.last_probability = speech, probability
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:
        return self.speech


class TestErrorRateScope(unittest.TestCase):
    def test_wer_and_cer_need_a_reference_sentence(self):
        for fn in (word_error_rate, character_error_rate):
            with self.subTest(metric=fn.__name__):
                with self.assertRaises(EmptyReference):
                    fn("", "고맙습니다!")

    def test_error_rates_are_computed_for_real_references(self):
        self.assertAlmostEqual(word_error_rate("가 나 다", "가 라 다"), 1 / 3)
        self.assertAlmostEqual(character_error_rate("정지", "정지해"), 1 / 2)

    def test_non_speech_cases_are_not_scored_as_speech(self):
        cases = load_cases(FIXTURE)
        rows = evaluate_speech(
            [c for c in cases if not c.is_speech_case], FakeTranscriber("무엇"),
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent,
        )
        self.assertEqual(rows, [], "정답이 없는 사례는 발화 평가 대상이 아니다")

    def test_speech_cases_are_not_scored_as_non_speech(self):
        cases = load_cases(FIXTURE)
        rows = evaluate_non_speech(
            [c for c in cases if c.is_speech_case], FakeTranscriber(""), None,
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        self.assertEqual(rows, [])


class TestNonSpeechMetrics(unittest.TestCase):
    def cases(self):
        return [c for c in load_cases(FIXTURE) if not c.is_speech_case]

    def test_clean_rejection_records_no_false_positive_and_no_hallucination(self):
        t = FakeTranscriber("")
        rows = evaluate_non_speech(
            self.cases(), t, FakeVad(speech=False, probability=0.02),
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        self.assertTrue(rows)
        report = EvalReport(non_speech=rows)
        self.assertEqual(report.vad_false_positive_rate(), 0.0)
        self.assertEqual(report.hallucination_rate(), 0.0)
        self.assertEqual(report.final_request_rate(), 0.0)

    def test_hallucinated_sentence_is_counted_not_turned_into_an_error_rate(self):
        rows = evaluate_non_speech(
            self.cases(), FakeTranscriber("고맙습니다!"), FakeVad(speech=True, probability=0.9),
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        report = EvalReport(non_speech=rows)
        self.assertEqual(report.vad_false_positive_rate(), 1.0)
        self.assertEqual(report.hallucination_rate(), 1.0)
        # 신뢰도 0.9가 기준을 넘으므로 final 요청까지 갔다 — 가장 나쁜 경우다.
        self.assertEqual(report.final_request_rate(), 1.0)
        self.assertTrue(all(r.hallucinated_text == "고맙습니다!" for r in rows))
        # 환각이 있어도 WER·CER은 만들지 않는다.
        self.assertIsNone(EvalReport(non_speech=rows).mean_wer())

    def test_low_confidence_hallucination_does_not_create_a_final_request(self):
        rows = evaluate_non_speech(
            self.cases(), FakeTranscriber("어", confidence=0.1),
            FakeVad(speech=True, probability=0.8),
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        report = EvalReport(non_speech=rows)
        self.assertEqual(report.hallucination_rate(), 1.0)
        self.assertEqual(report.final_request_rate(), 0.0, "되묻기로 끝나야 한다")

    def test_vad_rejection_stops_before_final_even_if_transcript_appears(self):
        rows = evaluate_non_speech(
            self.cases(), FakeTranscriber("정지", confidence=0.99),
            FakeVad(speech=False),
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        self.assertTrue(all(r.final_request_created is False for r in rows))

    def test_skipping_vad_leaves_the_field_unmeasured(self):
        """측정하지 않은 것을 "오탐 없음"으로 적지 않는다."""
        rows = evaluate_non_speech(
            self.cases(), FakeTranscriber(""), None,
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        self.assertTrue(all(r.vad_false_positive is None for r in rows))
        self.assertIsNone(EvalReport(non_speech=rows).vad_false_positive_rate())


class TestSpeedMetrics(unittest.TestCase):
    def test_rtf_and_throughput_are_reciprocals(self):
        s = SpeedMetrics(audio_sec=2.0, process_sec=5.5)
        self.assertAlmostEqual(s.rtf, 2.75)
        self.assertAlmostEqual(s.throughput, 0.3636363, places=6)
        self.assertAlmostEqual(s.rtf * s.throughput, 1.0)

    def test_measured_throughput_below_one_means_slower_than_real_time(self):
        """실측 처리 배수 0.364 -> RTF 약 2.75. 실시간보다 느리다."""
        s = SpeedMetrics(audio_sec=1.0, process_sec=1.0 / 0.364)
        self.assertAlmostEqual(s.throughput, 0.364, places=3)
        self.assertAlmostEqual(s.rtf, 2.747, places=3)
        self.assertGreater(s.rtf, 1.0)
        self.assertLess(s.throughput, 1.0)

    def test_mean_rtf_is_not_the_reciprocal_of_mean_throughput(self):
        """느린 사례 하나가 RTF 평균만 크게 튀운다. 집계값을 함께 남긴다."""
        rows = [
            NonSpeechCaseResult(case_id="a", tags=(),
                                speed=SpeedMetrics(audio_sec=2.0, process_sec=2.0)),
            NonSpeechCaseResult(case_id="b", tags=(),
                                speed=SpeedMetrics(audio_sec=2.0, process_sec=60.0)),
        ]
        report = EvalReport(non_speech=rows)
        mean = report.mean_speed(rows)
        agg = report.aggregate_speed(rows)
        self.assertAlmostEqual(mean["rtf"], 15.5)          # (1 + 30) / 2
        self.assertAlmostEqual(mean["throughput"], 0.5167, places=4)
        self.assertNotAlmostEqual(mean["rtf"], 1 / mean["throughput"], places=3)
        # 집계값은 총 처리 / 총 음성이라 서로 역수다.
        self.assertAlmostEqual(agg["rtf"], 62.0 / 4.0)
        self.assertAlmostEqual(agg["rtf"] * agg["throughput"], 1.0)

    def test_audio_and_process_seconds_are_kept_separately(self):
        d = SpeedMetrics(audio_sec=2.0, process_sec=5.5).to_dict()
        self.assertEqual(set(d), {"audio_sec", "process_sec", "rtf", "throughput"})

    def test_representative_speed_is_the_aggregate_not_the_per_case_mean(self):
        rows = [
            NonSpeechCaseResult(case_id="a", tags=(),
                                speed=SpeedMetrics(audio_sec=2.0, process_sec=2.0)),
            NonSpeechCaseResult(case_id="b", tags=(),
                                speed=SpeedMetrics(audio_sec=2.0, process_sec=60.0)),
        ]
        block = EvalReport(non_speech=rows).to_dict()["non_speech"]["speed"]
        self.assertEqual(block["representative"]["basis"], "aggregate_total")
        self.assertAlmostEqual(block["representative"]["rtf"], 62.0 / 4.0)
        # 사례별 평균은 참고 자리에만 있고, 라벨이 붙어 있다.
        ref = block["per_case_reference"]
        self.assertIn("대표값으로 쓰지 않는다", ref["note"])
        self.assertAlmostEqual(ref["mean"]["rtf"], 15.5)

    def test_rtf_distribution_shows_the_spread(self):
        rows = [
            NonSpeechCaseResult(case_id=str(i), tags=(),
                                speed=SpeedMetrics(audio_sec=2.0, process_sec=p))
            for i, p in enumerate((3.6, 3.79, 3.68, 3.49, 42.22))
        ]
        dist = EvalReport(non_speech=rows).rtf_distribution(rows)
        self.assertEqual(dist["n"], 5)
        self.assertAlmostEqual(dist["min"], 1.745, places=3)
        self.assertAlmostEqual(dist["max"], 21.11)
        self.assertLess(dist["median"], dist["max"] / 5, "중앙값이 최대값에 끌려갔다")

    def test_report_marks_the_evaluation_path(self):
        d = EvalReport(model_load_sec=1.0).to_dict()
        self.assertEqual(d["execution_path"], "evaluation")

    def test_model_load_time_is_not_mixed_into_case_speed(self):
        rows = evaluate_non_speech(
            [c for c in load_cases(FIXTURE) if not c.is_speech_case][:1],
            FakeTranscriber(""), None,
            sample_rate_hz=SR, fixture_dir=FIXTURE.parent, min_final_confidence=0.6,
        )
        report = EvalReport(model_load_sec=5.38, non_speech=rows)
        d = report.to_dict()
        self.assertEqual(d["model_load_sec"], 5.38)
        self.assertLess(
            d["non_speech"]["speed"]["per_case_reference"]["mean"]["process_sec"], 5.38
        )


class TestNoArbitraryPassCriteria(unittest.TestCase):
    def test_report_has_no_pass_or_fail_field(self):
        report = EvalReport(
            model_load_sec=1.0,
            speech=[SpeechCaseResult(case_id="c", tags=(), reference="정지", wer=1.0)],
            non_speech=[NonSpeechCaseResult(case_id="n", tags=("침묵",))],
        )
        flat = str(report.to_dict())
        for banned in ("pass", "fail", "threshold", "합격", "기준치"):
            self.assertNotIn(banned, flat, f"임의 합격 기준({banned})이 들어갔다")


class TestProfileVerificationStatus(unittest.TestCase):
    def catalog(self, default_profile_id="verified-1"):
        return SttProfileCatalog(
            catalog_version="1.0", default_profile_id=default_profile_id,
            profiles={
                "verified-1": config("verified-1", SttVerification.VERIFIED),
                "candidate-1": config(
                    "candidate-1", SttVerification.CANDIDATE, model_name="large-v3-turbo"
                ),
            },
        )

    def test_default_profile_must_be_verified(self):
        with self.assertRaises(PolicyError) as ctx:
            self.catalog(default_profile_id="candidate-1")
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)

    def test_verified_profile_can_be_default(self):
        self.assertEqual(self.catalog().default().model_name, "small")

    def test_candidate_profile_is_selectable_but_marked(self):
        c = self.catalog()
        self.assertEqual(c.candidate_ids(), ("candidate-1",))
        self.assertEqual(c.verified_ids(), ("verified-1",))
        self.assertIs(c.get("candidate-1").verification, SttVerification.CANDIDATE)

    def test_unknown_profile_is_refused_with_a_reason(self):
        with self.assertRaises(PolicyError) as ctx:
            self.catalog().get("nope")
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_MISSING)

    def test_profile_key_must_match_its_own_id(self):
        with self.assertRaises(PolicyError):
            SttProfileCatalog(
                catalog_version="1.0", default_profile_id="a",
                profiles={"a": config("b", SttVerification.VERIFIED)},
            )

    def test_missing_default_is_refused(self):
        with self.assertRaises(PolicyError):
            SttProfileCatalog(
                catalog_version="1.0", default_profile_id="ghost",
                profiles={"a": config("a", SttVerification.VERIFIED)},
            )


class TestSynthesisIsDeterministic(unittest.TestCase):
    def test_same_spec_gives_same_bytes(self):
        a = synthesize("generated:noise:0.5:0.02", SR)
        self.assertEqual(a, synthesize("generated:noise:0.5:0.02", SR))
        self.assertEqual(len(a), int(0.5 * SR) * 2)

    def test_fixture_cases_are_split_by_reference_presence(self):
        cases = load_cases(FIXTURE)
        speech = [c for c in cases if c.is_speech_case]
        non_speech = [c for c in cases if not c.is_speech_case]
        self.assertTrue(speech and non_speech)
        for c in non_speech:
            with self.subTest(case=c.case_id):
                self.assertTrue(c.audio.startswith("generated:"))
        for c in speech:
            with self.subTest(case=c.case_id):
                self.assertFalse(c.audio.startswith("generated:"),
                                 "합성음에 정답 문장을 붙이지 않는다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
