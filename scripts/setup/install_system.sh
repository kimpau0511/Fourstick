#!/usr/bin/env bash
# 시스템 패키지(ROS 2 Lyrical·Gazebo·MoveIt·파이썬 apt 패키지)를 확인하고, 동의하면 설치한다.
#
#   ./scripts/setup/install_system.sh            # 무엇이 빠졌는지만 보여 준다(설치하지 않는다)
#   ./scripts/setup/install_system.sh --install  # sudo apt-get install (관리자 권한 필요)
#
# 지원: Ubuntu 26.04 LTS(개발 PC: WSL2) + ROS 2 Lyrical apt 저장소. ROS 저장소 설정은 ROS 공식 설치 문서를 따른다
# (docs/SETUP.md 1단계). 이 스크립트는 저장소를 추가하지 않는다 — 없으면 멈추고 안내만 한다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LIST="$ROOT/scripts/setup/apt-packages.txt"
. /etc/os-release
if [[ "${VERSION_ID:-}" != "26.04" ]]; then
  echo "[system] 경고: Ubuntu ${VERSION_ID:-?} — 확인된 환경은 Ubuntu 26.04다(ROS 2 Lyrical 대상)." >&2
fi
POLICY="$(apt-cache policy ros-lyrical-ros-base 2>/dev/null)"
if ! grep -q "Candidate: [0-9]" <<<"$POLICY"; then
  echo "[system] ROS 2 Lyrical apt 저장소가 설정돼 있지 않다(ros-lyrical-* 후보 없음)." >&2
  echo "[system] docs/SETUP.md 1단계대로 ros2-apt-source를 설치한 뒤 sudo apt update 후 다시 실행한다." >&2
  exit 3
fi
mapfile -t PKGS < <(grep -vE '^\s*(#|$)' "$LIST")
MISSING=()
for p in "${PKGS[@]}"; do
  dpkg -s "$p" >/dev/null 2>&1 || MISSING+=("$p")
done
if [[ ${#MISSING[@]} -eq 0 ]]; then
  echo "[system] 필요한 apt 패키지 ${#PKGS[@]}개 모두 설치됨"
  exit 0
fi
echo "[system] 빠진 패키지 ${#MISSING[@]}개: ${MISSING[*]}"
if [[ "${1:-}" != "--install" ]]; then
  echo "[system] 설치하려면: $0 --install   (sudo apt-get install -y ...)"
  exit 1
fi
sudo apt-get update && sudo apt-get install -y "${MISSING[@]}"
