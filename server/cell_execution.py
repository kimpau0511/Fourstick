"""Shared execution authority for a single workcell."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CellExecutionLease:
    """One execution authority; only its holder can release it."""

    owner: str
    operation_id: str
    started_at: float
    token: str

    def to_dict(self) -> dict:
        return {"owner": self.owner, "operation_id": self.operation_id,
                "started_at": self.started_at}


class CellExecutionManager:
    """Serialize motion in one workcell without silently queueing requests."""

    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._active: CellExecutionLease | None = None
        # 여러 동작을 한 묶음으로 잡는 예약(반복 작업, 2026-10-07). 예약이 있으면 같은 예약 토큰을
        # 가진 요청만 임대를 받는다 — 묶음의 동작 사이 틈에 다른 명령이 끼어들지 못한다.
        self._reservation: CellExecutionLease | None = None

    def reserve(self, *, owner: str, operation_id: str,
                alongside_active: bool = False) -> CellExecutionLease | None:
        """셀을 한 묶음(예: 반복 작업)에 예약한다. 다른 임대·예약이 있으면 None.

        `alongside_active`: 지금 임대(예: 재시작 뒤 이어받은 실행)가 있어도 예약한다 — 그 임대가 끝난
        뒤에도 셀을 잡아 두기 위한 것(중단된 반복의 확인 전 잠금). 예약끼리는 겹치지 않는다."""
        if not owner or not operation_id:
            raise ValueError("owner and operation_id are required")
        with self._lock:
            if (self._active is not None and not alongside_active) or self._reservation is not None:
                return None
            self._reservation = CellExecutionLease(
                owner=owner, operation_id=operation_id,
                started_at=float(self._clock()), token=uuid.uuid4().hex)
            return self._reservation

    def unreserve(self, reservation: CellExecutionLease | None) -> bool:
        if reservation is None:
            return False
        with self._lock:
            if self._reservation is None or self._reservation.token != reservation.token:
                return False
            self._reservation = None
            return True

    def reservation(self) -> CellExecutionLease | None:
        with self._lock:
            return self._reservation

    def current(self) -> CellExecutionLease | None:
        with self._lock:
            return self._active

    def try_acquire(self, *, owner: str, operation_id: str,
                    reservation: str | None = None) -> CellExecutionLease | None:
        """`reservation`: 예약 묶음의 토큰. 셀이 예약돼 있으면 그 토큰을 가진 요청만 받는다."""
        if not owner or not operation_id:
            raise ValueError("owner and operation_id are required")
        with self._lock:
            if self._active is not None:
                return None
            if self._reservation is not None and reservation != self._reservation.token:
                return None
            lease = CellExecutionLease(owner=owner, operation_id=operation_id,
                started_at=float(self._clock()), token=uuid.uuid4().hex)
            self._active = lease
            return lease

    def release(self, lease: CellExecutionLease | None) -> bool:
        """Release only the currently active lease."""
        if lease is None:
            return False
        with self._lock:
            if self._active is None or self._active.token != lease.token:
                return False
            self._active = None
            return True
