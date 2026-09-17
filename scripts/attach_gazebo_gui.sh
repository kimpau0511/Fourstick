#!/usr/bin/env bash
# 실행 중인 조립 Gazebo **서버에 GUI만 다시 붙인다** (md/개발플랜.md 8-08).
#
#   ./scripts/attach_gazebo_gui.sh
#
# WSLg의 X11 연결이 끊기면 GUI만 죽는다(서버는 분리되어 남는다). 이 스크립트는
# 그 GUI를 다시 띄운다. **서버를 종료하거나 재시작하지 않는다.**
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# 대상 셀을 고른다. 기본은 단순 조립 셀, --workcell은 작업 셀이다.
if [[ "${1:-}" == "--workcell" ]]; then
  LOG_DIR="${FORSTICK2_WORKCELL_LOG_DIR:-/tmp/forstick2_workcell}"
  export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
  WORLD_NAME="forstick2_fr3_2f85_workcell"
  WORLD_SDF="$ROOT/config/gazebo/fr3_2f85_workcell.sdf"
  GUI_PIDFILE="gz_gui"
else
  LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
  export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_gripper}"
  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-43}"
  WORLD_NAME="forstick2_fr3_cell"
  WORLD_SDF="$ROOT/config/gazebo/fr3_2f85_cell.sdf"
  GUI_PIDFILE="gz_gui_gripper"
fi
# GUI 렌더 엔진은 ogre(v1). 근거: reports/workcell/gui_rendering.json
GZ_ENGINE_GUI="${FORSTICK2_GZ_RENDER_ENGINE_GUI:-ogre}"

source /opt/ros/lyrical/setup.bash
# **메시 탐색 경로를 GUI에도 준다.** 빼먹으면 GUI가 package:// 메시를 풀지
# 못하고 렌더 루프에서 멈춘다(실측: 어떤 서비스에도 응답하지 않고 CPU 356%).
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
OVERLAY="/tmp/forstick2_gazebo/overlay"
export AMENT_PREFIX_PATH="$OVERLAY:${AMENT_PREFIX_PATH:-}"
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO:$OVERLAY/share"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO:$OVERLAY/share"

# ogre2는 GLX를 쓴다. wayland 네이티브로는 렌더 창 생성이 실패한다(실측).
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
# 이 환경은 DRI3가 없어 하드웨어 GL을 쓸 수 없다(실측). ogre v1 +
# 소프트웨어 렌더링이 동작하는 조합이다.
export LIBGL_ALWAYS_SOFTWARE="${FORSTICK2_GZ_FORCE_SOFTWARE_GL:-1}"

if ! timeout 20 gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/"; then
  echo "[attach] 조립 Gazebo 서버가 실행 중이 아니다. 먼저 띄운다:" >&2
  echo "    FORSTICK2_ASSEMBLY_YAW_RAD=<선언값> ./scripts/run_gazebo_fr3_2f85_gui.sh" >&2
  exit 2
fi
if [[ -z "${DISPLAY:-}" ]]; then
  echo "[attach] DISPLAY가 없다 — GUI를 띄울 수 없다." >&2
  echo "[attach] 필요한 환경: WSLg(Windows 11 기본) 또는 X 서버 + DISPLAY." >&2
  echo "[attach] **서버는 그대로 둔다.**" >&2
  exit 5
fi
if [[ -f "$LOG_DIR/$GUI_PIDFILE.pid" ]] \
   && kill -0 "$(cat "$LOG_DIR/$GUI_PIDFILE.pid")" 2>/dev/null; then
  echo "[attach] GUI가 이미 실행 중이다: pid=$(cat "$LOG_DIR/$GUI_PIDFILE.pid")"
  echo "[attach] **종료하지 않는다.** 다시 띄우려면 직접 종료한다:"
  echo "    kill \$(cat $LOG_DIR/$GUI_PIDFILE.pid)"
  exit 0
fi

# 이 파티션의 GUI 클라이언트가 이미 있는지 **감지한다.** 반복 실행으로 창이
# 3개까지 쌓인 적이 있다(실측). **기존 창을 임의로 종료하지 않는다.**
GUI_PIDS="$(ps -eo pid,args | grep -F "gz-sim-gui-client" \
  | grep -v "sh -c" | grep -v grep | awk '{print $1}' | tr '\n' ' ' || true)"
if [[ -n "${GUI_PIDS// /}" ]]; then
  echo "[attach] Gazebo GUI 창이 이미 있다: $GUI_PIDS" >&2
  echo "[attach] **종료하지 않는다.** 정리가 필요하면:" >&2
  echo "    ./scripts/stop_gazebo_fr3_2f85.sh --gui-only" >&2
  exit 0
fi

nohup gz sim -g -v "${FORSTICK2_GZ_VERBOSITY:-3}" \
  --render-engine-gui "$GZ_ENGINE_GUI" \
  > "$LOG_DIR/$GUI_PIDFILE.log" 2>&1 &
echo "$!" > "$LOG_DIR/$GUI_PIDFILE.pid"
echo "[attach] GUI pid=$! log=$LOG_DIR/$GUI_PIDFILE.log"
sleep 8
if kill -0 "$(cat "$LOG_DIR/$GUI_PIDFILE.pid")" 2>/dev/null; then
  echo "[attach] GUI가 떴다. 창 제목의 [WARN:COPY MODE]는 WSLg가 프레임을"
  echo "[attach] 복사 경로로 전달한다는 표시이며 오류가 아니다."
  python3 "$ROOT/scripts/place_gui_camera.py" --world "$WORLD_NAME" \
    --world-sdf "$WORLD_SDF" | sed 's/^/  /'
else
  echo "[attach] GUI가 뜨지 못했다. 원인:" >&2
  tail -5 "$LOG_DIR/$GUI_PIDFILE.log" >&2
  echo "[attach] **서버는 그대로 둔다.**" >&2
  exit 6
fi
