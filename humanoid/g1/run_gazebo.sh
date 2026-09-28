#!/usr/bin/env bash
# G1 Gazebo 서버(멈춘 상태로 뜬다 — 제어기가 스텝을 민다).
# FR3 작업 셀(파티션 forstick2_fr3_workcell)과 **다른 파티션**이다. 서로 건드리지 않는다.
#
#   humanoid/g1/run_gazebo.sh          # 보행 검증 세계(관절별 ApplyJointForce) — verify_walk.py
#   humanoid/g1/run_gazebo.sh --nav    # 연속 제어 세계(PD 플러그인 + 컨베이어) — nav_controller.py
#   humanoid/g1/run_gazebo.sh --manip  # 팔·손 조작 실험 세계(손 달린 G1 + 받침대·물체) — manip_controller.py
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -z "${AMENT_PREFIX_PATH:-}" ]] && source /opt/ros/lyrical/setup.bash
OUT=/tmp/forstick2_humanoid
mkdir -p "$OUT"
if [[ "${1:-}" == "--wbc" ]]; then
  # GR00T 균형 정책 시험 세계(조작 세계와 같은 로봇·장면, 하체 15관절 PD 묶음). 설정: config/manip_gr00t_site.json
  export GZ_PARTITION="${GZ_PARTITION_HUMANOID_WBC:-forstick2_humanoid_wbc}"
  WORLD_NAME=forstick2_humanoid_wbc
  WORLD_FILE="$OUT/g1_wbc_world.sdf"
  NAME=gz_wbc
  BUILD="$OUT/pd_build"
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$BUILD:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
  FORSTICK2_G1_MANIP_SITE="$ROOT/humanoid/g1/config/manip_gr00t_site.json" \
    python3 "$ROOT/humanoid/g1/build_world.py" --manip >/dev/null
elif [[ "${1:-}" == "--manip" ]]; then
  export GZ_PARTITION="${GZ_PARTITION_HUMANOID_MANIP:-forstick2_humanoid_manip}"
  WORLD_NAME=forstick2_humanoid_manip
  WORLD_FILE="$OUT/g1_manip_world.sdf"
  NAME=gz_manip
  BUILD="$OUT/pd_build"
  if [[ ! -f "$BUILD/libG1PdController.so" ]]; then
    cmake -S "$ROOT/humanoid/g1/plugin" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD" -j8 >/dev/null
  fi
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$BUILD:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
  python3 "$ROOT/humanoid/g1/build_world.py" --manip >/dev/null   # FORSTICK2_G1_MANIP_SITE로 다른 모델 비교 가능
elif [[ "${1:-}" == "--nav" ]]; then
  export GZ_PARTITION="${GZ_PARTITION_HUMANOID_NAV:-forstick2_humanoid_nav}"
  WORLD_NAME=forstick2_humanoid_nav
  WORLD_FILE="$OUT/g1_nav_world.sdf"
  NAME=gz_nav
  BUILD="$OUT/pd_build"
  if [[ ! -f "$BUILD/libG1PdController.so" ]]; then
    cmake -S "$ROOT/humanoid/g1/plugin" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD" -j8 >/dev/null
  fi
  export GZ_SIM_SYSTEM_PLUGIN_PATH="$BUILD:${GZ_SIM_SYSTEM_PLUGIN_PATH:-}"
  python3 "$ROOT/humanoid/g1/build_world.py" --nav >/dev/null
else
  export GZ_PARTITION="${GZ_PARTITION_HUMANOID:-forstick2_humanoid}"
  WORLD_NAME=forstick2_humanoid
  WORLD_FILE="$OUT/g1_world.sdf"
  NAME=gz_server
  python3 "$ROOT/humanoid/g1/build_world.py" >/dev/null
fi
if [[ -f "$OUT/$NAME.pid" ]] && kill -0 "$(cat "$OUT/$NAME.pid")" 2>/dev/null; then
  kill "$(cat "$OUT/$NAME.pid")"; sleep 3
fi
nohup gz sim -s -v 2 "$WORLD_FILE" > "$OUT/$NAME.log" 2>&1 &
echo $! > "$OUT/$NAME.pid"
for _ in $(seq 1 60); do
  gz service -l 2>/dev/null | grep -q "/world/$WORLD_NAME/control" && break; sleep 1
done
echo "[humanoid] Gazebo pid=$(cat "$OUT/$NAME.pid") world=$WORLD_NAME partition=$GZ_PARTITION (paused)"
