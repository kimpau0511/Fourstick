#!/usr/bin/env bash
# FR3-WMS + 커플링 + Robotiq 2F-85 조립 Gazebo 실행 (md/개발플랜.md 8-08).
#
# **arm-only 기준선을 건드리지 않는다.** 별도 world 이름·별도 파티션을 쓰고,
# config/gazebo/fr3wms_arm.urdf.xacro와 fr3wms_controllers.yaml은 읽기만 한다.
#
# 장착 값은 config/profiles/fr3wms_to_robotiq_2f85_mounting.json에서 읽는다.
# **Profile에 없는 값을 만들지 않는다.** yaw는 미확보이므로, 시뮬레이션을 돌리려면
# 사용자가 명시적으로 선언해야 한다:
#
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_gripper.sh
#
# 그 값은 **선언이며 검증이 아니다**. 기록에 declared로 남고 pick/place를
# 열어 주지 않는다.
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
FR3_URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
ROBOTIQ_REPO="${FORSTICK2_ROBOTIQ_REPO:-/home/asd/external/robotiq_ros}"
ROBOTIQ_DESC="$ROBOTIQ_REPO/grippers/robotiq_description"
# arm-only 기준선(fr3_cell.sdf)을 건드리지 않기 위해 조립 전용 world를 쓴다.
WORLD="$ROOT/config/gazebo/fr3_2f85_cell.sdf"
XACRO_FILE="$ROOT/config/gazebo/fr3wms_with_2f85.urdf.xacro"
ARM_CONTROLLERS="$ROOT/config/gazebo/fr3wms_controllers.yaml"
GRIPPER_CONTROLLERS="$ROOT/config/gazebo/fr3wms_gripper_controllers.yaml"
MOUNTING="$ROOT/config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
OVERLAY="$LOG_DIR/overlay"
# 조립 모델은 arm-only와 **다른 파티션·도메인**에서 돈다. 두 모델이 같은
# controller_manager를 잡으면 서로 망친다(실측으로 확인한 문제다).
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_gripper}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-43}"
WORLD_NAME="forstick2_fr3_cell"

for path in "$FR3_URDF" "$ROBOTIQ_DESC/urdf/robotiq_2f_85_macro.urdf.xacro" \
            "$ROBOTIQ_DESC/urdf/ur_to_robotiq_adapter.urdf.xacro"; do
  if [[ ! -f "$path" ]]; then
    echo "[run_gazebo_fr3_gripper] 자산이 없다: $path" >&2
    echo "[run_gazebo_fr3_gripper] asset.missing — 없는 자산을 대체하지 않는다." >&2
    exit 3
  fi
done

if [[ -z "${FORSTICK2_ASSEMBLY_YAW_RAD:-}" ]]; then
  echo "[run_gazebo_fr3_gripper] **장착 방향(yaw)이 미확보다.**" >&2
  echo "[run_gazebo_fr3_gripper] Profile: $(python3 -c "import json;print(json.load(open('$MOUNTING'))['mounting_profile_version'])")" >&2
  echo "[run_gazebo_fr3_gripper] 근거: reports/mounting/interface_comparison.json" >&2
  echo "[run_gazebo_fr3_gripper] 필요한 입력: 커플링 도면의 핀/키 위치 또는 조립 사진" >&2
  echo "" >&2
  echo "[run_gazebo_fr3_gripper] 시뮬레이션만 돌리려면 값을 **명시적으로 선언**한다:" >&2
  echo "    FORSTICK2_ASSEMBLY_YAW_RAD=0 $0" >&2
  echo "[run_gazebo_fr3_gripper] 그 값은 declared이며 pick/place를 열지 않는다." >&2
  exit 4
fi

mkdir -p "$LOG_DIR"
source /opt/ros/lyrical/setup.bash

# 고정 버전(v1.1.0) robotiq_description을 $(find)이 찾도록 오버레이를 만든다.
# **저장소에 복사하지 않는다** — 심볼릭 링크만 만든다.
mkdir -p "$OVERLAY/share/ament_index/resource_index/packages" \
         "$OVERLAY/share/robotiq_description"
touch "$OVERLAY/share/ament_index/resource_index/packages/robotiq_description"
for dir in urdf meshes config launch; do
  ln -sfn "$ROBOTIQ_DESC/$dir" "$OVERLAY/share/robotiq_description/$dir"
