#!/usr/bin/env bash
# GR00T 균형 정책 시험 세계(파티션 forstick2_humanoid_wbc)용 실행기.
#   humanoid/g1/run_wbc.sh gr00t_controller.py --rtf 1.0 --reset-world
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$1"; shift
[[ "$script" != /* && -f "$HERE/$script" ]] && script="$HERE/$script"
FORSTICK2_G1_SITE="$HERE/config/manip_gr00t_site.json" \
GZ_PARTITION_HUMANOID="${GZ_PARTITION_HUMANOID_WBC:-forstick2_humanoid_wbc}" \
  exec "$HERE/run_controller.sh" "$script" "$@"
