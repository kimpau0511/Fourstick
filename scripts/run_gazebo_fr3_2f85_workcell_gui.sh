#!/usr/bin/env bash
# FR3-WMS + GRP-CPL-062 + Robotiq 2F-85 **작업 셀** Gazebo 실행 (8-08 우선순위 1·4).
#
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh --urdf-only
#
# 격리:
#   world      forstick2_fr3_2f85_workcell   (config/gazebo/fr3_2f85_workcell.sdf)
#   partition  forstick2_fr3_workcell
#   domain     44
# arm-only(forstick2_fr3 / 42), 단순 조립 셀(forstick2_fr3_gripper / 43),
# forstick 8090 데모와 **모두 다르다.** 다른 파티션의 Gazebo는 건드리지 않는다.
#
# 서버와 GUI를 분리해 띄운다. GUI의 X11 연결이 끊겨도 world와
# controller_manager가 남는다(실측 근거: md/8단계_2F85_장착.md 9.2).
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FR3_REPO="${FORSTICK2_FR3_REPO:-/home/asd/external/frcobot_ros2}"
FR3_URDF="$FR3_REPO/fairino_description/urdf/FR3WMS.urdf"
ROBOTIQ_REPO="${FORSTICK2_ROBOTIQ_REPO:-/home/asd/external/robotiq_ros}"
ROBOTIQ_DESC="$ROBOTIQ_REPO/grippers/robotiq_description"
WORKCELL_JSON="$ROOT/config/workcell/fr3_2f85_workcell.json"
WORLD="$ROOT/config/gazebo/fr3_2f85_workcell.sdf"
XACRO_FILE="$ROOT/config/gazebo/fr3wms_with_2f85.urdf.xacro"
ARM_CONTROLLERS="$ROOT/config/gazebo/fr3wms_controllers.yaml"
GRIPPER_CONTROLLERS="$ROOT/config/gazebo/fr3wms_gripper_controllers.yaml"
MOUNTING="$ROOT/config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
LOG_DIR="${FORSTICK2_WORKCELL_LOG_DIR:-/tmp/forstick2_workcell}"
OVERLAY="/tmp/forstick2_gazebo/overlay"
URDF_DIR="$LOG_DIR/workcell"
WORLD_NAME="forstick2_fr3_2f85_workcell"
MODEL_NAME="fr3wms_2f85_workcell"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"

URDF_ONLY=0
[[ "${1:-}" == "--urdf-only" ]] && URDF_ONLY=1

say() { printf '[workcell] %s\n' "$*"; }
fail() { printf '[workcell] %s\n' "$*" >&2; }

# ── 0. 장착 yaw (미확보 값을 임의로 넣지 않는다) ───────────────────────
if [[ -z "${FORSTICK2_ASSEMBLY_YAW_RAD:-}" ]]; then
  fail "**장착 yaw가 미확보다.** 시뮬레이션을 돌리려면 명시적으로 선언한다:"
  fail "    FORSTICK2_ASSEMBLY_YAW_RAD=0 $0"
  fail "그 값은 declared로 기록되고 pick/place를 열지 않는다."
  fail "필요한 입력: config/mounting/grp_cpl_062_inputs.json의 mounting_yaw_rad"
  exit 4
fi

for path in "$FR3_URDF" "$ROBOTIQ_DESC/urdf/robotiq_2f_85_macro.urdf.xacro" \
            "$ROBOTIQ_DESC/urdf/ur_to_robotiq_adapter.urdf.xacro" \
            "$WORKCELL_JSON"; do
  if [[ ! -f "$path" ]]; then
    fail "자산이 없다: $path"
    fail "asset.missing — 없는 자산을 대체하지 않는다."
    exit 3
  fi
done

mkdir -p "$LOG_DIR" "$URDF_DIR"
source /opt/ros/lyrical/setup.bash

alive() { [[ -f "$LOG_DIR/$1.pid" ]] && kill -0 "$(cat "$LOG_DIR/$1.pid")" 2>/dev/null; }

# ── 1. world SDF를 설정에서 생성한다(설정과 world가 어긋나지 않게) ──────
python3 "$ROOT/scripts/build_workcell_world.py" | sed 's/^/  /'

# ── 2. 로봇 설명 생성 ──────────────────────────────────────────────────
# robotiq_description 오버레이(단순 조립 셀과 공유한다 — 읽기만 한다).
mkdir -p "$OVERLAY/share/ament_index/resource_index/packages" \
         "$OVERLAY/share/robotiq_description"
