"""함수 안 import가 모듈 import를 가리지 않는지 — 서버 코드.

함수 본문 어디에서든 `import X`를 하면 X는 그 함수 **전체**에서 지역 변수가 된다.
그 import보다 앞에서 모듈의 X를 쓰면 UnboundLocalError가 난다. `build_runtime`에서
이 때문에 작업 셀 어댑터 등록이 실패해 FR3가 '로봇 미설정'으로 떨어진 적이 있다.
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _module_imports(tree: ast.Module) -> set[str]:
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
    return names


def _own_nodes(func: ast.AST):
    """중첩 함수·클래스를 뺀 이 함수 본문의 노드."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _read_before_local_import(func: ast.AST, module: set[str]) -> list[str]:
    """모듈에서 가져온 이름을, 이 함수가 다시 import하기 **전에** 읽는 경우."""
    first_import: dict[str, int] = {}
    loads: dict[str, int] = {}
    for node in _own_nodes(func):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                name = (a.asname or a.name).split(".")[0] if isinstance(node, ast.Import) \
                    else (a.asname or a.name)
                first_import[name] = min(first_import.get(name, node.lineno), node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id] = min(loads.get(node.id, node.lineno), node.lineno)
    return sorted(n for n, line in first_import.items()
                  if n in module and n in loads and loads[n] < line)


class ImportShadowingTest(unittest.TestCase):
    def test_server_functions_do_not_shadow_module_imports(self):
        problems = []
        for path in sorted((ROOT / "server").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = _module_imports(tree)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for name in _read_before_local_import(node, module):
                        problems.append(f"{path.relative_to(ROOT)}:{node.lineno} "
                                        f"{node.name}()가 {name}를 다시 import하기 전에 읽는다")
        self.assertEqual(problems, [])

    def test_build_runtime_keeps_importlib_global(self):
        from server.runtime import build_runtime
        self.assertNotIn("importlib", build_runtime.__code__.co_varnames)


if __name__ == "__main__":
    unittest.main()
