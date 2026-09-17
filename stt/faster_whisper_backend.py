"""실제 faster-whisper를 `Transcriber` 계약에 연결한다 (md/개발플랜.md 4-05).

모델 이름·장치·compute_type·언어·캐시 경로는 `SttModelConfig`에서 온다. 코드에
두지 않는다(계획.md 27장). 모델 파일은 HuggingFace 캐시(프로젝트 밖)에 받는다.

실패를 예외로 흘리지 않고 `TranscriberUnavailable`로 올려 상위 계층이
ReasonCode로 바꾼다. 다루는 실패:
  - 패키지 미설치
  - 모델 로딩 실패(이름 오류·네트워크·메모리)
  - 로딩 타임아웃
  - 오디오 손상(길이 불일치, 빈 입력)
  - 전사 타임아웃

로딩은 `load()`로 분리했다. 최초 로딩 시간을 일반 단위 테스트 시간에서 떼어내기
위해서다(md/개발플랜.md 4단계, 테스트 60초 제한 유지).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from core.policy import SttModelConfig
from core.reason_codes import ReasonCode
from stt.whisper_backend import Transcript


class TranscriberUnavailable(Exception):
    """전사 백엔드 실패. 상위 계층이 ReasonCode로 바꾼다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass
class LoadReport:
    """최초 로딩 측정값. 일반 테스트 시간과 분리해 보고하기 위해 남긴다."""

    model_name: str
    device: str
    compute_type: str
    load_sec: float


@dataclass
class TranscribeReport:
    """한 번의 전사 측정값."""

    audio_sec: float
    process_sec: float
    #: 실시간 배수 = 오디오 길이 / 처리 시간. 1보다 크면 실시간보다 빠르다.
    realtime_factor: float
    text: str
    confidence: float
    language: str


@dataclass
class FasterWhisperBackend:
    """`Transcriber` 구현."""

    config: SttModelConfig
    _model: Any | None = None
    load_report: LoadReport | None = None
    reports: list[TranscribeReport] = field(default_factory=list)
    #: 영속 executor. 제한시간 초과 시 shutdown을 기다리지 않기 위해 둔다.
    _pool: ThreadPoolExecutor | None = field(default=None, repr=False, compare=False)

    # ── 로딩 ────────────────────────────────────────────────────────────
    def load(self) -> LoadReport:
        """모델을 올린다. 이미 올라가 있으면 기존 보고를 돌려준다."""
        if self._model is not None and self.load_report is not None:
            return self.load_report
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriberUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE,
                "faster-whisper가 설치되지 않았다 (requirements-stt.txt 참고)",
            ) from exc

        kwargs: dict[str, Any] = {
            "device": self.config.device,
            "compute_type": self.config.compute_type,
        }
        if self.config.cache_dir:
            kwargs["download_root"] = self.config.cache_dir

        started = time.monotonic()
        pool = self._executor()
        future = pool.submit(WhisperModel, self.config.model_name, **kwargs)
        try:
            self._model = future.result(timeout=self.config.load_timeout_sec)
        except FutureTimeout as exc:
            raise TranscriberUnavailable(
                ReasonCode.EXEC_RESULT_TIMEOUT,
                f"모델 로딩이 {self.config.load_timeout_sec}s를 넘겼다",
            ) from exc
        except Exception as exc:  # noqa: BLE001 — 이름 오류·네트워크·메모리
            raise TranscriberUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE, f"모델 로딩 실패: {exc}"
            ) from exc
        self.load_report = LoadReport(
            model_name=self.config.model_name,
            device=self.config.device,
            compute_type=self.config.compute_type,
            load_sec=time.monotonic() - started,
        )
        return self.load_report

    def _executor(self) -> ThreadPoolExecutor:
        """영속 executor.

        `with ThreadPoolExecutor(...)`를 쓰면 안 된다. 블록을 벗어날 때
        `shutdown(wait=True)`가 호출돼서, 제한시간에 걸려 예외를 올리려 해도
        작업 스레드가 끝날 때까지 호출자가 막힌다(제한 0.3s / 실제 복귀 3.0s
        실측). 제한시간이 호출자 지연을 실제로 묶어 주지 못하는 상태였다.
        """
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="whisper"
            )
        return self._pool

    def close(self, *, wait: bool = False) -> None:
        """executor를 닫는다. 기본은 기다리지 않는다."""
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=True)
            self._pool = None

    # ── 전사 ────────────────────────────────────────────────────────────
    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript:
        if sample_rate_hz != self.config.sample_rate_hz:
            raise TranscriberUnavailable(
                ReasonCode.CONFIG_UNIT_MISMATCH,
                f"입력 샘플레이트({sample_rate_hz})가 설정"
                f"({self.config.sample_rate_hz})과 다르다",
            )
        if not audio:
            raise TranscriberUnavailable(
                ReasonCode.STT_NO_SPEECH, "빈 오디오는 전사할 수 없다"
            )
        if len(audio) % 2:
            raise TranscriberUnavailable(
                ReasonCode.STT_STREAM_ABORTED,
                "PCM16 입력 길이가 홀수 바이트다 — 오디오가 손상됐다",
            )
        self.load()

        samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
        audio_sec = samples.shape[0] / sample_rate_hz

        started = time.monotonic()
        pool = self._executor()
        future = pool.submit(self._run, samples)
        try:
            text, confidence, language = future.result(
                timeout=self.config.transcribe_timeout_sec
            )
        except FutureTimeout as exc:
            # 스레드를 죽일 수 없다. 기다리지 않고 올린다 — 작업은 계속 돈다.
            raise TranscriberUnavailable(
                ReasonCode.STT_TRANSCRIBE_TIMEOUT,
                f"전사가 {self.config.transcribe_timeout_sec}s를 넘겼다"
                " (추론 스레드는 끝날 때까지 남는다)",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise TranscriberUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE, f"전사 실패: {exc}"
            ) from exc
        process_sec = time.monotonic() - started

        self.reports.append(
            TranscribeReport(
                audio_sec=audio_sec,
                process_sec=process_sec,
                realtime_factor=(audio_sec / process_sec) if process_sec > 0 else 0.0,
                text=text,
                confidence=confidence,
                language=language,
            )
        )
        return Transcript(text=text, confidence=confidence)

    def _run(self, samples: np.ndarray) -> tuple[str, float, str]:
        """실제 전사. 신뢰도는 세그먼트 평균 로그확률을 0~1로 환산한다.

        faster-whisper는 세그먼트별 `avg_logprob`(음수)을 준다. 되묻기 판정에
        쓰려면 0~1 값이 필요해 `exp()`로 바꾼다 — 확률 해석이 아니라 **비교
        가능한 단조 지표**로만 쓴다. 기준값은 실측 분포를 보고 정한다.
        """
        segments, info = self._model.transcribe(  # type: ignore[union-attr]
            samples,
            language=self.config.language,
            vad_filter=False,   # VAD는 세션의 SileroVadBackend가 담당한다
        )
        texts: list[str] = []
        weights: list[float] = []
        scores: list[float] = []
        for seg in segments:
            texts.append(seg.text)
            duration = max(seg.end - seg.start, 1e-6)
            weights.append(duration)
            scores.append(float(np.exp(seg.avg_logprob)))
        text = "".join(texts).strip()
        if scores:
            total = sum(weights)
            confidence = float(sum(s * w for s, w in zip(scores, weights)) / total)
        else:
            # 세그먼트가 없으면 전사할 음성이 없었다는 뜻이다.
            confidence = 0.0
        return text, min(max(confidence, 0.0), 1.0), info.language
