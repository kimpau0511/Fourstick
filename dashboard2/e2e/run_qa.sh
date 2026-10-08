#!/usr/bin/env bash
# 요구사항 QA를 단계별로 한 번에 돌린다. 분류(A~F)와 근거는 문서/QA_자동화_분류.md(저장소 밖, 로컬).
#
#   bash dashboard2/e2e/run_qa.sh          1단계(A) — 서버 없이 도는 것 전부
#   bash dashboard2/e2e/run_qa.sh server   1단계 + 2단계(B 읽기) — 작업 셀 서버 API
#     서버 주소는 FORSTICK2_BACKEND_URL(기본값은 spec의 옛 주소라 닿지 않는다 — 예: https://foursticks.xos.kr)
#
# 1단계: 백엔드 테스트(tests/run_all.py) · 요구사항별 백엔드 대응(backend_map.py) · 웹 JS 테스트 ·
#        eslint · vite build · 화면 QA(Playwright ui·ui-general, 서버 응답은 가짜)
# 2단계: Playwright api — 읽기와 확인 카드 생성·취소만(로봇은 움직이지 않는다). 서버가 꺼져 있으면 "미실행".
#
# 이 스크립트가 **돌리지 않는 것**(QA 결정 D5 — 팀원 허락·PC1 자원이 필요):
#   - 로봇을 움직이는 검사(E): QA_ROBOT=1 npm run qa:api, scripts/verify_*_e2e.py
#   - PC1 평가(B·D, 로봇은 안 움직이지만 공유 GPU·MoveIt 사용): scripts/run_workcell_command_eval.sh 등
#   그래서 QA_ROBOT은 여기서 강제로 비운다.
#
# 하나가 실패해도 끝까지 돌리고(CODE_RULES 4번과 같은 이유) 단계별 결과를 e2e-results/qa_summary.md에 남긴다.
# 판정은 통과 / 실패 / 미실행뿐이다 — 미실행을 통과로 세지 않는다.
set -uo pipefail

DASH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(cd "$DASH/.." && pwd)"
OUT="$DASH/e2e-results"
SUMMARY="$OUT/qa_summary.md"
mkdir -p "$OUT/logs"
# 이전 실행의 결과가 이번 요약에 섞이지 않게 지운다(로그·보고서·Playwright 결과).
rm -f "$OUT"/logs/*.log "$OUT"/backend_map.md "$OUT"/report_ui.md "$OUT"/report_api.md "$OUT"/results.json "$SUMMARY"
unset QA_ROBOT
# Windows 기본 인코딩(cp949) 대신 UTF-8로 — 테스트가 인코딩 없이 연 한국어 파일에서 UnicodeDecodeError가
# 75건 났다(2026-10-08). 리눅스(작업 셀 PC)의 기본과 같은 조건으로 맞춘다.
export PYTHONUTF8=1

# 백엔드 테스트용 파이썬: 저장소 venv(Windows·리눅스) → PYTHON 환경변수 → python3
if [[ -n "${PYTHON:-}" ]]; then PY="$PYTHON"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then PY="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then PY="$ROOT/.venv/bin/python"
else PY=python3; fi

rows=()
step() { # 이름 명령...
  local name="$1"; shift
  local log="$OUT/logs/${name// /_}.log" t0=$SECONDS rc
  echo "[qa] ${name} …"
  "$@" > "$log" 2>&1; rc=$?
  local verdict="통과"; [[ $rc -ne 0 ]] && verdict="실패(종료 코드 $rc)"
  # Playwright는 전부 건너뛰어도 종료 코드 0이다 — 통과한 시험이 하나도 없으면 "미실행"으로 적는다
  # (2026-10-08: 서버 주소가 닿지 않아 10개가 모두 건너뛰었는데 "통과"로 보였다).
  if [[ $rc -eq 0 ]] && grep -qE '^[[:space:]]+[0-9]+ skipped' "$log" && ! grep -qE '^[[:space:]]+[0-9]+ passed' "$log"; then
    verdict="미실행(전부 건너뜀)"
  fi
  rows+=("| ${name} | ${verdict} | $((SECONDS - t0))초 | \`e2e-results/logs/${name// /_}.log\` |")
  echo "[qa] ${name}: ${verdict}"
}

cd "$ROOT"
step "백엔드 테스트" env FORSTICK_TEST_TIMEOUT_SEC="${FORSTICK_TEST_TIMEOUT_SEC:-180}" "$PY" tests/run_all.py
step "요구사항별 백엔드 대응" "$PY" dashboard2/e2e/backend_map.py "$OUT/backend_map.md"
step "웹 JS 테스트" bash -c 'node --test tests/web/*.test.mjs'
cd "$DASH"
step "eslint" npm run lint
step "build" npm run build
step "화면 QA" npx playwright test --project=ui --project=ui-general
node e2e/report.mjs "$OUT/report_ui.md" > /dev/null 2>&1 || true

if [[ "${1:-}" == server ]]; then
  # 파일을 이름으로 고정한다 — api 폴더에 이동 시험(robot.spec.js)이 있고, 새 spec이 생겨도 여기로 딸려 오지 않게.
  step "서버 API(읽기)" npx playwright test --project=api e2e/api/backend.spec.js
  node e2e/report.mjs "$OUT/report_api.md" > /dev/null 2>&1 || true
fi

{
  echo "# QA 실행 요약 — $(date '+%Y-%m-%d %H:%M') · 커밋 $(git -C "$ROOT" rev-parse --short HEAD)$(git -C "$ROOT" diff --quiet || echo ' (+커밋 안 된 변경)')"
  echo
  echo "| 단계 | 결과 | 걸린 시간 | 로그 |"
  echo "|---|---|---|---|"
  printf '%s\n' "${rows[@]}"
  echo
  echo "돌리지 않은 것: 로봇 이동 검사(E)·PC1 평가(B·D) — 팀원 허락 뒤 따로 실행."
  for f in backend_map report_ui report_api; do
    [[ -f "$OUT/$f.md" ]] && { echo; echo "## $f"; echo; cat "$OUT/$f.md"; }
  done
} > "$SUMMARY"
echo "[qa] 요약: $SUMMARY"
printf '%s\n' "${rows[@]}" | grep -q '실패' && exit 1
exit 0
