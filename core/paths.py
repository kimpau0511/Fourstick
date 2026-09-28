"""실행 경로 한곳. 개발자 개인 경로를 코드에 두지 않는다.

우선순위: 환경변수 → `config/local.env`(사용자 PC 전용, Git 제외) → 프로젝트 상대 기본값.
셸 스크립트는 `scripts/lib/env.sh`가 같은 규칙으로 정한다.

**출처 기록은 여기와 무관하다.** `config/profiles/*.json`·`config/assets/third_party_assets.json` 안의
경로는 측정값이 어디서 나왔는지 적은 기록이라 바꾸지 않는다(실행에 쓰지 않는다).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV = ROOT / "config" / "local.env"


def _local_env() -> dict[str, str]:
    """`config/local.env`의 `KEY=VALUE`(주석·빈 줄 무시, 따옴표 벗김). 없으면 빈 값."""
    out: dict[str, str] = {}
    try:
        text = LOCAL_ENV.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        out[key] = os.path.expandvars(os.path.expanduser(value.strip().strip('"').strip("'")))
    return out


def setting(name: str, default: str | Path) -> Path:
    value = os.environ.get(name) or _local_env().get(name)
    return Path(value) if value else Path(default)


def third_party_dir() -> Path:
    return setting("FORSTICK2_THIRD_PARTY", ROOT / "third_party")


def workcell_log_dir() -> Path:
    """작업 셀 실행 상태·로그(시뮬레이션 상태 파일·정지 요청 파일 포함). 인스턴스마다 다르게 둘 수 있다."""
    return setting("FORSTICK2_WORKCELL_LOG_DIR", "/tmp/forstick2_workcell")


def gazebo_dir() -> Path:
    return setting("FORSTICK2_GZ_LOG_DIR", "/tmp/forstick2_gazebo")


def evidence_dir() -> Path:
    """실행에 필요한 검토 근거(자기충돌 검토·루프 폐쇄 쌍) — Git에 포함된 스냅숏."""
    return setting("FORSTICK2_EVIDENCE_DIR", ROOT / "config" / "evidence")
