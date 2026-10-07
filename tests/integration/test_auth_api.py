"""로그인 경계 — `/v1/auth/*`와 출입 검사 (2026-10-06 결정, `server/auth.py`).

구글은 부르지 않는다. `AuthService.exchange`에 구글 토큰 엔드포인트가 줄 ID 토큰 claims를 끼운다.
시각도 주입해 만료를 결정적으로 검사한다.
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from integration.asgi_client import HttpClient, WebSocketSession  # noqa: E402
from server.asgi import Application  # noqa: E402
from core.constants import TASK_PLAN_SCHEMA_VERSION  # noqa: E402
from server.auth import COOKIE_NAME, check_id_token, link_ids, LoginError  # noqa: E402
from storage.records import RequestRecord  # noqa: E402
from server.config import ServerConfig  # noqa: E402
from server.runtime import build_runtime  # noqa: E402

CLIENT_ID = "fixture-client.apps.googleusercontent.com"
NOW = 1_800_000_000.0


def claims(email="operator@example.com", sub="sub-1", **over):
    base = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": NOW + 600,
            "email": email, "email_verified": True, "sub": sub, "name": "김민우", "picture": "https://example.com/p.png"}
    base.update(over)
    return base


class _AuthBase(unittest.IsolatedAsyncioTestCase):
    """로그인 시험 공통 준비(구글·시각 주입). 테스트는 아래 하위 클래스에."""
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = dataclasses.replace(
            ServerConfig.from_env(), db_path=Path(self.tmp.name) / "web.sqlite3",
            enable_stt=False, llm_config_name="__absent__.json",
            require_login=True, google_client_id=CLIENT_ID, google_client_secret="fixture-secret",
            public_origin="https://foursticks.example", login_session_ttl_sec=43200.0,
        )
        runtime = build_runtime(self.config)
        self.addCleanup(runtime.repository.close)
        self.repo = runtime.repository
        self.app = Application(runtime=runtime, config=self.config)
        self.clock = [NOW]
        self.app.auth.now = lambda: self.clock[0]
        self.next_claims = claims()
        self.codes = []
        self.app.auth.exchange = lambda code, cfg: (self.codes.append((code, cfg.public_origin)), self.next_claims)[1]
        self.client = HttpClient(self.app)

    # 화면(dashboard2/src/auth.js)이 보내는 것과 같은 헤더 — 로그인 CSRF 검사용.
    BROWSER = [(b"x-requested-with", b"XMLHttpRequest"), (b"origin", b"https://foursticks.example")]

    async def login(self, code="qa-code", headers=None):
        return await self.client.request("POST", "/v1/auth/google", {"code": code},
                                         headers=self.BROWSER if headers is None else headers)

    @staticmethod
    def cookie_of(response):
        raw = response.headers["set-cookie"]
        return raw.split(";")[0].split("=", 1)[1], raw

    def with_cookie(self, token):
        return [(b"cookie", f"{COOKIE_NAME}={token}".encode())]


class AuthApiTest(_AuthBase):
    async def test_protected_api_needs_login(self):
        for path in ("/health", "/v1/config", "/v1/sim-demo", "/v1/history"):
            res = await self.client.get(path)
            self.assertEqual(res.status, 401, path)
            self.assertEqual(res.json()["reason_code"], "session.unauthenticated")

    async def test_stop_and_static_do_not_need_login(self):
        # 정지는 세션이 끊겨도 받아야 한다(결정 Q5). 출입 검사가 끼어들지 않는다는 것을, 로그인을 끈
        # 서버와 **같은 응답**이 나오는지로 확인한다(정지 결과 자체는 정지 라우트 테스트 몫).
        paths = ("/v1/stop", "/v1/sim-demo/stop", "/v1/sim-demo/goals/g1/stop", "/v1/humanoid/stop")
        gated = [(await self.client.post(p, {})).status for p in paths]
        self.app.auth.config = dataclasses.replace(self.config, require_login=False)
        open_ = [(await self.client.post(p, {})).status for p in paths]
        self.app.auth.config = self.config
        self.assertEqual(gated, open_)
        self.assertNotIn(401, gated)
        self.assertNotEqual((await self.client.get("/")).status, 401)
        # 정지 해제는 동작을 다시 여는 것이라 로그인이 필요하다.
        self.assertEqual((await self.client.post("/v1/stop/release", {})).status, 401)

    async def test_registered_account_logs_in_and_gets_a_session(self):
        self.repo.add_account("Operator@Example.com", at=NOW)
        res = await self.login()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.json()["user"], {"email": "operator@example.com", "name": "김민우", "picture": "https://example.com/p.png"})
        self.assertEqual(self.codes, [("qa-code", "https://foursticks.example")])  # 교환 redirect_uri = 화면 origin
        token, raw = self.cookie_of(res)
        for attr in ("HttpOnly", "SameSite=Lax", "Secure", "Max-Age=43200", "Path=/"):
            self.assertIn(attr, raw)
        me = await self.client.request("GET", "/v1/auth/me", headers=self.with_cookie(token))
        self.assertEqual(me.status, 200)
        self.assertEqual((await self.client.request("GET", "/health", headers=self.with_cookie(token))).status, 200)
        account = self.repo.get_account("operator@example.com")
        self.assertEqual(account.google_sub, "sub-1")
        self.assertEqual(account.first_login_at, NOW)
        # 토큰 원문은 DB에 없다
        self.assertIsNone(self.repo.get_login_session(token))

    async def test_login_without_browser_headers_is_refused(self):
        # 로그인 CSRF: X-Requested-With가 없거나 다른 출처에서 온 로그인 요청은 교환 전에 거절한다.
        self.repo.add_account("operator@example.com", at=NOW)
        for headers in ([], [(b"x-requested-with", b"XMLHttpRequest"), (b"origin", b"https://evil.example")]):
            res = await self.login(headers=headers)
            self.assertEqual(res.status, 403, headers)
            self.assertEqual(res.json()["reason_code"], "session.login_failed")
        self.assertEqual(self.codes, [])  # 구글 교환까지 가지 않았다

    async def test_websocket_from_another_origin_is_refused_even_with_cookie(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        evil = [*self.with_cookie(token), (b"origin", b"https://evil.example")]
        async with WebSocketSession(self.app, "/v1/sim-view/stream", headers=evil) as ws:
            pass
        self.assertEqual(ws.close_code, 4401)

    async def test_slow_google_does_not_delay_stop(self):
        # 구글 교환은 스레드에서 한다 — 느린 구글 응답 동안에도 정지 요청이 먼저 끝나야 한다(결정 Q5).
        import asyncio
        import time as _time
        self.repo.add_account("operator@example.com", at=NOW)
        done = []

        def slow(code, cfg):
            _time.sleep(0.5)
            return self.next_claims
        self.app.auth.exchange = slow

        async def stop():
            await self.client.post("/v1/stop", {})
            done.append("stop")

        async def login():
            await self.login()
            done.append("login")
        await asyncio.gather(login(), stop())
        self.assertEqual(done, ["stop", "login"])

    async def test_unregistered_account_is_refused_with_email(self):
        res = await self.login()
        self.assertEqual(res.status, 403)
        self.assertEqual(res.json()["reason_code"], "session.account_not_registered")
        self.assertEqual(res.json()["email"], "operator@example.com")
        self.assertNotIn("set-cookie", res.headers)

    async def test_disabled_account_is_refused_and_loses_sessions(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        self.repo.set_account_enabled("operator@example.com", False)
        self.assertEqual((await self.client.request("GET", "/v1/auth/me", headers=self.with_cookie(token))).status, 401)
        res = await self.login()
        self.assertEqual(res.status, 403)
        self.assertEqual(res.json()["reason_code"], "session.account_disabled")

    async def test_same_email_from_another_google_account_is_refused(self):
        self.repo.add_account("operator@example.com", at=NOW)
        self.assertEqual((await self.login()).status, 200)
        self.next_claims = claims(sub="sub-other")
        res = await self.login()
        self.assertEqual(res.status, 403)
        self.assertEqual(res.json()["reason_code"], "session.account_not_registered")

    async def test_session_expires_after_ttl(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        self.clock[0] = NOW + 43200
        me = await self.client.request("GET", "/v1/auth/me", headers=self.with_cookie(token))
        self.assertEqual(me.status, 401)
        self.assertEqual(me.json()["reason_code"], "session.expired")

    async def test_logout_deletes_the_session(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        res = await self.client.request("POST", "/v1/auth/logout", headers=self.with_cookie(token))
        self.assertEqual(res.status, 200)
        self.assertIn("Max-Age=0", res.headers["set-cookie"])
        self.assertEqual((await self.client.request("GET", "/v1/auth/me", headers=self.with_cookie(token))).status, 401)

    async def test_missing_google_settings_refuse_login(self):
        self.app.auth.config = dataclasses.replace(self.config, google_client_secret="")
        res = await self.login()
        self.assertEqual(res.status, 503)
        self.assertEqual(res.json()["reason_code"], "config.missing")

    async def test_websocket_without_login_is_refused(self):
        async with WebSocketSession(self.app, "/v1/sim-view/stream") as ws:
            pass
        self.assertFalse(ws.accepted)
        self.assertEqual(ws.close_code, 4401)

    async def test_login_can_be_turned_off_for_development(self):
        self.app.auth.config = dataclasses.replace(self.config, require_login=False)
        self.assertEqual((await self.client.get("/health")).status, 200)


class LoginConfigFromEnvTest(unittest.TestCase):
    def test_login_values_ignore_surrounding_whitespace_and_cr(self):
        # Windows에서 만든 설정 파일의 \r이 값 끝에 붙어도 같은 값으로 읽는다(2026-10-07 실제 장애).
        from unittest import mock
        env = {"FORSTICK2_GOOGLE_CLIENT_ID": f" {CLIENT_ID}\r", "FORSTICK2_GOOGLE_CLIENT_SECRET": "fixture-secret\r",
               "FORSTICK2_PUBLIC_ORIGIN": "https://foursticks.example/\r"}
        with mock.patch.dict("os.environ", env):
            config = ServerConfig.from_env()
        self.assertEqual(config.google_client_id, CLIENT_ID)
        self.assertEqual(config.google_client_secret, "fixture-secret")
        self.assertEqual(config.public_origin, "https://foursticks.example")


class CommandActorTest(_AuthBase):
    """명령을 보낸 사람 기록(결정 Q10)."""

    def fake_plan(self):
        """계획 생성은 LLM이 필요하다 — 요청 기록을 만들고 request_id를 돌려주는 가짜로 바꾼다(기록 경로는 진짜)."""
        def create_plan(session_id, utterance, stt_inference_id=None):
            self.repo.save_request(RequestRecord(request_id="req-1", utterance=utterance,
                                                 schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=NOW))
            return {"ok": True, "request_id": "req-1"}
        self.app.api.create_plan = create_plan

    async def test_history_shows_who_sent_the_command(self):
        # 실제 HTTP 명령 → asgi가 처리 전에 정한 보낸 사람 → 응답 뒤 기록 → /v1/history의 요청자
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        self.fake_plan()
        res = await self.client.request("POST", "/v1/plan", {"session_id": "s1", "utterance": "A자재를 컨베이어로"},
                                        headers=self.with_cookie(token))
        self.assertEqual(res.status, 200)
        history = await self.client.request("GET", "/v1/history", headers=self.with_cookie(token))
        row = next(c for c in history.json()["commands"] if c["request_id"] == "req-1")
        self.assertEqual(row["actor"], {"email": "operator@example.com", "name": "김민우"})

    def test_stop_is_not_taken_as_who_started_the_job(self):
        # 정지 응답에도 대상 작업 id가 실린다 — 정지를 누른 사람(먼저 기록돼도)을 작업 요청자로 보지 않는다
        self.repo.add_account("a@example.com", at=NOW)
        self.repo.add_account("b@example.com", at=NOW)
        base = {"method": "POST", "status": 200, "request_id": None, "plan_id": None, "execution_id": None, "goal_id": None}
        self.repo.append_command_actor({**base, "audit_id": "1", "recorded_at": NOW, "actor_email": "b@example.com",
                                        "path": "/v1/sim-demo/stop", "job_id": "simjob_1"})
        self.assertEqual(self.repo.command_actors_for(job_ids=["simjob_1"]), {})
        self.repo.append_command_actor({**base, "audit_id": "2", "recorded_at": NOW + 1, "actor_email": "a@example.com",
                                        "path": "/v1/sim-demo/confirm", "job_id": "simjob_1"})
        self.repo.append_command_actor({**base, "audit_id": "3", "recorded_at": NOW + 2, "actor_email": "b@example.com",
                                        "path": "/v1/sim-demo/confirm", "status": 409, "job_id": "simjob_1"})
        self.assertEqual(self.repo.command_actors_for(job_ids=["simjob_1"])["simjob_1"]["email"], "a@example.com")

    async def test_logged_in_stop_records_the_account(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        await self.client.request("POST", "/v1/sim-demo/stop", {}, headers=self.with_cookie(token))
        rows = self.repo._conn.execute("SELECT actor_email FROM command_actors").fetchall()
        self.assertEqual([r["actor_email"] for r in rows], ["operator@example.com"])

    async def test_stop_without_login_is_recorded_without_actor(self):
        await self.client.post("/v1/sim-demo/stop", {})
        rows = self.repo._conn.execute("SELECT actor_email, path FROM command_actors").fetchall()
        self.assertEqual([(r["actor_email"], r["path"]) for r in rows], [(None, "/v1/sim-demo/stop")])

    async def test_reads_and_login_are_not_commands(self):
        self.repo.add_account("operator@example.com", at=NOW)
        token, _ = self.cookie_of(await self.login())
        await self.client.request("GET", "/v1/sim-demo", headers=self.with_cookie(token))
        await self.client.request("GET", "/health", headers=self.with_cookie(token))
        self.assertEqual(self.repo._conn.execute("SELECT COUNT(*) FROM command_actors").fetchone()[0], 0)


class LinkIdsTest(unittest.TestCase):
    def test_takes_top_level_and_own_job_only(self):
        body = b'{"request_id": "req-1", "job": {"job_id": "simjob_mine"}, "status": {"running_job": {"job_id": "simjob_other"}}}'
        self.assertEqual(link_ids(body), {"request_id": "req-1", "job_id": "simjob_mine"})

    def test_does_not_borrow_another_jobs_id(self):
        # 명령 응답에 같이 실린 다른 작업(진행 중·최근 목록)에 이 사람을 붙이지 않는다
        body = b'{"job": null, "running_job": {"job_id": "simjob_other"}, "recent_jobs": [{"job_id": "simjob_x"}]}'
        self.assertEqual(link_ids(body), {})


class IdTokenCheckTest(unittest.TestCase):
    def test_accepts_google_token_for_this_app(self):
        self.assertEqual(check_id_token(claims(), client_id=CLIENT_ID, now=NOW)["sub"], "sub-1")

    def test_refuses_any_mismatch(self):
        for over, reason in [
            ({"iss": "https://evil.example"}, "session.login_failed"),
            ({"aud": "other-app"}, "session.login_failed"),
            ({"aud": [CLIENT_ID, "other-app"]}, "session.login_failed"),
            ({"exp": NOW}, "session.login_failed"),
            ({"email_verified": False}, "session.account_not_registered"),
            ({"email": ""}, "session.account_not_registered"),
        ]:
            with self.assertRaises(LoginError, msg=str(over)) as ctx:
                check_id_token(claims(**over), client_id=CLIENT_ID, now=NOW)
            self.assertEqual(ctx.exception.reason.value, reason, over)


if __name__ == "__main__":
    unittest.main()
