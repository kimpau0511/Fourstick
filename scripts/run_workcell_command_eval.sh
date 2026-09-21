#!/usr/bin/env bash
# 작업 셀 명령 평가 (8-13). ROS 환경을 소스해 기하 검사기(MoveIt)가 붙게 한다.
#
#   ./scripts/run_workcell_command_eval.sh                       실제 모델 + double, dev+sealed
#   ./scripts/run_workcell_command_eval.sh --provider double     double만
#   ./scripts/run_workcell_command_eval.sh --set dev --repeat 2  재현성
#
# 전제: 작업 셀 Gazebo·MoveIt이 돌고 있다(없으면 기하 검사가 ASK로 남고 그대로 기록된다).
#       실제 모델 결과를 원하면 vLLM 서버가 떠 있어야 한다. 없으면 double만 기록된다.
# **실행하지 않는다.** 계획·판정만 본다. 실제 로봇에 연결하지 않는다.
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export FORSTICK2_WORKCELL_ROBOT=1
export FORSTICK2_FAKE_ROBOT=0
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
if [[ -z "${AMENT_PREFIX_PATH:-}" ]] && [[ -f /opt/ros/lyrical/setup.bash ]]; then
  # shellcheck disable=SC1091
  source /opt/ros/lyrical/setup.bash
fi
if ! "$ROOT/.venv/bin/python" -c "import rclpy" 2>/dev/null; then
  echo "[eval] rclpy를 쓸 수 없다 — 기하 검사기가 붙지 않아 안전 판단이 ASK로 남는다." >&2
fi
exec "$ROOT/.venv/bin/python" -u "$ROOT/scripts/workcell_command_eval.py" "$@"
