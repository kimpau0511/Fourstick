#!/usr/bin/env bash
# 조립 Gazebo 인스턴스만 정지한다 (md/개발플랜.md 8-08).
#
#   ./scripts/stop_gazebo_fr3_2f85.sh              # 서버 + GUI + rsp
#   ./scripts/stop_gazebo_fr3_2f85.sh --gui-only   # GUI 창만
#
# **이 프로젝트 인스턴스만 건드린다.** pid 파일을 기준으로 종료하고, pid 파일이
# 없는 잔여 GUI 창은 목록만 보여 준다(다른 프로젝트의 Gazebo일 수 있다).
set -eo pipefail

LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
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
    for _ in $(seq 1 10); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    echo "[stop] $name pid=$pid 정지"
  else
    echo "[stop] $name pid=$pid 이미 종료됨"
  fi
  rm -f "$file"
}

stop_one gz_gui_gripper
if [[ "$GUI_ONLY" -eq 0 ]]; then
  stop_one rsp_gripper
  stop_one gz_sim_gripper
else
  echo "[stop] --gui-only: 서버와 robot_state_publisher는 그대로 둔다."
fi

# pid 파일에 없는 잔여 GUI 창: **우리 파티션의 것만** 정리한다.
# 다른 프로젝트의 Gazebo는 파티션이 달라 여기서 걸러진다.
OURS="${GZ_PARTITION:-forstick2_fr3_gripper}"
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
