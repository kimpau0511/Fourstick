"""발화 시작·종료 상태기계와 VAD 백엔드 경계 (md/개발플랜.md 4-04).

Silero VAD 같은 실제 모델은 `VadBackend`를 구현해 주입한다. 이 모듈은 모델에
의존하지 않는다 — 판정 시간(`speech_start_sec`, `silence_end_sec`)은 `SttPolicy`
에서 오고, 프레임 단위 음성 여부만 백엔드에 묻는다.

모델을 분리한 이유: 상태기계 규칙은 모델과 무관하게 검증할 수 있어야 한다.
실제 모델을 설치하지 않고도 시작·종료 판정, 짧은 잡음 무시, 발화 중 짧은 무음
무시를 결정적으로 확인한다.

**실제 시간을 쓰지 않는다.** 프레임의 오디오 시각(초)만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from core.policy import SttPolicy


@runtime_checkable
class VadBackend(Protocol):
    """프레임 하나가 음성인지 판정한다."""

    def is_speech(self, frame: bytes, sample_rate_hz: int) -> bool: ...


class SpeechEvent(str, Enum):
    NONE = "none"
    SPEECH_START = "speech_start"
    SPEECH_END = "speech_end"

    def __str__(self) -> str:
        return self.value


@dataclass
class SpeechStateMachine:
    """음성/무음 판정을 발화 경계로 바꾼다.

    규칙:
    - 무음 상태에서 음성이 `speech_start_sec` 이상 이어지면 SPEECH_START.
      그보다 짧은 음성은 잡음으로 보고 무시한다.
    - 발화 상태에서 무음이 `silence_end_sec` 이상 이어지면 SPEECH_END.
      그보다 짧은 무음은 발화 중 쉼으로 보고 무시한다.
    - 같은 경계를 두 번 내보내지 않는다.
    """

    policy: SttPolicy
    speaking: bool = False
    _run_start: float | None = None
    _run_is_speech: bool | None = None

    def feed(self, *, is_speech: bool, at_sec: float) -> SpeechEvent:
        """프레임 판정을 넣고 발생한 경계를 돌려준다."""
        if self._run_is_speech is not is_speech:
            self._run_is_speech = is_speech
            self._run_start = at_sec
        run_len = at_sec - (self._run_start if self._run_start is not None else at_sec)

        if not self.speaking and is_speech and run_len >= self.policy.speech_start_sec:
            self.speaking = True
            self._run_start = at_sec
            return SpeechEvent.SPEECH_START
        if self.speaking and not is_speech and run_len >= self.policy.silence_end_sec:
            self.speaking = False
            self._run_start = at_sec
            return SpeechEvent.SPEECH_END
        return SpeechEvent.NONE

    def force_end(self) -> SpeechEvent:
        """flush/stop처럼 외부에서 발화를 끊는 경우."""
        if not self.speaking:
            return SpeechEvent.NONE
        self.speaking = False
        self._run_start = None
        self._run_is_speech = None
        return SpeechEvent.SPEECH_END
