#!/usr/bin/env bash
# 연속 제어 세계(파티션 forstick2_humanoid_nav)용 실행기. run_controller.sh와 같고 파티션만 다르다.
#   humanoid/g1/run_nav.sh nav_controller.py --rtf 1.0
#   humanoid/g1/run_nav.sh g1cmd.py status
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$1"; shift
[[ "$script" != /* && -f "$HERE/$script" ]] && script="$HERE/$script"
GZ_PARTITION_HUMANOID="${GZ_PARTITION_HUMANOID_NAV:-forstick2_humanoid_nav}" \
  exec "$HERE/run_controller.sh" "$script" "$@"
