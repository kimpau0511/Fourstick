"""테스트 러너 — 전체 실행시간 제한과 무한 대기 탐지.

`계획.md` 13장 기반 통과 조건 4("각 테스트에 최대 실행시간이 있어 무한 대기가
없음")를 러너 수준에서 충족한다. **테스트 내부에 실제 대기를 추가하지 않는다.**

두 겹으로 막는다.
1. `faulthandler.dump_traceback_later(deadline, exit=True)` — 제한을 넘기면
   모든 스레드의 스택을 덤프하고 프로세스를 종료한다. 어디서 멈췄는지 남는다.
2. 실행 스크립트(`scripts/run_tests.sh`)의 `timeout` — 프로세스가 덤프조차
   못 하는 상황(네이티브 블로킹 등)까지 잡는다.

기본 제한은 환경변수 `FORSTICK_TEST_TIMEOUT_SEC`로 바꿀 수 있다. 실물 Adapter가
들어오면 정상 소요 시간을 재고 이 값을 올린다.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT_SEC = 60.0


def main() -> int:
    sys.path.insert(0, str(ROOT))
    deadline = float(os.environ.get("FORSTICK_TEST_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC))

    faulthandler.enable()
    # 제한을 넘기면 스택을 덤프하고 종료한다. 테스트가 끝나면 취소한다.
    faulthandler.dump_traceback_later(deadline, exit=True)

    started = time.monotonic()
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT / "tests")
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    faulthandler.cancel_dump_traceback_later()

    elapsed = time.monotonic() - started
    print(f"\n총 실행시간 {elapsed:.3f}s / 제한 {deadline:.0f}s")
    if elapsed > deadline * 0.5:
        print(
            "경고: 실행시간이 제한의 절반을 넘었다. 제한값을 재평가하거나 느린 "
            "테스트를 확인한다.",
            file=sys.stderr,
        )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
