#!/usr/bin/env bash
# 실제 모델 통합 테스트. 일반 단위 테스트(60초 제한)와 분리된 경로다.
#
#   ./scripts/run_stt_model_tests.sh
#   STT_MODEL_TEST_TIMEOUT_SEC=1200 ./scripts/run_stt_model_tests.sh
#
# 모델 다운로드와 최초 로딩이 포함되므로 제한을 따로 둔다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIMIT="${STT_MODEL_TEST_TIMEOUT_SEC:-900}"

echo "[stt_model_tests] 제한 ${LIMIT}s"
set +e
FORSTICK_STT_MODEL_TESTS=1 timeout --signal=KILL "$LIMIT" \
    "$ROOT/.venv/bin/python" -m unittest discover -s "$ROOT/tests" \
    -t "$ROOT/tests" -p 'test_real_*.py' -v
status=$?
set -e
if [ "$status" -eq 137 ] || [ "$status" -eq 124 ]; then
    echo "[stt_model_tests] 실패: 제한 ${LIMIT}s 초과 — 무한 대기 의심" >&2
    exit 1
fi
exit "$status"
