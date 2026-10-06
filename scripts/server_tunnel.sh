#!/usr/bin/env bash
# PC1 웹 서버를 외부 서버(도메인 입구)로 내보내는 SSH 역방향 터널.
#
#   ./scripts/server_tunnel.sh            터널을 띄우고 감시한다(끊기면 다시 잇는다, 포그라운드)
#   ./scripts/server_tunnel.sh --check    한 번만 점검: PC1 앱 · 서버 쪽 터널 끝 · 공개 주소
#   ./scripts/server_tunnel.sh --stop     이 스크립트가 띄운 감시 루프와 ssh를 끈다
#
# 구조: 브라우저 → https://도메인 → 서버 앞단 → 서버 127.0.0.1:<원격 포트>
#        └─(이 ssh -R)→ PC1 127.0.0.1:<FORSTICK2_PORT>
# 서버는 사내망이 달라 PC1에 먼저 닿을 수 없다. PC1이 서버로 나가는 SSH 연결을
# 거꾸로 쓴다. **서버에는 아무것도 설치·업로드하지 않는다** — 앱·ROS·Gazebo·STT는
# PC1에만 있고, 서버는 HTTPS 앞단과 sshd(+ 점검용 curl)만 쓴다. 서버 Python 버전과 무관하다.
#
# 지키는 것:
# - 포워딩 실패를 성공으로 보지 않는다. `ExitOnForwardFailure=yes` — 원격 포트가
#   이미 쓰이고 있거나(기존 서버 앱) sshd가 포워딩을 막으면 ssh가 바로 죽고 이유를 남긴다.
# - 연결 끊김을 감지한다. `ServerAliveInterval`×`ServerAliveCountMax` 동안 서버가
#   답하지 않으면 ssh가 끝나고, 감시 루프가 다시 잇는다.
# - "ssh가 살아 있다"를 "터널이 된다"로 치지 않는다. 같은 연결(ControlMaster)로 서버에서
#   127.0.0.1:<원격 포트>/v1/config를 받아, **PC1 앱과 같은 robot_id**가 오는지 본다.
#   기존 서버 앱(fake_dev)이 그 포트를 쥐고 있으면 여기서 드러난다.
# - 호스트 키를 자동으로 받아들이지 않는다(StrictHostKeyChecking=yes). 처음 한 번은
#   사람이 `ssh`로 접속해 지문을 확인해 known_hosts에 넣는다.
# - 원격 포트에 기본값을 두지 않는다. 도메인이 어느 포트로 오는지는 서버에서 확인한 값만 쓴다.
#
# 서버 authorized_keys(터널 전용 키 한 줄) — 셸·PTY·에이전트·X11·-L 포워딩을 막고,
# -R은 허용한 포트만, 명령은 `check <포트>` 선택자만 받는다:
#   restrict,port-forwarding,permitopen="none",
#   permitlisten="127.0.0.1:<운영 포트>",permitlisten="127.0.0.1:<시험 포트>",
#   command="case $SSH_ORIGINAL_COMMAND in 'check <운영 포트>') exec curl -s -m 5
#     http://127.0.0.1:<운영 포트>/v1/config;; ... *) echo denied >&2; exit 1;; esac"
#   ssh-ed25519 <공개키> <주석>
# (실제 줄은 md/exec-plans/2026-10-06-server-tunnel.md)
#
# 설정(환경변수 → config/local.env 순):
#   FORSTICK2_TUNNEL_TARGET       계정@서버          (필수)
#   FORSTICK2_TUNNEL_REMOTE_PORT  서버 쪽 포트       (필수, 서버에서 확인한 도메인 백엔드 포트)
#   FORSTICK2_TUNNEL_KEY          개인 키 경로       (선택, 없으면 ssh 기본 키)
#   FORSTICK2_TUNNEL_SSH_PORT     서버 sshd 포트     (기본 22)
#   FORSTICK2_TUNNEL_PUBLIC_URL   공개 주소          (선택, 예: https://foursticks.xos.kr)
#   FORSTICK2_TUNNEL_KNOWN_HOSTS  호스트 키 파일     (선택, 없으면 ~/.ssh/known_hosts)
#   FORSTICK2_PORT                PC1 웹 포트        (기본 8092 — run_web_workcell.sh와 같다)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/config/local.env" ]]; then
  set -a; source "$ROOT/config/local.env"; set +a
