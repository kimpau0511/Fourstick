#!/usr/bin/env bash
# FR3-WMS 관절 한계 시험 — PC1에서 실행.
#
# 돌고 있는 작업 셀(파티션 forstick2_fr3_workcell, ROS 도메인 44)은 건드리지 않는다.
# 팔 단독 셀을 별도 파티션(forstick2_fr3)·도메인(42)으로 **화면 없이**(gz sim -s) 띄운다.
# 띄우는 방식은 scripts/run_gazebo_fr3.sh와 같다(같은 xacro·월드·컨트롤러 YAML·spawn·spawner).
# 다른 점: GUI 없음, 모든 프로세스 nice 19.
# (robot_state_publisher는 필요하다 — gz_ros2_control 3.x가 robot_description 토픽에서 모델을 받는다.)
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
  # 진단: 컨트롤러 매니저는 Gazebo 프로세스 안에서 돈다 — 한계·경고 메시지를 종류별로 센다
  # (시각·숫자를 지워 같은 메시지를 묶는다). 로그를 지우기 전에 화면에 남긴다.
  if [[ -f "$LOG/gz_sim.log" ]]; then
    echo "[진단] gz_sim 로그의 한계·경고·오류 메시지(종류별 횟수, 상위 30)"
    grep -iE 'limit|saturat|clamp|reject|abort|error|warn' "$LOG/gz_sim.log" \
      | sed -E 's/\[[0-9]+\.[0-9]+\]//g; s/-?[0-9]+\.[0-9]+/N/g' \
      | sort | uniq -c | sort -rn | head -30 || true
  fi
  if [[ $rc -eq 0 ]]; then rm -rf "$LOG"; echo "[정리] 시험 로그 삭제함 — 위 표를 복사해 보내주세요"
  else echo "[정리] 실패(종료 코드 $rc) — 로그를 남겨 둠: $LOG"; fi
  exit $rc
}
trap finish EXIT

# 메시 경로 해석(run_gazebo_fr3.sh와 같다 — 자산을 복사하지 않고 링크만 둔다).
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO"
# gz_ros2_control 플러그인은 /opt/ros/lyrical/lib에 있다. 돌고 있는 작업 셀 Gazebo도 이 경로를 준다.
export GZ_SIM_SYSTEM_PLUGIN_PATH="/opt/ros/lyrical/lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}"
# 공식 URDF의 메시 경로는 "../meshes/..."(URDF 파일 기준)다. URDF를 한 단계 아래에 두어
# $LOG/meshes로 풀리게 한다.
ln -sfn "$FR3_REPO/fairino_description/meshes" "$LOG/meshes"
mkdir -p "$LOG/urdf"
URDF_OUT="$LOG/urdf/fr3wms_arm.urdf"
ros2 run xacro xacro "$XACRO_FILE" "fr3_urdf:=$FR3_URDF" "controller_yaml:=$CONTROLLERS" > "$URDF_OUT"

echo "[1/4] Gazebo 서버(화면 없음)"
start gz_sim gz sim -s -r -v 3 "$WORLD"
for _ in $(seq 60); do
  gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create" && break
  sleep 1
done
gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create" || { echo "Gazebo가 60초 안에 뜨지 않았다" >&2; exit 5; }

# robot_state_publisher: URDF를 명령행 인자로 넘기면 파서가 깨진다 — params 파일로 준다(run_gazebo_fr3.sh와 같다).
python3 - "$URDF_OUT" "$LOG/rsp_params.yaml" <<'PYGEN'
import sys
from pathlib import Path
urdf = Path(sys.argv[1]).read_text(encoding="utf-8")
body = "\n".join("      " + line for line in urdf.splitlines())
Path(sys.argv[2]).write_text(
    "robot_state_publisher:\n  ros__parameters:\n    use_sim_time: true\n"
    "    robot_description: |\n" + body + "\n", encoding="utf-8")
PYGEN
start rsp ros2 run robot_state_publisher robot_state_publisher --ros-args --params-file "$LOG/rsp_params.yaml"
sleep 3

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
