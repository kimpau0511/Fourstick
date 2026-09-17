"""정상·오류 예제 검증 (md/개발플랜.md 1-11).

완료 조건: "정상·오류 예제가 모두 schema 검증을 통과하거나 의도한 이유 코드로
실패". 예제 파일이 실제로 그렇게 동작하는지 검사한다.

오류 예제는 파일명이 거부 층을 나타낸다.
  structure__* — 구조(Pydantic/JSON Schema)에서 거부
  meaning__*   — 형태는 올바르므로 schema는 통과하고 core 계약에서 거부
`examples/INDEX.json`이 각 파일의 기대 이유 코드를 갖는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import jsonschema
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.loader import (
    load_capability_profile,
    load_freshness_policy,
    load_safety_policy,
    load_stop_policy,
    load_stt_model_config,
    load_stt_policy,
    load_stt_profile_catalog,
    load_resource_catalog,
    load_skill_catalog,
    load_planning_policy,
    load_llm_provider_config,
)
from core.boundary import BoundaryValidationError
from server.schemas import (
    SttStartModel,
    SttTranscriptModel,
    parse_task_plan,
    task_plan_json_schema,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
INDEX = json.loads((EXAMPLES / "INDEX.json").read_text(encoding="utf-8"))


def load(rel: str):
    return json.loads((EXAMPLES / rel).read_text(encoding="utf-8"))


def check_example(rel: str):
    """예제를 해당 경계로 통과시킨다. 실패하면 BoundaryValidationError."""
    if rel.startswith("task_plan/"):
        return parse_task_plan(load(rel)).to_core()
    if rel.startswith("config/"):
        payload = load(rel)
        # 파일명으로 어떤 설정인지 구분한다. 여러 설정이 같은 폴더에 있다.
        if "stop_policy" in rel:
            return load_stop_policy(payload)
        if "safety_policy" in rel:
            return load_safety_policy(payload)
        if "freshness_policy" in rel:
            return load_freshness_policy(payload)
        if "llm_provider" in rel:
            return load_llm_provider_config(payload)
        if "planning_policy" in rel:
            return load_planning_policy(payload)
        if "skill" in rel:
            return load_skill_catalog(payload)
        if "resource" in rel:
            return load_resource_catalog(payload)
        if "stt_profiles" in rel:
            return load_stt_profile_catalog(payload)
        if "stt_policy" in rel:
            return load_stt_policy(payload)
        if "stt_model" in rel:
            return load_stt_model_config(payload)
        return load_capability_profile(payload)
    if rel.startswith("stt/"):
        payload = load(rel)
        model = SttStartModel if payload.get("type") == "start" else SttTranscriptModel
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise BoundaryValidationError.__new__(BoundaryValidationError) from exc
    raise AssertionError(f"분류되지 않은 예제: {rel}")


class TestValidExamples(unittest.TestCase):
    def test_every_valid_example_passes(self):
        self.assertTrue(INDEX["valid"])
        for rel in INDEX["valid"]:
            with self.subTest(example=rel):
                check_example(rel)   # 예외가 없어야 한다

    def test_valid_plans_pass_json_schema_too(self):
        schema = task_plan_json_schema()
        for rel in INDEX["valid"]:
            if not rel.startswith("task_plan/"):
                continue
            with self.subTest(example=rel):
                jsonschema.validate(load(rel), schema)

    def test_carry_example_declares_its_terminal_hold(self):
        plan = check_example("task_plan/valid_carry.json")
        self.assertEqual(plan.terminal_hold, "obj_a")

    def test_transfer_example_ends_empty_handed(self):
        self.assertIsNone(check_example("task_plan/valid_transfer.json").terminal_hold)


class TestInvalidExamples(unittest.TestCase):
    def test_every_invalid_example_fails_with_the_declared_reason(self):
        self.assertTrue(INDEX["invalid"])
        for rel, meta in INDEX["invalid"].items():
            with self.subTest(example=rel):
                with self.assertRaises(BoundaryValidationError) as ctx:
                    check_example(rel)
                expected = meta.get("reason")
                if expected is not None:
                    self.assertEqual(str(ctx.exception.reason), expected)

    def test_file_name_prefix_matches_the_rejecting_layer(self):
        for rel, meta in INDEX["invalid"].items():
            with self.subTest(example=rel):
                name = rel.split("/", 1)[1]
                self.assertTrue(
                    name.startswith(meta["rejected_by"] + "__"),
                    f"{rel}의 파일명 접두사가 거부 층({meta['rejected_by']})과 다르다",
                )

    def test_structure_examples_are_rejected_by_json_schema(self):
        schema = task_plan_json_schema()
        for rel, meta in INDEX["invalid"].items():
            if not rel.startswith("task_plan/") or meta["rejected_by"] != "structure":
                continue
            with self.subTest(example=rel):
                with self.assertRaises(jsonschema.ValidationError):
                    jsonschema.validate(load(rel), schema)

    def test_meaning_examples_pass_json_schema(self):
        """의미 위반은 형태가 올바르므로 schema를 통과하는 것이 정상이다."""
        schema = task_plan_json_schema()
        for rel, meta in INDEX["invalid"].items():
            if not rel.startswith("task_plan/") or meta["rejected_by"] != "meaning":
                continue
            with self.subTest(example=rel):
                jsonschema.validate(load(rel), schema)


class TestIndexIsComplete(unittest.TestCase):
    """예제를 추가하고 INDEX에 안 넣는 실수를 막는다."""

    def test_every_example_file_is_listed(self):
        listed = set(INDEX["valid"]) | set(INDEX["invalid"])
        on_disk = {
            str(p.relative_to(EXAMPLES))
            for p in EXAMPLES.rglob("*.json")
            if p.name != "INDEX.json"
        }
        self.assertEqual(on_disk, listed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
