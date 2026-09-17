#!/usr/bin/env bash
# GUI 시연 실행 (md/개발플랜.md 8-08 우선순위 4).
#
#   ./scripts/demo_fr3_2f85_gui.sh
#
# run_gazebo_fr3_2f85_gui.sh로 띄운 인스턴스에 붙어 6단계를 실행한다.
# 인스턴스를 띄우거나 종료하지 않는다 — 없으면 그 사실만 알린다.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_gripper}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-43}"

source /opt/ros/lyrical/setup.bash

# gz service -l은 응답이 느릴 때가 있다(20초로는 부족했다 — 실측).
if ! timeout 60 gz service -l 2>/dev/null | grep -q "/world/forstick2_fr3_cell/"; then
  echo "[demo] 조립 Gazebo가 실행 중이 아니다. 먼저 띄운다:" >&2
  echo "    FORSTICK2_ASSEMBLY_YAW_RAD=<선언값> ./scripts/run_gazebo_fr3_2f85_gui.sh" >&2
  exit 2
fi
if [[ ! -f "$LOG_DIR/urdf/fr3wms_with_2f85.urdf" ]]; then
  echo "[demo] 조립 URDF가 없다: $LOG_DIR/fr3wms_with_2f85.urdf" >&2
  exit 3
fi

echo "[demo] Gazebo 창을 보면서 진행한다 — 6단계, 약 1분 30초"
exec python3 "$ROOT/scripts/demo_fr3_2f85_gui.py" "$@"
