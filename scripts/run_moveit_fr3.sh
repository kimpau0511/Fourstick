#!/usr/bin/env bash
# FR3-WMS arm-only MoveIt2 실행 (md/개발플랜.md 8-07).
#
# 전제: scripts/run_gazebo_fr3.sh로 Gazebo·컨트롤러가 이미 돌고 있다.
# **기존 world·xacro·controller 설정을 바꾸지 않는다.** 여기서는 MoveIt만 얹는다.
#
# - FAIRINO 자산은 외부 경로로만 참조한다. URDF·메시를 저장소에 복사하지 않는다.
# - 생성물(URDF/SRDF/파라미터)은 로그 디렉터리에만 둔다.
# - 자기충돌 행렬은 collisions_updater로 생성한다. **--default/--always를 주지
#   않는다** — 그 옵션은 실제로 충돌하는 쌍을 계획 성공을 위해 제외하는 것이다.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
FR3_URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
XACRO_FILE="$ROOT/config/gazebo/fr3wms_arm.urdf.xacro"
CONTROLLERS="$ROOT/config/gazebo/fr3wms_controllers.yaml"
MOVEIT_DIR="$ROOT/config/moveit"
LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"
REGEN_SRDF="${FORSTICK2_MOVEIT_REGEN_SRDF:-0}"

if [[ ! -f "$FR3_URDF" ]]; then
  echo "[run_moveit_fr3] 외부 FR3 URDF가 없다: $FR3_URDF" >&2
  echo "[run_moveit_fr3] asset.missing — 라이선스 미확인 자산은 저장소에 두지 않는다." >&2
  exit 3
fi

mkdir -p "$LOG_DIR"
source /opt/ros/lyrical/setup.bash
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO"

URDF_OUT="$LOG_DIR/fr3wms_arm.urdf"
MOVEIT_URDF="$LOG_DIR/fr3wms_arm.moveit.urdf"
SRDF_OUT="$LOG_DIR/fr3wms_arm.srdf"
PARAMS="$LOG_DIR/move_group_params.yaml"

# 1) Gazebo 검증과 같은 래퍼 xacro로 URDF를 만든다(같은 모델이어야 한다).
ros2 run xacro xacro "$XACRO_FILE" \
  "fr3_urdf:=$FR3_URDF" "controller_yaml:=$CONTROLLERS" > "$URDF_OUT"

# 2) MoveIt용 URDF: 공식 URDF의 메시 경로가 상대경로(../meshes/...)다.
#    geometric_shapes는 상대경로를 못 읽고 죽는다(실측: segfault).
#    **자산을 복사하지 않고** URI만 외부 절대경로로 바꾼 생성물을 만든다.
python3 - "$URDF_OUT" "$FR3_REPO/fairino_description" "$MOVEIT_URDF" <<'PYGEN'
import sys, pathlib
src, pkg, dst = sys.argv[1:4]
text = pathlib.Path(src).read_text(encoding="utf-8")
count = text.count('filename="../meshes/')
text = text.replace('filename="../meshes/', f'filename="file://{pkg}/meshes/')
pathlib.Path(dst).write_text(text, encoding="utf-8")
print(f"[run_moveit_fr3] 메시 URI 치환 {count}건 -> {dst}")
PYGEN

# 3) SRDF(자기충돌 행렬 포함). 이미 있으면 다시 만들지 않는다 —
#    검증에 쓴 행렬이 조용히 바뀌지 않게 한다.
if [[ "$REGEN_SRDF" == "1" || ! -f "$SRDF_OUT" ]]; then
  ros2 run moveit_setup_assistant collisions_updater \
    --urdf "$MOVEIT_URDF" --srdf "$MOVEIT_DIR/fr3wms_arm.base.srdf" \
    --output "$SRDF_OUT" --trials 100000 --min-collision-fraction 0.95 --verbose \
    > "$LOG_DIR/collisions_updater.log" 2>&1 || true
  if [[ ! -s "$SRDF_OUT" ]]; then
    echo "[run_moveit_fr3] SRDF 생성 실패 — $LOG_DIR/collisions_updater.log" >&2
    exit 4
  fi
  echo "[run_moveit_fr3] 생성 직후: $(grep -c disable_collisions "$SRDF_OUT") 쌍 비활성"
  # **근거 없는 제외를 걷어낸다.** collisions_updater가 "Never"로 비활성화한
  # 쌍 중 독립 검토가 근접을 찾은 쌍은 다시 검사 대상으로 되살린다
  # (FR3-WMS에서 실측: 15쌍 중 12쌍이 0.2 mm 안까지 접근한다).
  python3 "$ROOT/scripts/prune_self_collision_srdf.py" "$SRDF_OUT" \
    "$ROOT/reports/moveit/self_collision_review.json" \
    > "$LOG_DIR/srdf_prune.json"
  echo "[run_moveit_fr3] 검토 후: $(grep -c disable_collisions "$SRDF_OUT") 쌍 비활성"
else
  echo "[run_moveit_fr3] 기존 SRDF 사용: $SRDF_OUT (재생성은 FORSTICK2_MOVEIT_REGEN_SRDF=1)"
fi

# 4) 파라미터 조립 후 move_group 실행
python3 "$ROOT/scripts/build_moveit_params.py" \
  "$MOVEIT_URDF" "$SRDF_OUT" "$MOVEIT_DIR" "$PARAMS"

# **패턴으로 프로세스를 죽이지 않는다.** 같은 이름의 노드가 다른 프로젝트
# (forstick 8090 데모)에도 있다. 우리가 띄운 것만 pid 파일로 관리한다.
PIDFILE="$LOG_DIR/move_group.pid"
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "[run_moveit_fr3] 이미 실행 중: pid=$(cat "$PIDFILE") — 재시작하지 않는다"
  echo "[run_moveit_fr3] 종료가 필요하면 kill \$(cat $PIDFILE)"
  exit 0
fi
nohup ros2 run moveit_ros_move_group move_group \
  --ros-args --params-file "$PARAMS" \
  > "$LOG_DIR/move_group.log" 2>&1 &
echo $! > "$PIDFILE"
echo "[run_moveit_fr3] move_group pid=$! log=$LOG_DIR/move_group.log pidfile=$PIDFILE"
echo "[run_moveit_fr3] domain=$ROS_DOMAIN_ID partition=$GZ_PARTITION"
