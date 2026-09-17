#!/usr/bin/env bash
# 웹 화면(상태·렌더) 테스트. 브라우저 없이 node로 돈다.
#
#   ./scripts/run_web_ui_tests.sh
#
# 여기서 확인하는 상태: PASS / BLOCK / ASK / 실행 중 / 실행 완료 /
# 개별 실행 취소 / STOP 확인 / STOP 미확인.
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "[run_web_ui_tests] node가 없다. 화면 테스트를 건너뛴다." >&2
  exit 3
fi

cd "$ROOT"
exec node --test tests/web/state.test.mjs tests/web/render.test.mjs
