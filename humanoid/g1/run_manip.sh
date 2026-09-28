#!/usr/bin/env bash
# 팔·손 조작 실험 세계(파티션 forstick2_humanoid_manip)용 실행기.
#   humanoid/g1/run_manip.sh manip_controller.py --rtf 1.0 --reset-world
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$1"; shift
[[ "$script" != /* && -f "$HERE/$script" ]] && script="$HERE/$script"
FORSTICK2_G1_SITE="${FORSTICK2_G1_MANIP_SITE:-$HERE/config/manip_site.json}" \
GZ_PARTITION_HUMANOID="${GZ_PARTITION_HUMANOID_MANIP:-forstick2_humanoid_manip}" \
  exec "$HERE/run_controller.sh" "$script" "$@"
