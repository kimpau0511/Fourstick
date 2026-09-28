#!/usr/bin/env bash
# FR3 작업 셀 한 번에 띄우기: Gazebo → MoveIt → 웹 서버(백그라운드). 각 단계가 준비될 때까지 기다린다.
#
#   ./scripts/fr3_up.sh                 웹 http://127.0.0.1:8092 (FORSTICK2_PORT)
#   FORSTICK2_LAN=1 ./scripts/fr3_up.sh 같은 망의 다른 기기에서도(0.0.0.0, 인증 없음 — 믿을 수 있는 사설망만)
#   FORSTICK2_STT=0 ./scripts/fr3_up.sh 음성 입력 없이(STT 모델을 올리지 않는다)
#
# 끄기: ./scripts/fr3_down.sh   · 사전 점검: ./scripts/doctor.sh
# 장착 yaw는 시뮬레이션 선언값 0을 쓴다(FORSTICK2_ASSEMBLY_YAW_RAD) — pick/place 일반 경로를 열지 않는다.
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
export FORSTICK2_ASSEMBLY_YAW_RAD="${FORSTICK2_ASSEMBLY_YAW_RAD:-0}"
export GZ_PARTITION="${GZ_PARTITION:-forstick2_fr3_workcell}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-44}"
PORT="${FORSTICK2_PORT:-8092}"
LOG="$FORSTICK2_WORKCELL_LOG_DIR"
mkdir -p "$LOG"
say() { printf '[fr3-up] %s\n' "$*"; }

"$ROOT/scripts/doctor.sh" > "$LOG/doctor.log" 2>&1 || { cat "$LOG/doctor.log"; say "사전 점검 실패 — 멈춘다"; exit 2; }
say "사전 점검 통과 (partition=$GZ_PARTITION domain=$ROS_DOMAIN_ID log=$LOG)"

say "1/3 Gazebo 작업 셀"
"$ROOT/scripts/run_gazebo_fr3_2f85_workcell_gui.sh" 2>&1 | tee "$LOG/up_gazebo.log" | sed 's/^/  /'
grep -q "world 기동 확인" "$LOG/up_gazebo.log" || { say "Gazebo가 뜨지 않았다 — $LOG/gz_server.log"; exit 3; }

say "2/3 MoveIt(move_group + planning scene)"
"$ROOT/scripts/run_moveit_workcell.sh" 2>&1 | tee "$LOG/up_moveit.log" | sed 's/^/  /'

say "3/3 웹 서버 :$PORT"
if [[ -f "$LOG/web.pid" ]] && kill -0 "$(cat "$LOG/web.pid")" 2>/dev/null; then
  say "이미 실행 중: pid=$(cat "$LOG/web.pid")"
else
  launcher="$ROOT/scripts/run_web_workcell.sh"
  [[ "${FORSTICK2_LAN:-0}" == "1" ]] && launcher="$ROOT/scripts/run_web_lan.sh"
  FORSTICK2_PORT="$PORT" nohup "$launcher" > "$LOG/web.log" 2>&1 &
  echo $! > "$LOG/web.pid"
fi
for _ in $(seq 1 120); do
  code="$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v1/config" || true)"
  [[ "$code" == "200" ]] && break
  kill -0 "$(cat "$LOG/web.pid")" 2>/dev/null || { say "웹 서버가 죽었다 — $LOG/web.log"; tail -5 "$LOG/web.log"; exit 4; }
  sleep 3
done
[[ "$code" == "200" ]] || { say "웹 서버가 6분 안에 응답하지 않았다 — $LOG/web.log"; exit 4; }
sim="$(curl -s -m 5 "http://127.0.0.1:$PORT/v1/sim-demo")"
python3 - "$sim" <<'PY'
import json, sys
d = json.loads(sys.argv[1] or "{}")
print(f"[fr3-up] 시뮬레이션 시연 enabled={d.get('enabled')} · 자재 {len(d.get('materials') or [])}개 · "
      f"Qwen 해석 {'켜짐' if d.get('intent_available') else '꺼짐(' + str(d.get('intent_reason')) + ')'}")
PY
say "준비 완료 — http://127.0.0.1:$PORT  (끄기: ./scripts/fr3_down.sh)"
