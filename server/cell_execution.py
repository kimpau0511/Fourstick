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

    def current(self) -> CellExecutionLease | None:
        with self._lock:
            return self._active

    def try_acquire(self, *, owner: str, operation_id: str) -> CellExecutionLease | None:
        if not owner or not operation_id:
            raise ValueError("owner and operation_id are required")
        with self._lock:
            if self._active is not None:
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
