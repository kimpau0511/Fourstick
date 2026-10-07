"""작업 셀 인스턴스별 경로 (2026-10-07, 현장 1 / 현장 2).

같은 로봇으로 두 셀(현장)을 따로 띄우려면 셀 설정 파일과 상태·정지 파일 폴더가 셀마다 달라야 한다.
환경 변수가 없으면 지금까지의 값(현장 1)을 그대로 쓴다 — 현장 1 동작은 바뀌지 않는다.

- `FORSTICK2_WORKCELL_CONFIG`: 작업 셀 설정 JSON(기본: 활성 매니페스트 config/workcell/active.json의 workcell_config)
- `FORSTICK2_WORKCELL_LOG_DIR`: 상태·정지·로그 폴더(기본 /tmp/forstick2_workcell)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTIVE_MANIFEST = ROOT / "config/workcell/active.json"
DEFAULT_LOG_DIR = Path("/tmp/forstick2_workcell")


def workcell_config() -> Path:
    value = os.environ.get("FORSTICK2_WORKCELL_CONFIG")
    if value:
        return Path(value)
    # 공통 코드에 셀 이름을 두지 않는다 — 활성 셀은 매니페스트가 정한다(서버와 같은 규칙).
    manifest = json.loads(ACTIVE_MANIFEST.read_text(encoding="utf-8"))
    return ACTIVE_MANIFEST.parent / manifest["workcell_config"]


def log_dir() -> Path:
    value = os.environ.get("FORSTICK2_WORKCELL_LOG_DIR")
    return Path(value) if value else DEFAULT_LOG_DIR

