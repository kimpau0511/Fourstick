#!/usr/bin/env bash
# forstick2 웹 UI + 백엔드를 한 명령으로 띄운다.
#
#   ./scripts/run_web.sh                 localhost:8092
#   FORSTICK2_PORT=9092 ./scripts/run_web.sh
#   FORSTICK2_HOST=0.0.0.0 ./scripts/run_web.sh    (기본은 localhost다)
#   FORSTICK2_STT=0 ./scripts/run_web.sh           STT 모델 로딩 없이 띄운다
#
# 기본 바인딩은 localhost다. 개발용 서버를 실수로 네트워크에 열지 않는다.
# 포트가 이미 쓰이고 있으면 **임의로 종료하지 않고** 점유 프로세스를 보고한다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 이 PC 전용 설정(LLM 주소·STT 모델 등). Git에 넣지 않는다 — 예시는 config/local.env.example.
# 파일 안에서 ${VAR:-기본값} 형태로 쓰므로 명령줄에서 준 값이 우선한다.
if [[ -f "$ROOT/config/local.env" ]]; then
  set -a; source "$ROOT/config/local.env"; set +a
  echo "[run_web] 로컬 설정 적용: config/local.env"
fi
# STT GPU 추론용 CUDA 라이브러리(cuBLAS·cuDNN) 경로.
if [[ -n "${FORSTICK2_CUDA_LIB_DIR:-}" ]]; then
  for d in cublas cudnn cuda_nvrtc; do
    [[ -d "$FORSTICK2_CUDA_LIB_DIR/$d/lib" ]] \
      && LD_LIBRARY_PATH="$FORSTICK2_CUDA_LIB_DIR/$d/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  done
  export LD_LIBRARY_PATH
fi
HOST="${FORSTICK2_HOST:-127.0.0.1}"
PORT="${FORSTICK2_PORT:-8092}"

if command -v ss >/dev/null 2>&1 && ss -tlnp 2>/dev/null | grep -q ":${PORT} "; then
  echo "[run_web] 포트 ${PORT}가 이미 사용 중이다. 프로세스를 종료하지 않는다." >&2
  echo "[run_web] 점유 프로세스:" >&2
  ss -tlnp 2>/dev/null | grep ":${PORT} " >&2 || true
  echo "" >&2
  echo "[run_web] 필요한 조치 중 하나를 고르세요:" >&2
  echo "  1) 다른 포트로 실행:   FORSTICK2_PORT=9092 ./scripts/run_web.sh" >&2
  echo "  2) 점유 프로세스를 직접 확인한 뒤 종료하고 다시 실행" >&2
  exit 3
fi

export FORSTICK2_HOST="$HOST"
export FORSTICK2_PORT="$PORT"
echo "[run_web] http://${HOST}:${PORT}  (Ctrl+C로 종료)"
exec "$ROOT/.venv/bin/python" -m uvicorn \
  --factory server.asgi:create_app \
  --host "$HOST" --port "$PORT" \
  --ws wsproto --no-access-log --app-dir "$ROOT" \
  --timeout-graceful-shutdown "${FORSTICK2_GRACEFUL_SHUTDOWN_SEC:-5}"
