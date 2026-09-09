#!/usr/bin/env bash
# 로봇 스택(Gazebo+MoveIt + API 서버) 전환 스크립트.
#
#   ./start_robot.sh panda
#   ./start_robot.sh ur5e
#
# 지금 떠 있는 로봇 스택(있다면)을 내리고, 지정한 로봇의 Gazebo+MoveIt을
# 새로 띄운 뒤 API 서버를 FORSTICK_ROBOT=<로봇>으로 재기동한다.
# vLLM은 로봇과 무관하므로 건드리지 않는다 — 따로 이미 떠 있어야 함
# (CLAUDE.md "실행 방법" 1번 참고).
#
# 한 번에 하나의 로봇 스택만 가동 가능하다 — panda_gazebo_moveit.launch.py와
# ur5e_robotiq_gazebo.launch.py는 각자 독립된 Gazebo 서버 + 네임스페이스
# 없는 동일 이름 노드(move_group, robot_state_publisher 등)를 띄우기 때문에
# 동시 실행 시 충돌한다.

# -u(nounset)는 안 씀 — /opt/ros/lyrical/setup.bash 자체가 내부적으로
# unset 변수(AMENT_TRACE_SETUP_FILES 등)를 참조해서 nounset과 호환 안 됨.
set -eo pipefail

ROBOT="${1:-}"
if [[ "$ROBOT" != "panda" && "$ROBOT" != "ur5e" ]]; then
    echo "사용법: $0 {panda|ur5e}" >&2
    exit 1
fi

FORSTICK_DIR="/home/asd/forstick"
RUN_DIR="$FORSTICK_DIR/.run"
LOG_DIR="$FORSTICK_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

API_PID_FILE="$RUN_DIR/api_server.pid"
GAZEBO_PID_FILE="$RUN_DIR/gazebo.pid"

if [[ "$ROBOT" == "panda" ]]; then
    LAUNCH_FILE="$FORSTICK_DIR/gazebo_robot/panda_gazebo_moveit.launch.py"
    READY_PATTERN="You can start planning now!"
else
    LAUNCH_FILE="$FORSTICK_DIR/gazebo_robot/ur5e_robotiq_gazebo.launch.py"
    READY_PATTERN="You can start planning now!"
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
GAZEBO_LOG="$LOG_DIR/gazebo_${ROBOT}_${TIMESTAMP}.log"
API_LOG="$LOG_DIR/api_server_${ROBOT}_${TIMESTAMP}.log"

# ------------------------------------------------------------------
# 1) 기존 스택 정지
# ------------------------------------------------------------------

stop_by_pidfile() {
    local pidfile="$1" label="$2"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[start_robot] $label 종료 중 (PID $pid)"
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 15); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[start_robot] $label 강제 종료 (PID $pid)"
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pidfile"
    fi
}

echo "[start_robot] === 기존 스택 정지 ==="
stop_by_pidfile "$API_PID_FILE" "API 서버"
stop_by_pidfile "$GAZEBO_PID_FILE" "Gazebo+MoveIt 런치"

# ros2 launch가 종료돼도 자식 노드(move_group, robot_state_publisher,
# static_transform_publisher, parameter_bridge)가 고아로 남는 경우가 있어서
# (panda->ur5e 전환 때 실제로 겪음) 이름으로 한 번 더 정리한다. 또한 pid
# 파일 없이(이 스크립트 첫 실행 전에) 수동으로 띄워둔 프로세스가 있을 수도
# 있으므로 ros2 launch/uvicorn 자체도 패턴으로 한 번 더 잡는다.
# "uvicorn api_server:app" / "ros2 launch .*_gazebo.*launch.py" 패턴은
# vLLM(entrypoint가 vllm.entrypoints...)과 겹치지 않으므로 안전.
ORPHAN_PATTERNS=(
    "moveit_ros_move_group/move_group"
    "robot_state_publisher/robot_state_publisher"
    "tf2_ros/static_transform_publisher"
    "ros_gz_bridge/parameter_bridge"
    "ros2 launch .*_gazebo.*\.launch\.py"
    "uvicorn api_server:app"
    # Gazebo 시뮬레이터 실체(gz-sim-main/gui-client)는 ros2 launch 부모를
    # 죽여도 안 죽고 남는 경우가 있었다(실제로 겪음) — 안 지우면 다음
    # 로봇의 gz sim이 "Another world of the same name is running"으로
    # 죽는다. gz_tools_vendor의 ruby 래퍼 셸까지 같이 잡는다.
    "gz-sim-main"
    "gz-sim-gui-client"
    "gz_tools_vendor/bin/gz"
)
for pattern in "${ORPHAN_PATTERNS[@]}"; do
    pkill -f "$pattern" 2>/dev/null && echo "[start_robot] 잔여 프로세스 정리: $pattern" || true