touch "$OVERLAY/share/ament_index/resource_index/packages/robotiq_description"
for dir in urdf meshes config launch; do
  ln -sfn "$ROBOTIQ_DESC/$dir" "$OVERLAY/share/robotiq_description/$dir"
done
export AMENT_PREFIX_PATH="$OVERLAY:${AMENT_PREFIX_PATH:-}"
export GZ_SIM_RESOURCE_PATH="${GZ_SIM_RESOURCE_PATH:-}:$FR3_REPO:$OVERLAY/share"
export ROS_PACKAGE_PATH="${ROS_PACKAGE_PATH:-}:$FR3_REPO:$OVERLAY/share"
# ogre2가 쓰는 EGL은 소프트웨어 렌더링 강제를 거부한다(~/.bashrc가 걸어 둔다).
if [[ "${FORSTICK2_GZ_FORCE_SOFTWARE_GL:-0}" == "1" ]]; then
  export LIBGL_ALWAYS_SOFTWARE=1
else
  export LIBGL_ALWAYS_SOFTWARE=0
fi
# ogre2는 GLX를 쓴다. wayland 네이티브로는 렌더 창 생성이 실패한다(실측).
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
# 상대 메시(../meshes/...)가 URDF 옆에서 풀리도록 링크를 둔다.
ln -sfn "$FR3_REPO/fairino_description/meshes" "$LOG_DIR/meshes"

# 받침대 상판 높이를 설정에서 읽는다. 스크립트에 수치를 두지 않는다.
BASE_XYZ="$(python3 - "$WORKCELL_JSON" <<'PYGEN'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
frames = data["frames"]
name, out = "fr3_base_frame", [0.0, 0.0, 0.0]
while name is not None:
    frame = frames[name]
    out = [a + b for a, b in zip(out, frame["xyz_m"])]
    name = frame["parent"]
print(" ".join(str(round(v, 6)) for v in out))
PYGEN
)"
TCP_XYZ="$(python3 - "$MOUNTING" <<'PYGEN'
import json, sys
profile = json.load(open(sys.argv[1], encoding="utf-8"))
values = [item["value"] for item in profile["tcp_xyz"]]
print("MISSING" if any(v is None for v in values)
      else " ".join(str(v) for v in values))
PYGEN
)"
if [[ "$TCP_XYZ" == "MISSING" ]]; then
  fail "TCP 오프셋이 Profile에 없다 — 0으로 대체하지 않는다."
  exit 5
fi

URDF_OUT="$URDF_DIR/fr3wms_with_2f85.urdf"
ros2 run xacro xacro "$XACRO_FILE" \
  "fr3_urdf:=$FR3_URDF" \
  "arm_controller_yaml:=$ARM_CONTROLLERS" \
  "gripper_controller_yaml:=$GRIPPER_CONTROLLERS" \
  "gripper_yaw_rad:=$FORSTICK2_ASSEMBLY_YAW_RAD" \
  "tcp_xyz:=$TCP_XYZ" \
  "base_xyz:=$BASE_XYZ" > "$URDF_OUT"
say "URDF 생성: $URDF_OUT ($(wc -c < "$URDF_OUT") bytes)"
say "base_xyz = $BASE_XYZ (받침대 상판) · TCP = $TCP_XYZ · yaw(선언) = $FORSTICK2_ASSEMBLY_YAW_RAD"

# visual과 collision 메시를 **둘 다** 확인한다. 하나라도 풀리지 않으면 멈춘다.
python3 - "$URDF_OUT" "$OVERLAY/share/robotiq_description" <<'PYMESH' || exit 7
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

urdf, overlay = Path(sys.argv[1]), sys.argv[2]
base = urdf.parent
missing, counted = [], {"visual": 0, "collision": 0}
root = ET.parse(urdf).getroot()
for link in root.findall("link"):
    for tag in ("visual", "collision"):
        for node in link.findall(tag):
            for mesh in node.iter("mesh"):
                name = mesh.get("filename", "")
                counted[tag] += 1
                if name.startswith("package://robotiq_description"):
                    path = Path(name.replace("package://robotiq_description", overlay))
                elif name.startswith("file://"):
                    path = Path(name[len("file://"):])
                elif name.startswith(("model://", "/")):
                    continue
                else:
                    path = base / name
                if not path.is_file():
                    missing.append(f"{link.get('name')}/{tag}: {path}")
