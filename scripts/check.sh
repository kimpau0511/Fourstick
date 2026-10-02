#!/usr/bin/env bash
# 완료 검사 — CODE_RULES.md 10번. 누가(어떤 AI가) 작업했든 이 명령 통과가 "완료"다.
#
#   ./scripts/check.sh                 Python은 python3
#   PYTHON=python ./scripts/check.sh   Windows처럼 python3가 다른 버전을 가리킬 때
#
# 백엔드: ruff(정적 분석) · pyright(타입) · 단위·통합 테스트 · 웹 JS 테스트
# 프론트(dashboard2): eslint · vite build
# 도구 설치: pip install -r requirements-dev.txt, (cd dashboard2 && npm ci)
#
# 하나가 실패해도 끝까지 돌리고 실패한 단계를 모두 알린다 — 하나 고치고 다시
# 돌렸더니 다른 데서 또 막히는 일을 줄이려고(CODE_RULES.md 4번과 같은 이유).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON:-python3}"
failed=()

step() {
    local name="$1"; shift
    echo "[check] ${name}"
    if ! "$@"; then failed+=("$name"); fi
}

cd "$ROOT"
step "ruff"        "$PY" -m ruff check .
step "pyright"     "$PY" -m pyright
step "tests"       "$ROOT/scripts/run_tests.sh"
step "web tests"   node --test tests/web/*.test.mjs
step "eslint"      npm --prefix dashboard2 run lint
step "build"       npm --prefix dashboard2 run build

if [ "${#failed[@]}" -gt 0 ]; then
    echo "[check] 실패: ${failed[*]}" >&2
    exit 1
fi
echo "[check] 전부 통과"
