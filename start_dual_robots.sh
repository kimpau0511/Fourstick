#!/usr/bin/env bash
# Panda + UR5e Gazebo+MoveIt+API 서버를 동시에(격리해서) 띄우는 스크립트.
#
#   ./start_dual_robots.sh
#
# start_robot.sh(하나만 전환)와 달리, 이건 둘 다 계속 떠 있게 하는 용도.
# 노드 이름(move_group, robot_state_publisher, controller_manager 등)과
# 서비스/액션 이름(/compute_ik 등)은 두 로봇이 동일해서 그대로 같이 띄우면
# 충돌한다 — 그래서 노드를 리네임하는 대신, 로봇별로 통신 계층 자체를
# 완전히 격리한다:
#   - ROS_DOMAIN_ID: Panda=0, UR5e=1 (Cyclone DDS가 도메인 다르면
#     서로 discover 자체를 안 함 — /compute_ik, move_group 등 동일 이름
#     노드/서비스가 로봇 간에 안 보임)
#   - GZ_PARTITION: Panda=forstick_panda, UR5e=forstick_ur5e (Gazebo
#     Transport 쪽의 같은 개념 — 같은 pallets_world.sdf를 그대로 써도
#     "Another world of the same name is running" 없이 둘 다 뜬다)
#
# 카메라 스트림(/scene_camera/image -> MJPEG)은 WSLg GUI 캡처가 아니라
# ROS 토픽 구독 방식이라 헤드리스 Gazebo에서도 그대로 동작한다. WSLg
# 렌더링 부하를 줄이려고 Panda는 헤드리스(gazebo_gui:=false)로 띄운다.
#
# [버그 발견 — 원인 확인, 아래에서 수정함] 처음 이 스크립트를 테스트할 때
# gz_ros2_control의 GazeboSimSystem::write()에서 세그폴트로 gz sim이 죽는
# 게 반복 재현됐다 — 원인은 GZ_PARTITION 격리가 실제로는 전혀 안 먹히고
# 있었기 때문이었다: 아래 GZ_PART 배열을 원래 GZ_PARTITION이라는 이름으로
# 선언해뒀었는데, `export GZ_PARTITION="${GZ_PARTITION[$robot]}"`처럼 같은
# 이름으로 스칼라를 export하려 하면 bash가 이미 있는 연관 배열의 원소
# 대입으로 처리해버려서 실제 환경변수는 자식 프로세스에 전혀 안 넘어갔다
# (연관 배열은 원래 export 자체가 안 됨). 그 결과 두 로봇이 계속 같은
# (기본) Gazebo Transport 파티션에서 같은 world 이름으로 서로의 토픽/
# 엔티티를 공유하며 충돌했던 것 — Gazebo GUI 창 하나에 로봇이 두 대 같이
# 보이는 것도, 이 세그폴트도 전부 이 버그의 증상이었다. 배열 이름을
# GZ_PART로 바꿔 export가 실제로 먹히게 고친 뒤로는 재현 안 됨(아래
# launch_robot_gazebo()의 크래시 감지+재시도는 혹시 모를 재발 대비용으로
# 남겨둠).
#
# vLLM은 로봇 무관 공유 자원이라 안 건드림 — 따로 이미 떠 있어야 함.

set -eo pipefail

FORSTICK_DIR="/home/asd/forstick"
RUN_DIR="$FORSTICK_DIR/.run"
LOG_DIR="$FORSTICK_DIR/logs"
mkdir -p "$RUN_DIR" "$LOG_DIR"

GZ_SIM_PLUGIN_PATH="/opt/ros/lyrical/lib"
READY_PATTERN="You can start planning now!"

