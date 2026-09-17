"""STT 평가 — 발화 사례와 비발화 사례를 서로 다른 지표로 측정한다.

**정정 근거**: 이전 판은 침묵·잡음에도 WER·CER을 계산했다. 정답 문장이 빈
사례에 편집거리를 적용하면 "삽입 오류 개수"가 나올 뿐 전사 정확도가 아니다.
잡음에서 `'고맙습니다!'`가 나왔을 때 CER 6.0이 찍힌 것이 그 예다 — 6은 오류율이
아니라 환각 문자 수다. 그래서 두 평가를 분리한다.

| 사례 | 정답 | 지표 |
|---|---|---|
| 발화 | 문장 있음 | WER, CER, 처리 속도 |
| 비발화 | 빈 문자열 | VAD 오탐 여부, transcript 발생 여부, final 요청 생성 여부, 환각 발생률, 처리 속도 |

처리 속도는 두 값을 함께 기록한다.
- `rtf` = 처리 시간 / 음성 길이 — 1보다 크면 실시간보다 느리다
- `throughput` = 음성 길이 / 처리 시간 — 1보다 작으면 실시간보다 느리다

처리 시간·음성 길이·모델 최초 로딩 시간을 각각 따로 저장한다. 로딩은 한 번만
일어나므로 사례별 속도에 섞지 않는다.

요구정의서에 WER·CER·지연의 수치 기준이 없다. **합격 판정을 하지 않는다.**
"""

from __future__ import annotations

import json
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import numpy as np

from core.reason_codes import ReasonCode

#: 정답이 빈 사례를 비발화로 본다. 태그가 아니라 정답 유무로 판정한다.
NON_SPEECH_TAGS = ("침묵", "잡음")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    reference: str
    tags: tuple[str, ...]
    audio: str
    note: str = ""

    @property
    def is_speech_case(self) -> bool:
        return bool(self.reference.strip())


@dataclass(frozen=True)
class SpeedMetrics:
    """처리 속도. 두 표현을 함께 남겨 해석 오류를 막는다."""

    audio_sec: float
    process_sec: float

    @property
    def rtf(self) -> float:
        """처리 시간 / 음성 길이. 1보다 크면 실시간보다 느리다."""
        return self.process_sec / self.audio_sec if self.audio_sec > 0 else 0.0

    @property
    def throughput(self) -> float:
        """음성 길이 / 처리 시간. 1보다 작으면 실시간보다 느리다."""
        return self.audio_sec / self.process_sec if self.process_sec > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "audio_sec": self.audio_sec,
            "process_sec": self.process_sec,
            "rtf": self.rtf,
            "throughput": self.throughput,
        }


@dataclass(frozen=True)
class Skip:
    reason: ReasonCode
    detail: str


@dataclass(frozen=True)
class SpeechCaseResult:
    """정답 문장이 있는 사례. WER·CER을 계산한다."""

    case_id: str
    tags: tuple[str, ...]
    reference: str
    hypothesis: str | None = None
    confidence: float | None = None
    wer: float | None = None
    cer: float | None = None
    speed: SpeedMetrics | None = None
    skip: Skip | None = None

    @property
    def measured(self) -> bool:
        return self.skip is None

    def to_dict(self) -> dict:
        return {
            "id": self.case_id, "tags": list(self.tags), "reference": self.reference,
            "hypothesis": self.hypothesis, "confidence": self.confidence,
            "wer": self.wer, "cer": self.cer,
            "speed": self.speed.to_dict() if self.speed else None,
            "skipped_reason": self.skip.reason.value if self.skip else None,
            "skipped_detail": self.skip.detail if self.skip else "",
        }


