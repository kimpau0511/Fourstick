#!/usr/bin/env bash
# Gazebo pick/place **시뮬레이션 E2E** 시연 (8-11).
#
#   FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh pallet_1 mat_a
#   FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh \
#       pallet_1 mat_a --stop-at place_approach
#
# **시뮬레이터 데모 검증이다.** 실제 로봇 pick/place 가능 판정이 아니다.
# 물체는 시뮬레이션 고정 장치로 움직인다 — 마찰 파지·물체 감지가 아니다.
#
# 전제: 작업 셀 Gazebo와 MoveIt이 돌고 있다.
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh
#   ./scripts/run_moveit_workcell.sh
#
# 일반 웹 UI·API의 pick/place는 이 스크립트와 무관하게 계속
# capability.profile_incomplete로 막혀 있다.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${FORSTICK2_SIM_PICK_PLACE_DEMO:-}" != "1" ]]; then
  echo "[sim-e2e] 거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다." >&2
  echo "[sim-e2e] 이 시연은 시뮬레이터 전용이다. 실제 pick/place 활성화가 아니다." >&2
  exit 3
fi

if [[ -z "${AMENT_PREFIX_PATH:-}" ]]; then
  # shellcheck disable=SC1091
  source /opt/ros/lyrical/setup.bash
fi
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"

echo "[sim-e2e] 시뮬레이터 이송 시연 · domain=$ROS_DOMAIN_ID partition=$GZ_PARTITION"
echo "[sim-e2e] **실하드웨어가 아니다.** real_hardware_ready는 항상 false다."
exec python3 "$ROOT/scripts/demo_workcell_pick_place.py" "$@"
