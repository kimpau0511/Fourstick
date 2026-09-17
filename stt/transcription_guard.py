"""전사 실행 가드 — 운영 경로의 시간·동시성 예산 (md/개발플랜.md 4-03 보강).

이 모듈이 있는 이유는 실측으로 드러난 두 가지 문제다.

**1. 발화가 아닌 신호가 세션을 오래 붙잡는다.** 220Hz 순음 2초를 small/cpu/int8로
전사하면 42~65초가 걸렸다(RTF 21~32). 침묵·잡음도 3.5~3.8초씩 썼다. 발화로
판정되지 않은 오디오를 전사에 넘기지 않는 것이 1차 방어이고(세션이 담당),
그래도 오래 걸리는 호출을 끊는 것이 2차 방어다(이 모듈).

**2. `with ThreadPoolExecutor(...)` 안에서 timeout을 잡아도 호출자는 안 풀린다.**
`with` 블록의 `__exit__`가 `shutdown(wait=True)`를 호출하기 때문이다. 제한
0.3초로 3초짜리 작업을 감싸면 3.0초 뒤에 복귀한다(실측). 그래서 여기서는
영속 executor를 쓰고 shutdown을 기다리지 않는다.

**끊을 수 있는 것과 없는 것.** 파이썬에서 실행 중인 스레드를 죽일 수 없고,
CTranslate2 추론을 중간에 취소할 수단도 없다. 그러므로:

- **세션은 제한시간에 즉시 풀려난다.** 호출자는 기다리지 않는다.
- **버려진 작업은 계속 돈다.** 끝날 때까지 동시 처리 슬롯을 점유한다.
- 그 사이 들어온 요청은 대기열에서 무기한 기다리지 않고 `stt.capacity_exceeded`로
  **즉시 거절**된다. 복구 가능(재시도 가능) 실패다.

이 성질을 숨기지 않고 `GuardOutcome.abandoned`로 드러낸다.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Callable

from core.policy import SttPolicy
from core.reason_codes import ReasonCode
from stt.whisper_backend import Transcript


@dataclass(frozen=True)
class GuardOutcome:
    """전사 1회의 결과. 성공·실패를 한 형태로 담는다."""

    #: 성공했을 때의 전사 결과. 실패면 None이다.
    transcript: Transcript | None
    #: 호출자가 기다린 시간(초). 제한시간 초과면 예산에 가깝다.
    waited_sec: float
    reason: ReasonCode | None = None
    #: 재시도로 해결될 수 있는 실패인가. 성공이면 None이다.
    recoverable: bool | None = None
    detail: str = ""
    #: 제한시간을 넘겨 결과를 버렸는가. 작업 스레드는 아직 돌고 있다.
    abandoned: bool = False

    @property
    def ok(self) -> bool:
        return self.transcript is not None


class TranscriptionGuard:
    """동시 처리 수와 제한시간을 정책으로 강제한다.

    `max_concurrent_transcriptions`만큼만 동시에 돌린다. 슬롯이 없으면 기다리지
    않고 거절한다 — 대기열을 만들면 지연이 눈에 보이지 않게 쌓인다.
    """

    def __init__(self, policy: SttPolicy, *, clock: Callable[[], float] = time.monotonic):
        self.policy = policy
        self._clock = clock
        # 영속 executor. 제한시간 초과 시 shutdown을 기다리지 않는다.
        self._pool = ThreadPoolExecutor(
            max_workers=policy.max_concurrent_transcriptions,
            thread_name_prefix="stt-transcribe",
        )
        #: 아직 끝나지 않은 작업. 버려진 작업도 여기 남아 슬롯을 점유한다.
        self._inflight: list[Future] = []

    # ── 조회 ────────────────────────────────────────────────────────────
    def _reap(self) -> None:
        self._inflight = [f for f in self._inflight if not f.done()]

    @property
    def inflight(self) -> int:
        self._reap()
        return len(self._inflight)

    @property
    def free_slots(self) -> int:
        return max(0, self.policy.max_concurrent_transcriptions - self.inflight)

    # ── 실행 ────────────────────────────────────────────────────────────
    def run(
        self, transcriber, audio: bytes, sample_rate_hz: int, *,
        deadline_sec: float | None = None,
    ) -> GuardOutcome:
        """전사를 실행한다. 예외를 올리지 않고 이유 코드로 돌려준다."""
        if self.free_slots <= 0:
            return GuardOutcome(
                transcript=None, waited_sec=0.0,
                reason=ReasonCode.STT_CAPACITY_EXCEEDED, recoverable=True,
                detail=(
                    f"동시 전사 {self.policy.max_concurrent_transcriptions}건이 모두"
                    " 사용 중이다 — 대기열에 넣지 않고 거절한다"
                ),
            )
        budget = deadline_sec if deadline_sec is not None else self.policy.transcribe_deadline_sec
        started = self._clock()
        future = self._pool.submit(transcriber.transcribe, audio, sample_rate_hz)
        self._inflight.append(future)
        try:
            transcript = future.result(timeout=budget)
        except FutureTimeout:
            # 스레드를 죽일 수 없다. 세션만 놓아주고 작업은 버린다.
            future.cancel()   # 아직 시작 안 했으면 취소된다. 돌고 있으면 무효다.
            return GuardOutcome(
                transcript=None, waited_sec=self._clock() - started,
                reason=ReasonCode.STT_TRANSCRIBE_TIMEOUT, recoverable=True,
                detail=(
                    f"전사가 예산 {budget}s를 넘겼다 — 결과를 버린다."
                    " 추론 스레드는 끝날 때까지 슬롯을 점유한다"
                ),
                abandoned=True,
            )
        except Exception as exc:  # noqa: BLE001 — 백엔드 실패를 이유 코드로 바꾼다
            return GuardOutcome(
                transcript=None, waited_sec=self._clock() - started,
                reason=getattr(exc, "reason", ReasonCode.STT_BACKEND_UNAVAILABLE),
                # 백엔드가 없거나 설정이 틀린 것은 재시도로 풀리지 않는다.
                recoverable=False, detail=str(exc)[:200],
            )
        return GuardOutcome(
            transcript=transcript, waited_sec=self._clock() - started
        )

    def close(self, *, wait: bool = False) -> None:
        """executor를 닫는다. 기본은 기다리지 않는다 — 버려진 작업에 묶이지 않는다."""
        self._pool.shutdown(wait=wait, cancel_futures=True)