@dataclass(frozen=True)
class NonSpeechCaseResult:
    """정답이 빈 사례. 전사 정확도가 아니라 오탐·환각을 본다."""

    case_id: str
    tags: tuple[str, ...]
    #: VAD가 음성이라고 오탐했는가. None이면 VAD를 돌리지 않았다.
    vad_false_positive: bool | None = None
    #: VAD가 본 최대 음성 확률.
    vad_max_probability: float | None = None
    #: 전사가 빈 문자열이 아니었는가.
    transcript_emitted: bool | None = None
    #: 세션이 final 요청을 만들었는가(계획 생성으로 넘어갔는가).
    final_request_created: bool | None = None
    #: 환각으로 나온 문장. 없으면 빈 문자열.
    hallucinated_text: str = ""
    confidence: float | None = None
    speed: SpeedMetrics | None = None
    skip: Skip | None = None

    @property
    def measured(self) -> bool:
        return self.skip is None

    def to_dict(self) -> dict:
        return {
            "id": self.case_id, "tags": list(self.tags),
            "vad_false_positive": self.vad_false_positive,
            "vad_max_probability": self.vad_max_probability,
            "transcript_emitted": self.transcript_emitted,
            "final_request_created": self.final_request_created,
            "hallucinated_text": self.hallucinated_text,
            "confidence": self.confidence,
            "speed": self.speed.to_dict() if self.speed else None,
            "skipped_reason": self.skip.reason.value if self.skip else None,
            "skipped_detail": self.skip.detail if self.skip else "",
        }


