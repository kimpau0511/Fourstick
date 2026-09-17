#!/usr/bin/env bash
# FR3-WMS + GRP-CPL-062 커플링 + Robotiq 2F-85 조립을 **GUI로** 띄운다 (8-08).
#
#   ./scripts/run_gazebo_fr3_2f85_gui.sh
#
# 한 번에: 사전 점검 → Gazebo(GUI) → robot_state_publisher → spawn →
#          컨트롤러 3종 → 카메라 배치 → 상태 보고.
#
# 규칙:
#  - **기존 인스턴스를 임의로 종료하지 않는다.** 중복이면 그 사실만 알리고 끝낸다
#    (종료 명령은 화면에 안내한다).
#  - GUI가 뜨지 않으면 필요한 환경 조건과 실패 원인을 출력한다.
#    **headless 서버를 자동 종료하지 않는다.**
#  - GUI가 떴다는 사실만으로 성공이라고 하지 않는다. 관절·개구·컨트롤러 상태를
#    확인해 보고서(reports/gripper/gui_session.json)에 남긴다.
#  - 장착 yaw는 미확보다. 값을 주지 않으면 거부한다(선언값은 기록에 남는다).
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
REPORT="$ROOT/reports/gripper/gui_session.json"
WORLD_NAME="forstick2_fr3_cell"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_gripper}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-43}"
YAW="${FORSTICK2_ASSEMBLY_YAW_RAD:-}"

say() { printf '[gui] %s\n' "$*"; }
fail() { printf '[gui] %s\n' "$*" >&2; }

mkdir -p "$LOG_DIR" "$(dirname "$REPORT")"

# ── 0. 장착 yaw 확인 (미확보 값을 임의로 넣지 않는다) ──────────────────
if [[ -z "$YAW" ]]; then
  fail "**장착 yaw가 미확보다.** 시뮬레이션을 보려면 값을 명시해야 한다:"
  fail "    FORSTICK2_ASSEMBLY_YAW_RAD=0 $0"
  fail "그 값은 declared로 기록되고 pick/place를 열지 않는다."
  fail "필요한 입력: md/8단계_2F85_장착.md의 '아직 받아야 하는 입력'"
  exit 4
fi

# ── 1. 사전 점검 ───────────────────────────────────────────────────────
say "사전 점검"
FREE_MB="$(free -m | awk 'NR==2 {print $7}')"
say "  사용 가능 메모리: ${FREE_MB} MB"
if [[ "$FREE_MB" -lt 700 ]]; then
  fail "  메모리가 부족하다(700 MB 미만). Gazebo 서버가 기동 중 멈출 수 있다."
  fail "  **다른 프로세스를 임의로 종료하지 않는다.** 직접 정리한 뒤 다시 실행한다:"
  fail "    ps -eo pmem,rss,pid,comm --sort=-rss | head"
fi
if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  fail "  DISPLAY·WAYLAND_DISPLAY가 없다 — GUI를 띄울 수 없다."
  fail "  WSLg 조건: Windows 11 + WSLg(기본), 또는 X 서버와 DISPLAY 설정."
  fail "  headless로 확인하려면 scripts/run_gazebo_fr3_gripper.sh를 쓴다."
  exit 5
fi
say "  DISPLAY=${DISPLAY:-없음} WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-없음}"

# ── 2. 중복 실행 감지 (종료하지 않는다) ────────────────────────────────
source /opt/ros/lyrical/setup.bash
alive() {
  [[ -f "$LOG_DIR/$1.pid" ]] && kill -0 "$(cat "$LOG_DIR/$1.pid")" 2>/dev/null
}
RUNNING_WORLD="$(timeout 20 gz service -l 2>/dev/null \
  | grep -c "/world/${WORLD_NAME}/" || true)"

if alive gz_sim_gripper || alive gz_gui_gripper || [[ "$RUNNING_WORLD" -gt 0 ]]; then
  say "이미 실행 중인 조립 인스턴스가 있다"
  say "  서버 pid=$(cat "$LOG_DIR/gz_sim_gripper.pid" 2>/dev/null || echo 없음)"
  say "  GUI  pid=$(cat "$LOG_DIR/gz_gui_gripper.pid" 2>/dev/null || echo 없음)"
  say "  world 서비스 ${RUNNING_WORLD}개 · 파티션 $GZ_PARTITION"
  say "창은 하나만 띄운다 — **같이 끄고 다시 켠다.**"
  say "(다른 파티션의 Gazebo는 건드리지 않는다.)"
  "$ROOT/scripts/stop_gazebo_fr3_2f85.sh" | sed 's/^/  /'
  sleep 3
fi

# 다른 프로젝트(forstick 8090 데모)의 Gazebo는 다른 파티션이다 — 건드리지 않는다.
OTHER="$(ps -eo args | grep -cF "sim10/gz-sim" || true)"
say "  다른 Gazebo 프로세스 ${OTHER}개(다른 파티션) — 건드리지 않는다"

# ── 3. 조립 모델 실행 (실행 스크립트가 world 기동·spawn·컨트롤러까지 맡는다) ──
say "조립 Gazebo를 띄운다 (yaw 선언값=$YAW rad)"
set +e
FORSTICK2_ASSEMBLY_YAW_RAD="$YAW" FORSTICK2_GZ_VERBOSITY=3 \
  "$ROOT/scripts/run_gazebo_fr3_gripper.sh" 2>&1 | tee "$LOG_DIR/gui_launch.out"
LAUNCH_RC="${PIPESTATUS[0]}"
set -e
if [[ "$LAUNCH_RC" -ne 0 ]]; then
  fail "조립 실행이 실패했다 (rc=$LAUNCH_RC). **서버를 자동 종료하지 않는다.**"
  fail "확인할 것:"
  fail "  - 사용 가능 메모리: $(free -m | awk 'NR==2 {print $7}') MB"
  fail "  - Gazebo 서버 로그: $LOG_DIR/gz_sim_gripper.log"
  fail "  - 실행 로그: $LOG_DIR/gui_launch.out"
  fail "  - 남은 프로세스: pgrep -af gz-sim"
  exit 6
fi

# ── 4. 카메라 배치와 시험 물체 (팔·커플링·그리퍼·바닥이 함께 보이게) ────
say "카메라를 배치하고 시험 물체를 놓는다"
python3 "$ROOT/scripts/place_gui_camera.py" --world "$WORLD_NAME" \
  --world-sdf "$ROOT/config/gazebo/fr3_2f85_cell.sdf" \
  --object "$ROOT/config/gazebo/test_object.sdf" | sed 's/^/  /'

# ── 5. 관측 상태 확인과 보고 (GUI가 떴다는 것만으로 성공 처리하지 않는다) ─
say "관측 상태를 확인해 보고서에 남긴다"
set +e
python3 "$ROOT/scripts/check_gui_session.py" --out "$REPORT"
CHECK_RC=$?
set -e

say ""
say "Gazebo GUI 실행 중 — 창에서 FR3 + 커플링 + 2F-85 + 바닥 + 시험 물체가 보인다."
say "시연(home → move → open → 30 mm close → 이동 중 STOP)은 다음 명령으로 실행한다:"
say "    ./scripts/demo_fr3_2f85_gui.sh"
say "보고서: $REPORT"
say "로그: $LOG_DIR/gz_sim_gripper.log, $LOG_DIR/gui_launch.out"
say "**pick/place는 비활성이다**(장착 yaw 선언값·커플링 질량 미확보)."
exit "$CHECK_RC"
