#!/usr/bin/env bash
# 텍스트 명령 시연 (8-09 우선 작업 3).
#
#   ./scripts/demo_workcell_commands.sh
#
# 전제:
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh
#   ./scripts/run_moveit_workcell.sh
#   FORSTICK2_PORT=8093 ./scripts/run_web_workcell.sh
#
# 반복 실행할 수 있다. 끝에 안전 home으로 돌아간다.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
BASE="${FORSTICK2_WEB_BASE:-http://127.0.0.1:8093}"

source /opt/ros/lyrical/setup.bash

if ! curl -sf "$BASE/health" >/dev/null 2>&1; then
  echo "[demo] 웹 서버가 없다: $BASE" >&2
  echo "[demo] 먼저 띄운다: FORSTICK2_PORT=8093 ./scripts/run_web_workcell.sh" >&2
  exit 2
fi
if ! timeout 60 gz service -l 2>/dev/null | grep -q "/world/forstick2_fr3_2f85_workcell/"; then
  echo "[demo] 작업 셀 Gazebo가 없다." >&2
  echo "[demo] 먼저 띄운다: FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh" >&2
  exit 3
fi

echo "[demo] Gazebo 창을 보면서 진행한다 — 6개 명령"
exec python3 "$ROOT/scripts/demo_workcell_commands.py" --base "$BASE" "$@"
