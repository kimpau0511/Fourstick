#!/usr/bin/env bash
# 테스트 실행 스크립트 — 전체 시간 제한을 두 겹으로 적용한다.
#
#   ./scripts/run_tests.sh              기본 제한(60s)
#   FORSTICK_TEST_TIMEOUT_SEC=120 ./scripts/run_tests.sh
#
# 1겹: tests/run_all.py의 faulthandler가 제한을 넘기면 스택을 덤프하고 종료
# 2겹: 아래 timeout이 덤프조차 못 하는 상황까지 잡는다(제한 + 유예 10s)
#
# 테스트 내부에 실제 대기를 추가하지 않는다. 제한은 러너 수준에만 둔다.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIMIT="${FORSTICK_TEST_TIMEOUT_SEC:-60}"
GRACE=$((LIMIT + 10))

echo "[run_tests] 제한 ${LIMIT}s (외부 timeout ${GRACE}s)"

set +e
FORSTICK_TEST_TIMEOUT_SEC="$LIMIT" timeout --signal=KILL "$GRACE" \
    python3 "$ROOT/tests/run_all.py"
status=$?
set -e

if [ "$status" -eq 137 ] || [ "$status" -eq 124 ]; then
    echo "[run_tests] 실패: 외부 timeout(${GRACE}s)에 걸렸다 — 무한 대기 의심" >&2
    exit 1
fi
exit "$status"
