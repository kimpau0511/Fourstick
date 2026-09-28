# forstick2 공통 실행 환경. 실행 스크립트가 `source`한다(직접 실행하지 않는다).
#
# 우선순위: 이미 설정된 환경변수 → config/local.env(사용자 PC 전용, Git 제외) → 프로젝트 상대 기본값.
# 파이썬 쪽은 core/paths.py가 같은 규칙을 쓴다. 개발자 개인 경로를 여기 두지 않는다.
FORSTICK2_ROOT="${FORSTICK2_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
if [[ -f "$FORSTICK2_ROOT/config/local.env" ]]; then
  # 이미 export된 값은 덮지 않는다.
  while IFS='=' read -r _k _v; do
    _k="${_k#export }"; _k="${_k// /}"
    [[ -z "$_k" || "$_k" == \#* ]] && continue
    if [[ -z "${!_k:-}" ]]; then
      _v="${_v%\"}"; _v="${_v#\"}"; _v="${_v%\'}"; _v="${_v#\'}"
      export "$_k=$(eval echo "$_v")"
    fi
  done < <(grep -E '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$FORSTICK2_ROOT/config/local.env")
fi
export FORSTICK2_THIRD_PARTY="${FORSTICK2_THIRD_PARTY:-$FORSTICK2_ROOT/third_party}"
export FORSTICK2_FR3_REPO="${FORSTICK2_FR3_REPO:-$FORSTICK2_THIRD_PARTY/frcobot_ros2}"
export FORSTICK2_ROBOTIQ_REPO="${FORSTICK2_ROBOTIQ_REPO:-$FORSTICK2_THIRD_PARTY/robotiq_ros}"
export FORSTICK2_ROS_SETUP="${FORSTICK2_ROS_SETUP:-/opt/ros/lyrical/setup.bash}"
export FORSTICK2_WORKCELL_LOG_DIR="${FORSTICK2_WORKCELL_LOG_DIR:-/tmp/forstick2_workcell}"
export FORSTICK2_GZ_LOG_DIR="${FORSTICK2_GZ_LOG_DIR:-/tmp/forstick2_gazebo}"
export FORSTICK2_VENV="${FORSTICK2_VENV:-$FORSTICK2_ROOT/.venv}"

# ROS 환경(한 번만). gz_ros2_control 플러그인은 ROS lib에 있다 — 개발 PC는 ~/.bashrc가 넣어 주던 값이다.
forstick2_source_ros() {
  if [[ -z "${AMENT_PREFIX_PATH:-}" ]]; then
    if [[ ! -f "$FORSTICK2_ROS_SETUP" ]]; then
      echo "[forstick2] ROS 환경 파일이 없다: $FORSTICK2_ROS_SETUP (docs/SETUP.md 1단계)" >&2
      return 1
    fi
    # ROS setup.bash는 set -u(nounset)에서 죽는다 — 잠시 끈다.
    local had_u=0
    [[ $- == *u* ]] && had_u=1 && set +u
    # shellcheck disable=SC1090
    source "$FORSTICK2_ROS_SETUP"
    [[ $had_u -eq 1 ]] && set -u
  fi
  local ros_lib
  ros_lib="$(dirname "$FORSTICK2_ROS_SETUP")/lib"
  case ":${GZ_SIM_SYSTEM_PLUGIN_PATH:-}:" in
    *":$ros_lib:"*) ;;
    *) export GZ_SIM_SYSTEM_PLUGIN_PATH="$ros_lib${GZ_SIM_SYSTEM_PLUGIN_PATH:+:$GZ_SIM_SYSTEM_PLUGIN_PATH}" ;;
  esac
}