# 로봇별 설정: 이름 / 도메인ID / GZ 파티션 / launch 파일 / API 포트
ROBOTS=(panda ur5e)
declare -A DOMAIN_ID=( [panda]=0 [ur5e]=1 )
# [주의] 이 배열 이름을 GZ_PARTITION으로 지으면 안 됨 — 아래에서
# `export GZ_PARTITION="${GZ_PARTITION[$robot]}"`로 스칼라를 export하려
# 해도, 이미 같은 이름의 연관 배열이 있으면 bash가 배열 원소[0] 대입으로
# 처리해버려서 실제 환경변수 GZ_PARTITION은 자식 프로세스에 전혀 안 넘어감
# (연관 배열은 애초에 export 자체가 안 됨) — 실제로 이 버그로 격리가 통째로
# 안 먹혀서 두 로봇이 같은 Gazebo 파티션(=같은 화면)에 같이 떴었음.
declare -A GZ_PART=( [panda]=forstick_panda [ur5e]=forstick_ur5e )
declare -A LAUNCH_FILE=(
    [panda]="$FORSTICK_DIR/gazebo_robot/panda_gazebo_moveit.launch.py"
    [ur5e]="$FORSTICK_DIR/gazebo_robot/ur5e_robotiq_gazebo.launch.py"
)
declare -A API_PORT=( [panda]=8090 [ur5e]=8091 )
# 로봇별로 화면을 따로 보고 싶다는 요청이 있어서 둘 다 GUI로 띄운다 —
# GZ_PARTITION이 진짜로 격리되면(위 버그 수정 후) 각 창엔 자기 로봇만
# 보여야 정상. 카메라 스트리밍(/gazebo-stream.mjpeg)은 헤드리스에서도
# 되니, WSLg 부하가 부담되면 필요한 쪽만 false로 바꿔도 됨.
declare -A GAZEBO_GUI=( [panda]=true [ur5e]=true )

# ------------------------------------------------------------------
# 0) 기존 스택 정지 (단일 모드 start_robot.sh 잔여 + 이전 듀얼 모드 잔여)
# ------------------------------------------------------------------

stop_by_pidfile() {
    local pidfile="$1" label="$2"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[start_dual] $label 종료 중 (PID $pid)"
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 15); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[start_dual] $label 강제 종료 (PID $pid)"
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pidfile"
    fi
}

echo "[start_dual] === 기존 스택 정지 (단일/듀얼 모드 잔여 전부) ==="
# 단일 모드(start_robot.sh) PID 파일 — 포트 8090 충돌 방지용으로도 정지.
stop_by_pidfile "$RUN_DIR/api_server.pid" "단일모드 API 서버"
stop_by_pidfile "$RUN_DIR/gazebo.pid" "단일모드 Gazebo+MoveIt"
# 듀얼 모드 자신의 PID 파일 (이전 실행분).
for robot in "${ROBOTS[@]}"; do
    stop_by_pidfile "$RUN_DIR/api_${robot}.pid" "${robot} API 서버"
    stop_by_pidfile "$RUN_DIR/gazebo_${robot}.pid" "${robot} Gazebo+MoveIt"
done

# 이름 기반 고아 프로세스 정리 — launch 파일 경로 패턴은 로봇마다 달라서
# 필요하면 한쪽만 정리할 수도 있지만, 여기선 전체 재기동이므로 둘 다 정리.
ORPHAN_PATTERNS=(
    "moveit_ros_move_group/move_group"
    "robot_state_publisher/robot_state_publisher"
    "tf2_ros/static_transform_publisher"
    "ros_gz_bridge/parameter_bridge"
    "ros2 launch .*_gazebo.*\.launch\.py"
    "uvicorn api_server:app"
    "gz-sim-main"
    "gz-sim-gui-client"
    "gz_tools_vendor/bin/gz"
)
for pattern in "${ORPHAN_PATTERNS[@]}"; do
    pkill -f "$pattern" 2>/dev/null && echo "[start_dual] 잔여 프로세스 정리: $pattern" || true
done
sleep 3
for pattern in "${ORPHAN_PATTERNS[@]}"; do
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "[start_dual] 잔여 프로세스 강제 종료: $pattern"
        pkill -9 -f "$pattern" 2>/dev/null || true
    fi
