#!/usr/bin/env bash
# FR3 작업 셀 끄기: 웹 서버 → MoveIt·브리지·rsp(ROS 노드는 SIGINT로 정상 종료를 먼저) → Gazebo.
#
#   ./scripts/fr3_down.sh
#
# ROS 노드를 곧바로 강제 종료하지 않는다 — Fast DDS 공유 메모리가 남아 다음 실행이 데이터를 못 받는 일이 있었다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
LOG="$FORSTICK2_WORKCELL_LOG_DIR"
say() { printf '[fr3-down] %s\n' "$*"; }
wait_gone() { for _ in $(seq 1 "$2"); do kill -0 "$1" 2>/dev/null || return 0; sleep 1; done; return 1; }

if [[ -f "$LOG/web.pid" ]]; then
  pid="$(cat "$LOG/web.pid")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"; wait_gone "$pid" 30 || { kill -INT "$pid" 2>/dev/null; wait_gone "$pid" 15; }
    kill -0 "$pid" 2>/dev/null && say "웹 서버 pid=$pid 가 아직 살아 있다 — 강제 종료하지 않는다(확인 후 직접 정리)" \
      || say "웹 서버 정지"
  fi
  rm -f "$LOG/web.pid"
fi
for name in scene_bridge move_group rsp; do
  f="$LOG/$name.pid"
  [[ -f "$f" ]] || continue
  pid="$(cat "$f")"
  kill -0 "$pid" 2>/dev/null && { kill -INT "$pid"; wait_gone "$pid" 15 && say "$name 정상 종료"; }
done
"$ROOT/scripts/stop_gazebo_workcell.sh"
