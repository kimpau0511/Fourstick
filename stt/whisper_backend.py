"""전사 백엔드 경계 (md/개발플랜.md 4-05, 4-07 관련).

요구정의서의 faster-whisper(large-v3-turbo)는 이 `Transcriber`를 구현해 주입한다.
**모델과 경로를 이 모듈에 두지 않는다**(계획.md 27장) — 모델 ID·경로는 Provider
설정에서 온다.

실제 모델 구현체는 아직 없다. 설치와 배선은 이 단계 밖이고, 상태기계·partial
revision·final 확정 규칙은 백엔드 없이도 검증할 수 있도록 경계를 분리했다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Transcript:
    """전사 결과. 신뢰도를 함께 돌려주지 않는 백엔드는 쓸 수 없다 —
    되묻기 판정(4-07)이 신뢰도를 요구한다."""

    text: str
    confidence: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence가 0~1 범위를 벗어난다: {self.confidence}")


@runtime_checkable
class Transcriber(Protocol):
    """오디오 구간을 전사한다. 호출자가 구간을 잘라서 넘긴다."""

    def transcribe(self, audio: bytes, sample_rate_hz: int) -> Transcript: ...