for item in missing:
    print(f"[workcell] 메시를 풀 수 없다: {item}", file=sys.stderr)
if missing:
    print(f"[workcell] 메시 {len(missing)}건이 풀리지 않는다."
          " asset.missing — 형상 없이 띄우지 않는다.", file=sys.stderr)
    raise SystemExit(1)
print(f"[workcell] 메시 확인 완료 — visual {counted['visual']}건 ·"
      f" collision {counted['collision']}건")
PYMESH

# MoveIt용 URDF: 상대 메시 경로를 절대 file:// 로 바꾼다(geometric_shapes가
# 상대경로에서 죽는다 — 8-07 실측).
python3 - "$URDF_OUT" "$FR3_REPO/fairino_description" \
         "$URDF_DIR/fr3wms_with_2f85.moveit.urdf" <<'PYGEN'
import pathlib, sys
source, package, target = sys.argv[1:4]
text = pathlib.Path(source).read_text(encoding="utf-8")
count = text.count('filename="../meshes/')
text = text.replace('filename="../meshes/', f'filename="file://{package}/meshes/')
pathlib.Path(target).write_text(text, encoding="utf-8")
print(f"[workcell] MoveIt용 메시 URI 치환 {count}건 -> {target}")
PYGEN

# derive_workcell_poses.py가 /tmp/forstick2_gazebo/workcell/ 를 본다.
mkdir -p /tmp/forstick2_gazebo/workcell
ln -sfn "$URDF_DIR/fr3wms_with_2f85.moveit.urdf" \
        /tmp/forstick2_gazebo/workcell/fr3wms_with_2f85.moveit.urdf

if [[ "$URDF_ONLY" -eq 1 ]]; then
  say "--urdf-only: Gazebo를 띄우지 않고 끝낸다."
  exit 0
fi

# ── 3. 중복 실행: 이 셀 인스턴스만 같이 끄고 다시 켠다 ─────────────────
RUNNING="$(timeout 40 gz service -l 2>/dev/null | grep -c "/world/$WORLD_NAME/" || true)"
if alive gz_server || alive gz_gui || [[ "$RUNNING" -gt 0 ]]; then
  say "이미 실행 중인 작업 셀 인스턴스가 있다 — 같이 끄고 다시 켠다"
  "$ROOT/scripts/stop_gazebo_workcell.sh" | sed 's/^/  /'
  sleep 3
fi
OTHER="$(ps -eo args | grep -cF "sim10/gz-sim" || true)"
say "다른 Gazebo 프로세스 ${OTHER}개(다른 파티션) — 건드리지 않는다"

# ── 4. 서버 ────────────────────────────────────────────────────────────
start() {
  local name="$1"; shift
  nohup "$@" > "$LOG_DIR/$name.log" 2>&1 &
  echo "$!" > "$LOG_DIR/$name.pid"
  say "$name pid=$! log=$LOG_DIR/$name.log"
}
VERBOSITY="${FORSTICK2_GZ_VERBOSITY:-3}"
GZ_ENGINE="${FORSTICK2_GZ_RENDER_ENGINE:-ogre2}"
start gz_server gz sim -s -v "$VERBOSITY" -r --render-engine "$GZ_ENGINE" "$WORLD"

say "world 기동 대기 (최대 180초)"
WORLD_UP=0
for _ in $(seq 1 60); do
  if ! kill -0 "$(cat "$LOG_DIR/gz_server.pid")" 2>/dev/null; then
    fail "Gazebo 서버가 죽었다 — $LOG_DIR/gz_server.log"
    exit 6
  fi
  if timeout 6 gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/create"; then
    WORLD_UP=1; break
  fi
  sleep 3
done
if [[ "$WORLD_UP" -eq 0 ]]; then
  fail "world가 180초 안에 기동하지 않았다. **서버를 자동 종료하지 않는다.**"
  fail "  로그: $LOG_DIR/gz_server.log"
  fail "  사용 가능 메모리: $(free -m | awk 'NR==2 {print $7}') MB"
  exit 6
fi
say "world 기동 확인: $WORLD_NAME"