done
export AMENT_PREFIX_PATH="$OVERLAY:${AMENT_PREFIX_PATH:-}"
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO:$OVERLAY/share"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO:$OVERLAY/share"
# WSLg에는 d3d12 하드웨어 GL 드라이버가 있다. 소프트웨어 렌더링을 강제하면
# ogre2가 쓰는 EGL이 거부한다("Not allowed to force software rendering
# when API explicitly selects a hardware device" — 실측). 기본을 끈다.
# ~/.bashrc가 LIBGL_ALWAYS_SOFTWARE=1을 걸어 둔다. 그대로 두면 ogre2가 쓰는
# EGL이 거부한다("Not allowed to force software rendering when API
# explicitly selects a hardware device" — 실측). WSLg에는 d3d12 하드웨어
# GL 드라이버가 있으므로 **덮어쓴다.** 소프트웨어 렌더링이 필요하면
# FORSTICK2_GZ_FORCE_SOFTWARE_GL=1로 되돌린다.
if [[ "${FORSTICK2_GZ_FORCE_SOFTWARE_GL:-0}" == "1" ]]; then
  export LIBGL_ALWAYS_SOFTWARE=1
else
  export LIBGL_ALWAYS_SOFTWARE=0
fi
# ogre2는 GLX를 쓴다. QT_QPA_PLATFORM=wayland로 띄우면 렌더 창 생성이
# 실패한다("currentGLContext was specified with no current GL context"
# — 실측). xcb(Xwayland)를 쓴다. WSLg가 창 제목에 [WARN:COPY MODE]를
# 붙이는 것은 이 경로의 버퍼 복사 표시이며 오류가 아니다.
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
# FR3 공식 URDF의 메시 경로는 **상대경로**(`../meshes/FR3WMS/...`)다. 그래서
# 생성 URDF를 `$LOG_DIR/urdf/` 안에 두고 `$LOG_DIR/meshes` 링크를 옆에 둔다 —
# 그러면 `../meshes/...`가 `$LOG_DIR/meshes/...`로 풀린다.
# (URDF를 `$LOG_DIR`에 바로 두면 `/tmp/meshes/...`를 찾다가 visual·collision
#  메시 28건을 모두 놓쳤다. 실측으로 확인한 문제다.)
mkdir -p "$LOG_DIR/urdf"
ln -sfn "$FR3_REPO/fairino_description/meshes" "$LOG_DIR/meshes"

# Profile에서 TCP 오프셋을 읽는다. 값이 없으면 멈춘다.
TCP_XYZ="$(python3 - "$MOUNTING" <<'PYGEN'
import json, sys
profile = json.load(open(sys.argv[1], encoding="utf-8"))
values = [item["value"] for item in profile["tcp_xyz"]]
if any(value is None for value in values):
    print("MISSING")
else:
    print(" ".join(str(value) for value in values))
PYGEN
)"
if [[ "$TCP_XYZ" == "MISSING" ]]; then
  echo "[run_gazebo_fr3_gripper] TCP 오프셋이 Profile에 없다 — 0으로 대체하지 않는다." >&2
  exit 5
fi

URDF_OUT="$LOG_DIR/urdf/fr3wms_with_2f85.urdf"
ros2 run xacro xacro "$XACRO_FILE" \
  "fr3_urdf:=$FR3_URDF" \
  "arm_controller_yaml:=$ARM_CONTROLLERS" \
  "gripper_controller_yaml:=$GRIPPER_CONTROLLERS" \
  "gripper_yaw_rad:=$FORSTICK2_ASSEMBLY_YAW_RAD" \
  "tcp_xyz:=$TCP_XYZ" > "$URDF_OUT"
echo "[run_gazebo_fr3_gripper] URDF 생성: $URDF_OUT ($(wc -c < "$URDF_OUT") bytes)"
echo "[run_gazebo_fr3_gripper] 링크 $(grep -c '<link' "$URDF_OUT")개, 조인트 $(grep -c '<joint' "$URDF_OUT")개"
echo "[run_gazebo_fr3_gripper] yaw(선언) = $FORSTICK2_ASSEMBLY_YAW_RAD rad · TCP = $TCP_XYZ"

# 상대 메시 경로가 실제로 파일로 풀리는지 **확인한다.** 풀리지 않으면 Gazebo가
# 조용히 형상 없이 뜬다(충돌 형상까지 사라져 물리 결과가 달라진다).
# XML로 읽는다 — grep은 주석 처리된 <mesh>까지 세어 잘못 막았다(실측).
python3 - "$URDF_OUT" <<'PYMESH' || exit 7
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

urdf = Path(sys.argv[1])
base = urdf.parent
missing, checked = [], 0
for mesh in ET.parse(urdf).getroot().iter("mesh"):
    name = mesh.get("filename", "")
    if name.startswith(("package://", "model://", "file://", "/")):
        continue
    checked += 1
    if not (base / name).is_file():
        missing.append(name)
