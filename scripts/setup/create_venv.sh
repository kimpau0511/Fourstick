#!/usr/bin/env bash
# 웹 서버 가상환경을 만든다(.venv, --system-site-packages — rclpy·gz 바인딩·apt 파이썬 패키지를 빌려 쓴다).
#
#   ./scripts/setup/create_venv.sh
#
# 고정 목록: requirements.txt(개발 PC .venv와 같은 버전). 다른 곳에 두려면 FORSTICK2_VENV.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
PY="${FORSTICK2_SYSTEM_PYTHON:-/usr/bin/python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 12), sys.version' \
  || { echo "[venv] 파이썬 3.12 이상이 필요하다($PY). 개발 PC: 3.14.4" >&2; exit 2; }
if [[ ! -x "$FORSTICK2_VENV/bin/python" ]]; then
  "$PY" -m venv --system-site-packages "$FORSTICK2_VENV"
  echo "[venv] 만듦: $FORSTICK2_VENV"
fi
"$FORSTICK2_VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
forstick2_source_ros || true
"$FORSTICK2_VENV/bin/python" - <<'PY'
import importlib
bad = []
for m in ("uvicorn", "wsproto", "numpy", "pydantic", "yaml", "faster_whisper", "onnxruntime", "rclpy"):
    try:
        importlib.import_module(m)
    except Exception as exc:  # noqa: BLE001
        bad.append(f"{m}: {exc}")
print("[venv] 모듈 확인:", "모두 있음" if not bad else "없음 → " + "; ".join(bad))
raise SystemExit(1 if bad else 0)
PY
