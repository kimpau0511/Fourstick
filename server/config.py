"""웹 서버 설정 (md/개발플랜.md 7단계).

주소·포트·설정 파일 경로·DB 경로를 코드에 두지 않는다. 기본값은 환경변수로
바꿀 수 있고, 기본 바인딩은 **localhost**다 — 개발용 서버를 실수로 네트워크에
열지 않기 위해서다.

LLM 접속 설정은 여기 없다. `examples/config/valid_llm_provider_qwen3.json`처럼
공급자 설정 파일에서 읽고, **브라우저에는 내려보내지 않는다.**
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 기본 포트. 8090(forstick 데모)과 겹치지 않게 8092를 쓴다.
DEFAULT_PORT = 8092
DEFAULT_HOST = "127.0.0.1"


@dataclass(frozen=True)
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: 정적 파일(웹 UI) 디렉터리.
    web_dir: Path = ROOT / "html"
    #: 설정 파일 디렉터리.
    config_dir: Path = ROOT / "examples" / "config"
    #: 로봇 구성(조합형 Profile)과 제3자 자산 manifest 디렉터리.
    #: 예제 설정과 섞지 않는다 — 여기는 실제 로봇 구성이 들어온다.
    robot_config_dir: Path = ROOT / "config"
    #: SQLite 파일. 브라우저는 여기 직접 접근하지 않는다.
    db_path: Path = ROOT / "reports" / "web.sqlite3"
    #: LLM 공급자 설정 파일 이름. 없으면 계획 생성을 사용할 수 없는 상태로 둔다.
    llm_config_name: str = "valid_llm_provider_qwen3.json"
    #: 실제 로봇 Profile 파일. 없으면 '로봇 미설정'으로 표시한다.
    robot_profile_name: str | None = None
    #: 개발용 Fake Adapter를 등록할지. 실제 로봇이 없을 때 흐름을 시험하기 위함.
    enable_fake_robot: bool = True
    #: STT 실제 모델을 쓸지. 끄면 STT를 사용할 수 없는 상태로 표시한다.
    enable_stt: bool = True
    #: 세션 유휴 만료 시간(초). 넘으면 만료로 표시하고 계획을 복원하지 않는다.
    #: 개발용 기본값이며 운영 정책이 정해지면 정책 파일로 옮긴다.
    session_idle_timeout_sec: float = 3600.0
    #: 계획 TTL(초). ExecutionPermit이 만료된 계획을 거부하는 기준이다.
    plan_ttl_sec: float = 600.0
    #: 시뮬레이션 작업 셀 Adapter에 연결할지 (8-08 우선순위 6).
    #: 켜면 Fake Adapter 대신 매니페스트가 가리키는 Adapter를 등록하고, 자원
    #: 카탈로그도 그 작업 셀 것으로 바꾼다. **실제 하드웨어가 아니다.**
    enable_workcell_robot: bool = False
    #: 활성 작업 셀 매니페스트. **여기에만 로봇·셀 이름이 있다** — 공통 코드가
    #: 모델 이름으로 분기하지 않게 하려고(계획.md 27장) 파일 하나로 모았다.
    workcell_manifest: Path = ROOT / "config" / "workcell" / "active.json"
    #: 클라이언트(탭) 등록 후 이벤트 구독까지 허용하는 유예(초).
    #: 이 구간에도 다른 탭이 같은 세션을 가져가지 못하게 한다. **브라우저는 이
    #: 값을 `/v1/config`에서 받아 쓴다** — 화면 코드에 숫자를 중복하지 않는다.
    client_grace_sec: float = 5.0

    @staticmethod
    def from_env() -> "ServerConfig":
        def path_env(name: str, default: Path) -> Path:
            value = os.environ.get(name)
            return Path(value) if value else default

        def flag(name: str, default: bool) -> bool:
            value = os.environ.get(name)
            if value is None:
                return default
            return value.strip().lower() not in ("0", "false", "no", "off")

        return ServerConfig(
            host=os.environ.get("FORSTICK2_HOST", DEFAULT_HOST),
            port=int(os.environ.get("FORSTICK2_PORT", str(DEFAULT_PORT))),
            web_dir=path_env("FORSTICK2_WEB_DIR", ROOT / "html"),
            config_dir=path_env("FORSTICK2_CONFIG_DIR", ROOT / "examples" / "config"),
            robot_config_dir=path_env("FORSTICK2_ROBOT_CONFIG_DIR", ROOT / "config"),
            db_path=path_env("FORSTICK2_DB", ROOT / "reports" / "web.sqlite3"),
            llm_config_name=os.environ.get(
                "FORSTICK2_LLM_CONFIG", "valid_llm_provider_qwen3.json"
            ),
            robot_profile_name=os.environ.get("FORSTICK2_ROBOT_PROFILE") or None,
            enable_fake_robot=flag("FORSTICK2_FAKE_ROBOT", True),
            enable_workcell_robot=flag("FORSTICK2_WORKCELL_ROBOT", False),
            workcell_manifest=path_env(
                "FORSTICK2_WORKCELL_MANIFEST",
                ROOT / "config" / "workcell" / "active.json"),
            enable_stt=flag("FORSTICK2_STT", True),
            session_idle_timeout_sec=float(
                os.environ.get("FORSTICK2_SESSION_IDLE_SEC", "3600")
            ),
            plan_ttl_sec=float(os.environ.get("FORSTICK2_PLAN_TTL_SEC", "600")),
            client_grace_sec=float(
                os.environ.get("FORSTICK2_CLIENT_GRACE_SEC", "5")
            ),
        )
