"""시연 셀의 이동 속도 설정(웹에서 고른 %) — 저장과 검증만 한다 (2026-10-06).

정책(범위·간격·기본값)은 작업 셀 설정(매니페스트 `motion`)에서 온다. 여기서는 사용자가 고른 값이
그 정책을 지키는지 보고 파일에 남긴다. 실행 중인 작업은 바꾸지 않는다 — 다음 작업의 실행 인자
(`--speed-percent`)로만 쓰인다. 저장 파일이 처음 없으면 정책의 초기값이다. 손상된 저장값은 조회·실행을 차단한다.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.motion_speed import MotionSpeedPolicy
from core.policy import PolicyError


class MotionSettings:
    def __init__(self, policy: MotionSpeedPolicy, path: Path, *,
                 clock: Callable[[], float] = time.time):
        self.policy = policy
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def percent(self) -> int:
        with self._lock:
            return self._percent()

    def _percent(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.policy.default_percent
        except (OSError, ValueError) as exc:
            from core.reason_codes import ReasonCode
            raise PolicyError(ReasonCode.CONFIG_INVALID, "저장된 이동 속도를 읽지 못했습니다") from exc
        return self.policy.check_percent(data.get("speed_percent") if isinstance(data, dict) else None)

    def snapshot(self) -> int:
        return self.policy.execution_percent(self.percent())

    def current(self) -> dict[str, Any]:
        return self._response(self.percent())

    def _response(self, percent: int) -> dict[str, Any]:
        p = self.policy
        return {"speed_percent": percent, "default": p.default_percent,
                "min": p.min_percent, "max": p.max_percent, "step": p.step_percent,
                "applies_to": "next_job", "is_simulated": True}

    def set_percent(self, value: Any) -> dict[str, Any]:
        """정책 밖 값은 PolicyError다. 고쳐서 저장하지 않는다."""
        percent = self.policy.check_percent(value)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"speed_percent": percent, "updated_at": self._clock()},
                                      ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        return self._response(percent)