done
sleep 2

# ------------------------------------------------------------------
# 1) 두 로봇의 Gazebo+MoveIt 기동 — 순차적으로, 크래시 시 자동 재시도.
# ------------------------------------------------------------------

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
declare -A GAZEBO_LOG
declare -A API_LOG
SETTLE_SEC=10
MAX_ATTEMPTS=3

# 실패한 시도의 잔여 프로세스만 정리(다른 로봇/이전 성공 스택은 건드리지 않음).
kill_gazebo_attempt() {
    local pid="$1"
    kill -9 "$pid" 2>/dev/null || true
    sleep 1
}

launch_robot_gazebo() {
    local robot="$1" attempt
    for (( attempt=1; attempt<=MAX_ATTEMPTS; attempt++ )); do
        GAZEBO_LOG[$robot]="$LOG_DIR/gazebo_${robot}_${TIMESTAMP}_attempt${attempt}.log"
        echo "[start_dual] === ${robot} Gazebo+MoveIt 기동 시도 ${attempt}/${MAX_ATTEMPTS} (ROS_DOMAIN_ID=${DOMAIN_ID[$robot]}, GZ_PARTITION=${GZ_PART[$robot]}) ==="
        (
            source /opt/ros/lyrical/setup.bash
            export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
            export GZ_SIM_SYSTEM_PLUGIN_PATH="$GZ_SIM_PLUGIN_PATH"
            export ROS_DOMAIN_ID="${DOMAIN_ID[$robot]}"
            export GZ_PARTITION="${GZ_PART[$robot]}"
            exec ros2 launch "${LAUNCH_FILE[$robot]}" "gazebo_gui:=${GAZEBO_GUI[$robot]}"
        ) > "${GAZEBO_LOG[$robot]}" 2>&1 &
        local gazebo_pid=$!
        echo "$gazebo_pid" > "$RUN_DIR/gazebo_${robot}.pid"
        echo "[start_dual] ${robot} Gazebo+MoveIt PID=$gazebo_pid, 로그: ${GAZEBO_LOG[$robot]}"

        echo "[start_dual] ${robot} move_group 준비 대기 중..."
        local crashed=false
        local deadline=$((SECONDS + 90))
        while ! grep -qF "$READY_PATTERN" "${GAZEBO_LOG[$robot]}" 2>/dev/null; do
            if ! kill -0 "$gazebo_pid" 2>/dev/null || grep -qE "Segmentation fault|\[gazebo-1\]: process has died" "${GAZEBO_LOG[$robot]}" 2>/dev/null; then
                echo "[start_dual] ${robot} Gazebo+MoveIt이 기동 중 죽었습니다(시도 ${attempt}). 로그: ${GAZEBO_LOG[$robot]}"
                crashed=true
                break
            fi
            if (( SECONDS > deadline )); then
                echo "[start_dual] ${robot} move_group 준비 대기 타임아웃(90초, 시도 ${attempt}). 로그: ${GAZEBO_LOG[$robot]}"
                crashed=true
                break
            fi
            sleep 2
        done

        if ! $crashed; then
            echo "[start_dual] ${robot} move_group 준비 완료, 컨트롤러 안정화 대기 ${SETTLE_SEC}초..."
            sleep "$SETTLE_SEC"
            # move_group이 준비됐다고 뜬 뒤에도 뒤늦게 죽는 경우가 있어(위 파일
            # 상단 주석의 GZ_PARTITION 버그 참고, 지금은 고쳐짐) 안정화 대기
            # 후 한 번 더 확인.
            if ! kill -0 "$gazebo_pid" 2>/dev/null || grep -qE "Segmentation fault|\[gazebo-1\]: process has died" "${GAZEBO_LOG[$robot]}" 2>/dev/null; then
                echo "[start_dual] ${robot} Gazebo+MoveIt이 안정화 대기 중 죽었습니다(시도 ${attempt}). 로그: ${GAZEBO_LOG[$robot]}"
                crashed=true
            fi
        fi

        if ! $crashed; then
            echo "[start_dual] ${robot} Gazebo+MoveIt 기동 성공 (시도 ${attempt}/${MAX_ATTEMPTS})"
            return 0
        fi

        kill_gazebo_attempt "$gazebo_pid"
        if (( attempt == MAX_ATTEMPTS )); then
            echo "[start_dual] 오류: ${robot} Gazebo+MoveIt이 ${MAX_ATTEMPTS}번 시도 모두 실패했습니다. 마지막 로그: ${GAZEBO_LOG[$robot]}" >&2
            return 1
        fi
        echo "[start_dual] ${robot} 재시도 전 정리 중..."
        sleep 3
    done
}

