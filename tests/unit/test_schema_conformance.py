"""생성된 JSON Schema와 경계 모델의 일치 확인.

두 가지를 본다.
  1. 구조 일치 — schema의 속성·필수·추가속성 금지가 모델 정의와 같은가
  2. 동작 일치 — 같은 payload에 대해 jsonschema 검증과 모델 검증이 같은 판정을
     내리는가(구조 범위 안에서)

"구조 범위 안에서"가 중요하다. JSON Schema는 형태만 본다. 지원 스킬·인자
조합·버전·plan hash·TTL 같은 의미 규칙은 schema로 표현하지 않고 core 계약이
본다(md/검증범위_구분.md 참고). 그래서 "schema는 통과하지만 core가 거부하는"
payload가 정상적으로 존재하며, 이 테스트가 그 경계를 명시적으로 확인한다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import jsonschema
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.loader import (
    CapabilityProfileModel,
    FreshnessPolicyModel,
    SafetyPolicyModel,
    StopPolicyModel,
    config_json_schemas,
)
from core.boundary import BoundaryValidationError
from server.schemas import (
    ExecutionResultModel,
    TaskPlanModel,
    parse_task_plan,
    task_plan_json_schema,
)

BOUNDARY_MODELS: dict[str, type[BaseModel]] = {
    "task_plan": TaskPlanModel,
    "capability_profile": CapabilityProfileModel,
    "safety_policy": SafetyPolicyModel,
    "freshness_policy": FreshnessPolicyModel,
    "stop_policy": StopPolicyModel,
    "execution_result": ExecutionResultModel,
}

VALID_PLAN = {
    "plan_id": "p", "robot_id": "r", "profile_id": "prof", "profile_version": "1.0",
    "created_at": 1_000.0, "ttl_sec": 60.0,
    "steps": [{"skill": "home"}, {"skill": "move", "args": {"to": "loc_a"}}],
}


class TestSchemaMatchesModel(unittest.TestCase):
    """구조 일치 — 스키마가 모델에서 생성됐음을 형태로 확인한다."""

    def test_schema_is_valid_json_schema(self):
        for name, model in BOUNDARY_MODELS.items():
            with self.subTest(model=name):
                schema = model.model_json_schema()
                jsonschema.Draft202012Validator.check_schema(schema)

    def test_properties_match_model_fields(self):
        for name, model in BOUNDARY_MODELS.items():
            with self.subTest(model=name):
                schema = model.model_json_schema()
                self.assertEqual(set(schema["properties"]), set(model.model_fields))

    def test_required_matches_fields_without_defaults(self):
        for name, model in BOUNDARY_MODELS.items():
            with self.subTest(model=name):
                schema = model.model_json_schema()
                required = set(schema.get("required", []))
                expected = {n for n, f in model.model_fields.items() if f.is_required()}
                self.assertEqual(required, expected)

    def test_unknown_fields_are_forbidden_in_schema(self):
        """extra="forbid"가 schema의 additionalProperties=False로 나타나야 한다."""
        for name, model in BOUNDARY_MODELS.items():
            with self.subTest(model=name):
                self.assertIs(model.model_json_schema().get("additionalProperties"), False)

    def test_exposed_schemas_come_from_the_same_models(self):
        self.assertEqual(task_plan_json_schema(), TaskPlanModel.model_json_schema())
        exposed = config_json_schemas()
        for key, model in (
            ("capability_profile", CapabilityProfileModel),
            ("safety_policy", SafetyPolicyModel),
            ("freshness_policy", FreshnessPolicyModel),
            ("stop_policy", StopPolicyModel),
        ):
            with self.subTest(key=key):
                self.assertEqual(exposed[key], model.model_json_schema())


class TestSchemaAndModelAgreeOnStructure(unittest.TestCase):
    """동작 일치 — 구조 범위 안에서는 같은 판정을 내린다."""

    def _schema_ok(self, payload) -> bool:
        try:
            jsonschema.validate(payload, task_plan_json_schema())
            return True
        except jsonschema.ValidationError:
            return False

    def _model_ok(self, payload) -> bool:
        try:
            parse_task_plan(payload)
            return True
        except BoundaryValidationError:
            return False

    def test_valid_payload_passes_both(self):
        self.assertTrue(self._schema_ok(VALID_PLAN))
        self.assertTrue(self._model_ok(VALID_PLAN))

    def test_structural_errors_are_rejected_by_both(self):
        for payload, label in [
            ({**VALID_PLAN, "surprise": 1}, "미지의 필드"),
            ({**VALID_PLAN, "ttl_sec": "60"}, "타입 불일치"),
            ({**VALID_PLAN, "steps": []}, "빈 배열"),
            ({k: v for k, v in VALID_PLAN.items() if k != "plan_id"}, "필수 키 누락"),
            ({**VALID_PLAN, "steps": [{"skill": "move", "args": {"to": 0.4}}]}, "인자 타입"),
        ]:
            with self.subTest(label=label):
                self.assertFalse(self._schema_ok(payload), f"{label}: schema가 통과시켰다")
                self.assertFalse(self._model_ok(payload), f"{label}: 모델이 통과시켰다")


class TestMeaningIsOutsideSchemaScope(unittest.TestCase):
    """의미 규칙은 schema 범위 밖이다 — core 계약이 본다.

    아래 payload들은 **형태가 올바르므로 schema를 통과하는 것이 정상**이고,
    core 계약에서 거부돼야 한다. 이 구분이 깨지면(예: schema가 스킬 목록을
    enum으로 갖게 되면) 계약의 단일 출처가 둘로 갈라진다.
    """

    def _schema_ok(self, payload) -> bool:
        try:
            jsonschema.validate(payload, task_plan_json_schema())
            return True
        except jsonschema.ValidationError:
            return False

    def test_meaning_violations_pass_schema_but_fail_core(self):
        for payload, label in [
            ({**VALID_PLAN, "steps": [{"skill": "weld"}]}, "계약에 없는 스킬"),
            ({**VALID_PLAN, "steps": [{"skill": "pick", "args": {"object": "o"}}]}, "필수 인자 누락"),
            ({**VALID_PLAN, "steps": [{"skill": "move", "args": {"to": "x", "speed": "f"}}]},
             "허용되지 않은 인자"),
            ({**VALID_PLAN, "schema_version": "1.0"}, "지원하지 않는 버전"),
            ({**VALID_PLAN, "steps": [{"skill": "move", "args": {"to": ""}}]}, "빈 인자 값"),
        ]:
            with self.subTest(label=label):
                self.assertTrue(
                    self._schema_ok(payload), f"{label}: 형태는 맞으므로 schema를 통과해야 한다"
                )
                with self.assertRaises(BoundaryValidationError):
                    parse_task_plan(payload).to_core()

    def test_schema_does_not_encode_the_skill_list(self):
        """스킬 목록은 core/constants.py가 유일한 출처다. schema에 enum으로
        중복 정의하면 두 곳을 같이 고쳐야 하고 어긋날 수 있다."""
        step_schema = task_plan_json_schema()["$defs"]["TaskStepModel"]["properties"]["skill"]
        self.assertNotIn("enum", step_schema)


if __name__ == "__main__":
    unittest.main(verbosity=2)
