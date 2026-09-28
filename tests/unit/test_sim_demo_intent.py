"""자재 작업 발화 분류기(`server/sim_demo_intent.py`) 단위 검증.

확인하는 것:
 - 출력 스키마는 **세 필드뿐**이다. 더 있거나 빠지면 거부한다
 - `intent`·`material_id`는 열거값·이 셀의 자재 후보만 받는다
 - `confidence`는 0~1 수이고 임계값 미만이면 실행 후보가 아니다
 - `unknown`과 `restore`는 실행 후보가 아니다
 - JSON이 아니거나 서버가 없으면 실패로 끝난다 — **추측해서 채우지 않는다**

모델 서버를 부르지 않는다. `client`는 기록용 대역이다.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_intent import (  # noqa: E402
    EXECUTABLE_INTENTS,
    INTENTS,
    IntentClassifier,
    output_json_schema,
    parse_output,
    user_prompt,
    validate,
)

CANDIDATES = ("material_a", "material_b", "material_c")
#: 분류기에 넘기는 자리 목록(선언된 자리 객체처럼 `id`만 보면 된다).
PLACE_IDS = ("loc_pallet_1", "loc_pallet_2", "loc_pallet_3",
             "slot_1", "slot_2", "slot_3", "loc_conveyor", "loc_safe_home")
PLACE_ROWS = tuple(types.SimpleNamespace(id=pid, kind="pallet", label=pid)
                   for pid in PLACE_IDS)
ROWS = [{"model": "material_a", "korean": "A자재", "korean_colors": ["주황"]},
        {"model": "material_b", "korean": "B자재", "korean_colors": ["파랑"]},
        {"model": "material_c", "korean": "C자재", "korean_colors": ["초록"]}]


#: 이 셀이 선언한 자리(팔레트·검증된 컨베이어 자리·자리 미지정 컨베이어·안전 자세).
PLACES = ("loc_pallet_1", "loc_pallet_2", "loc_pallet_3",
          "slot_1", "slot_2", "slot_3", "loc_conveyor", "loc_safe_home")


def ok(payload, min_confidence=0.7, places=PLACES):
    """스키마는 **선언형 작업 다섯 칸**이다. 읽기 쉽게 `intent`로 적어도
    `action`으로 옮기고, 말하지 않은 자리는 `None`으로 채운다."""
    if not isinstance(payload, dict):
        return validate(payload, candidates=CANDIDATES,
                        min_confidence=min_confidence, places=places)
    body = dict(payload)
    if "intent" in body:
        body["action"] = body.pop("intent")
    if "action" in body:                      # 필드 누락 시험은 그대로 둔다
        body.setdefault("source_resource", None)
        body.setdefault("destination_resource", None)
    return validate(body, candidates=CANDIDATES, min_confidence=min_confidence,
                    places=places)


class SchemaTest(unittest.TestCase):
    def test_schema_lists_only_this_cells_materials_and_places(self):
        schema = output_json_schema(CANDIDATES, PLACE_IDS)
        self.assertEqual(sorted(schema["required"]),
                         ["action", "confidence", "destination_resource",
                          "material_id", "source_resource"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["action"]["enum"], list(INTENTS))
        self.assertEqual(schema["properties"]["material_id"]["enum"],
                         ["material_a", "material_b", "material_c", None])
        for field in ("source_resource", "destination_resource"):
            with self.subTest(field=field):
                self.assertEqual(schema["properties"][field]["enum"],
                                 [*PLACE_IDS, None])

    def test_schema_has_no_room_for_a_free_plan(self):
        """**관절값·좌표·경로를 담을 칸이 없다.** 선언형 작업뿐이다."""
        schema = output_json_schema(CANDIDATES, PLACE_IDS)
        self.assertEqual(sorted(schema["properties"]), sorted(schema["required"]))
        for banned in ("joint_rad", "pose", "trajectory", "waypoints", "xyz"):
            self.assertNotIn(banned, schema["properties"])

    def test_prompt_carries_the_declared_places(self):
        from server.sim_demo_places import Place, prompt_lines

        places = (Place(id="slot_2", kind="conveyor_slot", label="컨베이어 2번 위치",
                        slot="slot_2"),)
        text = user_prompt("저기 두 번째 자리에", ROWS, prompt_lines(places))
        self.assertIn("slot_2", text)
        self.assertIn("컨베이어 2번 위치", text)

    def test_prompt_carries_candidates_and_utterance(self):
        text = user_prompt("그거 컨베이어에 올려줘", ROWS)
        self.assertIn("material_a", text)
        self.assertIn("주황", text)
        self.assertIn("그거 컨베이어에 올려줘", text)

    def test_code_fence_is_stripped_but_content_is_not_repaired(self):
        self.assertEqual(
            parse_output('```json\n{"intent":"transfer","material_id":null,'
                         '"confidence":0.4}\n```')["intent"], "transfer")
        with self.assertRaises(ValueError):
            parse_output("transfer 입니다")


class ValidateTest(unittest.TestCase):
    def test_accepts_a_well_formed_confident_intent(self):
        result = ok({"intent": "transfer", "material_id": "material_a",
                     "confidence": 0.93})
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "transfer")
        self.assertEqual(result.material_id, "material_a")
        self.assertIn(result.intent, EXECUTABLE_INTENTS)

    def test_rejects_extra_or_missing_fields(self):
        cases = (
            {"intent": "transfer", "material_id": "material_a"},
            {"intent": "transfer", "material_id": "material_a", "confidence": 0.9,
             "plan": ["move", "pick"]},
            {"intent": "transfer", "confidence": 0.9},
        )
        for payload in cases:
            with self.subTest(payload=payload):
                result = ok(payload)
                self.assertFalse(result.ok)
                self.assertEqual(result.failure, "schema")

    def test_rejects_a_free_plan_instead_of_the_schema(self):
        for payload in ("이송하겠습니다", ["transfer"], 3):
            with self.subTest(payload=payload):
                self.assertFalse(ok(payload).ok)

    def test_rejects_unknown_intent_value(self):
        result = ok({"intent": "pick_and_place", "material_id": None,
                     "confidence": 0.99})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "schema")

    def test_rejects_a_material_that_is_not_in_this_cell(self):
        result = ok({"intent": "transfer", "material_id": "material_z",
                     "confidence": 0.99})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "candidate")

    def test_rejects_bad_confidence_values(self):
        for value in (True, "0.9", None, 1.4, -0.1):
            with self.subTest(value=value):
                result = ok({"intent": "transfer", "material_id": "material_a",
                             "confidence": value})
                self.assertFalse(result.ok)
                self.assertEqual(result.failure, "schema")

    def test_low_confidence_is_not_an_execution_candidate(self):
        result = ok({"intent": "transfer", "material_id": "material_a",
                     "confidence": 0.42})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "confidence")
        self.assertIn("0.42", result.reason)

    def test_unknown_is_not_an_execution_candidate(self):
        result = ok({"intent": "unknown", "material_id": None, "confidence": 0.99})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "unknown")

    def test_restore_is_classified_but_never_executed_from_speech(self):
        """복구는 시연 카드의 버튼이 맡는다 — 발화 해석으로 돌리지 않는다."""
        result = ok({"intent": "restore", "material_id": "material_a",
                     "confidence": 0.99})
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "unknown")
        self.assertNotIn("restore", EXECUTABLE_INTENTS)


class FakeClient:
    """`OpenAiCompatClient.chat`과 같은 모양의 기록용 대역."""

    def __init__(self, content=None, error=None):
        self.config = types.SimpleNamespace(model_id="qwen3-8b-awq")
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    def chat(self, *, system, user, json_schema, schema_name):
        self.calls.append({"system": system, "user": user,
                           "json_schema": json_schema, "schema_name": schema_name})
        if self.error is not None:
            raise self.error
        return types.SimpleNamespace(content=self.content, reasoning="",
                                     latency_sec=0.12)


class ClassifierTest(unittest.TestCase):
    def test_sends_the_structured_output_schema_and_validates_the_answer(self):
        client = FakeClient('{"action":"return","material_id":"material_b",'
                            '"source_resource":"slot_1",'
                            '"destination_resource":"loc_pallet_2",'
                            '"confidence":0.88}')
        result = IntentClassifier(client=client, min_confidence=0.7).classify(
            "컨베이어 1번 자리의 B를 2번 팔레트로", ROWS, PLACE_ROWS)
        self.assertTrue(result.ok)
        self.assertEqual(result.intent, "return")
        self.assertEqual(result.material_id, "material_b")
        self.assertEqual(result.source_resource, "slot_1")
        self.assertEqual(result.destination_resource, "loc_pallet_2")
        self.assertEqual(result.model_id, "qwen3-8b-awq")
        sent = client.calls[0]["json_schema"]
        self.assertEqual(sent["properties"]["material_id"]["enum"],
                         ["material_a", "material_b", "material_c", None])
        # **자리도 열거값이다** — 모델이 없는 자리를 쓸 칸이 없다.
        self.assertEqual(sent["properties"]["destination_resource"]["enum"],
                         [*PLACE_IDS, None])
        # 자유 계획을 담을 칸이 없다.
        self.assertNotIn("joint_rad", sent["properties"])
        self.assertNotIn("pose", sent["properties"])
        self.assertIs(sent["additionalProperties"], False)

    def test_unparseable_output_fails_without_guessing(self):
        client = FakeClient("A 자재를 옮기면 될 것 같습니다.")
        result = IntentClassifier(client=client, min_confidence=0.7).classify("x", ROWS)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "schema")
        self.assertIsNone(result.intent)

    def test_a_dead_model_server_is_reported_not_guessed(self):
        client = FakeClient(error=RuntimeError("연결 실패"))
        result = IntentClassifier(client=client, min_confidence=0.7).classify("x", ROWS)
        self.assertFalse(result.ok)
        self.assertEqual(result.failure, "unavailable")
        self.assertIn("연결 실패", result.reason)

    def test_raw_output_is_kept_for_the_screen(self):
        client = FakeClient('{"action":"transfer","material_id":"material_a",'
                            '"source_resource":null,"destination_resource":null,'
                            '"confidence":0.3}')
        result = IntentClassifier(client=client, min_confidence=0.7).classify("x", ROWS)
        self.assertEqual(result.failure, "confidence")
        self.assertIn("material_a", result.raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
