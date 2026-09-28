#!/usr/bin/env bash
# 음성 인식 모델(faster-whisper small)을 미리 받는다. 받지 않아도 웹 서버가 처음 뜰 때 받는다(네트워크 필요).
#
#   ./scripts/setup/fetch_models.sh            # STT 모델만
#   ./scripts/setup/fetch_models.sh --qwen      # (선택) 계획 생성용 Qwen3-8B-AWQ도 — 약 6 GB, GPU 8 GB 이상
#
# 받는 곳: Hugging Face 캐시(HF_HOME, 기본 ~/.cache/huggingface). 개발 PC 기준 revision:
#   Systran/faster-whisper-small  536b0662742c02347bc0e980a01041f333bce120 (MIT)
#   Qwen/Qwen3-8B-AWQ             4da05a8edb55c6046cce958586c33b61da07bb79 (Apache-2.0)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/env.sh"
"$FORSTICK2_VENV/bin/python" - "${1:-}" <<'PY'
import sys
from huggingface_hub import snapshot_download
want = [("Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120", None)]
if sys.argv[1] == "--qwen":
    import os
    target = os.environ.get("FORSTICK2_QWEN_DIR") or os.path.expanduser("~/models/qwen3-8b-awq")
    want.append(("Qwen/Qwen3-8B-AWQ", "4da05a8edb55c6046cce958586c33b61da07bb79", target))
for repo, rev, local in want:
    try:
        # faster-whisper는 main을 찾는다 — main을 받고 기준 revision과 같은지 확인한다.
        path = snapshot_download(repo, revision="main" if local is None else rev,
                                 **({"local_dir": local} if local else {}))
    except Exception as exc:  # noqa: BLE001
        print(f"[models] 실패(model.network): {repo} — {exc}", file=sys.stderr)
        raise SystemExit(2)
    got = path.rstrip("/").split("/")[-1] if local is None else rev
    note = "기준과 같음" if got == rev else f"기준 {rev[:12]}와 다름(원본이 갱신됨 — 결과가 다를 수 있다)"
    print(f"[models] {repo}: {path} ({note})")
PY