for robot in "${ROBOTS[@]}"; do
    launch_robot_gazebo "$robot" || exit 1
done

# ------------------------------------------------------------------
# 2) 두 로봇의 API 서버 기동 (병렬, 각자 다른 포트)
# ------------------------------------------------------------------

for robot in "${ROBOTS[@]}"; do
    API_LOG[$robot]="$LOG_DIR/api_server_${robot}_${TIMESTAMP}.log"
    echo "[start_dual] === ${robot} API 서버 기동 (port=${API_PORT[$robot]}) ==="
    (
        cd "$FORSTICK_DIR"
        source venv/bin/activate
        source /opt/ros/lyrical/setup.bash
        export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
        export ROS_DOMAIN_ID="${DOMAIN_ID[$robot]}"
        export GZ_PARTITION="${GZ_PART[$robot]}"
        export FORSTICK_ROBOT="$robot"
        exec uvicorn api_server:app --host 0.0.0.0 --port "${API_PORT[$robot]}"
    ) > "${API_LOG[$robot]}" 2>&1 &
    API_PID=$!
    echo "$API_PID" > "$RUN_DIR/api_${robot}.pid"
    echo "[start_dual] ${robot} API 서버 PID=$API_PID, 로그: ${API_LOG[$robot]}"
done

for robot in "${ROBOTS[@]}"; do
    echo "[start_dual] ${robot} API 서버 기동 대기 중..."
    API_PID="$(cat "$RUN_DIR/api_${robot}.pid")"
    DEADLINE=$((SECONDS + 60))
    while ! grep -qE "Application startup complete|Traceback|RuntimeError" "${API_LOG[$robot]}" 2>/dev/null; do
        if ! kill -0 "$API_PID" 2>/dev/null; then
            echo "[start_dual] 오류: ${robot} API 서버 프로세스가 중간에 죽었습니다. 로그 확인: ${API_LOG[$robot]}" >&2
            exit 1
        fi
        if (( SECONDS > DEADLINE )); then
            echo "[start_dual] 오류: ${robot} API 서버 기동 대기 타임아웃(60초). 로그 확인: ${API_LOG[$robot]}" >&2
            exit 1
        fi
        sleep 2
    done
    if grep -qE "Traceback|RuntimeError" "${API_LOG[$robot]}"; then
        echo "[start_dual] 오류: ${robot} API 서버가 기동 중 실패했습니다. 로그 확인: ${API_LOG[$robot]}" >&2
        tail -20 "${API_LOG[$robot]}" >&2
        exit 1
    fi
done

echo "[start_dual] === 완료 ==="
sleep 1
echo "[start_dual] Panda: $(curl -s http://localhost:${API_PORT[panda]}/health)"
echo "[start_dual] UR5e : $(curl -s http://localhost:${API_PORT[ur5e]}/health)"
echo "[start_dual] http://localhost:${API_PORT[panda]}/console (Panda), http://localhost:${API_PORT[ur5e]}/console (UR5e) — vLLM은 건드리지 않았으니 이미 떠 있어야 합니다."
