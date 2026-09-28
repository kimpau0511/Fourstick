#!/usr/bin/env bash
# FR3 작업 셀 사전 점검. 아무것도 설치·실행·종료하지 않는다(읽기만).
#
#   ./scripts/doctor.sh          # 필수 항목이 하나라도 실패하면 종료 코드 1
#
# 결과: OK(통과) · WARN(선택 기능이 빠짐 — FR3 기본 실행은 된다) · FAIL(FR3 실행이 막힘)
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
FAILS=0
ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }

echo "[doctor] 저장소: $FORSTICK2_ROOT"
. /etc/os-release
[[ "${VERSION_ID:-}" == "26.04" ]] && ok "OS Ubuntu $VERSION_ID" || warn "OS ${PRETTY_NAME:-?} — 확인된 환경은 Ubuntu 26.04"
grep -qi microsoft /proc/version 2>/dev/null && ok "WSL2(개발 PC와 같은 환경)" || ok "리눅스 네이티브(WSL2가 아님 — 개발 PC와 다름, 미검증)"

if forstick2_source_ros 2>/dev/null; then ok "ROS: $FORSTICK2_ROS_SETUP (ROS_DISTRO=${ROS_DISTRO:-?})"; else bad "ROS 환경 파일 없음: $FORSTICK2_ROS_SETUP"; fi
command -v gz >/dev/null && ok "gz: $(gz sim --version 2>/dev/null | head -1)" || bad "gz 명령 없음(ros-lyrical-ros-gz)"
for pkg in ros_gz_sim ros_gz_bridge gz_ros2_control controller_manager joint_trajectory_controller \
           moveit_ros_move_group moveit_setup_assistant xacro robot_state_publisher; do
  ros2 pkg prefix "$pkg" >/dev/null 2>&1 && ok "ROS 패키지 $pkg" || bad "ROS 패키지 없음: $pkg (scripts/setup/install_system.sh)"
done
plugin="$(tr ':' '\n' <<<"${GZ_SIM_SYSTEM_PLUGIN_PATH:-}" | while read -r d; do ls "$d"/libgz_ros2_control-system.so 2>/dev/null; done | head -1)"
[[ -n "$plugin" ]] && ok "gz_ros2_control 플러그인: $plugin" || bad "gz_ros2_control 플러그인을 GZ_SIM_SYSTEM_PLUGIN_PATH에서 못 찾음"

if [[ -x "$FORSTICK2_VENV/bin/python" ]]; then
  miss="$("$FORSTICK2_VENV/bin/python" - <<'PY'
import importlib
out = []
for m in ("uvicorn", "wsproto", "numpy", "pydantic", "yaml", "rclpy", "gz.transport", "gz.msgs.pose_v_pb2"):
    try:
        importlib.import_module(m)
    except Exception as exc:  # noqa: BLE001
        out.append(f"{m}({type(exc).__name__})")
print(" ".join(out))
PY
)"
  [[ -z "$miss" ]] && ok "웹 가상환경 $FORSTICK2_VENV — 필수 모듈 있음" || bad "웹 가상환경 모듈 없음: $miss"
  "$FORSTICK2_VENV/bin/python" -c "import faster_whisper" 2>/dev/null \
    && ok "STT(faster-whisper) 설치됨" || warn "STT 없음 — 음성 입력만 꺼진다(FORSTICK2_STT=0으로 띄울 수 있다)"
else
  bad "웹 가상환경 없음: $FORSTICK2_VENV (./scripts/setup/create_venv.sh)"
fi

if "$ROOT/scripts/setup/fetch_assets.sh" --verify-only >/tmp/forstick2_doctor_assets.log 2>&1; then
  ok "외부 자산(FR3·Robotiq) 지정 commit 확인"
else
  bad "외부 자산: $(grep -m1 '실패' /tmp/forstick2_doctor_assets.log || tail -1 /tmp/forstick2_doctor_assets.log)"
fi
for f in config/workcell/active.json config/workcell/fr3_2f85_workcell.json config/workcell/fr3_2f85_workcell_poses.json \
         config/evidence/fr3_2f85_workcell.srdf config/evidence/loop_closure_pairs.json; do
  [[ -f "$ROOT/$f" ]] && ok "설정 $f" || bad "설정 없음: $f"
done

SMI="$(command -v nvidia-smi || true)"
[[ -z "$SMI" && -x /usr/lib/wsl/lib/nvidia-smi ]] && SMI=/usr/lib/wsl/lib/nvidia-smi   # WSL2 위치
if [[ -n "$SMI" ]] && "$SMI" >/dev/null 2>&1; then
  ok "GPU: $("$SMI" --query-gpu=name,memory.used,memory.total --format=csv,noheader | head -1)"
else
  warn "NVIDIA GPU 없음 — Gazebo 장면 카메라 렌더링(ogre2)이 느리거나 실패할 수 있고 vLLM은 못 쓴다(미검증)"
fi
mem="$(free -m | awk 'NR==2{print $7}')"
[[ "${mem:-0}" -ge 6000 ]] && ok "사용 가능 메모리 ${mem} MB" || warn "사용 가능 메모리 ${mem} MB — 6 GB 미만이면 Gazebo+MoveIt+웹+STT가 느릴 수 있다"
port="${FORSTICK2_PORT:-8092}"
ss -tln 2>/dev/null | grep -q ":$port " && warn "웹 포트 $port 사용 중(FORSTICK2_PORT로 바꾼다)" || ok "웹 포트 $port 비어 있음"
if curl -s -m 3 "http://127.0.0.1:${FORSTICK2_VLLM_PORT:-8000}/v1/models" | grep -q qwen3-8b-awq; then
  ok "vLLM qwen3-8b-awq 응답(:${FORSTICK2_VLLM_PORT:-8000})"
else
  warn "vLLM 없음 — 정확한 시연 명령·확인·STOP은 되고, 모호한 문장 해석·/v1/plan 계획 생성은 되묻기/불가"
fi
echo "[doctor] GZ_PARTITION=${GZ_PARTITION:-forstick2_fr3_workcell} ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-44} 로그=$FORSTICK2_WORKCELL_LOG_DIR"
if [[ "$FAILS" -gt 0 ]]; then echo "[doctor] FAIL ${FAILS}건 — 위 항목을 먼저 해결한다(docs/SETUP.md)"; exit 1; fi
echo "[doctor] 필수 항목 통과"
