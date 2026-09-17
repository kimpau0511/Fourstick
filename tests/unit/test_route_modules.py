"""라우트 모듈 분리 (md/개발플랜.md 3-04).

확인하는 것:

- 진입점(`server/asgi.py`)에 **경로 처리 본문이 없다.** 앱 조립·정적 파일·오류
  응답만 남는다.
- 경로가 담당 모듈에 있다: planning / stt / execution / robot / 공통.
- 라우트 모듈이 **전역 가변 상태를 만들지 않는다.** 모든 입력은
  `RouteContext`(Api·Runtime·설정·Repository)와 명시적 식별자로 들어온다.
- 모듈이 자기 경로가 아니면 `None`을 돌려준다(진입점이 다음 모듈에 묻는다).
"""

from __future__ import annotations

import ast
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server import asgi
from server.routes import HTTP_ROUTES, common, execution, planning, robot, session, stt

ROUTE_DIR = ROOT / "server" / "routes"

#: 경로 문자열이 어느 모듈에 있어야 하는가.
EXPECTED_OWNER = {
    "/v1/plan": "planning",
    "/v1/decision": "execution",
    "/v1/execute": "execution",
    "/v1/executions/": "execution",
    "/v1/stop": "execution",
    "/v1/state": "execution",
    "/v1/robots": "robot",
    "/v1/sessions": "session",
    "/health": "session",
    "/v1/config": "session",
}


def module_source(name: str) -> str:
    return (ROUTE_DIR / f"{name}.py").read_text(encoding="utf-8")


def code_strings(source: str) -> list[str]:
    """docstring을 뺀 코드 문자열만. 문단 언급을 코드로 오인하지 않기 위해."""
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.add(doc)
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and node.value not in docs
    ]


class TestEntryPointHasNoRoutes(unittest.TestCase):
    def test_entry_point_does_not_contain_api_paths(self):
        literals = code_strings((ROOT / "server" / "asgi.py").read_text(encoding="utf-8"))
        for path in EXPECTED_OWNER:
            with self.subTest(path=path):
                self.assertNotIn(path, literals)

    def test_entry_point_keeps_only_assembly_static_and_errors(self):
        source = (ROOT / "server" / "asgi.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        app = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "Application"
        )
        methods = {
            node.name for node in app.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertEqual(methods, {
            "__init__", "__call__", "_lifespan", "_http", "_body", "_route",
            "_static", "_websocket",
        })

    def test_entry_point_is_small(self):
        lines = (ROOT / "server" / "asgi.py").read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 250)


class TestPathsLiveInTheirModule(unittest.TestCase):
    def test_each_path_is_in_its_owner_module(self):
        for path, owner in EXPECTED_OWNER.items():
            with self.subTest(path=path):
                self.assertIn(path, code_strings(module_source(owner)))

    def test_no_module_claims_another_modules_path(self):
        for name in ("planning", "execution", "robot", "session"):
            literals = code_strings(module_source(name))
            for path, owner in EXPECTED_OWNER.items():
                if owner == name:
                    continue
                # 같은 접두사를 공유하는 경로는 예외로 둔다(/v1/sessions 계열).
                if path.startswith("/v1/sessions") and name == "session":
                    continue
                with self.subTest(module=name, path=path):
                    self.assertNotIn(path, literals)

    def test_websocket_handlers_are_in_their_modules(self):
        self.assertTrue(hasattr(execution, "events_socket"))
        self.assertTrue(hasattr(stt, "stt_socket"))
        self.assertFalse(hasattr(asgi, "_stt_event"))


class TestNoGlobalStateBetweenRoutes(unittest.TestCase):
    """라우트 사이에 '현재 상태'를 만들지 않는다."""

    def test_route_modules_have_no_mutable_module_level_state(self):
        for name in ("planning", "execution", "robot", "session", "stt"):
            tree = ast.parse(module_source(name))
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                for var in names:
                    with self.subTest(module=name, var=var):
                        # 상수만 허용한다(대문자 이름).
                        self.assertTrue(
                            var.isupper(),
                            f"{name}.py의 모듈 수준 변수 {var}는 가변 상태다",
                        )

    def test_route_modules_receive_everything_through_context(self):
        for name in ("planning", "execution", "robot", "session"):
            tree = ast.parse(module_source(name))
            handler = next(
                node for node in tree.body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle"
            )
            args = [a.arg for a in handler.args.args]
            with self.subTest(module=name):
                self.assertEqual(
                    args, ["ctx", "method", "path", "receive", "query"]
                )

    def test_context_exposes_repository_and_config(self):
        fields = set(common.RouteContext.__dataclass_fields__)
        self.assertEqual(fields, {"api", "runtime", "config", "hub", "read_body"})
        self.assertTrue(hasattr(common.RouteContext, "repository"))

    def test_modules_do_not_import_each_other(self):
        for name in ("planning", "robot", "session", "stt"):
            tree = ast.parse(module_source(name))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
            siblings = {
                f"server.routes.{other}"
                for other in ("planning", "execution", "robot", "session", "stt")
                if other != name
            }
            with self.subTest(module=name):
                self.assertEqual(imported & siblings, set())


class TestForeignPathsReturnNone(unittest.IsolatedAsyncioTestCase):
    async def test_modules_pass_on_paths_they_do_not_own(self):
        async def read_body(receive):
            return {}

        ctx = common.RouteContext(
            api=None, runtime=None, config=None, hub=common.EventHub(),
            read_body=read_body,
        )
        for module in HTTP_ROUTES:
            with self.subTest(module=module.__name__):
                result = await module.handle(ctx, "GET", "/nope", None, {})
                self.assertIsNone(result)

    async def test_route_order_is_declared(self):
        names = [m.__name__.rsplit(".", 1)[-1] for m in HTTP_ROUTES]
        self.assertEqual(
            set(names),
            {"session", "planning", "execution", "robot", "scene"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
