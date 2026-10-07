#!/usr/bin/env bash
# FR3-WMS 관절 한계 시험 — PC1에서 실행.
#
# 돌고 있는 작업 셀(파티션 forstick2_fr3_workcell, ROS 도메인 44)은 건드리지 않는다.
# 저장소의 팔 단독 실행 스크립트(scripts/run_gazebo_fr3.sh, 파티션 forstick2_fr3, 도메인 42)로
# 따로 띄우고, 시험이 끝나면(실패·Ctrl+C 포함) 이 스크립트가 띄운 프로세스만 끈다.
#
# 사용: bash run_joint_limit_test.sh <forstick 저장소 경로>
#   예) bash run_joint_limit_test.sh ~/forstick2
# 외부 FAIRINO 저장소 경로가 기본값(/home/asd/external/frcobot_ros2)과 다르면 FORSTICK2_FR3_REPO로 준다.
set -eo pipefail

REPO="${1:?forstick 저장소 경로를 인자로 주세요}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
export GZ_PARTITION=forstick2_fr3 ROS_DOMAIN_ID=42
LOG="$(mktemp -d /tmp/forstick_limit_test.XXXX)"
export FORSTICK2_GZ_LOG_DIR="$LOG"

source /opt/ros/lyrical/setup.bash
[[ -f "$URDF" ]] || { echo "URDF가 없다: $URDF" >&2; exit 3; }
echo "URDF sha256: $(sha256sum "$URDF" | cut -c1-12)  (프로젝트 기록: 73d01435d7be)"

# 같은 도메인·파티션에 이미 뭔가 떠 있으면 멈춘다(남의 시험을 끄지 않기 위해).
if [[ -n "$(ros2 node list --no-daemon 2>/dev/null)" ]] || [[ -n "$(timeout 5 gz topic -l 2>/dev/null)" ]]; then
  echo "도메인 42 / 파티션 forstick2_fr3에 이미 실행 중인 것이 있다 — 확인 후 다시 실행" >&2
  exit 4
fi

cleanup() {
  echo "[정리] 시험용 프로세스 종료"
  for pid in $(grep -oE 'pid=[0-9]+' "$LOG/launch.out" 2>/dev/null | cut -d= -f2); do
    kill "$pid" 2>/dev/null || true
  done
  # gz sim은 서버·GUI 자식 프로세스를 따로 띄운다. 이 파티션 환경을 가진 것만 끈다.
  for pid in $(pgrep -f 'gz sim' || true); do
    if tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qx 'GZ_PARTITION=forstick2_fr3'; then
      kill "$pid" 2>/dev/null || true
    fi
  done
}
# 프로세스를 먼저 끄고(pid는 로그에서 읽는다) 그다음 로그를 지운다.
# 성공하면 로그도 지우고(결과 표는 화면에 남는다), 실패하면 원인을 보도록 남긴다.
finish() {
  local rc=$?
  cleanup
  if [[ $rc -eq 0 ]]; then rm -rf "$LOG"; echo "[정리] 시험 로그 삭제함 — 위 표를 복사해 보내주세요"
  else echo "[정리] 실패(종료 코드 $rc) — 로그를 남겨 둠: $LOG"; fi
  exit $rc
}
trap finish EXIT

echo "[1/2] 팔 단독 Gazebo 실행 (로그: $LOG)"
bash "$REPO/scripts/run_gazebo_fr3.sh" | tee "$LOG/launch.out"

echo "[2/2] 관절 한계 시험"
python3 "$HERE/joint_limit_probe.py" "$URDF" | tee "$LOG/result.txt"
