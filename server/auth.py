"""로그인(구글만, 등록 계정만) — 인가 코드 교환·로그인 세션·출입 검사 (2026-10-06 결정 Q1~Q10).

지키는 것(이 모듈이 책임지는 불변 조건):
- **미리 등록된 이메일만 들인다.** 구글 계정이 있다는 것만으로 통과시키지 않는다.
  `email_verified`가 참인 ID 토큰의 이메일이 `accounts`에 있고 사용 가능일 때만 세션을 준다.
- 확인이 안 되면 들이지 않는다(설계 원칙 4). 구글 설정이 없거나, 교환이 실패하거나, 토큰 값이
  하나라도 어긋나면 거절한다. 쿠키가 없거나 모르는 값이거나 만료면 401이다.
- **정지는 로그인 없이 받는다**(`PUBLIC_POST`). 세션이 끊겨도 즉시 정지가 늦어지면 안 된다(결정 Q5).
  정지 해제(`/v1/stop/release`)는 동작을 다시 여는 것이라 여기 넣지 않는다.
- 세션 토큰 원문은 쿠키에만 있다. DB에는 SHA-256만 저장한다.
- 쿠키는 HttpOnly·SameSite=Lax(다른 사이트에서 보낸 POST에 쿠키가 실리지 않는다), 화면 주소가 https면 Secure.
- 로그인 요청은 `X-Requested-With` 헤더와 Origin(화면 주소)을 확인한다 — 다른 사이트가 남의 인가 코드로
  피해자 브라우저를 로그인시키는 것(로그인 CSRF)을 막는다. 구글 팝업 코드 흐름 지침이 요구한다.
- 구글 교환(네트워크)은 이벤트 루프 밖 스레드에서 한다 — 느린 구글 응답이 정지 요청을 늦추면 안 된다.

ID 토큰 서명 검사를 하지 않는 이유:
- 토큰을 브라우저에서 받지 않고 서버가 구글 토큰 엔드포인트에서 TLS로 직접 받는다. OpenID Connect Core
  3.1.3.7 6항이 이 경우 서명 대신 TLS 서버 검증으로 발급자를 확인해도 된다고 정한다. 그래서 의존성(키 조회·
  JWT 라이브러리)을 넣지 않고 iss·aud·exp·email_verified만 확인한다. 토큰을 브라우저에서 받게 바꾸면
  서명 검사가 필요해진다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Callable

from core.reason_codes import ReasonCode
from server.config import ServerConfig
from storage.records import AccountRecord
from storage.repository import Repository

COOKIE_NAME = "forstick2_session"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})

#: 로그인 없이 받는 요청. 로그인 자체와 정지만.
PUBLIC_POST = frozenset({"/v1/auth/google", "/v1/auth/logout", "/v1/stop", "/v1/sim-demo/stop", "/v1/humanoid/stop"})


def is_public(method: str, path: str) -> bool:
    if path == "/v1/auth/me" or (method == "POST" and path in PUBLIC_POST):
        return True
    # 목표 정지: POST /v1/sim-demo/goals/<id>/stop
    if method == "POST" and path.startswith("/v1/sim-demo/goals/") and path.endswith("/stop"):
        return True
    # 정적 화면 파일(구 웹 UI). 화면은 보여도 API는 막힌다.
    return method == "GET" and (path in ("/", "/index.html") or path.startswith("/static/"))


def cookie_token(headers: list[tuple[bytes, bytes]]) -> str | None:
    for key, value in headers:
        if key.lower() == b"cookie":
            jar = SimpleCookie()
            try:
                jar.load(value.decode("latin-1"))
            except Exception:  # noqa: BLE001 — 깨진 쿠키는 '없음'으로 본다
                return None
            if COOKIE_NAME in jar:
                return jar[COOKIE_NAME].value or None
    return None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class LoginError(Exception):
    """로그인 거절. HTTP 상태·이유 코드·화면에 돌려줄 값을 함께 담는다."""

    def __init__(self, status: int, reason: ReasonCode, message: str, email: str | None = None):
        super().__init__(message)
        self.status, self.reason, self.message, self.email = status, reason, message, email

    def to_dict(self) -> dict:
        body = {"error": self.message, "reason_code": self.reason.value}
        if self.email:
            body["email"] = self.email
        return body


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, "구글 ID 토큰을 읽지 못했다") from None


def check_id_token(claims: dict, *, client_id: str, now: float) -> dict:
    """구글 토큰 엔드포인트가 준 ID 토큰의 값을 확인한다. 하나라도 어긋나면 LoginError."""
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, "ID 토큰 발급자가 구글이 아니다")
    # 단일 audience만 받는다 — 다른 앱이 함께 들어 있는 토큰은 거절(OIDC Core 3.1.3.7 3항).
    if claims.get("aud") != client_id:
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, "ID 토큰이 이 앱(클라이언트 ID)의 것이 아니다")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now:
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, "ID 토큰이 만료됐다")
    if claims.get("email_verified") is not True or not claims.get("email") or not claims.get("sub"):
        raise LoginError(403, ReasonCode.SESSION_ACCOUNT_NOT_REGISTERED, "구글이 확인한 이메일이 없다")
    return claims


def google_exchange(code: str, config: ServerConfig) -> dict:
    """인가 코드를 구글 토큰 엔드포인트에서 ID 토큰으로 바꾼다. 확인 전 claims를 돌려준다."""
    body = urllib.parse.urlencode({
        "code": code, "client_id": config.google_client_id, "client_secret": config.google_client_secret,
        "redirect_uri": config.public_origin, "grant_type": "authorization_code",
    }).encode()
    request = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=config.google_http_timeout_sec) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, f"구글 코드 교환 실패({exc.code}): {detail}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, f"구글에 연결하지 못했다: {exc}") from None
    if not payload.get("id_token"):
        raise LoginError(502, ReasonCode.SESSION_LOGIN_FAILED, "구글 응답에 ID 토큰이 없다")
    return _jwt_claims(payload["id_token"])


def header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def origin_ok(headers: list[tuple[bytes, bytes]], public_origin: str) -> bool:
    """브라우저가 보낸 Origin이 화면 주소와 같은지. Origin이 없으면(브라우저 밖 요청) 통과 —
    쿠키를 실어 보내는 공격은 브라우저에서 오고, 브라우저는 POST·WebSocket에 Origin을 붙인다."""
    origin = header(headers, b"origin")
    return origin is None or (bool(public_origin) and origin.rstrip("/") == public_origin)


def public_user(account: AccountRecord) -> dict:
    return {"email": account.email, "name": account.name, "picture": account.picture}


@dataclass
class AuthService:
    repository: Repository
    config: ServerConfig
    #: 테스트가 구글 대신 끼우는 자리. (code, config) → claims
    exchange: Callable[[str, ServerConfig], dict] = google_exchange
    now: Callable[[], float] = time.time

    def login(self, code: str) -> tuple[AccountRecord, str]:
        """인가 코드 → 등록 계정 확인 → (계정, 새 세션 토큰)."""
        cfg = self.config
        if not (cfg.google_client_id and cfg.google_client_secret and cfg.public_origin):
            raise LoginError(503, ReasonCode.CONFIG_MISSING, "구글 로그인 설정(클라이언트 ID·보안 비밀·화면 주소)이 없다")
        if not code:
            raise LoginError(400, ReasonCode.CONFIG_MISSING, "code가 없다")
        claims = check_id_token(self.exchange(code, cfg), client_id=cfg.google_client_id, now=self.now())
        email = str(claims["email"]).strip().lower()
        at = self.now()
        token = secrets.token_urlsafe(32)
        opened = self.repository.open_login_session(
            email, google_sub=str(claims["sub"]), name=claims.get("name"), picture=claims.get("picture"),
            token_hash=_hash(token), at=at, expires_at=at + cfg.login_session_ttl_sec,
        )
        if opened is not None:
            return opened, token
        account = self.repository.get_account(email)  # 거절 사유만 고른다(세션은 이미 안 만들어졌다)
        if account is not None and account.google_sub and account.google_sub != claims["sub"]:
            raise LoginError(403, ReasonCode.SESSION_ACCOUNT_NOT_REGISTERED, "등록된 구글 계정과 다르다", email)
        if account is not None and not account.enabled:
            raise LoginError(403, ReasonCode.SESSION_ACCOUNT_DISABLED, "사용이 중지된 계정이다", email)
        raise LoginError(403, ReasonCode.SESSION_ACCOUNT_NOT_REGISTERED, "등록되지 않은 계정이다", email)

    def resolve(self, token: str | None) -> tuple[AccountRecord | None, ReasonCode]:
        """쿠키 토큰 → (계정, 이유). 계정이 None이면 이유가 거절 사유다."""
        if not token:
            return None, ReasonCode.SESSION_UNAUTHENTICATED
        row = self.repository.get_login_session(_hash(token))
        if row is None:
            return None, ReasonCode.SESSION_UNAUTHENTICATED
        email, expires_at = row
        if expires_at <= self.now():
            self.repository.delete_login_session(_hash(token))
            return None, ReasonCode.SESSION_EXPIRED
        account = self.repository.get_account(email)
        if account is None or not account.enabled:
            return None, ReasonCode.SESSION_ACCOUNT_DISABLED
        return account, ReasonCode.SESSION_UNAUTHENTICATED

    def refusal(self, method: str, path: str, token: str | None) -> tuple[int, list, bytes] | None:
        """출입 검사. 로그인이 필요한데 없으면 401 응답, 통과면 None(`server/asgi.py`가 모든 요청에 부른다)."""
        if not self.config.require_login or is_public(method, path):
            return None
        account, reason = self.resolve(token)
        if account is not None:
            return None
        body = json.dumps({"error": "로그인이 필요하다", "reason_code": reason.value}, ensure_ascii=False).encode("utf-8")
        return 401, [], body

    def ws_refused(self, path: str, headers: list[tuple[bytes, bytes]]) -> bool:
        """WebSocket 출입 검사. 로그인 + 다른 출처의 페이지가 쿠키로 붙는 것 거절."""
        if not self.config.require_login:
            return False
        if not origin_ok(headers, self.config.public_origin):
            return True
        return self.refusal("GET", path, cookie_token(headers)) is not None

    def logout(self, token: str | None) -> None:
        if token:
            self.repository.delete_login_session(_hash(token))

    def set_cookie(self, token: str) -> tuple[bytes, bytes]:
        return b"set-cookie", self._cookie(token, int(self.config.login_session_ttl_sec)).encode()

    def clear_cookie(self) -> tuple[bytes, bytes]:
        return b"set-cookie", self._cookie("", 0).encode()

    def _cookie(self, value: str, max_age: int) -> str:
        secure = "; Secure" if self.config.public_origin.startswith("https://") else ""
        return f"{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"
