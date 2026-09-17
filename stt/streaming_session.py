"""연결별 스트리밍 STT 세션 (md/개발플랜.md 4-03 ~ 4-08).

담는 범위:
  4-03 연결별 세션과 버퍼 제한
  4-05 rolling window 전사와 partial revision
  4-06 무음 또는 end에서 final 한 번만 확정
  4-07 승인된 현장 용어 후보정과 신뢰도 기록
  4-08 partial STOP 키워드의 LLM 우회 경로

담지 않는 범위: 프론트엔드(4-01·4-02·4-09), 실제 모델 설치, WebSocket 전송.
프레임을 넣으면 이벤트를 돌려주는 순수 객체다 — 전송 계층은 7단계가 붙인다.

DB 저장 방침:
- 이 모듈은 `sqlite3`를 모른다. 저장이 필요하면 `storage.Repository`
  인터페이스를 주입받는다.
- 저장하는 것은 **확정된 사용자 요청(`requests`)** 과 **STT 실행 기록
  (`stt_inferences`)** 이다. 실행 기록은 append-only다 — 되묻기로 끝난 시도와
  백엔드 실패도 남기고, 덮어쓰지 않는다. partial transcript와 PCM 원본은
  저장하지 않는다.
- 요청은 채택된 실행 기록의 식별자만 갖는다. 전사 본문과 측정값은 실행 기록에
  있다 — 요청에 복사하면 재전사 때 덮어쓰게 된다.
- **저장 실패를 성공으로 숨기지 않는다.** final 이벤트에 `persisted`와
  `persist_reason`을 담아 실패 책임과 복구 가능 여부를 함께 전달한다.
- `request_id`는 호출자가 발급해 세션에 주입한다. 그래서 이후 plan_id·
  plan_hash·execution_id와 같은 요청으로 이어붙일 수 있다.

전사 실행 방침 (장시간 처리 위험):
- **VAD가 발화로 판정한 적이 없는 오디오는 전사에 넘기지 않는다.** 순음 2초가
  42~65초를 쓰는 것을 실측했다(RTF 21~32). 발화가 아닌 신호로 세션이 붙잡히는
  경로를 먼저 없앤다.
- 전사는 `TranscriptionGuard`를 거친다. 제한시간과 동시 처리 수가 정책이다.
  제한시간 초과·용량 초과는 이유 코드와 복구 가능 여부로 돌아온다.
- Guard를 주입하지 않으면 전사를 직접 호출한다. 이 경로는 **평가·시험용**이며
  실행 기록에 `execution_path=evaluation`으로 남는다 — 운영 통계와 섞지 않는다.

**실제 시간을 쓰지 않는다.** 오디오 시각과 UTC 시각을 모두 주입받는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from core.policy import SttModelConfig, SttPolicy
from core.reason_codes import ReasonCode
from storage.repository import IntegrityViolation, Repository, StorageError
from storage.records import RequestRecord, SttExecutionPath, SttInferenceRecord
from stt.protocol import (
    ClientMessage,
    ServerMessage,
    SttSessionState,
    accepts,
    transition,
)
from stt.transcription_guard import GuardOutcome, TranscriptionGuard
from stt.vad import SpeechEvent, SpeechStateMachine, VadBackend
from stt.whisper_backend import Transcriber


@dataclass(frozen=True)
class SttEvent:
    """세션이 내보내는 이벤트. 서버 메시지 종류와 1:1로 대응한다."""

    kind: ServerMessage
    #: 이 이벤트가 속한 사용자 요청. plan_id·execution_id와 이어붙이는 고리다.
    request_id: str
    #: UTC epoch 초(주입값).
    at_utc: float
    text: str = ""
    confidence: float | None = None
    raw_text: str | None = None
    state: SttSessionState | None = None
    reason: ReasonCode | None = None
    detail: str = ""
    #: final 이벤트에서만 의미가 있다. 저장을 시도했는지와 결과.
    persisted: bool | None = None
    persist_reason: ReasonCode | None = None
    #: 저장 실패를 재시도로 복구할 수 있는지.
    persist_recoverable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "request_id": self.request_id,
            "at_utc": self.at_utc,
            "text": self.text,
            "confidence": self.confidence,
            "raw_text": self.raw_text,
            "state": self.state.value if self.state else None,
            "reason": self.reason.value if self.reason else None,
            "detail": self.detail,
            "persisted": self.persisted,
            "persist_reason": self.persist_reason.value if self.persist_reason else None,
            "persist_recoverable": self.persist_recoverable,
        }


@dataclass(frozen=True)
class TermCorrection:
    """승인된 현장 용어 후보정 규칙 (4-07). 사전은 설정에서 주입한다."""

    #: 잘못 전사되는 표현 -> 승인된 표현
    replacements: Mapping[str, str] = field(default_factory=dict)

    def apply(self, text: str) -> str:
        out = text
        for wrong, right in self.replacements.items():
            out = out.replace(wrong, right)
        return out


class SttSessionError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


class StreamingSTTSession:
    """WebSocket 연결 하나에 대응하는 세션."""

    def __init__(
        self,
        *,
        request_id: str,
        policy: SttPolicy,
        vad: VadBackend,
        transcriber: Transcriber,
        sample_rate_hz: int,
        schema_version: str,
        terms: TermCorrection | None = None,
        repository: Repository | None = None,
        session_id: str = "",
        owner_session_id: str | None = None,
        model_config: SttModelConfig | None = None,
        model_version: str = "",
        model_load_sec: float | None = None,
        confidence_metric: str = "",
        inference_id_factory: Callable[[int], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        guard: TranscriptionGuard | None = None,
        execution_path: SttExecutionPath = SttExecutionPath.OPERATIONAL,
    ):
        if sample_rate_hz <= 0:
            raise SttSessionError(ReasonCode.CONFIG_INVALID, "sample_rate_hz가 0 이하")
        self.request_id = request_id
        self.policy = policy
        self._vad = vad
        self._transcriber = transcriber
        self.sample_rate_hz = sample_rate_hz
        self._schema_version = schema_version
        self._terms = terms or TermCorrection()
        self._repo = repository
        #: 실행 기록에 남길 구성. 저장소를 붙였으면 함께 넘겨야 한다.
        self._model_config = model_config
        self._model_version = model_version
        self._model_load_sec = model_load_sec
        self._confidence_metric = confidence_metric
        #: STT 스트림 식별자. 한 스트림의 여러 시도(재전사·모델 교체)를 묶는다.
        self.session_id = session_id or request_id
        #: 확정된 요청이 귀속될 상위 세션(브라우저 세션). 없으면 None이다 —
        #: 어느 세션의 요청인지 추정하지 않는다.
        self.owner_session_id = owner_session_id
        self._inference_id_factory = inference_id_factory or (
            lambda attempt: f"{self.session_id}#stt{attempt}"
        )
        #: 처리 시간 측정용. 벽시계가 아니라 주입된 단조 시계를 쓴다.
        self._clock = clock
        #: 전사 실행 가드. 제한시간과 동시 처리 수를 강제한다.
        self._guard = guard
        #: 기록에 남을 경로. 세션은 기본이 운영이다 — 평가 하네스가 세션을
        #: 쓰는 경우에만 호출자가 EVALUATION으로 표시한다.
        self._execution_path = execution_path
        if (
            execution_path is SttExecutionPath.OPERATIONAL
            and repository is not None
            and guard is None
        ):
            # 운영 기록을 남기는 세션을 시간 예산 없이 돌리지 않는다. Guard가
            # 없으면 순음 하나가 세션을 수십 초 붙잡을 수 있다(실측 42~65초).
            raise SttSessionError(
                ReasonCode.CONFIG_MISSING,
                "운영 경로 세션에 TranscriptionGuard가 없다"
                " — 전사 제한시간과 동시 처리 수를 강제할 수단이 필요하다",
            )
        #: 오디오 입력이 처음 들어온 UTC 시각.
        self._input_at_utc: float | None = None
        #: VAD가 한 번이라도 음성으로 봤는가와 최대 확률. 실행 기록에 남긴다.
        self._vad_speech_seen = False
        self._vad_max_probability: float | None = None
        #: 이 세션이 이미 쓴 시도 번호. 저장소 조회 실패와 무관하게 늘어난다.
        self._attempts = 0

        self.state = SttSessionState.IDLE
        self._machine = SpeechStateMachine(policy)
        self._audio = bytearray()
        self._audio_sec = 0.0
        self._last_window_at = 0.0
        self._partial_text = ""
        self._final_emitted = False
        self._stop_requested = False

    # ── 상태 ────────────────────────────────────────────────────────────
    def _go(self, dst: SttSessionState, at_utc: float) -> SttEvent:
        self.state = transition(self.state, dst)
        return SttEvent(ServerMessage.STATE, self.request_id, at_utc, state=self.state)

    def _require(self, message: ClientMessage) -> None:
        if not accepts(self.state, message):
            raise SttSessionError(
                ReasonCode.STT_STREAM_ABORTED,
                f"{self.state} 상태에서 {message}를 처리할 수 없다",
            )

    # ── 제어 ────────────────────────────────────────────────────────────
    def start(self, *, at_utc: float) -> Sequence[SttEvent]:
        self._require(ClientMessage.START)
        events = [self._go(SttSessionState.LISTENING, at_utc)]
        events.insert(
            0, SttEvent(ServerMessage.READY, self.request_id, at_utc)
        )
        return tuple(events)

    def abort(self, *, at_utc: float) -> Sequence[SttEvent]:
        """4-08 — 음성 STOP. 전사·계획 생성을 기다리지 않고 즉시 정지로 직행한다."""
        self._require(ClientMessage.ABORT)
        self._stop_requested = True
        events = [
            SttEvent(
                ServerMessage.ERROR, self.request_id, at_utc,
                reason=ReasonCode.EXEC_STOPPED, detail="음성 정지 요청 — 계획 생성 우회",
            )
        ]
        if self.state is not SttSessionState.CLOSED:
            events.append(self._go(SttSessionState.CLOSED, at_utc))
        return tuple(events)

    def stop(self, *, at_utc: float) -> Sequence[SttEvent]:
        self._require(ClientMessage.STOP)
        events: list[SttEvent] = []
        if self.state is not SttSessionState.CLOSED:
            events.append(self._go(SttSessionState.CLOSED, at_utc))
        events.append(SttEvent(ServerMessage.CLOSED, self.request_id, at_utc))
        return tuple(events)

    # ── 오디오 ──────────────────────────────────────────────────────────
    def feed_audio(
        self, chunk: bytes, *, at_sec: float, at_utc: float
    ) -> Sequence[SttEvent]:
        """오디오 프레임 하나를 넣는다. `at_sec`은 오디오 시각(초)이다."""
        self._require(ClientMessage.AUDIO)
        events: list[SttEvent] = []

        # 4-03 버퍼 제한: 긴 발화는 정해진 오류로 종료한다.
        if at_sec > self.policy.max_audio_sec:
            events.append(
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=ReasonCode.STT_STREAM_ABORTED,
                    detail=f"오디오가 상한 {self.policy.max_audio_sec}s를 넘었다",
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)

        if self._input_at_utc is None:
            self._input_at_utc = at_utc
        self._audio.extend(chunk)
        self._audio_sec = at_sec

        event = self._machine.feed(
            is_speech=self._observe_vad(chunk), at_sec=at_sec
        )
        if event is SpeechEvent.SPEECH_START:
            events.append(self._go(SttSessionState.SPEAKING, at_utc))
            events.append(
                SttEvent(ServerMessage.SPEECH_START, self.request_id, at_utc)
            )
        elif event is SpeechEvent.SPEECH_END:
            # 4-06 무음에서 final 확정
            events.extend(self._finalize(at_sec=at_sec, at_utc=at_utc))
            return tuple(events)

        if self.state is SttSessionState.SPEAKING:
            events.extend(self._maybe_partial(at_sec=at_sec, at_utc=at_utc))
        return tuple(events)

    def flush(self, *, at_sec: float, at_utc: float) -> Sequence[SttEvent]:
        """4-06 — end에서 final 확정. 발화가 없었어도 한 번만 확정한다."""
        self._require(ClientMessage.FLUSH)
        self._machine.force_end()
        return self._finalize(at_sec=at_sec, at_utc=at_utc)

    def _observe_vad(self, chunk: bytes) -> bool:
        """VAD 판정을 받고 실행 기록에 남길 흔적을 모은다."""
        decision = self._vad.is_speech(chunk, self.sample_rate_hz)
        if decision:
            self._vad_speech_seen = True
        probability = getattr(self._vad, "last_probability", None)
        if isinstance(probability, (int, float)):
            self._vad_max_probability = max(
                self._vad_max_probability or 0.0, float(probability)
            )
        return decision

    # ── 전사 ────────────────────────────────────────────────────────────
    def _transcribe(self, audio: bytes) -> GuardOutcome:
        """전사 1회. Guard가 있으면 예산 안에서, 없으면 직접 호출한다."""
        if self._guard is not None:
            return self._guard.run(self._transcriber, audio, self.sample_rate_hz)
        started = self._clock()
        try:
            transcript = self._transcriber.transcribe(audio, self.sample_rate_hz)
        except Exception as exc:  # noqa: BLE001
            return GuardOutcome(
                transcript=None, waited_sec=self._clock() - started,
                reason=getattr(exc, "reason", ReasonCode.STT_BACKEND_UNAVAILABLE),
                recoverable=False, detail=str(exc)[:200],
            )
        return GuardOutcome(transcript=transcript, waited_sec=self._clock() - started)

    def _window(self, at_sec: float) -> bytes:
        """4-05 rolling window — 마지막 window_sec 구간만 전사한다."""
        bytes_per_sec = self.sample_rate_hz * 2   # PCM16 mono
        want = int(self.policy.window_sec * bytes_per_sec)
        return bytes(self._audio[-want:]) if want < len(self._audio) else bytes(self._audio)

    def _maybe_partial(self, *, at_sec: float, at_utc: float) -> Sequence[SttEvent]:
        if at_sec - self._last_window_at < self.policy.window_stride_sec:
            return ()
        self._last_window_at = at_sec
        outcome = self._transcribe(self._window(at_sec))
        if not outcome.ok:
            # partial 실패는 발화를 끝내지 않는다 — 다음 window에서 다시 시도한다.
            return (
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=outcome.reason, detail=outcome.detail,
                    persist_recoverable=outcome.recoverable,
                ),
            )
        result = outcome.transcript
        corrected = self._terms.apply(result.text)
        events: list[SttEvent] = []

        # 4-08 STOP 키워드 우회: partial에서 보이면 즉시 정지로 직행한다.
        if any(k in corrected for k in self.policy.stop_keywords):
            self._stop_requested = True
            events.append(
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=ReasonCode.EXEC_STOPPED,
                    detail="partial에서 정지 키워드 감지 — 계획 생성 우회",
                    text=corrected, confidence=result.confidence, raw_text=result.text,
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)

        # 4-05 partial revision: 내용이 바뀔 때만 내보낸다.
        if corrected != self._partial_text:
            self._partial_text = corrected
            events.append(
                SttEvent(
                    ServerMessage.PARTIAL, self.request_id, at_utc,
                    text=corrected, confidence=result.confidence, raw_text=result.text,
                )
            )
        return tuple(events)

    def _finalize(self, *, at_sec: float, at_utc: float) -> Sequence[SttEvent]:
        """4-06 — final은 발화당 한 번만. 이미 확정했으면 아무것도 하지 않는다."""
        if self._final_emitted or self._stop_requested:
            return ()
        self._final_emitted = True
        events: list[SttEvent] = []

        if not self._audio:
            events.append(
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=ReasonCode.STT_NO_SPEECH, detail="오디오가 없다",
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)

        # **VAD가 한 번도 발화로 보지 않았으면 전사하지 않는다.**
        # 침묵·잡음·순음을 전사에 넘기면 RTF 21~32짜리 추론이 세션을 붙잡는다.
        if not self._vad_speech_seen:
            self._append_inference(
                at_utc=at_utc, process_sec=0.0, transcript="", confidence=0.0,
                adopted=False, reason=ReasonCode.STT_NO_SPEECH,
            )
            events.append(
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=ReasonCode.STT_NO_SPEECH,
                    detail="VAD가 발화로 판정한 구간이 없다 — 전사하지 않는다",
                    persist_recoverable=True,
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)

        events.append(self._go(SttSessionState.FINALIZING, at_utc))
        outcome = self._transcribe(bytes(self._audio))
        if not outcome.ok:
            # 실패한 시도도 기록에 남긴다 — 어떤 구성이 실패했는지가 비교 자료다.
            self._append_inference(
                at_utc=at_utc, process_sec=outcome.waited_sec,
                transcript="", confidence=0.0, adopted=False, reason=outcome.reason,
            )
            events.append(
                SttEvent(
                    ServerMessage.ERROR, self.request_id, at_utc,
                    reason=outcome.reason, detail=outcome.detail,
                    persist_recoverable=outcome.recoverable,
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)
        result = outcome.transcript
        process_sec = outcome.waited_sec

        corrected = self._terms.apply(result.text)

        # 4-07 신뢰도 기준: 낮으면 계획 생성으로 넘기지 않고 되묻는다.
        if result.confidence < self.policy.min_final_confidence:
            # 되묻기로 끝난 시도도 남긴다. 다음 시도와 비교할 자료가 된다.
            self._append_inference(
                at_utc=at_utc, process_sec=process_sec, transcript=corrected,
                confidence=result.confidence, adopted=False,
                reason=ReasonCode.STT_LOW_CONFIDENCE,
            )
            events.append(
                SttEvent(
                    ServerMessage.CLARIFY, self.request_id, at_utc,
                    text=corrected, confidence=result.confidence, raw_text=result.text,
                    reason=ReasonCode.STT_LOW_CONFIDENCE,
                    detail="신뢰도가 기준 미만이다 — 다시 말해 달라고 요청한다",
                )
            )
            events.append(self._go(SttSessionState.CLOSED, at_utc))
            return tuple(events)

        persisted, persist_reason, recoverable = self._persist(
            corrected, at_utc, confidence=result.confidence, process_sec=process_sec
        )
        events.append(
            SttEvent(
                ServerMessage.FINAL, self.request_id, at_utc,
                text=corrected, confidence=result.confidence, raw_text=result.text,
                persisted=persisted, persist_reason=persist_reason,
                persist_recoverable=recoverable,
            )
        )
        events.append(self._go(SttSessionState.CLOSED, at_utc))
        return tuple(events)

    # ── 저장 ────────────────────────────────────────────────────────────
    def _inference(
        self, *, at_utc: float, process_sec: float, transcript: str,
        confidence: float, adopted: bool, reason: ReasonCode | None,
    ) -> SttInferenceRecord | None:
        """이번 시도의 실행 기록을 만든다. 구성이 없으면 만들지 않는다."""
        if self._model_config is None:
            return None
        self._attempts += 1
        attempt = self._attempts
        if self._repo is not None:
            # 이전 세션의 시도가 이미 있으면 이어서 센다.
            try:
                attempt = max(attempt, self._repo.next_stt_attempt_no(self.session_id))
            except Exception:  # noqa: BLE001 — 번호 조회 실패로 기록을 포기하지 않는다
                pass
        c = self._model_config
        return SttInferenceRecord(
            stt_inference_id=self._inference_id_factory(attempt),
            session_id=self.session_id,
            attempt_no=attempt,
            schema_version=self._schema_version,
            created_at=at_utc,
            model_name=c.model_name,
            model_version=self._model_version or "unknown",
            profile_id=c.profile_id,
            profile_version=c.config_version,
            verification=c.verification.value,
            device=c.device,
            compute_type=c.compute_type,
            language=c.language,
            audio_duration_ms=int(round(self._audio_sec * 1000)),
            processing_duration_ms=int(round(process_sec * 1000)),
            model_load_duration_ms=(
                None if self._model_load_sec is None
                else int(round(self._model_load_sec * 1000))
            ),
            transcript=transcript,
            confidence=confidence,
            confidence_metric=self._confidence_metric or "unknown",
            final_adopted=adopted,
            vad_speech_detected=self._vad_speech_seen,
            vad_max_probability=self._vad_max_probability,
            reason_code=reason,
            execution_path=self._execution_path,
            request_id=self.request_id if adopted else None,
        )

    def _append_inference(
        self, *, at_utc: float, process_sec: float, transcript: str,
        confidence: float, adopted: bool, reason: ReasonCode | None,
        link_request: bool = False,
    ) -> None:
        """채택되지 않은 시도를 기록한다. 실패해도 세션을 멈추지 않는다.

        보통 `request_id`는 비워 둔다 — 확정된 요청이 아직 없으므로 연결할
        대상이 없고, 세션 식별자로 같은 발화의 시도들이 묶인다. 요청이 이미
        저장돼 있는 경우(`link_request`)에만 그 요청에 연결한다.
        """
        if self._repo is None:
            return
        record = self._inference(
            at_utc=at_utc, process_sec=process_sec, transcript=transcript,
            confidence=confidence, adopted=adopted, reason=reason,
        )
        if record is None:
            return
        if link_request:
            record = replace(record, request_id=self.request_id)
        try:
            self._repo.append_stt_inference(record)
        except Exception:  # noqa: BLE001 — 기록 실패가 되묻기를 막지 않는다
            pass

    def _persist(
        self, utterance: str, at_utc: float, *, confidence: float, process_sec: float
    ) -> tuple[bool | None, ReasonCode | None, bool | None]:
        """확정된 요청과 채택된 실행 기록을 저장소에 남긴다.

        저장소가 없으면 (None, None, None) — 저장을 시도하지 않았다는 뜻이다.
        실패를 성공으로 숨기지 않고 이유와 복구 가능 여부를 함께 돌려준다.

        요청과 실행 기록은 서로를 가리키므로 한 트랜잭션에서 함께 넣는다.
        구성이 주입되지 않았으면(텍스트 입력·시험용) 요청만 저장한다.
        """
        if self._repo is None:
            return (None, None, None)
        inference = self._inference(
            at_utc=at_utc, process_sec=process_sec, transcript=utterance,
            confidence=confidence, adopted=True, reason=None,
        )
        request = RequestRecord(
            request_id=self.request_id,
            utterance=utterance,
            schema_version=self._schema_version,
            created_at=at_utc,
            selected_stt_inference_id=None if inference is None else inference.stt_inference_id,
            session_id=self.owner_session_id,
        )
        try:
            if inference is None:
                self._repo.save_request(request)
            else:
                self._repo.save_request_with_stt_inference(request, inference)
        except IntegrityViolation as exc:
            if self._same_request_already_stored(request):
                # 같은 내용의 요청이 이미 저장돼 있다. 요청은 그대로 두고 —
                # 이미 다른 시도를 채택했으므로 — 이번 시도만 덧붙인다.
                self._append_inference(
                    at_utc=at_utc, process_sec=process_sec, transcript=utterance,
                    confidence=confidence, adopted=False, reason=None,
                    link_request=True,
                )
                return (True, None, None)
            # 같은 request_id로 다른 내용이 이미 있다 — 재시도해도 같은 결과다.
            return (False, exc.reason, False)
        except StorageError as exc:
            return (False, exc.reason, True)
        except Exception:  # noqa: BLE001 — 예상 못한 저장 실패도 숨기지 않는다
            return (False, ReasonCode.CONFIG_INVALID, True)
        return (True, None, None)

    def _same_request_already_stored(self, request: RequestRecord) -> bool:
        """같은 내용의 확정 요청이 이미 저장돼 있는가."""
        if self._repo is None:
            return False
        try:
            stored = self._repo.get_request(request.request_id)
        except Exception:  # noqa: BLE001
            return False
        return (
            stored.utterance == request.utterance
            and stored.schema_version == request.schema_version
            and stored.created_at == request.created_at
        )

    # ── 조회 ────────────────────────────────────────────────────────────
    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def final_emitted(self) -> bool:
        return self._final_emitted

    @property
    def buffered_sec(self) -> float:
        return self._audio_sec
