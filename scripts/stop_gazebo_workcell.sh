#!/usr/bin/env bash
# 작업 셀 Gazebo 인스턴스만 정지한다 (8-08).
#
#   ./scripts/stop_gazebo_workcell.sh              # 서버 + GUI + rsp + move_group
#   ./scripts/stop_gazebo_workcell.sh --gui-only   # GUI 창만
#
# pid 파일을 기준으로 종료한다. pid 파일에 없는 잔여 GUI 창은 **우리 파티션의
# 것만** 정리한다 — 다른 프로젝트(forstick 8090 데모, arm-only, 단순 조립 셀)의
# Gazebo는 파티션이 달라 여기서 걸러진다.
set -eo pipefail

LOG_DIR="${FORSTICK2_WORKCELL_LOG_DIR:-/tmp/forstick2_workcell}"
OURS="${GZ_PARTITION:-forstick2_fr3_workcell}"
GUI_ONLY=0
[[ "${1:-}" == "--gui-only" ]] && GUI_ONLY=1

stop_one() {
  local name="$1" file="$LOG_DIR/$1.pid"
  if [[ ! -f "$file" ]]; then
    echo "[stop] $name: pid 파일이 없다"
    return
  fi
  local pid; pid="$(cat "$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    echo "[stop] $name pid=$pid 정지"
  else
    echo "[stop] $name pid=$pid 이미 종료됨"
  fi
  rm -f "$file"
}

stop_one gz_gui
if [[ "$GUI_ONLY" -eq 0 ]]; then
  stop_one scene_bridge
  stop_one move_group
  stop_one rsp
  stop_one gz_server
else
  echo "[stop] --gui-only: 서버·rsp·move_group은 그대로 둔다."
fi

for pid in $(ps -eo pid,args | grep -F "gz-sim-gui-client" \
             | grep -v "sh -c" | grep -v grep | awk '{print $1}' || true); do
  partition="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
               | sed -n 's/^GZ_PARTITION=//p')"
  if [[ "$partition" == "$OURS" ]]; then
    kill -9 "$pid" 2>/dev/null && echo "[stop] 잔여 GUI pid=$pid 정리 (파티션 $OURS)"
  else
    echo "[stop] GUI pid=$pid 는 다른 파티션(${partition:-없음}) — **건드리지 않는다**"
  fi
done