@dataclass
class EvalReport:
    """전체 측정 결과. 로딩 시간을 사례별 속도와 분리해 담는다."""

    model_load_sec: float | None = None
    speech: list[SpeechCaseResult] = field(default_factory=list)
    non_speech: list[NonSpeechCaseResult] = field(default_factory=list)

    # ── 발화 요약 ───────────────────────────────────────────────────────
    @property
    def speech_measured(self) -> list[SpeechCaseResult]:
        return [r for r in self.speech if r.measured]

    @property
    def non_speech_measured(self) -> list[NonSpeechCaseResult]:
        return [r for r in self.non_speech if r.measured]

    def mean_wer(self) -> float | None:
        rows = [r.wer for r in self.speech_measured if r.wer is not None]
        return sum(rows) / len(rows) if rows else None

    def mean_cer(self) -> float | None:
        rows = [r.cer for r in self.speech_measured if r.cer is not None]
        return sum(rows) / len(rows) if rows else None

    def aggregate_speed(self, rows: Sequence) -> dict | None:
        """전체 합으로 계산한 속도.

        사례별 RTF의 평균은 사례별 처리 배수 평균의 역수가 **아니다**
        (역수의 평균 != 평균의 역수). 한 사례가 유별나게 느리면 RTF 평균만
        크게 튄다. 그래서 총 처리 시간 / 총 음성 길이도 함께 남긴다.
        """
        speeds = [r.speed for r in rows if r.speed is not None]
        if not speeds:
            return None
        audio = sum(s.audio_sec for s in speeds)
        process = sum(s.process_sec for s in speeds)
        return {
            "audio_sec_total": audio,
            "process_sec_total": process,
            "rtf": process / audio if audio > 0 else None,
            "throughput": audio / process if process > 0 else None,
        }

    def rtf_distribution(self, rows: Sequence) -> dict | None:
        """사례별 RTF 분포. 대표값 하나로 뭉개지 않기 위해 둔다.

        실측에서 비발화 5건의 RTF가 1.74~21.11로 벌어졌다. 평균 6.81은 어느
        사례도 설명하지 못한다.
        """
        values = sorted(
            r.speed.rtf for r in rows if r.speed is not None and r.speed.rtf
        )
        if not values:
            return None

        def quantile(q: float) -> float:
            if len(values) == 1:
                return values[0]
            pos = q * (len(values) - 1)
            low = int(pos)
            high = min(low + 1, len(values) - 1)
            return values[low] + (values[high] - values[low]) * (pos - low)

        return {
            "n": len(values),
            "min": values[0],
            "median": quantile(0.5),
            "p90": quantile(0.9),
            "max": values[-1],
            "values": values,
        }

    def speed_block(self, rows: Sequence) -> dict | None:
        """속도 보고 한 덩어리.

        **대표값은 전체 합계(총 처리 ÷ 총 음성)다.** 사례별 평균은 참고로만
        남긴다 — 역수의 평균은 평균의 역수가 아니고, 느린 사례 하나가 평균을
        끌어올린다.
        """
        aggregate = self.aggregate_speed(rows)
        if aggregate is None:
            return None
        return {
            "representative": {"basis": "aggregate_total", **aggregate},
            "per_case_reference": {
                "note": "참고용. 대표값으로 쓰지 않는다",
                "mean": self.mean_speed(rows),
                "rtf_distribution": self.rtf_distribution(rows),
            },
        }

    def mean_speed(self, rows: Sequence) -> dict | None:
        speeds = [r.speed for r in rows if r.speed is not None]
        if not speeds:
            return None
        n = len(speeds)
        return {
            "audio_sec": sum(s.audio_sec for s in speeds) / n,
            "process_sec": sum(s.process_sec for s in speeds) / n,
            "rtf": sum(s.rtf for s in speeds) / n,
            "throughput": sum(s.throughput for s in speeds) / n,
        }

    # ── 비발화 요약 ─────────────────────────────────────────────────────
    def vad_false_positive_rate(self) -> float | None:
        rows = [r for r in self.non_speech_measured if r.vad_false_positive is not None]
        if not rows:
            return None
        return sum(1 for r in rows if r.vad_false_positive) / len(rows)

    def hallucination_rate(self) -> float | None:
        """비발화 사례에서 전사가 빈 문자열이 아니었던 비율."""
        rows = [r for r in self.non_speech_measured if r.transcript_emitted is not None]
        if not rows:
            return None
        return sum(1 for r in rows if r.transcript_emitted) / len(rows)

    def final_request_rate(self) -> float | None:
        rows = [r for r in self.non_speech_measured if r.final_request_created is not None]
        if not rows:
            return None
        return sum(1 for r in rows if r.final_request_created) / len(rows)

    def to_dict(self) -> dict:
        return {
            "model_load_sec": self.model_load_sec,
            # 평가 실행기는 Transcriber를 직접 호출한다 — 운영 경로가 아니다.
            "execution_path": "evaluation",
            "speech": {
                "measured": len(self.speech_measured),
                "skipped": len(self.speech) - len(self.speech_measured),
                "mean_wer": self.mean_wer(),
                "mean_cer": self.mean_cer(),
                "speed": self.speed_block(self.speech_measured),
                "cases": [r.to_dict() for r in self.speech],
            },
            "non_speech": {
                "measured": len(self.non_speech_measured),
                "skipped": len(self.non_speech) - len(self.non_speech_measured),
                "vad_false_positive_rate": self.vad_false_positive_rate(),
                "hallucination_rate": self.hallucination_rate(),
                "final_request_rate": self.final_request_rate(),
                "speed": self.speed_block(self.non_speech_measured),
                "cases": [r.to_dict() for r in self.non_speech],
            },
        }


# ── 거리 지표 (발화 사례 전용) ──────────────────────────────────────────


def _edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


class EmptyReference(ValueError):
    """정답이 빈 사례에 WER·CER을 계산하려 한 경우."""


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = reference.split(), hypothesis.split()
    if not ref:
        raise EmptyReference(
            "정답이 비어 있다 — 비발화 사례는 WER 대신 환각 발생률로 평가한다"
        )
    return _edit_distance(ref, hyp) / len(ref)


def character_error_rate(reference: str, hypothesis: str) -> float:
    ref = reference.replace(" ", "")
    hyp = hypothesis.replace(" ", "")
    if not ref:
        raise EmptyReference(
            "정답이 비어 있다 — 비발화 사례는 CER 대신 환각 발생률로 평가한다"
        )
    return _edit_distance(ref, hyp) / len(ref)


