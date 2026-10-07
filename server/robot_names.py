"""로봇 이름·호출어 설정(2026-10-07) — 로봇 관리 화면에서 정하고, 대시보드 음성 명령이 호출어로 쓴다.

- 이름은 **한글 2~4글자**만 받는다(빈 값·공백·너무 긴 이름·영문·숫자는 저장하지 않는다).
- 호출어는 저장하지 않고 늘 `이름 + '야'`로 만든다(지니 → 지니야).
- 값은 서버 파일(`robot_settings.json`, DB 옆)에 둔다 — 새로고침·다른 브라우저에서도 같은 이름이다.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

DEFAULT_NAME = "지니"
NAME_RE = re.compile(r"^[가-힣]{2,4}$")


class RobotNameError(ValueError):
    pass


def wake_word(name: str) -> str:
    return f"{name}야"


def validate(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise RobotNameError("로봇 이름을 입력하세요")
    value = name.strip()
    if len(value) > 4:
        raise RobotNameError("로봇 이름은 4글자까지입니다")
    if not NAME_RE.fullmatch(value):
        raise RobotNameError("로봇 이름은 한글 2~4글자로 입력하세요(공백·영문·숫자 제외)")
    return value


class RobotNames:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get("robots", {}) if isinstance(data, dict) else {}

    def get(self, robot_id: str) -> dict:
        with self._lock:
            row = self._read().get(robot_id) or {}
        try:
            name = validate(row.get("name"))
        except RobotNameError:
            name = None
        name = name or DEFAULT_NAME
        return {"robot_id": robot_id, "name": name, "wake_word": wake_word(name),
                "is_default": name == DEFAULT_NAME and not row, "updated_at": row.get("updated_at")}

    def set(self, robot_id: str, name) -> dict:
        value = validate(name)
        with self._lock:
            robots = self._read()
            robots[robot_id] = {"name": value, "updated_at": self._clock()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schema": "forstick2.robot_settings/1", "robots": robots},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        return self.get(robot_id)
