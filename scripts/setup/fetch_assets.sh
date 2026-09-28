#!/usr/bin/env bash
# 외부 로봇 자산을 **원본 출처에서 지정 버전으로** 받아 검증한다(저장소에 복사하지 않는다).
#
#   FORSTICK2_ACCEPT_FR3_TERMS=1 ./scripts/setup/fetch_assets.sh
#   ./scripts/setup/fetch_assets.sh --verify-only        # 받지 않고 검증만
#
# 받는 곳: ${FORSTICK2_THIRD_PARTY:-<저장소>/third_party} (Git 제외). 다른 곳에 이미 있으면
# config/local.env에 FORSTICK2_FR3_REPO / FORSTICK2_ROBOTIQ_REPO를 적고 --verify-only로 확인한다.
#
# 기준: config/assets/third_party_assets.json
#   FR3-WMS  FAIR-INNOVATION/frcobot_ros2 commit 5bed0b0263c8f1e95f51aa45079f904d463c5c50
#            (라이선스 파일 없음 — 재배포 조건 미확인. 그래서 이 저장소에 넣지 않고 사용자가 원본에서 받는다)
#            FR3WMS.urdf git blob 45f1ce294242234b1a3495e0aafcc7ecfe74c7fb
#   Robotiq  robotiq/ros tag v1.1.0 → commit 3ab3befccaa10468f80803ba687105c9d224d567 (BSD-3-Clause)
#
# 실패하면 이유 코드와 함께 멈춘다: asset.network · asset.commit_missing · asset.hash_mismatch ·
# asset.terms_not_accepted · asset.missing
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
VERIFY_ONLY=0
[[ "${1:-}" == "--verify-only" ]] && VERIFY_ONLY=1

FR3_URL=https://github.com/FAIR-INNOVATION/frcobot_ros2
FR3_COMMIT=5bed0b0263c8f1e95f51aa45079f904d463c5c50
FR3_URDF_BLOB=45f1ce294242234b1a3495e0aafcc7ecfe74c7fb
FR3_SPARSE=fairino_description
RQ_URL=https://github.com/robotiq/ros
RQ_TAG=v1.1.0
RQ_COMMIT=3ab3befccaa10468f80803ba687105c9d224d567
RQ_SPARSE=grippers/robotiq_description

fail() { echo "[assets] 실패 ($1): $2" >&2; exit "${3:-2}"; }
say() { echo "[assets] $*"; }

fetch() {   # fetch <dir> <url> <commit> <sparse-path>
  local dir="$1" url="$2" commit="$3" sparse="$4"
  if [[ -d "$dir/.git" ]]; then
    say "이미 있음: $dir — 지정 commit으로 맞춘다"
  else
    [[ -e "$dir" ]] && fail asset.missing "$dir 가 Git 복제가 아니다(직접 옮긴 폴더면 FORSTICK2_*_REPO로 가리키고 --verify-only)"
    mkdir -p "$(dirname "$dir")"
    git clone --filter=blob:none --no-checkout "$url" "$dir" \
      || fail asset.network "$url 복제 실패(네트워크·GitHub 접근 확인)"
    git -C "$dir" sparse-checkout set "$sparse" || fail asset.missing "sparse-checkout 실패"
  fi
  if ! git -C "$dir" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$dir" fetch --filter=blob:none origin "$commit" \
      || fail asset.commit_missing "$url 에서 commit $commit 을 받을 수 없다(원본에서 지워졌거나 네트워크 문제)"
  fi
  git -C "$dir" -c advice.detachedHead=false checkout --quiet "$commit" \
    || fail asset.commit_missing "commit $commit 체크아웃 실패"
}

verify() {  # verify <name> <dir> <commit> [<path> <blob>] <required files...>
  local name="$1" dir="$2" commit="$3"; shift 3
  [[ -d "$dir" ]] || fail asset.missing "$name 이 없다: $dir"
  local head
  head="$(git -C "$dir" rev-parse HEAD 2>/dev/null)" || fail asset.missing "$name: Git 복제가 아니다 ($dir)"
  [[ "$head" == "$commit" ]] || fail asset.hash_mismatch "$name commit $head ≠ 기준 $commit"
  for f in "$@"; do
    [[ -e "$dir/$f" ]] || fail asset.missing "$name 파일 없음: $dir/$f"
  done
  say "$name 확인: $dir @ ${commit:0:12}"
}

if [[ "$VERIFY_ONLY" -eq 0 ]]; then
  if [[ "${FORSTICK2_ACCEPT_FR3_TERMS:-0}" != "1" ]]; then
    cat >&2 <<EOF
[assets] FR3-WMS 설명 파일(FAIR-INNOVATION/frcobot_ros2)에는 라이선스 파일이 없다(2026-09-16 확인).
[assets] 이 저장소는 그 파일을 재배포하지 않는다. 사용자가 원본에서 직접 받아 로컬에서만 쓴다.
[assets] 소속 조직의 사용 조건을 확인한 뒤 명시적으로 동의해 실행한다:
[assets]     FORSTICK2_ACCEPT_FR3_TERMS=1 $0
EOF
    exit 3
  fi
  fetch "$FORSTICK2_FR3_REPO" "$FR3_URL" "$FR3_COMMIT" "$FR3_SPARSE"
  fetch "$FORSTICK2_ROBOTIQ_REPO" "$RQ_URL" "$RQ_COMMIT" "$RQ_SPARSE"
fi

verify "FR3-WMS" "$FORSTICK2_FR3_REPO" "$FR3_COMMIT" \
  fairino_description/urdf/FR3WMS.urdf fairino_description/meshes
blob="$(git -C "$FORSTICK2_FR3_REPO" rev-parse "HEAD:fairino_description/urdf/FR3WMS.urdf" 2>/dev/null)"
[[ "$blob" == "$FR3_URDF_BLOB" ]] || fail asset.hash_mismatch "FR3WMS.urdf blob $blob ≠ 기준 $FR3_URDF_BLOB"
# 작업 트리 파일이 커밋과 같은지(로컬 수정 여부)
[[ -z "$(git -C "$FORSTICK2_FR3_REPO" status --porcelain -- fairino_description)" ]] \
  || fail asset.hash_mismatch "FR3 작업 트리에 로컬 수정이 있다(git -C $FORSTICK2_FR3_REPO status)"
verify "Robotiq 2F-85" "$FORSTICK2_ROBOTIQ_REPO" "$RQ_COMMIT" \
  "$RQ_SPARSE/urdf/robotiq_2f_85_macro.urdf.xacro" "$RQ_SPARSE/urdf/ur_to_robotiq_adapter.urdf.xacro" \
  "$RQ_SPARSE/meshes"
[[ -z "$(git -C "$FORSTICK2_ROBOTIQ_REPO" status --porcelain -- "$RQ_SPARSE")" ]] \
  || fail asset.hash_mismatch "Robotiq 작업 트리에 로컬 수정이 있다"
say "완료 — FR3 작업 셀 자산 준비됨"