done
sleep 3
# gz-sim-main 등은 SIGTERM에 안 죽는 경우가 실제로 있었다 — 그대로 남아있으면
# 다음 로봇의 Gazebo가 "Another world of the same name is running"으로
# 죽으므로, 남은 게 있으면 SIGKILL로 확실히 정리한다.
for pattern in "${ORPHAN_PATTERNS[@]}"; do
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "[start_robot] 잔여 프로세스 강제 종료: $pattern"
        pkill -9 -f "$pattern" 2>/dev/null || true
    fi
done
sleep 2

# ------------------------------------------------------------------
# 2) 선택한 로봇의 Gazebo+MoveIt 기동
# ------------------------------------------------------------------

echo "[start_robot] === ${ROBOT} Gazebo+MoveIt 기동 ==="
(
    source /opt/ros/lyrical/setup.bash
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export GZ_SIM_SYSTEM_PLUGIN_PATH=/opt/ros/lyrical/lib
    exec ros2 launch "$LAUNCH_FILE"
) > "$GAZEBO_LOG" 2>&1 &
GAZEBO_PID=$!
echo "$GAZEBO_PID" > "$GAZEBO_PID_FILE"
echo "[start_robot] Gazebo+MoveIt PID=$GAZEBO_PID, 로그: $GAZEBO_LOG"

echo "[start_robot] move_group 준비 대기 중..."
DEADLINE=$((SECONDS + 90))
while ! grep -qF "$READY_PATTERN" "$GAZEBO_LOG" 2>/dev/null; do
    if ! kill -0 "$GAZEBO_PID" 2>/dev/null; then
        echo "[start_robot] 오류: Gazebo+MoveIt 프로세스가 중간에 죽었습니다. 로그 확인: $GAZEBO_LOG" >&2
        exit 1
    fi
    if (( SECONDS > DEADLINE )); then
        echo "[start_robot] 오류: move_group 준비 대기 타임아웃(90초). 로그 확인: $GAZEBO_LOG" >&2
        exit 1
    fi
    sleep 2
done
echo "[start_robot] move_group 준비 완료"

# ------------------------------------------------------------------
# 3) API 서버 기동 (FORSTICK_ROBOT=$ROBOT)
# ------------------------------------------------------------------

echo "[start_robot] === API 서버 기동 (FORSTICK_ROBOT=$ROBOT) ==="
(
    cd "$FORSTICK_DIR"
    source venv/bin/activate
    source /opt/ros/lyrical/setup.bash
    export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
    export FORSTICK_ROBOT="$ROBOT"
    exec uvicorn api_server:app --host 0.0.0.0 --port 8090
) > "$API_LOG" 2>&1 &
API_PID=$!
echo "$API_PID" > "$API_PID_FILE"
echo "[start_robot] API 서버 PID=$API_PID, 로그: $API_LOG"

echo "[start_robot] API 서버 기동 대기 중..."
DEADLINE=$((SECONDS + 60))
while ! grep -qE "Application startup complete|Traceback|RuntimeError" "$API_LOG" 2>/dev/null; do
    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo "[start_robot] 오류: API 서버 프로세스가 중간에 죽었습니다. 로그 확인: $API_LOG" >&2
        exit 1
    fi
    if (( SECONDS > DEADLINE )); then
        echo "[start_robot] 오류: API 서버 기동 대기 타임아웃(60초). 로그 확인: $API_LOG" >&2
        exit 1
    fi
    sleep 2
done

if grep -qE "Traceback|RuntimeError" "$API_LOG"; then
    echo "[start_robot] 오류: API 서버가 기동 중 실패했습니다 (FORSTICK_ROBOT 값 오타 등). 로그 확인: $API_LOG" >&2
    tail -20 "$API_LOG" >&2
    exit 1
fi

echo "[start_robot] === 완료 ==="
sleep 1
curl -s http://localhost:8090/health && echo
echo "[start_robot] http://localhost:8090/console 에서 확인하세요 (vLLM은 건드리지 않았으니 이미 떠 있어야 합니다)."