fi

TARGET="${FORSTICK2_TUNNEL_TARGET:-}"
REMOTE_PORT="${FORSTICK2_TUNNEL_REMOTE_PORT:-}"
KEY="${FORSTICK2_TUNNEL_KEY:-}"
SSH_PORT="${FORSTICK2_TUNNEL_SSH_PORT:-22}"
PUBLIC_URL="${FORSTICK2_TUNNEL_PUBLIC_URL:-}"
LOCAL_PORT="${FORSTICK2_PORT:-8092}"
STATE="${FORSTICK2_TUNNEL_DIR:-/tmp/forstick2_tunnel}"
# 15초 × 3회 = 약 45초 무응답이면 끊김으로 본다. 브라우저 WebSocket 재접속보다 짧게 잡는다.
ALIVE_SEC="${FORSTICK2_TUNNEL_ALIVE_SEC:-15}"
ALIVE_MAX="${FORSTICK2_TUNNEL_ALIVE_MAX:-3}"
# 서버 쪽 끝 점검 주기·연속 실패 허용. 3회(약 90초) 연속 실패면 연결을 새로 맺는다.
CHECK_SEC="${FORSTICK2_TUNNEL_CHECK_SEC:-30}"
FAIL_LIMIT="${FORSTICK2_TUNNEL_FAIL_LIMIT:-3}"
# 서버 쪽 점검 한 번의 상한. 원격 curl 제한 5초 + ssh 채널 여유.
REMOTE_CHECK_SEC=15
# 재접속 대기: 5초에서 두 배씩, 최대 60초. 서버를 두드리지 않으면서 복구는 1분 안에.
BACKOFF_MIN=5
BACKOFF_MAX=60

