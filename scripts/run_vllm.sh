#!/usr/bin/env bash
# (선택) 계획 생성·자연어 해석용 vLLM 서버. FR3 시뮬레이션의 정확한 명령·확인·STOP은 없어도 된다.
#
#   FORSTICK2_VLLM_BIN=/경로/venv/bin/vllm FORSTICK2_QWEN_DIR=~/models/qwen3-8b-awq ./scripts/run_vllm.sh
#
# 개발 PC: vLLM 0.28.0 · torch 2.13.0+cu130 · RTX 4060 Ti 8 GB · WSL2. 설정값의 근거는
# examples/config/valid_llm_provider_qwen3.json의 provenance.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
VLLM="${FORSTICK2_VLLM_BIN:-vllm}"
MODEL="${FORSTICK2_QWEN_DIR:-$HOME/models/qwen3-8b-awq}"
command -v "$VLLM" >/dev/null || { echo "[vllm] vllm 실행 파일이 없다: $VLLM (FORSTICK2_VLLM_BIN)" >&2; exit 2; }
[[ -f "$MODEL/config.json" ]] || { echo "[vllm] 모델이 없다: $MODEL — ./scripts/setup/fetch_models.sh --qwen" >&2; exit 3; }
# WSL2: pinned memory를 켜지 않으면 'UVA is not available'로 죽는다. venv bin이 PATH에 없으면 ninja를 못 찾는다(실측).
export PATH="$(dirname "$(command -v "$VLLM")"):$PATH"
grep -qi microsoft /proc/version 2>/dev/null && export VLLM_WSL2_ENABLE_PIN_MEMORY=1
exec "$VLLM" serve "$MODEL" --served-model-name qwen3-8b-awq --max-model-len 3072 \
  --gpu-memory-utilization "${FORSTICK2_VLLM_GPU_UTIL:-0.86}" --host 127.0.0.1 --port "${FORSTICK2_VLLM_PORT:-8000}"
