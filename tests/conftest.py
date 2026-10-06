"""pytest가 `tests/run_all.py`와 같은 테스트 패키지 기준을 쓰게 한다.

통합 테스트는 `integration.*`를 테스트 최상위 패키지로 import한다. 표준 러너는
`tests/`를 top-level로 두므로, pytest 수집에서도 같은 경로를 명시한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