# ── 5. GUI (실패해도 서버는 그대로 둔다) ───────────────────────────────
# **GUI는 기본으로 띄우지 않는다.** 이 환경의 Xwayland는 DRI3를 노출하지 않아
# GUI가 소프트웨어 렌더링만 할 수 있고, 부하가 오르면 프레임을 완성하지 못한다
# (실측: GUI 클라이언트 CPU 747%, 모든 GUI 서비스 무응답, load 15까지 상승 →
#  같은 기계의 웹 서버·장면 카메라·컨트롤러까지 느려졌다).
# 같은 장면을 **서버가 83% CPU로 렌더링**하므로 사용자가 셀을 보는 경로는
# 웹 화면(/v1/scene.png)에 둔다. 근거: reports/workcell/gui_rendering.json
# GUI 창이 필요하면 FORSTICK2_GZ_GUI=1로 켠다.
if [[ "${FORSTICK2_GZ_GUI:-0}" != "1" ]]; then
  say "GUI 창을 띄우지 않는다(기본). 작업 셀 화면은 웹에서 본다:"
  say "    FORSTICK2_PORT=8093 ./scripts/run_web_workcell.sh"
  say "    http://127.0.0.1:8093  (작업 셀 화면 카드)"
  say "GUI 창이 필요하면: FORSTICK2_GZ_GUI=1 $0"
  say "이 환경의 GUI 제약 근거: reports/workcell/gui_rendering.json"
elif [[ "${FORSTICK2_GZ_HEADLESS:-0}" == "1" ]]; then
  say "headless 요청 — GUI를 띄우지 않는다"
elif [[ -z "${DISPLAY:-}" ]]; then
  fail "DISPLAY가 없어 GUI를 띄울 수 없다."
  fail "필요한 환경: WSLg(Windows 11 기본) 또는 X 서버 + DISPLAY."
  fail "**서버는 그대로 둔다.** GUI만 붙이려면: ./scripts/attach_gazebo_gui.sh --workcell"
else
  # **GUI 렌더 엔진은 ogre(v1)다.** 이 환경의 Xwayland는 DRI3를 노출하지 않아
  # Mesa가 소프트웨어로 떨어지고, EGL로 장치를 고르는 ogre2는 초기화 뒤 응답이
  # 끊긴다(서비스 타임아웃, CPU 356~669%). 근거: reports/workcell/gui_rendering.json
  GZ_ENGINE_GUI="${FORSTICK2_GZ_RENDER_ENGINE_GUI:-ogre}"
  # ogre v1은 GLX를 쓴다. 하드웨어 GL이 없는 환경이므로 소프트웨어로 명시한다.
  LIBGL_ALWAYS_SOFTWARE="${FORSTICK2_GZ_FORCE_SOFTWARE_GL:-1}" \
    start gz_gui gz sim -g -v "$VERBOSITY" --render-engine-gui "$GZ_ENGINE_GUI"
  sleep 8
  if alive gz_gui; then
    say "GUI 붙음 pid=$(cat "$LOG_DIR/gz_gui.pid")"
    say "창 제목의 [WARN:COPY MODE]는 WSLg의 프레임 전달 표시이며 오류가 아니다"
  else
    fail "GUI가 뜨지 못했다 — $LOG_DIR/gz_gui.log"
    fail "**서버는 그대로 둔다.** 다시 붙이려면:"
    fail "    ./scripts/attach_gazebo_gui.sh --workcell"
  fi
fi

# ── 6. robot_state_publisher + spawn + 컨트롤러 ────────────────────────
RSP_PARAMS="$LOG_DIR/rsp_params.yaml"
python3 - "$URDF_OUT" "$RSP_PARAMS" <<'PYGEN'
import sys
from pathlib import Path
urdf = Path(sys.argv[1]).read_text(encoding="utf-8")
body = "\n".join("      " + line for line in urdf.splitlines())
Path(sys.argv[2]).write_text(
    "robot_state_publisher:\n  ros__parameters:\n    use_sim_time: true\n"
    "    robot_description: |\n" + body + "\n", encoding="utf-8")
PYGEN
start rsp ros2 run robot_state_publisher robot_state_publisher \
  --ros-args --params-file "$RSP_PARAMS"
sleep 5

ros2 run ros_gz_sim create -world "$WORLD_NAME" -file "$URDF_OUT" \
  -name "$MODEL_NAME" -x 0 -y 0 -z 0 > "$LOG_DIR/spawn.log" 2>&1 || true
