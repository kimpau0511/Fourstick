"""STT WebSocket 메시지와 세션 상태 계약 (md/개발플랜.md 1-09).

요구정의서의 음성 파이프라인(faster-whisper + Silero VAD + 현장 용어 후보정)을
WebSocket으로 노출할 때의 계약이다. 백엔드 구현체(whisper_backend 등)는 이
계약만 지키면 교체할 수 있다.

설계 근거:
- partial과 final을 타입으로 구분한다. partial을 명령으로 실행하면 문장이
  끝나기 전에 로봇이 움직인다.
- 신뢰도가 낮으면 계획 생성으로 넘기지 않고 되묻는다(요구정의서 3주차의
  "모호한 경우 실행 대신 되묻는 로직").
- 실패는 닫힘 코드가 아니라 `error` 메시지 + 공통 ReasonCode로 전달한다.
  소켓이 그냥 끊기면 클라이언트가 이유를 알 수 없다.
- 세션 상태 전이를 표로 고정한다. 임의 전이를 허용하지 않는다.
- 메시지 종류와 상태는 계약 자체이므로 이 모듈이 유일한 출처다
  (계획.md 27장 "허용되는 상수").
"""

from __future__ import annotations

from enum import Enum


class SttSessionState(str, Enum):
    """STT 세션 상태."""

    IDLE = "idle"              # 소켓 연결됨, 아직 오디오 없음
    LISTENING = "listening"    # 오디오 수신 중, VAD가 발화를 기다림
    SPEAKING = "speaking"      # VAD가 발화 시작을 감지함
    FINALIZING = "finalizing"  # 발화 종료 감지, 최종 전사 계산 중
    CLOSED = "closed"          # 세션 종료(정상 또는 오류)

    def __str__(self) -> str:
        return self.value


class ClientMessage(str, Enum):
    """클라이언트 -> 서버."""

    START = "start"      # 세션 시작(샘플레이트·언어 등 파라미터 포함)
    AUDIO = "audio"      # 오디오 청크
    FLUSH = "flush"      # 지금까지의 오디오로 최종 전사를 요청
    STOP = "stop"        # 세션 종료 요청
    #: 음성으로 로봇을 세우는 경로. 전사·계획 생성을 기다리지 않고 즉시
    #: 정지 요청으로 직행한다(개발플랜.md 4단계 "음성 STOP").
    ABORT = "abort"

    def __str__(self) -> str:
        return self.value


class ServerMessage(str, Enum):
    """서버 -> 클라이언트."""

    READY = "ready"            # start 수락
    SPEECH_START = "speech_start"
    PARTIAL = "partial"        # 중간 전사. 명령으로 실행하지 않는다
    FINAL = "final"            # 최종 전사. 계획 생성 입력이 된다
    #: 신뢰도가 낮거나 슬롯이 부족해 되묻는 경우.
    CLARIFY = "clarify"
    STATE = "state"            # 세션 상태 변경 통지
    ERROR = "error"            # 이유 코드를 담은 실패 통지
    CLOSED = "closed"

    def __str__(self) -> str:
        return self.value


#: 세션 상태 허용 전이표. 표에 없는 전이는 거부한다.
ALLOWED_TRANSITIONS: dict[SttSessionState, frozenset[SttSessionState]] = {
    SttSessionState.IDLE: frozenset({SttSessionState.LISTENING, SttSessionState.CLOSED}),
    SttSessionState.LISTENING: frozenset({
        SttSessionState.SPEAKING,
        SttSessionState.FINALIZING,   # flush를 발화 없이 받은 경우
        SttSessionState.CLOSED,
    }),
    SttSessionState.SPEAKING: frozenset({
        SttSessionState.FINALIZING,
        SttSessionState.CLOSED,
    }),
    SttSessionState.FINALIZING: frozenset({
        SttSessionState.LISTENING,    # 최종 전사 후 다음 발화를 기다린다
        SttSessionState.CLOSED,
    }),
    SttSessionState.CLOSED: frozenset(),   # 종료 후 재개하지 않는다
}

#: 각 상태에서 처리할 수 있는 클라이언트 메시지.
#: ABORT는 어느 상태에서나 받아야 한다 — 정지를 상태 때문에 막지 않는다.
ACCEPTED_CLIENT_MESSAGES: dict[SttSessionState, frozenset[ClientMessage]] = {
    SttSessionState.IDLE: frozenset({ClientMessage.START, ClientMessage.STOP, ClientMessage.ABORT}),
    SttSessionState.LISTENING: frozenset({
        ClientMessage.AUDIO, ClientMessage.FLUSH, ClientMessage.STOP, ClientMessage.ABORT
    }),
    SttSessionState.SPEAKING: frozenset({
        ClientMessage.AUDIO, ClientMessage.FLUSH, ClientMessage.STOP, ClientMessage.ABORT
    }),
    SttSessionState.FINALIZING: frozenset({ClientMessage.STOP, ClientMessage.ABORT}),
    SttSessionState.CLOSED: frozenset(),
}

#: 종료 상태.
TERMINAL_STATES: frozenset[SttSessionState] = frozenset({SttSessionState.CLOSED})


class InvalidSttTransition(Exception):
    def __init__(self, src: SttSessionState, dst: SttSessionState):
        self.src, self.dst = src, dst
        super().__init__(f"허용되지 않은 STT 세션 전이: {src} -> {dst}")


def can_transition(src: SttSessionState, dst: SttSessionState) -> bool:
    return dst in ALLOWED_TRANSITIONS[src]


def transition(src: SttSessionState, dst: SttSessionState) -> SttSessionState:
    if not can_transition(src, dst):
        raise InvalidSttTransition(src, dst)
    return dst


def accepts(state: SttSessionState, message: ClientMessage) -> bool:
    return message in ACCEPTED_CLIENT_MESSAGES[state]
