"""새 작업 화면의 서버 경계와 화면 정책 (html/목업 기준 재구현).

확인하는 것:

- 진입점이 `html/index.html`이고 새 자산(`/static/app.css`, `/static/js/*.js`)이
  정적 경로로 나간다
- **승인 UI 문구가 화면 파일 어디에도 없다** (계획 승인·승인 대기·승인 완료·
  계획 거부)
- 안전 판단은 PASS/BLOCK/ASK만 쓰고, 실행은 PASS에서만 열린다
- 전체 정지와 개별 실행 취소가 다른 동작·다른 결과로 남는다
- FR3-WMS 화면 데이터(구성 문구·지원 스킬·비활성 사유·배지)가 그대로 있다
- 실제 하드웨어·그리퍼·pick/place가 검증된 것처럼 보이지 않는다
- 폐기된 파일(`static/app.js`, `static/styles.css`)이 남아 있지 않다
- 화면 상태 테스트(node)가 통과한다
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from integration.test_session_isolation import IsolationCase

HTML = ROOT / "html"
STATIC = HTML / "static"
JS_DIR = STATIC / "js"

#: 화면에서 사라져야 하는 문구. 사용자 요구로 승인 단계를 화면에서 없앴다.
FORBIDDEN_TEXT = ("계획 승인", "승인 대기", "승인 완료", "계획 거부", "승인 필수")

#: 목업이 대체한 옛 구현 파일.
REPLACED = ("app.js", "styles.css")


def screen_files() -> list[Path]:
    return [HTML / "index.html", STATIC / "app.css", *sorted(JS_DIR.glob("*.js"))]


class TestScreenFiles(unittest.TestCase):
    def test_entry_point_is_index_html(self):
        index = (HTML / "index.html").read_text(encoding="utf-8")
        self.assertIn("/static/app.css", index)
        self.assertIn('type="module" src="/static/js/main.js"', index)
        self.assertIn("Robot Command Console", index)

    def test_replaced_files_are_gone(self):
        for name in REPLACED:
            with self.subTest(name=name):
                self.assertFalse((STATIC / name).exists(), f"{name}이 남아 있다")

    def test_replacement_is_recorded_before_deletion(self):
        record = ROOT / "reports/baseline/web_ui_replaced_2026-09-16.json"
        self.assertTrue(record.is_file(), "대체 기록이 없다")
        payload = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("static/app.js", payload["replaced"])
        self.assertIn("static/styles.css", payload["replaced"])
        self.assertIn("static/pcm-worklet.js", payload["kept"])
        self.assertIn("static/app.css", payload["replacement_map"]["static/styles.css"])

    def test_kept_assets_are_still_used(self):
        index = (HTML / "index.html").read_text(encoding="utf-8")
        self.assertIn("/static/icon.svg", index)
        backend = (JS_DIR / "backend-http.js").read_text(encoding="utf-8")
        # STT 오디오 계약은 그대로 쓴다.
        self.assertIn("/static/pcm-worklet.js", backend)
        self.assertIn("pcm-frame-processor", backend)

    def test_no_approval_wording_anywhere_in_the_screen(self):
        for path in screen_files():
            text = path.read_text(encoding="utf-8")
            for word in FORBIDDEN_TEXT:
                with self.subTest(file=path.name, word=word):
                    self.assertNotIn(word, text)

    def test_verdict_values_are_only_pass_block_ask(self):
        state = (JS_DIR / "state.js").read_text(encoding="utf-8")
        self.assertIn("PASS: 'PASS'", state)
        self.assertIn("BLOCK: 'BLOCK'", state)
        self.assertIn("ASK: 'ASK'", state)
        # 승인 상태값이 없다.
        self.assertNotIn("approved", state)
        self.assertNotIn("rejected", state)

    def test_execution_requires_pass_and_a_user_press(self):
        state = (JS_DIR / "state.js").read_text(encoding="utf-8")
        self.assertIn("export function canExecute", state)
        self.assertIn("state.validation.verdict !== VERDICT.PASS", state)
        main = (JS_DIR / "main.js").read_text(encoding="utf-8")
        # 실행은 버튼(execute-confirm)에서만 시작된다.
        self.assertIn("'execute-confirm'", main)
        self.assertIn("자동 실행 경로가 없다", main)

    def test_stop_and_cancel_are_separate_actions(self):
        main = (JS_DIR / "main.js").read_text(encoding="utf-8")
        self.assertIn("async function stopAll", main)
        self.assertIn("async function cancelExecution", main)
        self.assertIn("scope: global", main)
        self.assertIn("scope: execution", main)
        render = (JS_DIR / "render.js").read_text(encoding="utf-8")
        self.assertIn("전체 정지 결과", render)
        self.assertIn("실행 취소됨", render)
        self.assertIn("exec.canceled", render)
        self.assertIn("exec.stopped", render)

    def test_fr3_screen_data_is_present_and_honest(self):
        catalog = (JS_DIR / "catalog.js").read_text(encoding="utf-8")
        self.assertIn(
            "FAIRINO FR3-WMS + 2F-85 · Gazebo workcell simulation", catalog)
        self.assertIn("'home', 'move', 'stop'", catalog)
        self.assertIn("'pick', 'place'", catalog)
        self.assertIn("FR3 GAZEBO", catalog)
        self.assertIn(
            "2F-85 장착 근거, 그리퍼 close 안정성, 파지 관측, pick/place 재검증 미완료",
            catalog,
        )
        for badge in ("MoveIt2 검증 완료", "시뮬레이션 전용", "실하드웨어 미검증"):
            with self.subTest(badge=badge):
                self.assertIn(badge, catalog)
        # 검증되지 않은 값을 만들어 넣지 않는다.
        self.assertIn("'미확보'", catalog)
        self.assertIn("실기 하드웨어", catalog)

    def test_no_framework_and_no_build_step(self):
        """프레임워크를 쓰지 않고 빌드 단계도 없다(문구 언급은 허용).

        확인하는 것은 **실제 사용**이다 — import·script 태그·CDN 주소.
        """
        for path in screen_files():
            text = path.read_text(encoding="utf-8")
            for token in (
                "from 'react'", 'from "react"', "react-dom", "cdn.tailwindcss.com",
                "unpkg.com", "esm.sh", "require(", "vue", "svelte",
            ):
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text.lower())
        # 화면 실행 경로에 빌드 설정이 없다(목업 원본 폴더는 별개다).
        for name in ("package.json", "vite.config.ts", "tsconfig.json", "node_modules"):
            with self.subTest(name=name):
                self.assertFalse((HTML / name).exists())
        # 진입점은 우리 자산 두 개만 불러온다.
        index = (HTML / "index.html").read_text(encoding="utf-8")
        scripts = [
            line for line in index.splitlines()
            if "<script" in line or "<link rel=\"stylesheet\"" in line
        ]
        self.assertEqual(len(scripts), 2, scripts)

    def test_screen_state_tests_pass(self):
        """node로 화면 상태·렌더 테스트를 돌린다(없으면 건너뛴다)."""
        node = subprocess.run(
            ["bash", "-lc", "command -v node"], capture_output=True, text=True, timeout=60
        )
        if node.returncode != 0:
            self.skipTest("node가 없다")
        result = subprocess.run(
            [
                node.stdout.strip(),
                "--test",
                "tests/web/state.test.mjs",
                "tests/web/render.test.mjs",
            ],
            cwd=str(ROOT), capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-2000:])
        self.assertIn("# fail 0", result.stdout)


class TestScreenIsServed(IsolationCase):
    async def test_index_and_assets_are_served(self):
        index = await self.client.get("/")
        self.assertEqual(index.status, 200)
        self.assertIn("/static/js/main.js", index.text)
        for path, fragment in (
            ("/static/app.css", "css"),
            ("/static/js/main.js", "javascript"),
            ("/static/js/state.js", "javascript"),
            ("/static/js/render.js", "javascript"),
            ("/static/js/catalog.js", "javascript"),
            ("/static/js/backend-http.js", "javascript"),
            ("/static/js/backend-sim.js", "javascript"),
            ("/static/pcm-worklet.js", "javascript"),
            ("/static/icon.svg", "svg"),
        ):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 200)
                self.assertIn(fragment, response.headers["content-type"])

    async def test_old_assets_are_no_longer_served(self):
        for path in ("/static/app.js", "/static/styles.css"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status, 404)

    async def test_mockup_source_is_not_served(self):
        """목업 원본은 설계 근거로 저장소에 두지만 화면 경로로 나가지 않는다."""
        self.assertTrue((HTML / "목업" / "src" / "App.tsx").is_file())
        for path in ("/목업/index.html", "/static/../목업/src/App.tsx"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertIn(response.status, (403, 404))

    async def test_screen_endpoints_still_exist(self):
        """화면이 쓰는 경로가 그대로 있다(계약을 바꾸지 않았다)."""
        config = await self.client.get("/v1/config")
        self.assertEqual(config.status, 200)
        payload = config.json()
        self.assertIn("client_grace_sec", payload["session"])
        robots = await self.client.get("/v1/robots")
        self.assertEqual(robots.status, 200)

    async def test_grace_window_number_is_not_duplicated_in_screen_code(self):
        payload = (await self.client.get("/v1/config")).json()
        self.assertEqual(
            payload["session"]["client_grace_sec"], self.config.client_grace_sec
        )
        backend = (JS_DIR / "backend-http.js").read_text(encoding="utf-8")
        self.assertIn("client_grace_sec", backend)
        self.assertNotIn("graceSec = 5", backend)


if __name__ == "__main__":
    unittest.main()


class TestWorkcellScreen(unittest.TestCase):
    """작업 셀 화면 요구 (8-08 우선순위 6).

    화면 코드가 **좌표나 자원 목록을 만들어 두지 않는지** 본다. 표시되는
    자원·프레임은 서버 대조표에서만 와야 한다.
    """

    def test_workcell_card_exists_and_reads_server_table(self):
        render = (JS_DIR / "render.js").read_text(encoding="utf-8")
        self.assertIn("renderWorkcellCard", render)
        self.assertIn("작업 셀 자원", render)
        # 서버 대조표에서 읽는다.
        self.assertIn("workcellResources", render)
        self.assertIn("serverWorkcell", render)
        index = (ROOT / "html" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="card-workcell"', index)

    def test_screen_does_not_hardcode_scene_ids_or_coordinates(self):
        catalog = (JS_DIR / "catalog.js").read_text(encoding="utf-8")
        render = (JS_DIR / "render.js").read_text(encoding="utf-8")
        for banned in ("pallet_1_frame", "material_a_frame", "conveyor_frame",
                       "pallet_1_approach", "forstick2_fr3_2f85_workcell"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, catalog)
                self.assertNotIn(banned, render)

    def test_execution_card_shows_observed_values_only(self):
        render = (JS_DIR / "render.js").read_text(encoding="utf-8")
        self.assertIn("renderObservation", render)
        for label in ("planning scene", "컨트롤러", "관측 관절값",
                      "그리퍼 개구", "STOP 확인"):
            with self.subTest(label=label):
                self.assertIn(label, render)
        # 관측이 없으면 없다고 적는다.
        self.assertIn("아직 관측값이 없습니다", render)
        self.assertIn("관측 없음", render)

    def test_plan_card_shows_resource_to_scene_mapping(self):
        render = (JS_DIR / "render.js").read_text(encoding="utf-8")
        self.assertIn("자원 대조 (발화 → 씬 → 프레임)", render)
        self.assertIn("씬 대조표에 없습니다", render)

    def test_voice_final_drives_plan_and_stop_keywords_bypass_planning(self):
        main = (JS_DIR / "main.js").read_text(encoding="utf-8")
        # final만 계획 생성으로 간다.
        self.assertIn("handleFinalTranscript", main)
        self.assertIn("lastHandledFinal", main)
        # 정지 키워드는 서버 정책에서 온다(화면에 목록을 두지 않는다).
        self.assertIn("stop_keywords", main)
        for banned in ("'멈춰'", "'정지'", "'스톱'"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, main)
        # 정지 발화는 계획 생성을 거치지 않는다.
        self.assertIn("isStopUtterance", main)