# ── 오디오 ──────────────────────────────────────────────────────────────


def synthesize(spec: str, sample_rate_hz: int) -> bytes:
    """`generated:...` 명세를 결정적으로 합성한다(고정 시드)."""
    parts = spec.split(":")
    kind, seconds = parts[1], float(parts[2])
    n = int(seconds * sample_rate_hz)
    if kind == "silence":
        samples = np.zeros(n, dtype=np.float32)
    elif kind == "noise":
        rng = np.random.default_rng(12345)
        samples = rng.standard_normal(n).astype(np.float32) * float(parts[3])
    elif kind == "tone":
        t = np.arange(n, dtype=np.float32) / sample_rate_hz
        samples = np.sin(2 * np.pi * float(parts[3]) * t).astype(np.float32) * 0.25
    else:
        raise ValueError(f"알 수 없는 합성 종류: {kind!r}")
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def read_wav(path: Path, expected_sample_rate_hz: int) -> bytes:
    with wave.open(str(path), "rb") as wf:
        if wf.getnchannels() != 1:
            raise ValueError(f"mono가 아니다: {wf.getnchannels()}채널")
        if wf.getsampwidth() != 2:
            raise ValueError(f"PCM16이 아니다: {wf.getsampwidth() * 8}bit")
        if wf.getframerate() != expected_sample_rate_hz:
            raise ValueError(
                f"샘플레이트가 {wf.getframerate()}Hz다 (기대 {expected_sample_rate_hz}Hz)"
            )
        return wf.readframes(wf.getnframes())


