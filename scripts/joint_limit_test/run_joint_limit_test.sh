#!/usr/bin/env bash
# FR3-WMS 관절 한계 시험 — PC1에서 실행.
#
# 돌고 있는 작업 셀(파티션 forstick2_fr3_workcell, ROS 도메인 44)은 건드리지 않는다.
# 팔 단독 셀을 별도 파티션(forstick2_fr3)·도메인(42)으로 **화면 없이**(gz sim -s) 띄운다.
# 띄우는 방식은 scripts/run_gazebo_fr3.sh와 같다(같은 xacro·월드·컨트롤러 YAML·spawn·spawner).
# 다른 점: GUI 없음, robot_state_publisher 없음(시험에 불필요), 모든 프로세스 nice 19.
# 시험이 끝나면(실패·Ctrl+C 포함) 이 스크립트가 띄운 프로세스만 끈다.
#
# 사용: bash run_joint_limit_test.sh <forstick 저장소 경로>
# 외부 FAIRINO 저장소 경로가 기본값(/home/asd/external/frcobot_ros2)과 다르면 FORSTICK2_FR3_REPO로 준다.
set -eo pipefail

REPO="${1:?forstick 저장소 경로를 인자로 주세요}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
FR3_URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
WORLD="$REPO/config/gazebo/fr3_cell.sdf"
XACRO_FILE="$REPO/config/gazebo/fr3wms_arm.urdf.xacro"
CONTROLLERS="$REPO/config/gazebo/fr3wms_controllers.yaml"
WORLD_NAME=forstick2_fr3_cell
export GZ_PARTITION=forstick2_fr3 ROS_DOMAIN_ID=42
LOG="$(mktemp -d /tmp/forstick_limit_test.XXXX)"
PIDS=()

source /opt/ros/lyrical/setup.bash
for f in "$FR3_URDF" "$WORLD" "$XACRO_FILE" "$CONTROLLERS"; do
  [[ -f "$f" ]] || { echo "파일이 없다: $f" >&2; exit 3; }
done
echo "URDF sha256: $(sha256sum "$FR3_URDF" | cut -c1-12)  (프로젝트 기록: 73d01435d7be)"

# 같은 도메인·파티션에 이미 뭔가 떠 있으면 멈춘다(남의 시험을 끄지 않기 위해).
if [[ -n "$(ros2 node list --no-daemon 2>/dev/null)" ]] || [[ -n "$(timeout 5 gz topic -l 2>/dev/null)" ]]; then
  echo "도메인 42 / 파티션 forstick2_fr3에 이미 실행 중인 것이 있다 — 확인 후 다시 실행" >&2
  exit 4
fi

# 백그라운드로 띄우고 프로세스 그룹째 기록한다(gz sim은 자식 프로세스를 따로 띄운다).
start() { # 이름 명령...
  local name="$1"; shift
  setsid nice -n 19 "$@" > "$LOG/$name.log" 2>&1 &
  PIDS+=("$!")
  echo "[시작] $name pid=$!"
}

# 프로세스를 먼저 끄고 그다음 로그를 지운다.
# 성공하면 로그도 지우고(결과 표는 화면에 남는다), 실패하면 원인을 보도록 남긴다.
finish() {
  local rc=$?
  echo "[정리] 시험용 프로세스 종료"
  for pid in "${PIDS[@]}"; do kill -TERM -- "-$pid" 2>/dev/null || true; done
  sleep 3
  for pid in "${PIDS[@]}"; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  if [[ $rc -eq 0 ]]; then rm -rf "$LOG"; echo "[정리] 시험 로그 삭제함 — 위 표를 복사해 보내주세요"
  else echo "[정리] 실패(종료 코드 $rc) — 로그를 남겨 둠: $LOG"; fi
  exit $rc
}
trap finish EXIT

# 메시 경로 해석(run_gazebo_fr3.sh와 같다 — 자산을 복사하지 않고 링크만 둔다).
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO"
ln -sfn "$FR3_REPO/fairino_description/meshes" "$LOG/meshes"
URDF_OUT="$LOG/fr3wms_arm.urdf"
ros2 run xacro xacro "$XACRO_FILE" "fr3_urdf:=$FR3_URDF" "controller_yaml:=$CONTROLLERS" > "$URDF_OUT"

echo "[1/4] Gazebo 서버(화면 없음)"
start gz_sim gz sim -s -r -v 2 "$WORLD"
for _ in $(seq 60); do
  gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create" && break
  sleep 1
done
gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create" || { echo "Gazebo가 60초 안에 뜨지 않았다" >&2; exit 5; }

echo "[2/4] 팔 모델 생성"
nice -n 19 ros2 run ros_gz_sim create -world "$WORLD_NAME" -file "$URDF_OUT" \
  -name fr3wms -x 0 -y 0 -z 0 > "$LOG/spawn.log" 2>&1
tail -1 "$LOG/spawn.log"

echo "[3/4] 컨트롤러"
for controller in joint_state_broadcaster arm_trajectory_controller; do
  nice -n 19 ros2 run controller_manager spawner "$controller" \
    --controller-manager /controller_manager --controller-manager-timeout 60 \
    --param-file "$CONTROLLERS" > "$LOG/spawn_$controller.log" 2>&1
  echo "  $controller: $(tail -1 "$LOG/spawn_$controller.log")"
done

echo "[4/4] 관절 한계 시험"
nice -n 19 python3 "$HERE/joint_limit_probe.py" "$FR3_URDF"
