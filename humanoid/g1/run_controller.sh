#!/usr/bin/env bash
# G1 lockstep 제어기/검증 실행기 — 별도 venv(/home/asd/external/humanoid_venv) + gz python 바인딩.
#   humanoid/g1/run_controller.sh humanoid/g1/controller.py --plan stand:3
#   humanoid/g1/run_controller.sh humanoid/g1/verify_walk.py --repeats 3
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source /opt/ros/lyrical/setup.bash >/dev/null 2>&1
export GZ_PARTITION="${GZ_PARTITION_HUMANOID:-forstick2_humanoid}"
# protobuf는 gz-msgs 생성 코드와 맞는 시스템 것(4.21, Python 3.14 빌드)만 링크해 쓴다.
export PYTHONPATH="$HERE:$PYTHONPATH:${FORSTICK2_HUMANOID_SYSLINK:-/home/asd/external/humanoid_syslink}"
VENV="${FORSTICK2_HUMANOID_VENV:-/home/asd/external/humanoid_venv}"
script="${1:-$HERE/controller.py}"; shift || true
exec "$VENV/bin/python" "$script" "$@"
