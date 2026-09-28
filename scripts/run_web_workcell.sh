#!/usr/bin/env bash
# 작업 셀에 연결된 웹 UI 실행 (8-08 우선순위 6).
#
#   ./scripts/run_web_workcell.sh
#
# 전제: 작업 셀 Gazebo와 MoveIt이 돌고 있다.
#   FORSTICK2_ASSEMBLY_YAW_RAD=0 ./scripts/run_gazebo_fr3_2f85_workcell_gui.sh
#   ./scripts/run_moveit_workcell.sh
#
# 활성 작업 셀은 config/workcell/active.json이 정한다 — 서버 코드에는 로봇·셀
# 이름이 없다(계획.md 27장).
set -eo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export FORSTICK2_WORKCELL_ROBOT=1
# 작업 셀 Adapter가 붙으므로 개발용 Fake는 쓰지 않는다.
export FORSTICK2_FAKE_ROBOT="${FORSTICK2_FAKE_ROBOT:-0}"
# 작업 셀과 같은 격리에서 ROS에 말한다.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"

# **ROS 환경을 소스한다.** 웹 서버가 planning scene·컨트롤러에 말하려면
# rclpy가 필요하다. 소스하지 않으면 기하 검사기가 붙지 않아 안전 판단이
# 계속 ASK(geometry.validator_unavailable)로 남는다(실측).
if [[ -z "${AMENT_PREFIX_PATH:-}" ]]; then
  # shellcheck disable=SC1091
  source /opt/ros/lyrical/setup.bash
fi
if ! "$ROOT/.venv/bin/python" -c "import rclpy" 2>/dev/null; then
  echo "[web-wc] rclpy를 쓸 수 없다 — ROS 환경을 소스했는지 확인한다." >&2
  echo "[web-wc] 기하 검사기가 붙지 않으면 안전 판단이 ASK로 남는다." >&2
  exit 5
fi
# 작업 셀 Gazebo와 같은 파티션·도메인에서 말한다(위에서 export했다).
echo "[web-wc] rclpy 확인 · domain=$ROS_DOMAIN_ID partition=$GZ_PARTITION"

MANIFEST="$ROOT/config/workcell/active.json"
if [[ ! -f "$MANIFEST" ]]; then
  echo "[web-wc] 활성 작업 셀 매니페스트가 없다: $MANIFEST" >&2
  exit 3
fi
python3 - "$MANIFEST" <<'PYCHECK' || exit 4
import json, sys
from pathlib import Path
manifest = Path(sys.argv[1])
data = json.loads(manifest.read_text(encoding="utf-8"))
if not data.get("enabled"):
    print("[web-wc] 매니페스트의 enabled가 꺼져 있다", file=sys.stderr)
    raise SystemExit(1)
missing = []
for key in ("workcell_config", "poses", "resource_catalog", "capability_profile"):
    path = (manifest.parent / data[key]).resolve()
    if not path.is_file():
        missing.append(f"{key}: {path}")
if missing:
    for item in missing:
        print(f"[web-wc] 파일이 없다 — {item}", file=sys.stderr)
    print("[web-wc] config.missing — 값을 만들지 않는다.", file=sys.stderr)
    raise SystemExit(1)
print(f"[web-wc] 활성 작업 셀: {data['adapter_module']}"
      f" (is_simulated={data.get('is_simulated')})")
PYCHECK

echo "[web-wc] 주소: http://127.0.0.1:${FORSTICK2_PORT:-8092}"
echo "[web-wc] **실하드웨어가 아니다.** Gazebo 작업 셀에 붙는다."
exec "$ROOT/scripts/run_web.sh" "$@"
