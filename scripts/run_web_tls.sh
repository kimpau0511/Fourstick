#!/usr/bin/env bash
# 평문 8094 앞에 HTTPS(기본 8443)를 얹는다. 서버는 건드리지 않는다.
#
#   ./scripts/run_web_tls.sh                      https://0.0.0.0:8443 → http://127.0.0.1:8094
#   FORSTICK2_TLS_PORT=9443 ./scripts/run_web_tls.sh
#   FORSTICK2_PORT=8092 ./scripts/run_web_tls.sh   뒤쪽 서버 포트를 바꾼다
#
# **왜 필요한가** — 브라우저는 localhost가 아닌 평문 HTTP 출처에 마이크를 주지
# 않는다(보안 컨텍스트가 아니다). 사설 IP로 들어와 STT를 쓰려면 HTTPS가 필요하다.
#
# 인증서는 **자체 서명**이다. 처음 들어가면 브라우저가 경고를 띄운다 — 한 번
# 넘기면 그 출처는 보안 컨텍스트가 되고 마이크가 열린다.
#
# 인증서는 저장소 밖(`~/.config/forstick2/tls`)에 둔다. 개인 키를 프로젝트
# 디렉터리에 두지 않는다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_PORT="${FORSTICK2_TLS_PORT:-8443}"
TARGET_PORT="${FORSTICK2_PORT:-8094}"
CERT_DIR="${FORSTICK2_TLS_DIR:-$HOME/.config/forstick2/tls}"
CERT="$CERT_DIR/dev.crt"
KEY="$CERT_DIR/dev.key"

# 이 기기가 가진 주소를 모두 인증서에 담는다 — 주소마다 경고가 달라지지 않게.
WSL_DEV="$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)"
WSL_IP="$(ip -4 -o addr show dev "${WSL_DEV:-eth0}" scope global 2>/dev/null \
  | awk '{print $4}' | cut -d/ -f1 | head -1)"
GATEWAY="$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)"
WIN_IP=""
if [[ -x /mnt/c/Windows/System32/ipconfig.exe ]]; then
  WIN_IP="$(/mnt/c/Windows/System32/ipconfig.exe 2>/dev/null | tr -d '\r' \
    | awk '/IPv4/{print $NF}' | grep -v "^${GATEWAY}$" | head -1)"
fi

SAN="DNS:localhost,IP:127.0.0.1"
[[ -n "$WSL_IP" ]] && SAN="$SAN,IP:$WSL_IP"
[[ -n "$WIN_IP" ]] && SAN="$SAN,IP:$WIN_IP"

if [[ ! -f "$CERT" || ! -f "$KEY" ]]; then
  mkdir -p "$CERT_DIR"
  echo "[tls] 자체 서명 인증서를 만든다: $CERT"
  echo "[tls] 대상 주소: $SAN"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=forstick2-dev" -addext "subjectAltName=$SAN" 2>/dev/null
  chmod 600 "$KEY"
else
  echo "[tls] 기존 인증서를 쓴다: $CERT"
  echo "[tls] 담긴 주소: $(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
    | tail -1 | sed 's/^ *//')"
  echo "[tls] 주소가 바뀌었으면 이 파일을 지우고 다시 실행한다."
fi

if ! ss -tln 2>/dev/null | grep -q ":${TARGET_PORT} "; then
  echo "[tls] 뒤쪽 서버(${TARGET_PORT})가 떠 있지 않다. 먼저 띄운다:" >&2
  echo "      ./scripts/run_web_lan.sh" >&2
  exit 3
fi

echo "[tls] https://${WIN_IP:-127.0.0.1}:${TLS_PORT}/   ← 마이크가 열리는 주소"
echo "[tls] 평문 http://localhost:${TARGET_PORT}/ 은 그대로 쓸 수 있다."
if [[ -n "$WSL_IP" ]]; then
  cat <<EOF

[tls] Windows 사설 IP로 들어오려면 관리자 PowerShell에서 한 번:

  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=${TLS_PORT} \\
    connectaddress=${WSL_IP} connectport=${TLS_PORT}
  New-NetFirewallRule -DisplayName "forstick2 web ${TLS_PORT}" -Direction Inbound \\
    -Action Allow -Protocol TCP -LocalPort ${TLS_PORT}

EOF
fi

exec "$ROOT/.venv/bin/python" "$ROOT/scripts/tls_proxy.py" \
  --listen-port "$TLS_PORT" --target-port "$TARGET_PORT" --cert "$CERT" --key "$KEY"