for name in missing:
    print(f"[run_gazebo_fr3_gripper] 메시를 풀 수 없다: {base / name}",
          file=sys.stderr)
if missing:
    print(f"[run_gazebo_fr3_gripper] 상대 메시 {len(missing)}건이 풀리지 않는다."
          " asset.missing — 형상 없이 띄우지 않는다.", file=sys.stderr)
    raise SystemExit(1)
print(f"[run_gazebo_fr3_gripper] 상대 메시 {checked}건 확인 완료 ({base}/../meshes)")
PYMESH

# MoveIt용 URDF: 상대 메시 경로를 절대 file:// 로 바꾼다(geometric_shapes가
# 상대경로에서 죽는다 — 8-07에서 실측).
python3 - "$URDF_OUT" "$FR3_REPO/fairino_description" "$LOG_DIR/urdf/fr3wms_with_2f85.moveit.urdf" <<'PYGEN'
import pathlib
import sys

source, package, target = sys.argv[1:4]
text = pathlib.Path(source).read_text(encoding="utf-8")
count = text.count('filename="../meshes/')
text = text.replace('filename="../meshes/', f'filename="file://{package}/meshes/')
pathlib.Path(target).write_text(text, encoding="utf-8")
print(f"[run_gazebo_fr3_gripper] MoveIt용 메시 URI 치환 {count}건 -> {target}")
PYGEN

start() {
  local name="$1"; shift
  nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
  echo "$!" > "$LOG_DIR/$name.pid"
  echo "[run_gazebo_fr3_gripper] $name pid=$! log=$LOG_DIR/$name.log"
}

alive() {
  [[ -f "$LOG_DIR/$1.pid" ]] && kill -0 "$(cat "$LOG_DIR/$1.pid")" 2>/dev/null
}

if alive gz_sim_gripper; then
  echo "[run_gazebo_fr3_gripper] 서버가 이미 실행 중이다: pid=$(cat "$LOG_DIR/gz_sim_gripper.pid")"
  echo "[run_gazebo_fr3_gripper] 종료가 필요하면 kill \$(cat $LOG_DIR/gz_sim_gripper.pid)"
  exit 0
fi

# 1) Gazebo Sim (GUI 포함)
# 렌더 엔진은 환경마다 다르다(WSLg에서 ogre가 프레임을 만들지 못한 사례를
# 실측했다). 기본값을 두되 바꿀 수 있게 남긴다.
GZ_ENGINE="${FORSTICK2_GZ_RENDER_ENGINE:-ogre2}"
GZ_ENGINE_GUI="${FORSTICK2_GZ_RENDER_ENGINE_GUI:-$GZ_ENGINE}"
VERBOSITY="${FORSTICK2_GZ_VERBOSITY:-3}"

# 서버와 GUI를 **따로** 띄운다. `gz sim`이 한 프로세스로 둘을 띄우면 GUI의 X11
# 연결이 끊길 때 시뮬레이터까지 같이 죽는다(실측:
#   "[QT] The X11 connection broke: I/O error (code 1)"
#   "XIO: fatal IO error 17 (File exists) on X server \":0\""
# 직후 controller_manager와 world가 함께 사라졌다).
# 분리하면 GUI가 죽어도 시뮬레이션이 남고, GUI만 다시 붙일 수 있다.
# **headless 시뮬레이터를 자동 종료하지 않는다.**
start gz_sim_gripper gz sim -s -v "$VERBOSITY" -r \
  --render-engine "$GZ_ENGINE" "$WORLD"

# world가 실제로 올라왔는지 **확인하고** 넘어간다. 고정 sleep으로 넘어가면
# 서버가 기동 중 멈춰도 spawn이 "Waiting for service"로 무한 대기했다(실측).
echo "[run_gazebo_fr3_gripper] world 기동 대기 (최대 180초)"
WORLD_UP=0
for _ in $(seq 1 60); do
  if ! kill -0 "$(cat "$LOG_DIR/gz_sim_gripper.pid")" 2>/dev/null; then
    echo "[run_gazebo_fr3_gripper] Gazebo 서버가 죽었다 — $LOG_DIR/gz_sim_gripper.log" >&2
    exit 6
  fi
  if timeout 6 gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create"; then
    WORLD_UP=1; break
  fi
  sleep 3
done
if [[ "$WORLD_UP" -eq 0 ]]; then
  echo "[run_gazebo_fr3_gripper] world가 180초 안에 기동하지 않았다." >&2
  echo "[run_gazebo_fr3_gripper] **서버를 자동 종료하지 않는다.** 로그를 확인한다:" >&2
  echo "    $LOG_DIR/gz_sim_gripper.log" >&2
  echo "[run_gazebo_fr3_gripper] 사용 가능 메모리: $(free -m | awk 'NR==2 {print $7}') MB" >&2
  exit 6
