"""웹 서버 설정 (md/개발플랜.md 7단계).

주소·포트·설정 파일 경로·DB 경로를 코드에 두지 않는다. 기본값은 환경변수로
바꿀 수 있고, 기본 바인딩은 **localhost**다 — 개발용 서버를 실수로 네트워크에
열지 않기 위해서다.

LLM 접속 계약은 `examples/config/valid_llm_provider_qwen3.json` 같은 공급자 설정
파일에서 읽는다. 배치마다 다른 주소만 환경변수로 덮어쓸 수 있으며, 어느 값도
**브라우저에는 내려보내지 않는다.**
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
    #: 배치마다 다른 LLM 서버 주소. None이면 공급자 설정 파일 값을 쓴다.
    llm_base_url_override: str | None = None
    #: 실제 로봇 Profile 파일. 없으면 '로봇 미설정'으로 표시한다.
    robot_profile_name: str | None = None
    #: 개발용 Fake Adapter를 등록할지. 실제 로봇이 없을 때 흐름을 시험하기 위함.
    enable_fake_robot: bool = True
    #: STT 실제 모델을 쓸지. 끄면 STT를 사용할 수 없는 상태로 표시한다.
    enable_stt: bool = True
    #: STT 모델 계약 파일 이름. 기본은 검증된 저사양 구성이다.
    stt_model_config_name: str = "valid_stt_model_lowspec.json"
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
    #: 웹에서 **시뮬레이션 시연**(이송·복귀·resume·복구) 작업을 띄울 수 있게
    #: 할지. **활성 작업 셀이 시뮬레이션이면 기본으로 켠다** — 환경변수 없이도
    #: 시뮬레이터 셀에서는 시뮬레이션 명령이 기본 동작이다. 시뮬레이션이 아닌
    #: 셀에서는 이 값과 무관하게 열리지 않는다(`server/runtime.py`).
    #: `FORSTICK2_SIM_DEMO_WEB=0`으로 끌 수 있다.
    #: 일반 `/v1/plan`·`/v1/execute`의 pick/place 차단과는 무관하다.
    enable_sim_demo_web: bool = True
    #: 시연 **명령 입구**(`POST /v1/sim-demo/command`)만 끈다. 끄면 그 입구가 PASS_THROUGH로
    #: 답하고, 기존 웹 화면은 그 답을 받아 일반 경로(`/v1/plan` → `/v1/execute`)로 보낸다 —
    #: 화면 코드를 바꾸지 않고 일반 모드를 시험하는 스위치다. 시연 작업 실행기(일반 경로
    #: pick/place가 쓰는 것)와 다른 시연 API는 그대로 둔다. 기본은 켬(지금 동작 그대로).
    #: `FORSTICK2_SIM_DEMO_COMMAND=0`으로 끈다.
    enable_sim_demo_command: bool = True
    #: 모호한 자재 작업 발화를 Qwen 분류기로 보낼지. 끄면 규칙 해석만 쓴다.
    #: 분류기는 계획 생성과 **같은 LLM 설정**(`llm_config_name`)을 쓴다.
    enable_sim_demo_intent: bool = True
    #: 분류기 결과를 받아들이는 최소 confidence. 이 값 아래는 실행 후보가 아니다.
    #: **측정으로 정한 값이 아니다** — 보수적 기본값이고, 평가로 근거가 생기기
    #: 전까지 코드에서 자동으로 조정하지 않는다.
    sim_demo_intent_min_confidence: float = 0.7
    #: 확인 카드 만료(초). 사용자 요구값이다.
    sim_demo_confirm_ttl_sec: float = 60.0
    #: 로그인을 요구할지(구글만·등록 계정만, 2026-10-06 결정). **기본은 켬** — 보안 기본값(CODE_RULES 8).
    #: 켜져 있으면 로그인·정지·정적 파일 말고는 로그인 세션이 있어야 한다(`server/auth.py`).
    #: 구글 설정을 하기 전 개발 PC에서만 `FORSTICK2_REQUIRE_LOGIN=0`으로 끈다.
    require_login: bool = True
    #: 구글 OAuth 클라이언트 ID(공개 값). 화면(`dashboard2/.env`)과 같은 값이어야 한다.
    google_client_id: str = ""
    #: 구글 OAuth 클라이언트 보안 비밀. **환경변수로만** 받는다 — 저장소·화면에 두지 않는다.
    google_client_secret: str = ""
    #: 화면이 열리는 주소(origin). 구글 팝업 방식의 인가 코드 교환 redirect_uri가 이 값이다
    #: (구글 문서 identity/oauth2/web/guides/use-code-model, 2026-10-07 확인). https면 쿠키에 Secure를 붙인다.
    public_origin: str = ""
    #: 추가 로그인·WebSocket 허용 origin(쉼표 구분 환경변수). 기본은 비움.
    #: 기본 PUBLIC_ORIGIN을 유지하며 localhost 등 명시적으로 등록한 화면만 함께 사용한다.
    additional_public_origins: tuple[str, ...] = ()
    #: 로그인 유지 시간(초). 사용자 결정 2026-10-06 Q4 "한 근무 시간(12시간)".
    login_session_ttl_sec: float = 43200.0
    #: 구글 토큰 교환 요청 제한 시간(초). **측정값이 아니다** — 로그인 버튼이 끝없이 돌지 않게 둔 보수적 상한.
    google_http_timeout_sec: float = 10.0

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
            llm_base_url_override=os.environ.get("FORSTICK2_LLM_BASE_URL") or None,
            robot_profile_name=os.environ.get("FORSTICK2_ROBOT_PROFILE") or None,
            enable_fake_robot=flag("FORSTICK2_FAKE_ROBOT", True),
            enable_workcell_robot=flag("FORSTICK2_WORKCELL_ROBOT", False),
            workcell_manifest=path_env(
                "FORSTICK2_WORKCELL_MANIFEST",
                ROOT / "config" / "workcell" / "active.json"),
            enable_stt=flag("FORSTICK2_STT", True),
            stt_model_config_name=os.environ.get(
                "FORSTICK2_STT_MODEL_CONFIG", "valid_stt_model_lowspec.json"
            ),
            session_idle_timeout_sec=float(
                os.environ.get("FORSTICK2_SESSION_IDLE_SEC", "3600")
            ),
            plan_ttl_sec=float(os.environ.get("FORSTICK2_PLAN_TTL_SEC", "600")),
            client_grace_sec=float(
                os.environ.get("FORSTICK2_CLIENT_GRACE_SEC", "5")
            ),
            enable_sim_demo_web=flag("FORSTICK2_SIM_DEMO_WEB", True),
            enable_sim_demo_command=flag("FORSTICK2_SIM_DEMO_COMMAND", True),
            enable_sim_demo_intent=flag("FORSTICK2_SIM_DEMO_INTENT", True),
            sim_demo_intent_min_confidence=float(
                os.environ.get("FORSTICK2_SIM_DEMO_INTENT_MIN_CONFIDENCE", "0.7")
            ),
            sim_demo_confirm_ttl_sec=float(
                os.environ.get("FORSTICK2_SIM_DEMO_CONFIRM_TTL_SEC", "60")
            ),
            require_login=flag("FORSTICK2_REQUIRE_LOGIN", True),
            # 로그인 값은 파일에서 복사해 넣는 경우가 많다 — 앞뒤 공백·줄 끝 문자(\r)를 지운다.
            # 2026-10-07 Windows에서 만든 설정 파일의 \r이 값 끝에 붙어 출처 검사·구글 교환이 실패했다.
            google_client_id=os.environ.get("FORSTICK2_GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.environ.get("FORSTICK2_GOOGLE_CLIENT_SECRET", "").strip(),
            public_origin=os.environ.get("FORSTICK2_PUBLIC_ORIGIN", "").strip().rstrip("/"),
            additional_public_origins=tuple(
                origin.strip().rstrip("/")
                for origin in os.environ.get("FORSTICK2_ADDITIONAL_PUBLIC_ORIGINS", "").split(",")
                if origin.strip()
            ),
            login_session_ttl_sec=float(os.environ.get("FORSTICK2_LOGIN_SESSION_TTL_SEC", "43200")),
            google_http_timeout_sec=float(os.environ.get("FORSTICK2_GOOGLE_HTTP_TIMEOUT_SEC", "10")),
        )