def load_cases(path: Path) -> tuple[EvalCase, ...]:
    out: list[EvalCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out.append(
            EvalCase(
                case_id=row["id"], reference=row["reference"], tags=tuple(row["tags"]),
                audio=row["audio"], note=row.get("note", ""),
            )
        )
    return tuple(out)


def _prepare_audio(case: EvalCase, sample_rate_hz: int, fixture_dir: Path) -> bytes:
    if case.audio.startswith("generated:"):
        return synthesize(case.audio, sample_rate_hz)
    path = fixture_dir / case.audio
    if not path.exists():
        raise FileNotFoundError(case.audio)
    return read_wav(path, sample_rate_hz)


# ── 실행 ────────────────────────────────────────────────────────────────


def evaluate_speech(
    cases: Sequence[EvalCase], transcriber, *, sample_rate_hz: int, fixture_dir: Path
) -> list[SpeechCaseResult]:
    """정답 문장이 있는 사례만 WER·CER로 평가한다."""
    out: list[SpeechCaseResult] = []
    for case in cases:
        if not case.is_speech_case:
            continue
        try:
            audio = _prepare_audio(case, sample_rate_hz, fixture_dir)
        except FileNotFoundError as exc:
            out.append(
                SpeechCaseResult(
                    case_id=case.case_id, tags=case.tags, reference=case.reference,
                    skip=Skip(
                        ReasonCode.SAFETY_INSUFFICIENT_DATA,
                        f"녹음 파일이 없다: {exc}",
                    ),
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            out.append(
                SpeechCaseResult(
                    case_id=case.case_id, tags=case.tags, reference=case.reference,
                    skip=Skip(ReasonCode.STT_STREAM_ABORTED, f"오디오 준비 실패: {exc}"),
                )
            )
            continue

        audio_sec = len(audio) / 2 / sample_rate_hz
        started = time.monotonic()
        try:
            result = transcriber.transcribe(audio, sample_rate_hz)
        except Exception as exc:  # noqa: BLE001
            out.append(
                SpeechCaseResult(
                    case_id=case.case_id, tags=case.tags, reference=case.reference,
                    skip=Skip(
                        getattr(exc, "reason", ReasonCode.STT_BACKEND_UNAVAILABLE),
                        str(exc)[:200],
                    ),
                )
            )
            continue
        speed = SpeedMetrics(audio_sec=audio_sec, process_sec=time.monotonic() - started)
        out.append(
            SpeechCaseResult(
                case_id=case.case_id, tags=case.tags, reference=case.reference,
                hypothesis=result.text, confidence=result.confidence,
                wer=word_error_rate(case.reference, result.text),
                cer=character_error_rate(case.reference, result.text),
                speed=speed,
            )
        )
    return out


def evaluate_non_speech(
    cases: Sequence[EvalCase],
    transcriber,
    vad,
    *,
    sample_rate_hz: int,
    fixture_dir: Path,
    min_final_confidence: float,
) -> list[NonSpeechCaseResult]:
    """정답이 빈 사례를 오탐·환각으로 평가한다.

    `vad`가 None이면 VAD 항목은 None으로 남는다 — 측정하지 않은 것을 "오탐 없음"
    으로 적지 않는다.

    `final_request_created`는 세션 규칙을 그대로 적용해 판정한다: VAD가 음성으로
    보지 않으면 전사까지 가지 않고, 전사가 비었거나 신뢰도가 기준 미만이면
    되묻기로 끝나 final 요청이 만들어지지 않는다.
    """
    out: list[NonSpeechCaseResult] = []
    for case in cases:
        if case.is_speech_case:
            continue
        try:
            audio = _prepare_audio(case, sample_rate_hz, fixture_dir)
        except Exception as exc:  # noqa: BLE001
            out.append(
                NonSpeechCaseResult(
                    case_id=case.case_id, tags=case.tags,
                    skip=Skip(ReasonCode.STT_STREAM_ABORTED, f"오디오 준비 실패: {exc}"),
                )
            )
            continue

        vad_fp: bool | None = None
        vad_max: float | None = None
        if vad is not None:
            try:
                vad.reset()
                vad_fp = False
                vad_max = 0.0
                step = 512 * 2   # PCM16 512 샘플
                for i in range(0, len(audio) - step + 1, step):
                    if vad.is_speech(audio[i:i + step], sample_rate_hz):
                        vad_fp = True
                    vad_max = max(vad_max, vad.last_probability)
            except Exception as exc:  # noqa: BLE001
                out.append(
                    NonSpeechCaseResult(
                        case_id=case.case_id, tags=case.tags,
                        skip=Skip(
                            getattr(exc, "reason", ReasonCode.STT_BACKEND_UNAVAILABLE),
                            f"VAD 실패: {exc}",
                        ),
                    )
                )
                continue

        audio_sec = len(audio) / 2 / sample_rate_hz
        started = time.monotonic()
        try:
            result = transcriber.transcribe(audio, sample_rate_hz)
        except Exception as exc:  # noqa: BLE001
            out.append(
                NonSpeechCaseResult(
                    case_id=case.case_id, tags=case.tags,
                    vad_false_positive=vad_fp, vad_max_probability=vad_max,
                    skip=Skip(
                        getattr(exc, "reason", ReasonCode.STT_BACKEND_UNAVAILABLE),
                        str(exc)[:200],
                    ),
                )
            )
            continue
        speed = SpeedMetrics(audio_sec=audio_sec, process_sec=time.monotonic() - started)

        emitted = bool(result.text.strip())
        # 세션 규칙: VAD가 음성으로 보지 않으면 전사까지 가지 않는다. 전사가
        # 비었거나 신뢰도가 기준 미만이면 되묻기로 끝난다.
        reaches_transcription = vad_fp is not False
        final_created = bool(
            reaches_transcription and emitted and result.confidence >= min_final_confidence
        )
        out.append(
            NonSpeechCaseResult(
                case_id=case.case_id, tags=case.tags,
                vad_false_positive=vad_fp, vad_max_probability=vad_max,
                transcript_emitted=emitted, final_request_created=final_created,
                hallucinated_text=result.text if emitted else "",
                confidence=result.confidence, speed=speed,
            )
        )
    return out
