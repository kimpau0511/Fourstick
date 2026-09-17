"""결정적 가짜 전송 계층 (md/개발플랜.md 2-03~2-05 시나리오용).

실제 액션 서버(rclpy 등)를 흉내내되 **실제 시간을 쓰지 않는다**. 시나리오를
미리 지정하면 전송 수락/거부, 전송 응답 타임아웃, 결과 타임아웃, 종료 상태,
연결 손실, 상태 stale을 그대로 재현한다.

Adapter가 전송 계층 규칙을 지키는지 관측할 수 있도록 호출 기록을 남긴다
(md/STOP테스트_이관대조표.md "전송 계층으로 분류한 항목"):
  - send 호출 횟수(재전송 금지)
  - 결과 콜백 등록 여부와 대상 future
  - 취소 호출 기록
  - 결과 구독 여부
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class GoalStatus(str, Enum):
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"
    ABORTED = "aborted"


class SendOutcome(str, Enum):
    ACCEPT = "accept"            # 즉시 수락
    REJECT = "reject"            # 즉시 거부
    TIMEOUT_THEN_ACCEPT = "timeout_then_accept"   # 전송 응답 지연 후 늦은 수락
    TIMEOUT_NEVER = "timeout_never"               # 응답이 끝까지 안 옴


@dataclass
class FakeFuture:
    """결과 대기 객체. 콜백 등록을 기록해 Adapter가 실제로 구독했는지 본다."""

    key: str
    done: bool = False
    value: Any = None
    callbacks: list[Callable[["FakeFuture"], None]] = field(default_factory=list)
    #: 콜백이 예외를 던지도록 만들 때 사용.
    raise_in_callback: bool = False

    def add_done_callback(self, fn: Callable[["FakeFuture"], None]) -> None:
        self.callbacks.append(fn)
        if self.done:
            self._fire(fn)

    def resolve(self, value: Any) -> None:
        self.done, self.value = True, value
        for fn in list(self.callbacks):
            self._fire(fn)

    def _fire(self, fn: Callable[["FakeFuture"], None]) -> None:
        if self.raise_in_callback:
            raise RuntimeError("의도적으로 콜백에서 예외 발생")
        fn(self)


@dataclass
class Scenario:
    """채널별 시나리오. 지정하지 않으면 정상 동작이다."""

    send: SendOutcome = SendOutcome.ACCEPT
    result_status: GoalStatus = GoalStatus.SUCCEEDED
    #: 결과 응답이 대기 시간 안에 오지 않는 경우.
    result_timeout: bool = False
    #: 취소 요청에 ACK를 주는가.
    cancel_ack: bool = True
    #: 연결 자체가 끊어진 상태.
    connected: bool = True
    #: 결과 콜백에서 예외를 던지게 한다.
    raise_in_result_callback: bool = False


@dataclass
class CallLog:
    sends: list[str] = field(default_factory=list)
    cancels: list[str] = field(default_factory=list)
    result_subscriptions: list[str] = field(default_factory=list)


class FakeTransport:
    """채널별로 goal 전송·취소·결과를 흉내낸다."""

    def __init__(self, scenarios: dict[str, Scenario] | None = None):
        self._scenarios = dict(scenarios or {})
        self.log = CallLog()
        self._pending_futures: dict[str, FakeFuture] = {}
        self._seq = 0

    def scenario(self, channel: str) -> Scenario:
        return self._scenarios.setdefault(channel, Scenario())

    def set_scenario(self, channel: str, scenario: Scenario) -> None:
        self._scenarios[channel] = scenario

    def _next(self, channel: str) -> str:
        self._seq += 1
        return f"{channel}:goal{self._seq}"

    # ── 전송 ────────────────────────────────────────────────────────────
    def send(self, channel: str, request_id: str) -> tuple[str, FakeFuture | None]:
        """전송한다. (결과, accept_future) 반환.

        accept_future가 None이면 이미 수락/거부가 확정된 것이고, 아니면
        나중에 `deliver_late_accept()`로 수락 응답이 도착한다.
        """
        self.log.sends.append(request_id)
        sc = self.scenario(channel)
        if not sc.connected:
            return ("connection_lost", None)
        if sc.send is SendOutcome.REJECT:
            return ("rejected", None)
        if sc.send is SendOutcome.ACCEPT:
            return ("accepted", None)
        fut = FakeFuture(key=request_id)
        self._pending_futures[request_id] = fut
        return ("send_timeout", fut)

    def accepted_goal_ref(self, channel: str) -> str:
        return self._next(channel)

    def deliver_late_accept(self, channel: str, request_id: str) -> str | None:
        """지연된 수락 응답을 도착시킨다. TIMEOUT_NEVER면 아무 일도 없다."""
        fut = self._pending_futures.get(request_id)
        if fut is None:
            return None
        if self.scenario(channel).send is SendOutcome.TIMEOUT_NEVER:
            return None
        ref = self._next(channel)
        fut.resolve(ref)
        return ref

    # ── 결과 ────────────────────────────────────────────────────────────
    def subscribe_result(self, channel: str, goal_ref: str) -> FakeFuture:
        self.log.result_subscriptions.append(goal_ref)
        sc = self.scenario(channel)
        fut = FakeFuture(key=goal_ref, raise_in_callback=sc.raise_in_result_callback)
        self._pending_futures[f"result:{goal_ref}"] = fut
        return fut

    def deliver_result(self, goal_ref: str, status: GoalStatus | None = None) -> None:
        fut = self._pending_futures.get(f"result:{goal_ref}")
        if fut is None:
            return
        channel = goal_ref.split(":", 1)[0]
        fut.resolve(status or self.scenario(channel).result_status)

    def result_arrives_in_time(self, channel: str) -> bool:
        return not self.scenario(channel).result_timeout

    def result_future(self, goal_ref: str) -> FakeFuture | None:
        """결과 future 조회. Adapter가 콜백을 실제로 걸었는지 확인하는 데 쓴다."""
        return self._pending_futures.get(f"result:{goal_ref}")

    def accept_future(self, request_id: str) -> FakeFuture | None:
        """수락 응답 future 조회. 같은 목적이다."""
        return self._pending_futures.get(request_id)

    # ── 취소 ────────────────────────────────────────────────────────────
    def cancel(self, channel: str, goal_ref: str) -> bool:
        """취소 요청 후 ACK 수신 여부를 돌려준다."""
        self.log.cancels.append(goal_ref)
        sc = self.scenario(channel)
        return bool(sc.connected and sc.cancel_ack)
