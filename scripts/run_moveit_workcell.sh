#!/usr/bin/env bash
# 작업 셀 MoveIt2 실행 (8-08 우선순위 3·4).
#
#   ./scripts/run_moveit_workcell.sh
#
# 전제: run_gazebo_fr3_2f85_workcell_gui.sh로 Gazebo·컨트롤러가 돌고 있다.
# 격리: partition forstick2_fr3_workcell · domain 44.
# arm-only(42)와 단순 조립 셀(43)의 move_group은 건드리지 않는다 — 우리가 띄운
# 것만 pid 파일로 관리한다.
#
# 자기충돌 행렬은 collisions_updater로 만들고, 근거 없는 제외를 걷어낸다.
# **--default/--always를 주지 않는다.**
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
MOVEIT_DIR="$ROOT/config/moveit"
LOG_DIR="${FORSTICK2_WORKCELL_LOG_DIR:-/tmp/forstick2_workcell}"
OVERLAY="/tmp/forstick2_gazebo/overlay"
URDF_DIR="$LOG_DIR/workcell"
MOVEIT_URDF="$URDF_DIR/fr3wms_with_2f85.moveit.urdf"
BASE_SRDF="$MOVEIT_DIR/fr3_2f85_workcell.base.srdf"
SRDF_OUT="$URDF_DIR/fr3_2f85_workcell.srdf"
PARAMS="$URDF_DIR/move_group_params.yaml"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
REGEN_SRDF="${FORSTICK2_MOVEIT_REGEN_SRDF:-0}"

say() { printf '[moveit-wc] %s\n' "$*"; }
fail() { printf '[moveit-wc] %s\n' "$*" >&2; }

if [[ ! -f "$MOVEIT_URDF" ]]; then
  fail "작업 셀 MoveIt URDF가 없다: $MOVEIT_URDF"
  fail "먼저 실행한다: FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh"
  exit 3
fi

source /opt/ros/lyrical/setup.bash
export AMENT_PREFIX_PATH="$OVERLAY:${AMENT_PREFIX_PATH:-}"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO:$OVERLAY/share"

# 1) 검증된 자세에서 SRDF를 다시 생성한다(손으로 옮겨 적지 않는다).
python3 "$ROOT/scripts/build_workcell_srdf.py" | sed 's/^/  /'

# 2) 자기충돌 행렬
if [[ "$REGEN_SRDF" == "1" || ! -f "$SRDF_OUT" ]]; then
  ros2 run moveit_setup_assistant collisions_updater \
    --urdf "$MOVEIT_URDF" --srdf "$BASE_SRDF" \
    --output "$SRDF_OUT" --trials 100000 --min-collision-fraction 0.95 --verbose \
    > "$LOG_DIR/collisions_updater.log" 2>&1 || true
  if [[ ! -s "$SRDF_OUT" ]]; then
    fail "SRDF 생성 실패 — $LOG_DIR/collisions_updater.log"
    exit 4
  fi
  say "생성 직후: $(grep -c disable_collisions "$SRDF_OUT") 쌍 비활성"
  python3 "$ROOT/scripts/prune_self_collision_srdf.py" "$SRDF_OUT" \
    "$ROOT/reports/moveit/self_collision_review.json" \
    "$ROOT/reports/gripper/self_collision_review.json" \
    > "$LOG_DIR/srdf_prune.json"
  say "검토 후: $(grep -c disable_collisions "$SRDF_OUT") 쌍 비활성"
  # collisions_updater는 기본 SRDF의 제외를 버린다(실측). 기구학적 루프 폐쇄
  # 쌍을 근거 파일에서 다시 넣는다.
  python3 "$ROOT/scripts/inject_loop_closure_srdf.py" "$SRDF_OUT" \
    > "$LOG_DIR/srdf_loop_closure.json"
  say "루프 폐쇄 주입 후: $(grep -c disable_collisions "$SRDF_OUT") 쌍 비활성"
else
  say "기존 SRDF 사용: $SRDF_OUT (재생성은 FORSTICK2_MOVEIT_REGEN_SRDF=1)"
fi

# 3) 파라미터 조립 후 move_group 실행
python3 "$ROOT/scripts/build_moveit_params.py" \
  "$MOVEIT_URDF" "$SRDF_OUT" "$MOVEIT_DIR" "$PARAMS"

PIDFILE="$LOG_DIR/move_group.pid"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  say "이미 실행 중: pid=$(cat "$PIDFILE") — 재시작하지 않는다"
  say "종료가 필요하면 kill \$(cat $PIDFILE)"
else
  nohup ros2 run moveit_ros_move_group move_group \
    --ros-args --params-file "$PARAMS" \
    > "$LOG_DIR/move_group.log" 2>&1 &
  echo $! > "$PIDFILE"
  say "move_group pid=$! log=$LOG_DIR/move_group.log"
fi

# 4) 작업 셀 환경 물체를 planning scene에 등록한다.
say "planning scene에 작업 셀 물체를 등록한다 (최대 90초 대기)"
python3 "$ROOT/scripts/publish_workcell_scene.py" | sed 's/^/  /'

say "domain=$ROS_DOMAIN_ID partition=$GZ_PARTITION"
