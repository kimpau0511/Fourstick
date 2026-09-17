#!/usr/bin/env bash
# 계획 생성 평가. 실제 LLM 호출이 포함되므로 일반 테스트(60초)와 분리한다.
#
#   ./scripts/run_plan_eval.sh                          실제 + Mock
#   PLAN_EVAL_MOCK_ONLY=1 ./scripts/run_plan_eval.sh    Mock만 (서버 불필요)
#   PLAN_EVAL_REPEATS=5 ./scripts/run_plan_eval.sh      반복 안정성 횟수
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIMIT="${PLAN_EVAL_TIMEOUT_SEC:-3600}"
echo "[plan_eval] 제한 ${LIMIT}s / 반복 ${PLAN_EVAL_REPEATS:-3}회"
exec timeout --signal=KILL "$LIMIT" "$ROOT/.venv/bin/python" "$ROOT/scripts/plan_eval.py" "$@"
