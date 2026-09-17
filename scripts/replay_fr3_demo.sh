#!/usr/bin/env bash
# FR3-WMS Gazebo 동작 재생 (8-07). ROS 환경을 준비한 뒤 재생 스크립트를 돌린다.
#
#   ./scripts/replay_fr3_demo.sh                 # camera → home → move → stop → (거부 단계)
#   ./scripts/replay_fr3_demo.sh home move       # 고른 단계만
#
# 전제: run_gazebo_fr3.sh, run_moveit_fr3.sh가 이미 돌고 있다.
# **서버를 종료하거나 재시작하지 않는다.**
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
source /opt/ros/lyrical/setup.bash
exec python3 "$ROOT/scripts/replay_fr3_demo.py" "$@"
