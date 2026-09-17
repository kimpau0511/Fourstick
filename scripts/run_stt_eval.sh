#!/usr/bin/env bash
# 실제 모델로 STT 평가를 돌린다. 일반 단위 테스트(60초 제한)와 분리된 경로다.
#
#   ./scripts/run_stt_eval.sh                            기본(검증된) Profile로 측정
#   STT_PROFILE=turbo-auto-int8 ./scripts/run_stt_eval.sh  특정 Profile로 측정
#
# 모델 다운로드와 최초 로딩 시간이 포함되므로 제한을 따로 둔다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CATALOG="${STT_CATALOG:-examples/config/valid_stt_profiles.json}"
LIMIT="${STT_EVAL_TIMEOUT_SEC:-1800}"

echo "[stt_eval] Profile 목록 $CATALOG / Profile ${STT_PROFILE:-기본} / 제한 ${LIMIT}s"
exec timeout --signal=KILL "$LIMIT" "$ROOT/.venv/bin/python" "$ROOT/scripts/stt_eval.py" "$CATALOG"
