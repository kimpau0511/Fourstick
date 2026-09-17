#!/usr/bin/env bash
# FR3-WMS arm-only Gazebo 실행 (md/개발플랜.md 8-03).
#
# - 제3자 자산을 저장소에 복사하지 않는다. 외부 경로를 참조한다.
# - **별도 GZ_PARTITION**을 쓴다. 기존 forstick 데모의 Gazebo와 섞이지 않게 한다.
# - GUI를 띄운다(headless 아님). GPU는 vLLM이 쓰고 있어 소프트웨어 렌더링으로 돈다.
# - 기존에 돌고 있는 forstick Gazebo·웹·Qwen 서버를 종료하지 않는다.
set -eo pipefail
# ROS setup.bash가 미설정 변수를 참조하므로 -u는 쓰지 않는다.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
FR3_URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
WORLD="$ROOT/config/gazebo/fr3_cell.sdf"
XACRO_FILE="$ROOT/config/gazebo/fr3wms_arm.urdf.xacro"
CONTROLLERS="$ROOT/config/gazebo/fr3wms_controllers.yaml"
LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3}"
# **ROS 격리**: gz transport 파티션만으로는 ROS 노드가 분리되지 않는다. 기존
# forstick 데모의 controller_manager와 섞이지 않게 도메인을 따로 쓴다.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-42}"

if [[ ! -f "$FR3_URDF" ]]; then
  echo "[run_gazebo_fr3] 외부 FR3 URDF가 없다: $FR3_URDF" >&2
  echo "[run_gazebo_fr3] 라이선스가 확인되지 않은 자산은 저장소에 두지 않는다." >&2
  echo "[run_gazebo_fr3] FORSTICK2_FR3_REPO로 외부 경로를 주거나 자산을 확보해야 한다." >&2
  exit 3
fi

mkdir -p "$LOG_DIR"
source /opt/ros/lyrical/setup.bash

# 메시(package://fairino_description/...)를 외부 경로에서 찾게 한다.
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO"
# GPU는 vLLM(Qwen3)이 쓰고 있다. 소프트웨어 렌더링으로 GUI를 띄운다.
export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"

# 공식 URDF의 메시 경로가 상대경로(meshes/...)다. 생성 URDF 옆에 메시
# 디렉터리를 심볼릭 링크로 놓아 해석되게 한다(자산을 복사하지 않는다).
ln -sfn "$FR3_REPO/fairino_description/meshes" "$LOG_DIR/meshes"

URDF_OUT="$LOG_DIR/fr3wms_arm.urdf"
ros2 run xacro xacro "$XACRO_FILE" \
  "fr3_urdf:=$FR3_URDF" "controller_yaml:=$CONTROLLERS" > "$URDF_OUT"
echo "[run_gazebo_fr3] URDF 생성: $URDF_OUT ($(wc -c < "$URDF_OUT") bytes)"

start() { # 이름 명령...
  local name="$1"; shift
  nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
  echo "[run_gazebo_fr3] $name pid=$! log=$LOG_DIR/$name.log"
}

# 1) Gazebo Sim (GUI 포함, 시간 진행 -r)
start gz_sim gz sim -v 2 -r --render-engine ogre --render-engine-gui ogre "$WORLD"
sleep 12

# 2) robot_state_publisher
#    URDF를 명령행 인자로 넘기면 파서가 깨진다(줄바꿈·주석). params 파일로 준다.
RSP_PARAMS="$LOG_DIR/rsp_params.yaml"
python3 - "$URDF_OUT" "$RSP_PARAMS" <<'PYGEN'
import sys
from pathlib import Path

urdf = Path(sys.argv[1]).read_text(encoding="utf-8")
body = "\n".join("      " + line for line in urdf.splitlines())
Path(sys.argv[2]).write_text(
    "robot_state_publisher:\n"
    "  ros__parameters:\n"
    "    use_sim_time: true\n"
    "    robot_description: |\n" + body + "\n",
    encoding="utf-8",
)
PYGEN
start rsp ros2 run robot_state_publisher robot_state_publisher \
  --ros-args --params-file "$RSP_PARAMS"
sleep 5

# 3) 모델 spawn (ros_gz_sim create)
ros2 run ros_gz_sim create -world forstick2_fr3_cell -file "$URDF_OUT" \
  -name fr3wms -x 0 -y 0 -z 0 > "$LOG_DIR/spawn.log" 2>&1 || true
echo "[run_gazebo_fr3] spawn 결과: $(tail -1 "$LOG_DIR/spawn.log")"
sleep 8

# 4) 컨트롤러 (gz_ros2_control 플러그인이 controller_manager를 띄운다)
# 컨트롤러 파라미터는 spawner의 --param-file로 넘긴다. gz 플러그인이 넘기는
# YAML은 controller_manager 노드의 파라미터로만 들어가서, 컨트롤러 노드가
# joints 같은 자기 파라미터를 보지 못한다(실측: init failure).
for controller in joint_state_broadcaster arm_trajectory_controller; do
  ros2 run controller_manager spawner "$controller" \
    --controller-manager /controller_manager --controller-manager-timeout 60 \
    --param-file "$CONTROLLERS" \
    > "$LOG_DIR/spawn_$controller.log" 2>&1 || true
  echo "[run_gazebo_fr3] $controller: $(tail -1 "$LOG_DIR/spawn_$controller.log")"
done

echo "[run_gazebo_fr3] world=$WORLD partition=$GZ_PARTITION domain=$ROS_DOMAIN_ID"
echo "[run_gazebo_fr3] 로그 디렉터리: $LOG_DIR"