say "spawn: $(tail -1 "$LOG_DIR/spawn.log")"
sleep 10

for controller in joint_state_broadcaster arm_trajectory_controller; do
  ros2 run controller_manager spawner "$controller" \
    --controller-manager /controller_manager --controller-manager-timeout 60 \
    --param-file "$ARM_CONTROLLERS" > "$LOG_DIR/spawn_$controller.log" 2>&1 || true
  say "$controller: $(tail -1 "$LOG_DIR/spawn_$controller.log")"
done
# 그리퍼는 **궤적 컨트롤러**를 쓴다. GripperActionController는 목표를 지나
# 관절 제한까지 닫히고 거기서 잠긴다(실측 근거는 컨트롤러 YAML 주석과
# reports/workcell/gripper_control.json).
ros2 run controller_manager spawner gripper_trajectory_controller \
  --controller-manager /controller_manager --controller-manager-timeout 60 \
  --param-file "$GRIPPER_CONTROLLERS" \
  > "$LOG_DIR/spawn_gripper_trajectory_controller.log" 2>&1 || true
say "gripper_trajectory_controller: $(tail -1 "$LOG_DIR/spawn_gripper_trajectory_controller.log")"

# ── 6b. 장면 영상 ROS 브리지 ──────────────────────────────────────────
# 웹 화면이 장면을 **실시간으로** 받으려면 gz 이미지 토픽을 ROS로 넘겨야 한다
# (웹 서버에는 gz-transport 파이썬 바인딩이 없고 rclpy는 있다).
# forstick 8090 데모와 같은 방식이다.
SCENE_TOPIC="$(python3 - "$WORKCELL_JSON" <<'PYGEN'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
camera = data.get("scene_camera") or {}
if not camera.get("enabled"):
    print("")
else:
    print(f"/world/{data['world_name']}/model/scene_camera/link/link/sensor"
          f"/camera/image")
PYGEN
)"
if [[ -n "$SCENE_TOPIC" ]]; then
  ROS_TOPIC="$(python3 -c "
import json,sys
print((json.load(open('$WORKCELL_JSON', encoding='utf-8')).get('scene_camera') or {})
      .get('ros_topic','/scene_camera/image'))")"
  start scene_bridge ros2 run ros_gz_bridge parameter_bridge \
    "$SCENE_TOPIC@sensor_msgs/msg/Image[gz.msgs.Image" \
    --ros-args -r "$SCENE_TOPIC:=$ROS_TOPIC"
  sleep 4
  if alive scene_bridge; then
    say "장면 영상 브리지: $SCENE_TOPIC → $ROS_TOPIC"
  else
    say "장면 영상 브리지가 뜨지 못했다 — $LOG_DIR/scene_bridge.log"
    say "웹 화면은 느린 경로(gz topic 단발 호출)로 떨어진다."
  fi
fi

# ── 7. 검증된 안전 home으로 세운다 ────────────────────────────────────
# 스폰 직후 자세는 전 관절 0이다. 그 자세는 팔이 낮게 누워 화면에서 배경과
# 섞이고(실측: 사용자가 "로봇팔이 안 보인다"고 지적했다), 무엇보다 실행용
# 자세가 아니다. **검증된 안전 home**으로 세운다 — 임의 자세가 아니다.
if [[ -f "$ROOT/config/workcell/fr3_2f85_workcell_poses.json" ]]; then
  python3 "$ROOT/scripts/goto_workcell_pose.py" --pose safe_home --seconds 6 \
    2>&1 | sed 's/^/  /' || say "안전 home 이동을 건너뛴다(위 출력 참조)"
else
  say "유도된 자세 파일이 없어 안전 home으로 세우지 않는다."
  say "    python3 scripts/derive_workcell_poses.py"
fi

say ""
say "작업 셀 실행 중 — world=$WORLD_NAME partition=$GZ_PARTITION domain=$ROS_DOMAIN_ID"
say "다음 단계:"
say "    python3 scripts/derive_workcell_poses.py     # 안전 home·접근 자세 유도"
say "    ./scripts/run_moveit_workcell.sh             # MoveIt2 planning scene"
say "    python3 scripts/verify_workcell.py           # 검증 10항목"
say "**pick/place는 비활성이다.** yaw 선언값·커플링 실측 질량·그리퍼 결함 미해결."
