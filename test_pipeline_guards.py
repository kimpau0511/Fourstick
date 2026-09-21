import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError

from pipeline import (
    MODEL_ID,
    SCHEMA_VERSION,
    TaskPlanDraft,
    Slots,
    check_plan_matches_slots,
    compute_plan_hash,
    extract_slots,
    process_utterance,
    validate_task_plan_for_execution,
)


def valid_plan(**overrides):
    steps = [
        {"skill": "move", "args": {"target": "1번 팔레트"}},
        {"skill": "pick", "args": {"object": "A자재", "from": "1번 팔레트"}},
        {"skill": "move", "args": {"target": "컨베이어"}},
        {"skill": "place", "args": {"object": "A자재", "to": "컨베이어"}},
        {"skill": "home", "args": {}},
    ]
    plan = {
        "plan_id": "plan-1",
        "plan_hash": compute_plan_hash("transfer", steps),
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "utterance": "1번 팔레트에서 A자재를 컨베이어로 옮겨",
        "robot_id": "ur5e",
        "intent": "transfer",
        "steps": steps,
        "ref_versions": {"schema_version": SCHEMA_VERSION, "model_id": MODEL_ID},
        "audit": {"stt_confidence": 1.0},
    }
    plan.update(overrides)
    return plan


class PipelineGuardTests(unittest.TestCase):
    def test_empty_plan_is_rejected_by_schema(self):
        with self.assertRaises(ValidationError):
            TaskPlanDraft.model_validate({"intent": "transfer", "steps": []})

    def test_stop_bypasses_slots_and_llm(self):
        with patch("pipeline.requests.post") as post:
            result = process_utterance("지금 멈춰", confidence=0.01)
        self.assertEqual("stop_requested", result["status"])
        post.assert_not_called()

    def test_low_confidence_is_gated_before_llm(self):
        with patch("pipeline.requests.post") as post:
            result = process_utterance(
                "1번 팔레트에서 A자재를 컨베이어로 옮겨", confidence=0.01
            )
        self.assertEqual("A-STT-CONFIDENCE", result["reason_code"])
        post.assert_not_called()

    def test_plan_must_match_slots(self):
        slots = Slots()
        slots.action = "transfer"
        slots.object = "A자재"
        slots.from_location = "1번 팔레트"
        slots.to_location = "컨베이어"
        steps = valid_plan()["steps"]
        steps[1]["args"]["object"] = "B자재"
        violations = check_plan_matches_slots({"steps": steps}, slots)
        self.assertEqual("E-SEM-002", violations[0]["code"])

    def test_execution_rejects_tampered_hash(self):
        codes = {v["code"] for v in validate_task_plan_for_execution(
            valid_plan(plan_hash="tampered"), "ur5e"
        )}
        self.assertIn("E-EXEC-HASH", codes)

    def test_execution_rejects_expired_or_wrong_robot(self):
        plan = valid_plan(
            expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            robot_id="panda",
        )
        codes = {v["code"] for v in validate_task_plan_for_execution(plan, "ur5e")}
        self.assertIn("E-EXEC-EXPIRED", codes)
        self.assertIn("E-EXEC-ROBOT", codes)

    def test_valid_plan_passes_execution_guard(self):
        self.assertEqual([], validate_task_plan_for_execution(valid_plan(), "ur5e"))

    def test_location_extraction_rejects_digit_prefixed_lookalike(self):
        # "11번 팔레트"가 "1번 팔레트"의 부분 문자열이라는 이유만으로
        # 실존하지 않는 위치로 오인되면 안 된다.
        slots = extract_slots("11번 팔레트에서 A자재를 꺼내서 컨베이어로 옮겨")
        self.assertIsNone(slots.from_location)

    def test_location_extraction_still_matches_real_location(self):
        slots = extract_slots("1번 팔레트에서 A자재를 꺼내서 컨베이어로 옮겨")
        self.assertEqual("1번 팔레트", slots.from_location)
        self.assertEqual("컨베이어", slots.to_location)


if __name__ == "__main__":
    unittest.main()
