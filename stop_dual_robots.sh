#!/usr/bin/env bash
# start_dual_robots.sh로 띄운 Panda+UR5e 스택을 둘 다 내리는 스크립트.
#
#   ./stop_dual_robots.sh
#
# vLLM은 로봇 무관 공유 자원이라 안 건드림.

set -eo pipefail

FORSTICK_DIR="/home/asd/forstick"
RUN_DIR="$FORSTICK_DIR/.run"

ROBOTS=(panda ur5e)

stop_by_pidfile() {
    local pidfile="$1" label="$2"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid="$(cat "$pidfile")"
        if kill -0 "$pid" 2>/dev/null; then
            echo "[stop_dual] $label 종료 중 (PID $pid)"
            kill "$pid" 2>/dev/null || true
            for _ in $(seq 1 15); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            if kill -0 "$pid" 2>/dev/null; then
                echo "[stop_dual] $label 강제 종료 (PID $pid)"
                kill -9 "$pid" 2>/dev/null || true
            fi
        fi
        rm -f "$pidfile"
    fi
}

echo "[stop_dual] === 듀얼 로봇 스택 정지 ==="
for robot in "${ROBOTS[@]}"; do
    stop_by_pidfile "$RUN_DIR/api_${robot}.pid" "${robot} API 서버"
    stop_by_pidfile "$RUN_DIR/gazebo_${robot}.pid" "${robot} Gazebo+MoveIt"
done

# ros2 launch가 종료돼도 남는 고아 자식(gz-sim-main 등)은 이름으로 한 번 더 정리.
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
    pkill -f "$pattern" 2>/dev/null && echo "[stop_dual] 잔여 프로세스 정리: $pattern" || true
done
sleep 3
for pattern in "${ORPHAN_PATTERNS[@]}"; do
    if pgrep -f "$pattern" > /dev/null 2>&1; then
        echo "[stop_dual] 잔여 프로세스 강제 종료: $pattern"
        pkill -9 -f "$pattern" 2>/dev/null || true
    fi
done

echo "[stop_dual] === 완료 ==="