mkdir -p "$STATE"
LOG="$STATE/tunnel.log"
# 제어 소켓은 짧은 경로에 둔다 — 유닉스 소켓 경로는 약 108자 제한이라 상태 폴더가
# 길면 ssh가 "too long for Unix domain socket"으로 죽는다(실측). 폴더마다 다른 이름.
CTL="/tmp/f2tun-$(printf '%s' "$STATE" | md5sum | cut -c1-8).sock"
# bash는 포그라운드 sleep이 끝날 때까지 trap을 미룬다 — 그 사이 ssh가 원격 포트를 쥐고
# 남아 다음 실행이 "포워딩 실패"로 막혔다(실측). 백그라운드 sleep + wait로 바로 깨운다.
nap() { sleep "$1" & wait $! || true; }
say() { printf '%s [tunnel] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG" >&2; }

stop_all() {
  if [[ -f "$STATE/loop.pid" ]] && kill -0 "$(cat "$STATE/loop.pid")" 2>/dev/null; then
    kill "$(cat "$STATE/loop.pid")" 2>/dev/null || true
  fi
  if [[ -f "$STATE/ssh.pid" ]] && kill -0 "$(cat "$STATE/ssh.pid")" 2>/dev/null; then
    kill "$(cat "$STATE/ssh.pid")" 2>/dev/null || true
  fi
  # ssh가 실제로 끝나야 서버 쪽 포트가 풀린다 — 복구 절차가 바로 기존 앱을 띄울 수 있게 기다린다.
  local i
  for i in $(seq 1 10); do
    [[ -f "$STATE/ssh.pid" ]] && kill -0 "$(cat "$STATE/ssh.pid")" 2>/dev/null || break
    sleep 1
  done
  rm -f "$STATE/loop.pid" "$STATE/ssh.pid" "$CTL"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_all; say "중지했다"; exit 0
fi

missing=()
[[ -z "$TARGET" ]] && missing+=("FORSTICK2_TUNNEL_TARGET")
[[ -z "$REMOTE_PORT" ]] && missing+=("FORSTICK2_TUNNEL_REMOTE_PORT")
if (( ${#missing[@]} )); then
  say "설정이 없다: ${missing[*]} — config/local.env에 적는다(값을 추정하지 않는다)"
  exit 2
fi
[[ -n "$KEY" && ! -r "$KEY" ]] && { say "개인 키를 읽을 수 없다: $KEY"; exit 2; }

SSH_OPTS=(-p "$SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes
          -o ConnectTimeout=15 -o "ServerAliveInterval=$ALIVE_SEC"
          -o "ServerAliveCountMax=$ALIVE_MAX")
[[ -n "$KEY" ]] && SSH_OPTS+=(-i "$KEY" -o IdentitiesOnly=yes)
[[ -n "${FORSTICK2_TUNNEL_KNOWN_HOSTS:-}" ]] \
  && SSH_OPTS+=(-o "UserKnownHostsFile=$FORSTICK2_TUNNEL_KNOWN_HOSTS")

# PC1 앱이 내는 robot_id. 서버 쪽 끝에서 같은 값이 와야 터널이 PC1에 닿은 것이다.
local_robot() {
  curl -s -m 5 "http://127.0.0.1:$LOCAL_PORT/v1/config" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["robot"]["robot_id"])' 2>/dev/null
}

# 서버에서 터널 끝을 읽는다. 셸 명령을 보내지 않는다 — 터널 전용 키는 authorized_keys의
# 강제 명령(command=)으로 `check <포트>` 선택자만 받아 고정된 curl 하나를 실행한다.
# 키가 새도 서버에서 임의 명령을 돌릴 수 없게 하기 위해서다(아래 AUTHORIZED_KEYS 참고).
remote_robot() {
  # 서버가 멈추면 이 명령도 멈춘다 — 시간 제한이 없으면 감시 루프 전체가 서 버린다(실측).
  timeout "$REMOTE_CHECK_SEC" ssh "${SSH_OPTS[@]}" -S "$CTL" -o ControlMaster=no "$TARGET" \
    "check $REMOTE_PORT" 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["robot"]["robot_id"])' 2>/dev/null
}

public_robot() {
  [[ -z "$PUBLIC_URL" ]] && return 0
  curl -s -m 10 "${PUBLIC_URL%/}/v1/config" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["robot"]["robot_id"])' 2>/dev/null
}

check_once() {
  local want got pub rc=0
  want="$(local_robot || true)"
  if [[ -z "$want" ]]; then say "PC1 앱 응답 없음: 127.0.0.1:$LOCAL_PORT"; return 1; fi
  got="$(remote_robot || true)"
  if [[ "$got" == "$want" ]]; then
    say "서버 쪽 끝 OK: 127.0.0.1:$REMOTE_PORT → PC1 (robot_id=$got)"
  else
    say "서버 쪽 끝 불일치: 기대 $want, 받은 값 '${got:-응답 없음}'"; rc=1
  fi
  if [[ -n "$PUBLIC_URL" ]]; then
    pub="$(public_robot || true)"
    if [[ "$pub" == "$want" ]]; then say "공개 주소 OK: $PUBLIC_URL (robot_id=$pub)"
    else say "공개 주소 불일치: '${pub:-응답 없음}' — 서버 앞단이 다른 포트로 보내는지 확인"; fi
  fi
  return $rc
}

start_ssh() {
  rm -f "$CTL"
  : > "$STATE/ssh.err"
  ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes \
      -o ControlMaster=yes -o "ControlPath=$CTL" \
      -R "127.0.0.1:$REMOTE_PORT:127.0.0.1:$LOCAL_PORT" "$TARGET" \
      2>>"$STATE/ssh.err" &
  echo $! > "$STATE/ssh.pid"
  # ExitOnForwardFailure는 포워딩 요청 응답을 받은 뒤 죽는다. 제어 소켓이 생기고
  # 프로세스가 살아 있으면 포워딩이 받아들여진 것이다.
  local i
  for i in $(seq 1 20); do
    kill -0 "$(cat "$STATE/ssh.pid")" 2>/dev/null || break
    [[ -S "$CTL" ]] && timeout 5 ssh -S "$CTL" -O check "$TARGET" 2>/dev/null && return 0
    sleep 1
  done
  wait "$(cat "$STATE/ssh.pid")" 2>/dev/null || true
  if grep -qi "remote port forwarding failed" "$STATE/ssh.err"; then
    say "포워딩 실패: 서버 127.0.0.1:$REMOTE_PORT 를 쓸 수 없다(기존 앱이 쓰고 있거나 sshd가 막음)"
  else
    say "ssh 연결 실패: $(tail -2 "$STATE/ssh.err" | tr '\n' ' ')"
  fi
  return 1
}

if [[ "${1:-}" == "--check" ]]; then
  if [[ -S "$CTL" ]] && timeout 5 ssh -S "$CTL" -O check "$TARGET" 2>/dev/null; then
    check_once; exit $?
  fi
  say "실행 중인 터널이 없다(제어 소켓 없음) — PC1 앱만 본다"
  [[ -n "$(local_robot || true)" ]] && say "PC1 앱 OK: 127.0.0.1:$LOCAL_PORT ($(local_robot))"
  exit 1
fi

if [[ -f "$STATE/loop.pid" ]] && kill -0 "$(cat "$STATE/loop.pid")" 2>/dev/null; then
  say "이미 실행 중: pid=$(cat "$STATE/loop.pid") (끄기: $0 --stop)"; exit 1
fi
echo $$ > "$STATE/loop.pid"
# 자기 ssh와 자기 pid 파일만 정리한다 — 다른 감시 루프를 같이 끄지 않는다.
cleanup_self() {
  if [[ -f "$STATE/ssh.pid" ]]; then kill "$(cat "$STATE/ssh.pid")" 2>/dev/null || true; fi
  if [[ "$(cat "$STATE/loop.pid" 2>/dev/null)" == "$$" ]]; then
    rm -f "$STATE/loop.pid" "$STATE/ssh.pid" "$CTL"
  fi
}
trap 'cleanup_self; say "종료"; exit 0' INT TERM

say "시작: $TARGET 127.0.0.1:$REMOTE_PORT → PC1 127.0.0.1:$LOCAL_PORT"
backoff=$BACKOFF_MIN
while true; do
  if [[ -z "$(local_robot || true)" ]]; then
    say "PC1 앱이 아직 없다(127.0.0.1:$LOCAL_PORT) — ${backoff}s 뒤 다시"
    nap "$backoff"; backoff=$(( backoff * 2 > BACKOFF_MAX ? BACKOFF_MAX : backoff * 2 )); continue
  fi
  if ! start_ssh; then
    nap "$backoff"; backoff=$(( backoff * 2 > BACKOFF_MAX ? BACKOFF_MAX : backoff * 2 )); continue
  fi
  say "터널 연결됨 (ssh pid=$(cat "$STATE/ssh.pid"))"
  backoff=$BACKOFF_MIN
  fails=0
  check_once || fails=1
  while kill -0 "$(cat "$STATE/ssh.pid")" 2>/dev/null; do
    nap "$CHECK_SEC"
    kill -0 "$(cat "$STATE/ssh.pid")" 2>/dev/null || break
    if check_once >/dev/null 2>&1; then fails=0
    else
      fails=$(( fails + 1 ))
      say "점검 실패 ${fails}/${FAIL_LIMIT}"
      if (( fails >= FAIL_LIMIT )); then
        say "연속 실패 — 연결을 새로 맺는다"
        kill "$(cat "$STATE/ssh.pid")" 2>/dev/null || true
        break
      fi
    fi
  done
  wait "$(cat "$STATE/ssh.pid")" 2>/dev/null || true
  say "연결 끊김 감지: $(tail -1 "$STATE/ssh.err" 2>/dev/null) — ${backoff}s 뒤 재접속"
  nap "$backoff"
done
