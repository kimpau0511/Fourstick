#!/usr/bin/env bash
# 같은 네트워크의 다른 기기(휴대폰 등)에서 웹 UI에 접속할 수 있게 띄운다.
#
#   ./scripts/run_web_lan.sh                       0.0.0.0:8094 (작업 셀 서버)
#   FORSTICK2_PORT=8092 ./scripts/run_web_lan.sh
#   FORSTICK2_LAN_LAUNCHER=scripts/run_web.sh ./scripts/run_web_lan.sh
#
# **이 콘솔에는 인증이 없다**(server/api.py: "localhost 전용이며 사용자 계정이
# 없다"). 0.0.0.0에 열면 같은 네트워크의 누구나 시뮬레이션 작업을 시작시킬 수
# 있다. 믿을 수 있는 사설망에서만 쓴다.
#
# 기본 launcher는 작업 셀 서버다 — 8094에서 돌던 것과 같은 구성이다.
# 바인딩만 0.0.0.0으로 바꾸므로 localhost 접속은 그대로 된다.
#
# WSL2는 NAT 뒤에 있다. 여기서 0.0.0.0에 바인딩하는 것만으로는 휴대폰이 닿지
# 않는다 — Windows 쪽에 portproxy와 방화벽 인바운드 규칙이 필요하다. 필요한
# 명령을 아래에 그대로 찍어 준다(관리자 PowerShell에서 실행한다).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export FORSTICK2_HOST=0.0.0.0
PORT="${FORSTICK2_PORT:-8094}"
export FORSTICK2_PORT="$PORT"
LAUNCHER="${FORSTICK2_LAN_LAUNCHER:-scripts/run_web_workcell.sh}"

# 이 WSL 인스턴스의 주소. 재부팅하면 바뀐다 — portproxy도 그때 다시 잡는다.
# **기본 경로가 나가는 장치**에서 읽는다. lo에도 전역 주소(10.255.255.254)가
# 붙어 있어서 그냥 첫 줄을 집으면 엉뚱한 값이 나온다(실측).
WSL_DEV="$(ip route show default 2>/dev/null | awk '{print $5}' | head -1)"
WSL_IP="$(ip -4 -o addr show dev "${WSL_DEV:-eth0}" scope global 2>/dev/null \
  | awk '{print $4}' | cut -d/ -f1 | head -1)"
# Windows 쪽 사설 IP. WSL 가상 어댑터(기본 경로의 게이트웨이)는 뺀다.
GATEWAY="$(ip route show default 2>/dev/null | awk '{print $3}' | head -1)"
WIN_IP=""
if [[ -x /mnt/c/Windows/System32/ipconfig.exe ]]; then
  WIN_IP="$(/mnt/c/Windows/System32/ipconfig.exe 2>/dev/null | tr -d '\r' \
    | awk '/IPv4/{print $NF}' | grep -v "^${GATEWAY}$" | head -1)"
fi

echo "[web-lan] 바인딩: 0.0.0.0:${PORT}  (localhost 접속도 그대로 된다)"
echo "[web-lan] WSL 주소   : ${WSL_IP:-알 수 없음}"
echo "[web-lan] Windows 주소: ${WIN_IP:-알 수 없음}"
if [[ -n "$WIN_IP" ]]; then
  echo "[web-lan] 휴대폰 접속 주소: http://${WIN_IP}:${PORT}/"
fi
echo "[web-lan] **인증이 없다.** 믿을 수 있는 사설망에서만 쓴다."
if [[ -n "$WSL_IP" ]]; then
  cat <<EOF

[web-lan] WSL2는 NAT 뒤에 있다. 관리자 PowerShell에서 한 번 실행한다:

  netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=${PORT} \\
    connectaddress=${WSL_IP} connectport=${PORT}
  New-NetFirewallRule -DisplayName "forstick2 web ${PORT}" -Direction Inbound \\
    -Action Allow -Protocol TCP -LocalPort ${PORT}

  확인:  netsh interface portproxy show v4tov4
  해제:  netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=${PORT}
         Remove-NetFirewallRule -DisplayName "forstick2 web ${PORT}"

  WSL을 다시 시작하면 WSL 주소가 바뀐다 — 그때 portproxy를 다시 잡는다.

EOF
fi

exec "$ROOT/$LAUNCHER" "$@"