fi
echo "[run_gazebo_fr3_gripper] world 기동 확인: $WORLD_NAME"

# GUI 클라이언트(선택). 실패해도 서버는 그대로 둔다.
if [[ "${FORSTICK2_GZ_HEADLESS:-0}" == "1" ]]; then
  echo "[run_gazebo_fr3_gripper] headless 요청 — GUI를 띄우지 않는다"
elif [[ -z "${DISPLAY:-}" ]]; then
  echo "[run_gazebo_fr3_gripper] DISPLAY가 없어 GUI를 띄우지 않는다." >&2
  echo "[run_gazebo_fr3_gripper] 필요한 환경: X 서버(WSLg 또는 X11)와 DISPLAY." >&2
  echo "[run_gazebo_fr3_gripper] **서버는 그대로 둔다.**" >&2
else
  # 창은 하나만 띄운다. 반복 실행으로 3개까지 쌓인 적이 있어(실측),
  # **우리 파티션의 잔여 창만** 정리하고 새로 띄운다. 다른 파티션의
  # Gazebo는 건드리지 않는다.
  for pid in $(ps -eo pid,args | grep -F "gz-sim-gui-client" \
               | grep -v "sh -c" | grep -v grep | awk '{print $1}' || true); do
    partition="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
                 | sed -n 's/^GZ_PARTITION=//p')"
    if [[ "$partition" == "$GZ_PARTITION" ]]; then
      kill -9 "$pid" 2>/dev/null \
        && echo "[run_gazebo_fr3_gripper] 잔여 GUI 창 pid=$pid 정리"
    else
      echo "[run_gazebo_fr3_gripper] GUI pid=$pid 는 다른 파티션(${partition:-없음})" \
           "— 건드리지 않는다"
    fi
  done
  start gz_gui_gripper gz sim -g -v "$VERBOSITY" --render-engine-gui "$GZ_ENGINE_GUI"
  sleep 8
  if alive gz_gui_gripper; then
    echo "[run_gazebo_fr3_gripper] GUI 붙음 pid=$(cat "$LOG_DIR/gz_gui_gripper.pid")"
  else
    echo "[run_gazebo_fr3_gripper] GUI가 뜨지 못했다 — $LOG_DIR/gz_gui_gripper.log" >&2
    echo "[run_gazebo_fr3_gripper] **서버는 그대로 둔다.** 다시 붙이려면:" >&2
    echo "    ./scripts/attach_gazebo_gui.sh" >&2
  fi
fi

# 2) robot_state_publisher (URDF를 params 파일로 준다 — 명령행은 파서가 깨진다)
RSP_PARAMS="$LOG_DIR/rsp_gripper_params.yaml"
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
start rsp_gripper ros2 run robot_state_publisher robot_state_publisher \
  --ros-args --params-file "$RSP_PARAMS"
sleep 5

# 3) spawn
ros2 run ros_gz_sim create -world "$WORLD_NAME" -file "$URDF_OUT" \
  -name fr3wms_2f85 -x 0 -y 0 -z 0 > "$LOG_DIR/spawn_gripper.log" 2>&1 || true
echo "[run_gazebo_fr3_gripper] spawn: $(tail -1 "$LOG_DIR/spawn_gripper.log")"
sleep 10

# 4) 컨트롤러 (팔 2종 + 그리퍼 1종)
for controller in joint_state_broadcaster arm_trajectory_controller; do
  ros2 run controller_manager spawner "$controller" \
    --controller-manager /controller_manager --controller-manager-timeout 60 \
    --param-file "$ARM_CONTROLLERS" \
    > "$LOG_DIR/spawn_$controller.gripper.log" 2>&1 || true
  echo "[run_gazebo_fr3_gripper] $controller: $(tail -1 "$LOG_DIR/spawn_$controller.gripper.log")"
done
ros2 run controller_manager spawner gripper_action_controller \
  --controller-manager /controller_manager --controller-manager-timeout 60 \
  --param-file "$GRIPPER_CONTROLLERS" \
  > "$LOG_DIR/spawn_gripper_action_controller.log" 2>&1 || true
echo "[run_gazebo_fr3_gripper] gripper_action_controller: $(tail -1 "$LOG_DIR/spawn_gripper_action_controller.log")"

echo "[run_gazebo_fr3_gripper] world=$WORLD_NAME partition=$GZ_PARTITION domain=$ROS_DOMAIN_ID"
echo "[run_gazebo_fr3_gripper] **이 조립은 yaw가 선언값이다. pick/place는 비활성이다.**"
