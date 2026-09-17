"""기하 검사 계약 (md/개발플랜.md 6-05) — 합성 Validator로 검증.

**실제 로봇 기구학·좌표를 만들지 않는다.** 여기 쓰는 Validator는 시험용이고,
"어떤 판정이 나와야 하는가"만 확인한다.

확인하는 것:
- 구현체 없음 → ASK (검사하지 않은 상태를 안전으로 보지 않는다)
- 환경 snapshot 없음·만료 → ASK, 구현체를 부르지 않는다
- 계획과 snapshot의 좌표계가 다름 → ASK (변환해 주지 않는다)
- 알려진 충돌 → BLOCK, 작업공간 이탈 → BLOCK (서로 다른 ReasonCode)
- 구현체 제한시간·예외 → ASK
- 입력이 불완전한데 ALLOW를 만들려는 시도 → 계약 위반으로 거부
- 구현체가 다른 snapshot·좌표계를 적은 ALLOW → ASK(verdict_invalid)
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.geometry import (
    EnvironmentSnapshot,
    GeometryDecision,
    GeometryError,
    GeometryReason,
    GeometryRequest,
    GeometryVerdict,
    snapshot_hash,
)
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceEntry, ResourceKind
from core.task_plan import TaskPlan, TaskStep
from robots.fake.geometry import DevFakeGeometryValidator, FakeCell
from validation.geometry_check import (
    NO_VALIDATOR_ID,
    check_geometry,
    geometry_rule_result,
)

FRAME = "synth_base"
NOW = 10_000.0


def plan(steps=None) -> TaskPlan:
    return TaskPlan(
        "plan_geom", "synth_robot", "synthetic", "test-1.0",
        steps or (
            TaskStep("move", {"to": "loc_a"}),
            TaskStep("pick", {"object": "obj_a", "from": "loc_a"}),
            TaskStep("move", {"to": "loc_b"}),
            TaskStep("place", {"object": "obj_a", "to": "loc_b"}),
            TaskStep("home"),
        ),
        created_at=NOW - 10, ttl_sec=600.0,
    )


def snapshot(**over) -> EnvironmentSnapshot:
    kw = dict(
        snapshot_id="synth_cell", snapshot_version="test-1.0",
        content_hash=snapshot_hash(["synth_cell", "test-1.0"]),
        frame_id=FRAME, captured_at=NOW - 1.0, ttl_sec=30.0, source="시험",
    )
    kw.update(over)
    return EnvironmentSnapshot(**kw)


def request(**over) -> GeometryRequest:
    target = over.pop("plan", None) or plan()
    kw = dict(
        plan=target, plan_hash=target.plan_hash(), robot_id="synth_robot",
        profile_id="synthetic", profile_version="test-1.0", frame_id=FRAME,
        checked_at=NOW, snapshot=snapshot(), timeout_sec=1.0,
    )
    kw.update(over)
    return GeometryRequest(**kw)


def catalog() -> ResourceCatalog:
    return ResourceCatalog(catalog_version="test-1.0", entries=(
        ResourceEntry("loc_a", ResourceKind.LOCATION, "A 위치", ("A 위치", "loc_a")),
        ResourceEntry("loc_b", ResourceKind.LOCATION, "B 위치", ("B 위치", "loc_b")),
        ResourceEntry("obj_a", ResourceKind.OBJECT, "A 자재", ("A 자재", "obj_a")),
    ))


def dev_validator(**over) -> DevFakeGeometryValidator:
    cell = FakeCell.from_catalog(catalog(), frame_id=FRAME, **over)
    return DevFakeGeometryValidator(cell)


class SlowValidator:
    validator_id = "slow-test"
    validator_version = "1.0"

    def __init__(self, delay: float = 1.0):
        self.delay = delay
        self.entered = threading.Event()

    def check(self, request):
        self.entered.set()
        time.sleep(self.delay)
        return GeometryVerdict(
            decision=GeometryDecision.ALLOW, validator_id=self.validator_id,
            validator_version=self.validator_version, input_complete=True,
            started_at=request.checked_at, finished_at=request.checked_at,
            snapshot_id=request.snapshot.snapshot_id, frame_id=request.frame_id,
        )


class BrokenValidator:
    validator_id = "broken-test"
    validator_version = "1.0"

    def __init__(self, error: Exception):
        self.error = error

    def check(self, request):
        raise self.error


class LyingValidator:
    """다른 snapshot·좌표계를 적은 ALLOW를 돌려주는 구현체."""

    validator_id = "lying-test"
    validator_version = "1.0"

    def check(self, request):
        return GeometryVerdict(
            decision=GeometryDecision.ALLOW, validator_id=self.validator_id,
            validator_version=self.validator_version, input_complete=True,
            started_at=request.checked_at, finished_at=request.checked_at,
            snapshot_id="다른_snapshot", snapshot_hash="0" * 64,
            frame_id="다른_좌표계",
        )


class ImpostorValidator:
    """자기 이름을 다른 구현체로 적는 경우."""

    validator_id = "impostor-test"
    validator_version = "1.0"

    def check(self, request):
        return GeometryVerdict(
            decision=GeometryDecision.BLOCK, validator_id="누군가_다른_검사기",
            validator_version="9.9", input_complete=True,
            started_at=request.checked_at, finished_at=request.checked_at,
            reasons=(GeometryReason(ReasonCode.GEOMETRY_COLLISION, "충돌"),),
        )


class TestNotCheckedIsNotSafe(unittest.TestCase):
    def test_missing_validator_is_ask(self):
        verdict = check_geometry(None, request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(
            ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE, verdict.reason_codes()
        )
        self.assertFalse(verdict.input_complete)
        self.assertEqual(verdict.validator_id, NO_VALIDATOR_ID)

    def test_allow_cannot_be_built_without_complete_input(self):
        with self.assertRaises(GeometryError) as ctx:
            GeometryVerdict(
                decision=GeometryDecision.ALLOW, validator_id="x",
                validator_version="1", input_complete=False,
                started_at=NOW, finished_at=NOW,
                snapshot_id="s", frame_id=FRAME,
            )
        self.assertIs(ctx.exception.reason, ReasonCode.GEOMETRY_VERDICT_INVALID)

    def test_allow_cannot_carry_reasons(self):
        with self.assertRaises(GeometryError):
            GeometryVerdict(
                decision=GeometryDecision.ALLOW, validator_id="x",
                validator_version="1", input_complete=True,
                started_at=NOW, finished_at=NOW, snapshot_id="s", frame_id=FRAME,
                reasons=(GeometryReason(ReasonCode.GEOMETRY_COLLISION, "충돌"),),
            )

    def test_allow_must_name_environment_and_frame(self):
        with self.assertRaises(GeometryError):
            GeometryVerdict(
                decision=GeometryDecision.ALLOW, validator_id="x",
                validator_version="1", input_complete=True,
                started_at=NOW, finished_at=NOW,
            )

    def test_block_reason_cannot_be_used_for_ask(self):
        with self.assertRaises(GeometryError):
            GeometryVerdict(
                decision=GeometryDecision.ASK, validator_id="x",
                validator_version="1", input_complete=False,
                started_at=NOW, finished_at=NOW,
                reasons=(GeometryReason(ReasonCode.GEOMETRY_COLLISION, "충돌"),),
            )


class TestEnvironmentInput(unittest.TestCase):
    def test_missing_snapshot_is_ask_and_validator_is_not_called(self):
        validator = dev_validator()
        called = []
        original = validator.check
        validator.check = lambda req: (called.append(req), original(req))[1]
        verdict = check_geometry(validator, request(snapshot=None))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(
            ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE, verdict.reason_codes()
        )
        self.assertEqual(called, [])

    def test_expired_snapshot_is_ask(self):
        old = snapshot(captured_at=NOW - 100.0, ttl_sec=10.0)
        verdict = check_geometry(dev_validator(), request(snapshot=old))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED, verdict.reason_codes())

    def test_snapshot_ttl_must_be_positive(self):
        with self.assertRaises(GeometryError):
            snapshot(ttl_sec=0.0)

    def test_different_frames_are_ask_not_converted(self):
        other = snapshot(frame_id="다른_좌표계")
        verdict = check_geometry(dev_validator(), request(snapshot=other))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_FRAME_UNKNOWN, verdict.reason_codes())
        self.assertIn("변환하지 않는다", verdict.reasons[0].detail)

    def test_unknown_frame_is_ask(self):
        """좌표계를 모르는 상태는 요청에 표현할 수 있어야 한다(빈 frame_id).

        그 상태를 통과로 바꾸지 않고 ASK로 돌리는 것이 이 층의 일이다.
        """
        verdict = check_geometry(dev_validator(), request(frame_id=""))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_FRAME_UNKNOWN, verdict.reason_codes())

    def test_snapshot_hash_changes_with_declaration(self):
        base = FakeCell.from_catalog(catalog(), frame_id=FRAME)
        changed = FakeCell.from_catalog(
            catalog(), frame_id=FRAME, blocked_locations=frozenset({"loc_b"})
        )
        self.assertNotEqual(base.content_hash(), changed.content_hash())


class TestDevFakeValidator(unittest.TestCase):
    def test_plan_inside_declared_cell_is_allowed(self):
        verdict = check_geometry(dev_validator(), request())
        self.assertIs(verdict.decision, GeometryDecision.ALLOW)
        self.assertTrue(verdict.input_complete)
        self.assertEqual(verdict.snapshot_id, "synth_cell")
        self.assertEqual(verdict.frame_id, FRAME)
        self.assertEqual(verdict.evidence["method"], "declared_symbols_only")

    def test_known_collision_is_block(self):
        validator = dev_validator(collisions=(("obj_a", "loc_b"),))
        verdict = check_geometry(validator, request())
        self.assertIs(verdict.decision, GeometryDecision.BLOCK)
        self.assertIn(ReasonCode.GEOMETRY_COLLISION, verdict.reason_codes())

    def test_workspace_violation_is_block_with_its_own_code(self):
        validator = dev_validator(blocked_locations=frozenset({"loc_b"}))
        verdict = check_geometry(validator, request())
        self.assertIs(verdict.decision, GeometryDecision.BLOCK)
        self.assertIn(
            ReasonCode.GEOMETRY_WORKSPACE_VIOLATION, verdict.reason_codes()
        )
        self.assertNotIn(ReasonCode.GEOMETRY_COLLISION, verdict.reason_codes())

    def test_location_outside_the_declaration_is_ask_not_allow(self):
        small = ResourceCatalog(catalog_version="test-small", entries=(
            ResourceEntry("loc_a", ResourceKind.LOCATION, "A 위치", ("A 위치", "loc_a")),
            ResourceEntry("obj_a", ResourceKind.OBJECT, "A 자재", ("A 자재", "obj_a")),
        ))
        validator = DevFakeGeometryValidator(
            FakeCell.from_catalog(small, frame_id=FRAME)
        )
        verdict = check_geometry(validator, request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(
            ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE, verdict.reason_codes()
        )
        self.assertFalse(verdict.input_complete)

    def test_validator_declares_it_checked_symbols_only(self):
        verdict = check_geometry(dev_validator(), request())
        self.assertEqual(verdict.validator_id, "dev-fake-geometry")


class TestValidatorFailures(unittest.TestCase):
    def test_timeout_is_ask(self):
        slow = SlowValidator(delay=1.0)
        started = time.monotonic()
        verdict = check_geometry(slow, request(timeout_sec=0.05))
        elapsed = time.monotonic() - started
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(
            ReasonCode.GEOMETRY_VALIDATOR_TIMEOUT, verdict.reason_codes()
        )
        # 제한시간이 실제로 동작한다(with ThreadPoolExecutor 함정 회피).
        self.assertLess(elapsed, 0.8)
        self.assertTrue(slow.entered.is_set())

    def test_exception_is_ask(self):
        verdict = check_geometry(
            BrokenValidator(RuntimeError("엔진 오류")), request()
        )
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_VALIDATOR_ERROR, verdict.reason_codes())
        self.assertIn("엔진 오류", verdict.reasons[0].detail)

    def test_contract_error_is_ask(self):
        verdict = check_geometry(
            BrokenValidator(
                GeometryError(ReasonCode.CONFIG_INVALID, "입력이 이상하다")
            ),
            request(),
        )
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_VALIDATOR_ERROR, verdict.reason_codes())

    def test_allow_about_a_different_snapshot_is_rejected(self):
        verdict = check_geometry(LyingValidator(), request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_VERDICT_INVALID, verdict.reason_codes())

    def test_verdict_claiming_another_validator_is_rejected(self):
        verdict = check_geometry(ImpostorValidator(), request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_VERDICT_INVALID, verdict.reason_codes())

    def test_non_verdict_return_is_rejected(self):
        class Nonsense:
            validator_id = "nonsense"
            validator_version = "1.0"

            def check(self, request):
                return {"decision": "allow"}

        verdict = check_geometry(Nonsense(), request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_VERDICT_INVALID, verdict.reason_codes())


class TestRuleResultShape(unittest.TestCase):
    def test_allow_renders_as_pass(self):
        row = geometry_rule_result(check_geometry(dev_validator(), request()))
        self.assertEqual(row["code"], "E-GEOM-001")
        self.assertEqual(row["status"], "pass")
        self.assertIsNone(row["reason_code"])

    def test_ask_renders_as_insufficient_data_and_is_recoverable(self):
        row = geometry_rule_result(check_geometry(None, request()))
        self.assertEqual(row["status"], "insufficient_data")
        self.assertEqual(
            row["reason_code"], ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE.value
        )
        self.assertTrue(row["recoverable"])

    def test_block_renders_as_block_and_is_not_recoverable(self):
        validator = dev_validator(blocked_locations=frozenset({"loc_b"}))
        row = geometry_rule_result(check_geometry(validator, request()))
        self.assertEqual(row["status"], "block")
        self.assertFalse(row["recoverable"])


class TestNoEngineDependency(unittest.TestCase):
    """계약이 특정 엔진·SDK에 묶이지 않았는지 고정한다."""

    def test_contract_module_names_no_engine(self):
        """주석·docstring이 아니라 **코드**를 본다.

        문단에서 "MoveIt에 종속되지 않는다"고 쓰는 것은 문제가 아니다. 문제는
        코드가 그 엔진을 import하거나 이름으로 부르는 것이다. 그래서 AST로
        import·식별자·코드 문자열만 모아 검사한다(문자열 훑기의 오검출 회피).
        """
        import ast

        tree = ast.parse((ROOT / "core" / "geometry.py").read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        tokens: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                tokens += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                tokens.append(node.module or "")
            elif isinstance(node, ast.Name):
                tokens.append(node.id)
            elif isinstance(node, ast.Attribute):
                tokens.append(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value not in docstrings:
                    tokens.append(node.value)
        blob = " ".join(tokens).lower()
        for engine in ("moveit", "fcl", "bullet", "pybullet", "urdf", "ros",
                       "trimesh", "open3d"):
            with self.subTest(engine=engine):
                self.assertNotIn(engine, blob)

    def test_contract_has_no_robot_numbers(self):
        source = (ROOT / "core" / "geometry.py").read_text(encoding="utf-8").lower()
        for banned in ("fr3", "fairino", "ur5e", "panda"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
