#!/usr/bin/env bash
# 웹 화면(상태·렌더) 테스트. 브라우저 없이 node로 돈다.
#
#   ./scripts/run_web_ui_tests.sh
#
# 여기서 확인하는 상태: PASS / BLOCK / ASK / 실행 중 / 실행 완료 /
# 개별 실행 취소 / STOP 확인 / STOP 미확인 / 시뮬레이션 시연 카드·명령.
#
# tests/web의 *.test.mjs를 **전부** 돌린다. 목록을 손으로 적으면 새 테스트가
# 조용히 빠진다(실측: 시연 카드·명령 테스트 16건이 빠져 있었다).
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v node >/dev/null 2>&1; then
  echo "[run_web_ui_tests] node가 없다. 화면 테스트를 건너뛴다." >&2
  exit 3
fi

cd "$ROOT"
shopt -s nullglob
FILES=(tests/web/*.test.mjs)
if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "[run_web_ui_tests] tests/web에 테스트 파일이 없다." >&2
  exit 4
fi
echo "[run_web_ui_tests] ${#FILES[@]}개 파일"
exec node --test "${FILES[@]}"
