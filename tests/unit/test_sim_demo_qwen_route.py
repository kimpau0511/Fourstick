"""규칙이 끝내지 못한 자재 작업 발화 → Qwen 해석 → 서버 검증 → 확인 카드 (2026-10-06).

지키는 것:
- "멈춰"·"정지"·"스톱"은 다른 모든 해석보다 먼저 STOP이다 — Qwen을 부르지 않는다.
- 규칙이 자재·출발·도착을 확정하면 규칙 결과를 쓴다(Qwen을 부르지 않는다).
- 규칙이 ASK이거나 "모형"·"물체"·"그거"·"저쪽"·"벨트" 같은 표현으로 확정하지 못하면 바로 끝내지
  않고 Qwen에 묻는다. Qwen 입력에는 원문·자재(id·이름·색·현재 위치)·자리·지원 동작·대화 맥락이 있다.
- Qwen 결과는 그대로 실행하지 않는다. 색·이름·맥락 근거, 현재 위치, 목적지 점유, 지원 동작,
  confidence, 시연 상태를 서버가 다시 본다. 어긋나면 되묻고(ASK) **작업 0건**이다.
- 통과해도 확인 카드까지만 간다 — 확인을 누르기 전에는 작업(= Gazebo 실행 프로세스)이 0건이다.

Gazebo를 움직이지 않는다. 실행 프로세스는 기록용 대역(Popen)이고, 작업 건수는 그 기록으로 센다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.unit.test_sim_demo_confirm import ClockBase, answer  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL  # noqa: E402
from tests.unit.test_simulation_demo_checkpoint import completed_result  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)


def qwen(intent="transfer", material=None, source=None, destination=None,
         confidence=0.9, reason="시험 근거"):
    return answer({"intent": intent, "material_id": material, "source_id": source,
                   "destination_id": destination, "confidence": confidence,
                   "reason": reason})


class QwenRouteBase(ClockBase):
    """검증된 컨베이어 칸 3개가 있는 시연 셀(`test_sim_demo_confirm.SlotAssignmentTest`와 같은 구성)."""

    def setUp(self):
        super().setUp()
        from server.sim_demo_jobs import SimDemoJobs

        grasp = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json")
                           .read_text(encoding="utf-8"))
        poses = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_poses.json")
                           .read_text(encoding="utf-8"))
        self.jobs = SimDemoJobs(
            workcell=WORKCELL, grasp_config=grasp, poses_config=poses,
            state_path=self.state_path,
            jobs_dir=self.tmp / "jobs", stop_request=self.tmp / "stop.json",
            popen=self.popen, environ={"PATH": "/usr/bin"})
        self.runtime.sim_demo_jobs = self.jobs

    def hold(self, model, slot):
        """자재가 그 컨베이어 칸에 놓인 기록(시연 유지)."""
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model=model, result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None, slot=slot)

    def assert_card(self, payload, *, action, material):
        """확인 카드 후보까지만 — 작업 0건."""
        self.assertEqual(payload["decision"], "CONFIRM", payload.get("reason"))
        self.assertEqual(self.job_count, 0, "확인 전에 작업이 만들어졌다")
        self.assertIsNone(payload["job"])
        self.assertEqual(payload["confirmation"]["action"], action)
        self.assertEqual(payload["confirmation"]["material"], material)

    def assert_asks(self, payload):
        self.assertEqual(payload["decision"], "ASK", payload.get("reason"))
        self.assertEqual(self.job_count, 0)
        self.assertIsNone(payload.get("confirmation"))


class RequestedSentencesTest(QwenRouteBase):
    """요청된 다섯 문장."""

    def test_green_model_to_the_conveyor_belt_goes_through_qwen(self):
        client = self.with_classifier(qwen(material="material_c", destination="loc_conveyor",
                                           reason="초록색 = C자재, 컨베이어 벨트 = 컨베이어"))
        _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
        self.assertEqual(len(client.calls), 1, "Qwen을 거치지 않았다")
        self.assert_card(payload, action="transfer", material="material_c")
        self.assertEqual(payload["intent_result"]["model_reason"],
                         "초록색 = C자재, 컨베이어 벨트 = 컨베이어")
        # Qwen 입력: 원문 · 자재(id·이름·색·현재 위치) · 자리 · 지원 동작 · 대화 맥락
        prompt = client.calls[0]["user"]
        for expected in ("사용자가 말한 원문", "초록색 모형을 컨베이어 벨트로 옮겨줘", "material_c", "원형 자재", "초록",
                         "현재 위치", "loc_conveyor", "slot_1", "지원 동작", "이어하기",
                         "빈자리 이동", "대화 맥락"):
            self.assertIn(expected, prompt)
        schema = client.calls[0]["json_schema"]
        self.assertEqual(sorted(schema["required"]),
                         ["confidence", "destination_id", "intent", "material_id",
                          "reason", "source_id"])

    def test_green_object_to_that_belt_goes_through_qwen(self):
        client = self.with_classifier(qwen(material="material_c", destination="loc_conveyor"))
        _, payload = self.command("초록 물체를 저쪽 벨트로 보내줘")
        self.assertEqual(len(client.calls), 1, "Qwen을 거치지 않았다")
        self.assert_card(payload, action="transfer", material="material_c")

    def test_that_green_thing_back_to_its_place(self):
        """색 치환으로 규칙이 C자재·복귀를 확정한다 — 규칙 결과를 쓴다(Qwen 없음)."""
        self.hold("material_c", "slot_1")
        client = self.with_classifier(qwen(intent="return", material="material_c"))
        _, payload = self.command("그 초록색 물건 원래 자리에 놔줘")
        self.assert_card(payload, action="return", material="material_c")
        self.assertEqual(client.calls, [], "규칙이 확정했는데 Qwen을 불렀다")

    def test_rule_return_that_contradicts_the_color_goes_to_qwen(self):
        """회귀(2026-10-06 발견): 자재 없는 복귀 규칙이 색을 무시하고 컨베이어의 **다른** 자재를
        골랐다. 말한 색과 다르면 규칙 결과를 쓰지 않고 Qwen·검증으로 넘긴다."""
        self.hold("material_a", "slot_1")                 # 컨베이어에는 A(주황)만 있다
        client = self.with_classifier(qwen(intent="return", material="material_c",
                                           destination="loc_pallet_3"))
        _, payload = self.command("그 초록색 모형 원래 자리에 놔줘")
        self.assertEqual(len(client.calls), 1)
        self.assertNotEqual(payload.get("material"), "material_a")
        self.assertNotEqual(payload["decision"], "CONFIRM")   # C는 이미 원래 자리
        self.assertEqual(self.job_count, 0)

    def test_move_that_asks_when_the_target_is_unclear(self):
        # 모델이 근거 없이 자재를 골라도 서버가 받지 않는다.
        client = self.with_classifier(qwen(material="material_a", destination="loc_conveyor"))
        _, payload = self.command("그거 옮겨줘")
        self.assertEqual(len(client.calls), 1, "규칙 실패를 바로 끝냈다(Qwen을 부르지 않았다)")
        self.assert_asks(payload)
        # 어떤 자재인지 구체적으로 묻는다 — 후보 자재 이름.
        for name in ("사각형 자재", "삼각형 자재", "원형 자재"):
            self.assertIn(name, payload["reason"])

    def test_stop_never_calls_qwen(self):
        for utterance in ("멈춰", "정지", "스톱", "그거 멈춰"):
            with self.subTest(utterance=utterance):
                client = self.with_classifier(qwen(material="material_a",
                                                   destination="loc_conveyor"))
                _, payload = self.command(utterance)
                self.assertEqual(payload["decision"], "STOP")
                self.assertEqual(client.calls, [])
                self.assertEqual(self.job_count, 0)


class ServerChecksTest(QwenRouteBase):
    """Qwen 결과를 그대로 실행하지 않는다."""

    def test_color_that_does_not_match_the_chosen_material_asks(self):
        self.with_classifier(qwen(material="material_a", destination="loc_conveyor"))
        _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
        self.assert_asks(payload)
        self.assertIn("원형 자재", payload["reason"])

    def test_no_name_no_color_no_context_asks_with_candidates(self):
        """여러 자재가 후보면 고르지 않는다(이전: 모델이 고른 자재로 확인 카드)."""
        self.with_classifier(qwen(material="material_a", destination="loc_conveyor"))
        _, payload = self.command("팔레트에 있는 물건 하나 컨베이어로 옮겨줘")
        self.assert_asks(payload)
        self.assertIn("어느 자재인지", payload["reason"])

    def test_context_grounds_a_pointing_word(self):
        """직전에 말한 자재 하나가 대화 맥락이면 "그거"를 그 자재로 받을 수 있다."""
        self.with_classifier(qwen(material="material_c", destination="loc_conveyor"))
        session = {"session_id": "ctx-1"}
        self.call("POST", "/v1/sim-demo/command",
                  {"mode": "simulation_demo", "source": "text",
                   "utterance": "C자재 지금 어디 있어", **session})
        _, payload = self.call("POST", "/v1/sim-demo/command",
                               {"mode": "simulation_demo", "source": "text",
                                "utterance": "그거 컨베이어로 옮겨줘", **session})
        self.assertIn(payload["decision"], ("CONFIRM", "ASK"))
        self.assertEqual(self.job_count, 0)
        if payload["decision"] == "CONFIRM":
            self.assertEqual(payload["confirmation"]["material"], "material_c")

    def test_stated_source_must_match_the_current_location(self):
        self.with_classifier(qwen(material="material_c", source="slot_2",
                                  destination="loc_conveyor"))
        _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
        self.assert_asks(payload)
        self.assertIn("지금 위치", payload["reason"])

    def test_occupied_destination_is_not_offered(self):
        self.hold("material_a", "slot_1")
        self.with_classifier(qwen(intent="move_slot", material="material_c",
                                  destination="slot_1"))
        _, payload = self.command("초록 모형을 저기 첫 번째 빈자리로 옮겨줘")
        self.assertNotEqual(payload["decision"], "CONFIRM")
        self.assertEqual(self.job_count, 0)

    def test_move_slot_to_an_empty_slot_becomes_a_transfer_card(self):
        self.with_classifier(qwen(intent="move_slot", material="material_c",
                                  destination="slot_2"))
        _, payload = self.command("초록 모형을 저기 두 번째 빈자리로 옮겨줘")
        self.assert_card(payload, action="transfer", material="material_c")
        self.assertEqual(payload["slot"], "slot_2")

    def test_low_confidence_asks_which_material(self):
        self.with_classifier(qwen(material="material_c", destination="loc_conveyor",
                                  confidence=0.4))
        _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
        self.assert_asks(payload)
        self.assertIn("어느 자재인지", payload["reason"])

    def test_invented_ids_and_actions_are_rejected(self):
        for content in (qwen(material="material_z", destination="loc_conveyor"),
                        qwen(material="material_c", destination="loc_somewhere"),
                        qwen(intent="teleport", material="material_c")):
            with self.subTest(content=content):
                self.with_classifier(content)
                _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
                self.assert_asks(payload)

    def test_qwen_failure_format_error_or_timeout_never_runs(self):
        cases = ({"error": RuntimeError("연결 실패")},
                 {"error": TimeoutError("timed out")},
                 {"content": "C자재를 옮기면 됩니다"},
                 {"content": json.dumps({"intent": "transfer", "material_id": "material_c",
                                         "confidence": 0.9})})          # 필드 누락
        for case in cases:
            with self.subTest(case=case):
                self.with_classifier(**case)
                _, payload = self.command("초록색 모형을 컨베이어 벨트로 옮겨줘")
                self.assert_asks(payload)

    def test_unregistered_color_asks_without_qwen(self):
        client = self.with_classifier(qwen(material="material_c", destination="loc_conveyor"))
        _, payload = self.command("보라색 모형을 컨베이어 벨트로 옮겨줘")
        self.assert_asks(payload)
        self.assertEqual(client.calls, [])

    def test_rule_resolved_command_keeps_the_rule_result(self):
        client = self.with_classifier(qwen(material="material_b", destination="loc_conveyor"))
        _, payload = self.command("A 자재를 컨베이어로 옮겨줘")
        self.assert_card(payload, action="transfer", material="material_a")
        self.assertEqual(client.calls, [])

    def test_confirming_the_qwen_card_starts_exactly_one_job(self):
        self.with_classifier(qwen(material="material_c", destination="loc_conveyor"))
        _, payload = self.command("초록 물체를 저쪽 벨트로 보내줘")
        self.assertEqual(self.job_count, 0)
        _, confirmed = self.confirm(payload["confirmation"]["token"])
        self.assertEqual(confirmed["decision"], "RUN")
        self.assertEqual(self.job_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
