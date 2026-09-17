"""실제 Silero VAD를 `VadBackend` 계약에 연결한다 (md/개발플랜.md 4-04).

모델 파일은 `faster-whisper` 패키지가 번들한 `silero_vad_v6.onnx`를 쓴다 —
별도 다운로드가 없고 파일이 프로젝트 안에 들어오지 않는다(설치된 site-packages).

**스트리밍 상태를 유지한다.** Silero는 순환 상태(h, c)와 직전 컨텍스트를 쓰는
모델이라, 프레임마다 상태를 새로 만들면 판정이 흔들린다. `faster_whisper.vad`의
헬퍼는 구간 단위로 상태를 초기화하므로 여기서는 ONNX 세션을 직접 열어 h·c와
컨텍스트를 호출 사이에 보존한다.

임계값·샘플레이트는 `SttModelConfig`에서 온다. 코드에 두지 않는다.

실패는 예외가 아니라 `VadUnavailable`로 올려 상위 계층이 ReasonCode로 바꾼다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np

from core.policy import SttModelConfig
from core.reason_codes import ReasonCode

#: Silero v6가 16kHz에서 요구하는 한 프레임 샘플 수와 컨텍스트 길이.
#: 모델 구조가 정한 값이므로 설정이 아니라 상수다.
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
SUPPORTED_SAMPLE_RATE_HZ = 16_000


class VadUnavailable(Exception):
    """모델 미설치·로딩 실패·설정 불일치. 상위 계층이 ReasonCode로 바꾼다."""

    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


def bundled_model_path() -> str:
    """설치된 faster-whisper의 Silero 자산 경로. 프로젝트 밖이다."""
    try:
        from faster_whisper.vad import get_assets_path
    except ImportError as exc:
        raise VadUnavailable(
            ReasonCode.STT_BACKEND_UNAVAILABLE,
            "faster-whisper가 설치되지 않아 Silero 자산을 찾을 수 없다",
        ) from exc
    path = os.path.join(get_assets_path(), "silero_vad_v6.onnx")
    if not os.path.exists(path):
        raise VadUnavailable(
            ReasonCode.STT_BACKEND_UNAVAILABLE, f"Silero 모델 파일이 없다: {path}"
        )
    return path


@dataclass
class SileroVadBackend:
    """`VadBackend` 구현. PCM16 mono 바이트를 받아 음성 여부를 돌려준다.

    한 프레임(512 샘플)이 모이지 않으면 직전 판정을 유지한다 — 판정을 못 한
    것을 무음으로 단정하지 않는다.
    """

    config: SttModelConfig
    _session: object | None = None
    _h: np.ndarray | None = None
    _c: np.ndarray | None = None
    _context: np.ndarray | None = None
    _buffer: np.ndarray | None = None
    _last_decision: bool = False
    last_probability: float = 0.0

    def __post_init__(self) -> None:
        if self.config.sample_rate_hz != SUPPORTED_SAMPLE_RATE_HZ:
            raise VadUnavailable(
                ReasonCode.CONFIG_INVALID,
                f"Silero v6 연결은 {SUPPORTED_SAMPLE_RATE_HZ}Hz만 지원한다 "
                f"(설정: {self.config.sample_rate_hz}Hz)",
            )
        self.reset()

    # ── 수명 ────────────────────────────────────────────────────────────
    def load(self) -> None:
        """ONNX 세션을 연다. 실패는 VadUnavailable로 올린다."""
        if self._session is not None:
            return
        try:
            import onnxruntime
        except ImportError as exc:
            raise VadUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE, "onnxruntime이 설치되지 않았다"
            ) from exc
        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        try:
            self._session = onnxruntime.InferenceSession(
                bundled_model_path(), providers=["CPUExecutionProvider"], sess_options=opts
            )
        except Exception as exc:  # noqa: BLE001 — 로딩 실패를 결과로 바꾼다
            raise VadUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE, f"Silero 모델 로딩 실패: {exc}"
            ) from exc

    def reset(self) -> None:
        """발화 사이에 순환 상태를 초기화한다."""
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)
        self._buffer = np.zeros((0,), dtype=np.float32)
        self._last_decision = False
        self.last_probability = 0.0

    # ── 판정 ────────────────────────────────────────────────────────────
    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool:
        if sample_rate_hz != self.config.sample_rate_hz:
            raise VadUnavailable(
                ReasonCode.CONFIG_UNIT_MISMATCH,
                f"입력 샘플레이트({sample_rate_hz})가 설정({self.config.sample_rate_hz})과 다르다",
            )
        self.load()
        if len(frame) % 2:
            raise VadUnavailable(
                ReasonCode.STT_STREAM_ABORTED, "PCM16 프레임 길이가 홀수 바이트다"
            )
        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
        self._buffer = np.concatenate([self._buffer, samples])

        while self._buffer.shape[0] >= FRAME_SAMPLES:
            chunk = self._buffer[:FRAME_SAMPLES]
            self._buffer = self._buffer[FRAME_SAMPLES:]
            self.last_probability = self._infer(chunk)
            self._last_decision = self.last_probability >= self.config.vad_threshold
        return self._last_decision

    def _infer(self, chunk: np.ndarray) -> float:
        batched = np.concatenate([self._context[0], chunk]).reshape(
            1, CONTEXT_SAMPLES + FRAME_SAMPLES
        ).astype(np.float32)
        try:
            output, self._h, self._c = self._session.run(  # type: ignore[union-attr]
                None, {"input": batched, "h": self._h, "c": self._c}
            )
        except Exception as exc:  # noqa: BLE001
            raise VadUnavailable(
                ReasonCode.STT_BACKEND_UNAVAILABLE, f"Silero 추론 실패: {exc}"
            ) from exc
        # 다음 프레임의 컨텍스트는 이번 프레임의 마지막 CONTEXT_SAMPLES다.
        self._context = chunk[-CONTEXT_SAMPLES:].reshape(1, CONTEXT_SAMPLES).astype(np.float32)
        return float(np.asarray(output).reshape(-1)[-1])
