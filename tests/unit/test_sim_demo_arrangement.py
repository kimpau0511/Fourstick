"""목표 배치 → 작업 순서 계획·실행 (Gazebo 시뮬레이션 시연).

- 순수 해석·계획: 의존성 순서, 사용자 순서 존중, 환경 제약, 관측 우선, 모순 차단
- 목표 실행: 단계마다 **상태 기록**으로 판정하고, 끝나면 목표 배치를 다시 대조
- 경로: 텍스트 → 확인 카드(CONFIRM_GOAL), 확인 전 child job 0건, 기존 계약 유지
"""

from __future__ import annotations

import json
import types
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_arrangement import (  # noqa: E402
    ANY_SLOT,
    GOAL_ARRANGE,
    ORIGIN,
    ArrangementError,
    ArrangementRequest,
    is_arrangement_like,
    parse_arrangement,
    plan_arrangement,
)
from server.sim_demo_goals import GOAL_RETURN_ALL, SimDemoGoalError  # noqa: E402
from server.sim_demo_jobs import materials_from_workcell  # noqa: E402
from tests.unit import test_sim_demo_goals as goal_tests  # noqa: E402
from tests.unit.test_sim_demo_goals import GoalsBase  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL  # noqa: E402

SLOTS = ["slot_1", "slot_2", "slot_3"]
MATERIALS = materials_from_workcell(WORKCELL)
HELD = "held_on_target"


def on(slot: str) -> dict:
    return {"state": HELD, "slot": slot}


def plan(text: str, objects: dict | None = None) -> dict:
    request = parse_arrangement(text, WORKCELL, SLOTS)
    return plan_arrangement(request, materials=MATERIALS, slot_names=SLOTS,
                            objects=objects or {})


def steps(result: dict) -> list[tuple]:
    return [(s["action"], s["material"], s["from"], s["to"]) for s in result["steps"]]


class ParseArrangementTest(unittest.TestCase):
    def test_two_targets_in_one_sentence(self):
        request = parse_arrangement(
            "A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘", WORKCELL, SLOTS)
        self.assertEqual(dict(request.targets),
                         {"material_a": "slot_1", "material_b": "slot_2"})
        self.assertEqual(request.priority, ("material_a", "material_b"))
        self.assertFalse(request.order_explicit)
        self.assertTrue(is_arrangement_like(request))

    def test_blocked_slot_is_environment_constraint(self):
        request = parse_arrangement(
            "2번 칸은 사용 금지야. C자재를 컨베이어에 올려줘", WORKCELL, SLOTS)
        self.assertEqual(request.blocked_slots, ("slot_2",))
        self.assertEqual(dict(request.targets), {"material_c": ANY_SLOT})

    def test_order_words_move_material_to_front(self):
        request = parse_arrangement(
            "A자재는 원래 자리로 돌려놓고 C자재를 먼저 컨베이어 3번에 올려",
            WORKCELL, SLOTS)
        self.assertTrue(request.order_explicit)
        self.assertEqual(request.priority[0], "material_c")

    def test_state_assertion_is_recorded_not_targeted(self):
        request = parse_arrangement(
            "B자재는 지금 컨베이어 3번에 있어, B자재를 원래 자리로 돌려놔",
            WORKCELL, SLOTS)
        self.assertEqual(dict(request.assertions), {"material_b": "slot_3"})
        self.assertEqual(dict(request.targets), {"material_b": ORIGIN})

    def test_source_slot_is_not_destination(self):
        request = parse_arrangement(
            "컨베이어 2번 위치의 A자재를 원래 자리로, C자재는 컨베이어 3번에",
            WORKCELL, SLOTS)
        self.assertEqual(dict(request.targets),
                         {"material_a": ORIGIN, "material_c": "slot_3"})

    def test_other_pallet_is_a_destination(self):
        # 2026-09-25 요구 변경: 다른 팔레트도 목적지다(자세·점유는 계획기가 본다).
        request = parse_arrangement("A자재를 3번 팔레트로 옮기고 B자재는 컨베이어로",
                                    WORKCELL, SLOTS)
        self.assertEqual(dict(request.targets),
                         {"material_a": "loc_pallet_3", "material_b": ANY_SLOT})
        own = parse_arrangement("A자재를 1번 팔레트로 옮기고 B자재는 컨베이어로",
                                WORKCELL, SLOTS)
        self.assertEqual(own.targets["material_a"], ORIGIN)

    def test_unknown_pallet_is_blocked(self):
        with self.assertRaises(ArrangementError) as caught:
            parse_arrangement("A자재를 7번 팔레트로 옮기고 B자재는 컨베이어로",
                              WORKCELL, SLOTS)
        self.assertEqual(caught.exception.decision, "BLOCK")

    def test_unknown_fragment_asks_instead_of_dropping(self):
        with self.assertRaises(ArrangementError) as caught:
            parse_arrangement("A자재는 컨베이어 1번, 그리고 로봇 춤춰", WORKCELL, SLOTS)
        self.assertEqual(caught.exception.decision, "ASK")

    def test_single_plain_command_stays_on_existing_path(self):
        request = parse_arrangement("A자재를 컨베이어로 옮겨줘", WORKCELL, SLOTS)
        self.assertFalse(is_arrangement_like(request))

    def test_not_arrangement(self):
        self.assertIsNone(parse_arrangement("안녕하세요", WORKCELL, SLOTS))


class PlanArrangementTest(unittest.TestCase):
    def test_swap_returns_both_then_transfers_with_dependencies(self):
        result = plan("A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘",
                      {"material_a": on("slot_2"), "material_b": on("slot_1")})
        self.assertEqual(steps(result), [
            ("return", "material_a", "slot_2", ORIGIN),
            ("return", "material_b", "slot_1", ORIGIN),
            ("transfer", "material_a", ORIGIN, "slot_1"),
            ("transfer", "material_b", ORIGIN, "slot_2"),
        ])
        self.assertEqual(result["steps"][2]["depends_on"], [1, 2])
        self.assertIn("B자재가 먼저 빠져야 한다", result["steps"][2]["reason"])
        self.assertEqual(result["expected"]["material_a"], "slot_1")

    def test_user_order_is_followed_when_no_dependency_blocks_it(self):
        result = plan("C자재를 먼저 컨베이어 3번에 올리고 A자재는 원래 자리로 돌려놔",
                      {"material_a": on("slot_2")})
        self.assertEqual(steps(result), [
            ("transfer", "material_c", ORIGIN, "slot_3"),
            ("return", "material_a", "slot_2", ORIGIN),
        ])
        self.assertEqual(result["notes"], [])

    def test_user_order_is_changed_with_explanation_when_slot_must_be_freed(self):
        result = plan("C자재를 먼저 컨베이어 2번에 놓고 A자재는 원래 자리로 돌려놔",
                      {"material_a": on("slot_2")})
        self.assertEqual(steps(result), [
            ("return", "material_a", "slot_2", ORIGIN),
            ("transfer", "material_c", ORIGIN, "slot_2"),
        ])
        self.assertEqual(result["steps"][1]["depends_on"], [1])

    def test_blocked_slot_displaces_occupant_and_is_skipped(self):
        result = plan("2번 칸은 사용 금지야. C자재를 컨베이어에 올려줘",
                      {"material_a": on("slot_2"), "material_b": on("slot_1")})
        self.assertEqual(steps(result), [
            ("return", "material_a", "slot_2", ORIGIN),
            ("transfer", "material_c", ORIGIN, "slot_3"),
        ])
        self.assertIn("쓰지 말라고", result["steps"][0]["reason"])
        self.assertEqual(result["blocked_slots"], ["slot_2"])

    def test_occupant_without_target_is_moved_out_of_wanted_slot(self):
        result = plan("C자재는 컨베이어 1번에 놓고 A자재는 컨베이어 3번에 놓아줘",
                      {"material_b": on("slot_1")})
        self.assertEqual(steps(result)[0], ("return", "material_b", "slot_1", ORIGIN))
        self.assertIn("C자재가", result["steps"][0]["reason"])

    def test_observation_wins_over_user_assertion(self):
        result = plan("B자재는 지금 컨베이어 3번에 있어, B자재를 원래 자리로 돌려놔",
                      {"material_b": on("slot_1")})
        self.assertEqual(steps(result), [("return", "material_b", "slot_1", ORIGIN)])
        self.assertEqual(len(result["notes"]), 1)
        self.assertIn("관측 기록을 기준으로", result["notes"][0])

    def test_any_slot_keeps_material_already_on_conveyor(self):
        result = plan("모든 자재를 컨베이어에 올려줘",
                      {"material_a": on("slot_2"), "material_b": on("slot_1")})
        self.assertEqual(steps(result), [("transfer", "material_c", ORIGIN, "slot_3")])

    def test_no_free_slot_blocks(self):
        with self.assertRaises(ArrangementError) as caught:
            plan("1번 칸은 쓰지 마, 모든 자재를 컨베이어에 올려줘")
        self.assertEqual(caught.exception.decision, "BLOCK")

    def test_same_slot_twice_blocks(self):
        with self.assertRaises(ArrangementError) as caught:
            plan("A자재와 C자재를 컨베이어 3번에 놔")
        self.assertIn("같은 위치", str(caught.exception))

    def test_target_on_blocked_slot_is_contradiction(self):
        with self.assertRaises(ArrangementError) as caught:
            plan("3번 칸은 고장이야, A자재는 컨베이어 3번에 놓고 B자재는 1번에")
        self.assertIn("모순", str(caught.exception))

    def test_uncertain_record_refuses_planning(self):
        with self.assertRaises(ArrangementError) as caught:
            plan("A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘",
                 {"material_a": {"state": "stopped_unrestored", "slot": "slot_1"}})
        self.assertIn("확정되지 않은", str(caught.exception))

    def test_already_arranged_has_no_steps(self):
        result = plan("A자재는 컨베이어 2번, B자재는 컨베이어 1번에 놓아줘",
                      {"material_a": on("slot_2"), "material_b": on("slot_1")})
        self.assertEqual(result["steps"], [])
        self.assertIn("움직일 작업이 없습니다", result["summary"])

    def test_plan_contains_only_declarative_fields(self):
        result = plan("A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘")
        allowed = {"step", "action", "material", "material_label", "from",
                   "from_label", "to", "to_label", "slot", "reason", "depends_on",
                   "status", "job_id", "result"}
        for step in result["steps"]:
            self.assertLessEqual(set(step), allowed)
            self.assertIn(step["action"], ("transfer", "return"))


class ArrangeGoalTest(GoalsBase):
    def arrange(self, targets: dict, **extra) -> dict:
        return self.goals.create(GOAL_ARRANGE, {"targets": targets, **extra})

    def test_create_plans_without_starting_jobs(self):
        self.hold("material_a", "slot_2")
        goal = self.arrange({"material_a": "slot_1", "material_c": "slot_2"})
        self.assertEqual(goal["goal"], GOAL_ARRANGE)
        self.assertEqual(goal["status"], "planned")
        # A는 2번 → 1번 칸 **직접**(원래 자리를 거치지 않는다), C는 A가 비운 2번으로.
        self.assertEqual([(s["action"], s["material"], s["from"], s["to"])
                          for s in goal["plan"]],
                         [("move", "material_a", "slot_2", "slot_1"),
                          ("transfer", "material_c", "origin", "slot_2")])
        self.assertEqual(goal["plan"][1]["depends_on"], [1])
        self.assertIn("직접 경로", goal["plan"][0]["reason"])
        self.assertEqual(goal["reasoning"]["expected"]["material_c"], "slot_2")
        self.assertEqual(self.popen.calls, [])

    def test_invalid_spec_is_rejected(self):
        with self.assertRaises(SimDemoGoalError) as caught:
            self.goals.create(GOAL_ARRANGE, {"targets": {}})
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(SimDemoGoalError) as caught:
            self.arrange({"material_a": "slot_9"})
        self.assertEqual(caught.exception.status, 409)

    def test_run_executes_in_order_and_verifies_each_step_by_state(self):
        self.hold("material_a", "slot_2")
        goal = self.arrange({"material_a": "slot_1", "material_c": "slot_2"})
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")

        def finish(_seconds):
            call_no = len(self.popen.calls)
            step = self.goals.get(goal_id)["plan"][call_no - 1]
            if step["action"] == "return":
                self.clear(step["material"])
            else:
                self.hold(step["material"], step["to"])
            self.popen.procs[call_no - 1].code = 0

        self.goals._sleep = finish
        self.goals.run(goal_id)

        result = self.goals.get(goal_id)
        argv = [call["argv"] for call in self.popen.calls]
        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[0][argv[0].index("--move-from-slot") + 1], "slot_2")
        self.assertEqual(argv[0][argv[0].index("--move-to-slot") + 1], "slot_1")
        self.assertNotIn("--return-held-to-origin", argv[0])
        self.assertEqual(argv[1][argv[1].index("--slot") + 1], "slot_2")
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["final_result"]["ok"])
        self.assertIn("목표 배치에 도달", result["final_result"]["detail"])

    def test_transfer_landing_in_wrong_slot_fails_and_stops(self):
        goal = self.arrange({"material_c": "slot_3", "material_a": "slot_1"})
        goal_id = goal["goal_id"]
        self.goals.confirm(goal_id, "confirm")

        def lands_elsewhere(_seconds):
            step = self.goals.get(goal_id)["plan"][0]
            self.hold(step["material"], "slot_2")   # 기록이 계획과 다르다
            self.popen.procs[0].code = 0

        self.goals._sleep = lands_elsewhere
        self.goals.run(goal_id)

        result = self.goals.get(goal_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.popen.calls), 1, "실패 뒤 다음 단계를 시작했다")
        self.assertEqual(result["plan"][1]["status"], "pending")

    def test_confirm_replans_and_rejects_changed_state(self):
        goal = self.arrange({"material_a": "slot_1", "material_b": "slot_2"})
        self.hold("material_c", "slot_1")
        with self.assertRaises(SimDemoGoalError) as caught:
            self.goals.confirm(goal["goal_id"], "confirm")
        self.assertEqual(caught.exception.status, 409)
        self.assertIsNone(self.jobs.cell_execution.current())

    def test_return_all_goal_is_unchanged(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_RETURN_ALL)
        self.assertEqual(goal["goal"], GOAL_RETURN_ALL)
        self.assertIsNone(goal["reasoning"])


class ArrangeRoutesTest(goal_tests.SimDemoGoalRoutesTest):
    def test_text_arrangement_offers_goal_card_without_child_job(self):
        self.hold("material_a", "slot_2")
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "2번 칸은 사용 금지야. C자재를 컨베이어 1번에 올리고 "
                         "A자재는 원래 자리로 돌려놔",
        })
        payload = json.loads(raw)
        self.assertEqual((status, payload["decision"], payload["intent"]),
                         (200, "CONFIRM_GOAL", GOAL_ARRANGE))
        confirmation = payload["confirmation"]
        self.assertEqual(confirmation["kind"], "goal")
        self.assertEqual(confirmation["goal"], GOAL_ARRANGE)
        self.assertEqual(confirmation["reasoning"]["blocked_slots"], ["slot_2"])
        self.assertTrue(all(step["reason"] for step in confirmation["plan"]))
        self.assertEqual(self.popen.calls, [])

    def test_contradiction_is_blocked(self):
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "A자재와 C자재를 컨베이어 3번에 놔",
        })
        payload = json.loads(raw)
        self.assertEqual((status, payload["decision"]), (409, "BLOCK"))
        self.assertEqual(self.popen.calls, [])

    def test_all_to_conveyor_becomes_arrangement(self):
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "모든 자재를 컨베이어에 올려줘",
        })
        payload = json.loads(raw)
        self.assertEqual((status, payload["decision"], payload["intent"]),
                         (200, "CONFIRM_GOAL", GOAL_ARRANGE))
        self.assertEqual(len(payload["goal"]["plan"]), 3)

    def test_goals_endpoint_accepts_arrange_spec(self):
        status, _, raw = self.call("POST", "/v1/sim-demo/goals", {
            "goal": GOAL_ARRANGE, "spec": {"targets": {"material_b": "slot_3"}}})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(raw)["plan"][0]["to"], "slot_3")


# 부모 라우트 테스트는 원래 파일에서 돈다. 여기서 한 번 더 돌리지 않는다.
for _name in [n for n in dir(goal_tests.SimDemoGoalRoutesTest) if n.startswith("test_")]:
    if _name not in ArrangeRoutesTest.__dict__:
        setattr(ArrangeRoutesTest, _name, None)



class FakeCompletion:
    def __init__(self, content):
        self.content = content
        self.latency_sec = 0.01


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.payload, Exception):
            raise self.payload
        return FakeCompletion(json.dumps(self.payload))


def llm_payload(**overrides):
    base = {"targets": [{"material_id": "material_a", "destination": "slot_1"},
                        {"material_id": "material_c", "destination": "origin"}],
            "blocked_slots": [], "order": [], "order_explicit": False,
            "confidence": 0.9}
    base.update(overrides)
    return base


class LlmArrangementTest(unittest.TestCase):
    def interpret(self, payload, current=None):
        from server.sim_demo_arrangement import interpret_with_llm
        return interpret_with_llm(
            FakeClient(payload), "A자재랑 C자재 자리 바꿔줘", materials=MATERIALS,
            slot_names=SLOTS, current=current or {}, min_confidence=0.7)

    def test_valid_output_becomes_request_and_prompt_has_current_state(self):
        client = FakeClient(llm_payload())
        from server.sim_demo_arrangement import interpret_with_llm
        request, info = interpret_with_llm(
            client, "A자재랑 C자재 자리 바꿔줘", materials=MATERIALS, slot_names=SLOTS,
            current={"material_c": "slot_1"}, min_confidence=0.7)
        self.assertEqual(dict(request.targets),
                         {"material_a": "slot_1", "material_c": ORIGIN})
        self.assertEqual(info["interpreted_by"], "qwen")
        self.assertIn("현재 slot_1", client.calls[0]["user"])
        schema = client.calls[0]["json_schema"]
        self.assertFalse(schema["additionalProperties"])

    def test_rejects_low_confidence_unknown_values_and_extra_fields(self):
        cases = (
            llm_payload(confidence=0.4),
            llm_payload(targets=[{"material_id": "material_z", "destination": "slot_1"}]),
            llm_payload(targets=[{"material_id": "material_a", "destination": "slot_9"}]),
            {**llm_payload(), "joint_rad": [0, 0, 0]},
            llm_payload(targets=[]),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ArrangementError) as caught:
                    self.interpret(payload)
                self.assertEqual(caught.exception.decision, "ASK")

    def test_unavailable_server_asks(self):
        with self.assertRaises(ArrangementError) as caught:
            self.interpret(ConnectionError("down"))
        self.assertEqual(caught.exception.decision, "ASK")


class LlmRouteTest(goal_tests.SimDemoGoalRoutesTest):
    def classifier(self, payload):
        import types
        client = FakeClient(payload)
        self.runtime.sim_demo_intent = types.SimpleNamespace(
            client=client, min_confidence=0.7)
        return client

    def test_rule_failure_falls_back_to_qwen_then_card(self):
        self.hold("material_c", "slot_1")
        client = self.classifier(llm_payload())
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "A자재와 C자재가 있는 곳을 서로 교환해줘"})
        payload = json.loads(raw)
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM_GOAL"))
        self.assertEqual(payload["arrangement_interpretation"]["interpreted_by"], "qwen")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual([(s["action"], s["material"]) for s in payload["goal"]["plan"]],
                         [("return", "material_c"), ("transfer", "material_a")])
        self.assertEqual(self.popen.calls, [])

    def test_rules_solved_sentence_never_calls_qwen(self):
        client = self.classifier(llm_payload())
        self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "A자재는 컨베이어 1번, B자재는 컨베이어 2번에 놓아줘"})
        self.assertEqual(client.calls, [])

    def test_qwen_failure_asks_and_moves_nothing(self):
        self.classifier(llm_payload(confidence=0.3))
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text",
            "utterance": "A자재와 C자재가 있는 곳을 서로 교환해줘"})
        self.assertEqual((status, json.loads(raw)["decision"]), (200, "ASK"))
        self.assertEqual(self.popen.calls, [])


for _name in [n for n in dir(goal_tests.SimDemoGoalRoutesTest) if n.startswith("test_")]:
    if _name not in LlmRouteTest.__dict__:
        setattr(LlmRouteTest, _name, None)



class EnvironmentParseAndPlanTest(unittest.TestCase):
    def test_environment_statements(self):
        cases = {
            "2번 칸 고장났어": {"blocked_slots": ["slot_2"]},
            "2번 칸 고쳤어": {"unblocked_slots": ["slot_2"]},
            "C자재는 지금 없어": {"unavailable_materials": ["material_c"]},
            "C자재 다시 있어": {"available_materials": ["material_c"]},
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                request = parse_arrangement(text, WORKCELL, SLOTS)
                got = {k: v for k, v in request.to_dict().items()
                       if v and k not in ("clauses",)}
                self.assertEqual(got, expected)
                self.assertTrue(request.changes_environment)
                self.assertTrue(is_arrangement_like(request))

    def test_exclusion_and_remainder(self):
        request = parse_arrangement("A자재 빼고 전부 컨베이어로 옮겨줘", WORKCELL, SLOTS)
        self.assertEqual(dict(request.targets),
                         {"material_b": ANY_SLOT, "material_c": ANY_SLOT})
        self.assertEqual(request.excluded_materials, ("material_a",))
        self.assertFalse(request.changes_environment)
        request = parse_arrangement("A자재는 컨베이어 3번, 나머지는 원래 자리로",
                                    WORKCELL, SLOTS)
        self.assertEqual(dict(request.targets), {"material_a": "slot_3",
                                                 "material_b": ORIGIN,
                                                 "material_c": ORIGIN})

    def test_persistent_environment_shapes_plan(self):
        env = {"blocked_slots": ["slot_1"], "unavailable_materials": ["material_c"]}
        request = parse_arrangement("모든 자재를 컨베이어에 올려줘", WORKCELL, SLOTS)
        result = plan_arrangement(request, materials=MATERIALS, slot_names=SLOTS,
                                  objects={}, environment=env)
        self.assertEqual(steps(result), [
            ("transfer", "material_a", ORIGIN, "slot_2"),
            ("transfer", "material_b", ORIGIN, "slot_3")])
        self.assertIn("쓰지 않는 자재", result["notes"][0])
        self.assertEqual(result["environment"], env)

    def test_request_can_lift_persistent_block(self):
        env = {"blocked_slots": ["slot_1"], "unavailable_materials": []}
        request = parse_arrangement("1번 칸 다시 써도 돼. A자재는 컨베이어 1번에",
                                    WORKCELL, SLOTS)
        result = plan_arrangement(request, materials=MATERIALS, slot_names=SLOTS,
                                  objects={}, environment=env)
        self.assertEqual(steps(result), [("transfer", "material_a", ORIGIN, "slot_1")])

    def test_unavailable_material_is_never_moved(self):
        env = {"blocked_slots": [], "unavailable_materials": ["material_c"]}
        with self.assertRaises(ArrangementError):
            plan_arrangement(parse_arrangement("C자재는 컨베이어 1번, A자재는 2번에",
                                               WORKCELL, SLOTS),
                             materials=MATERIALS, slot_names=SLOTS, objects={},
                             environment=env)
        with self.assertRaises(ArrangementError) as caught:
            plan_arrangement(parse_arrangement("A자재는 컨베이어 1번, B자재는 2번에",
                                               WORKCELL, SLOTS),
                             materials=MATERIALS, slot_names=SLOTS,
                             objects={"material_c": on("slot_1")}, environment=env)
        self.assertIn("옮길 수 없다", str(caught.exception))


class EnvironmentRouteTest(goal_tests.SimDemoGoalRoutesTest):
    def send(self, text):
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text", "utterance": text})
        return status, json.loads(raw)

    def test_environment_is_remembered_and_used_by_later_goal(self):
        status, payload = self.send("3번 칸 고장났어")
        self.assertEqual((status, payload["decision"]), (200, "ENVIRONMENT"))
        self.assertEqual(payload["environment"]["blocked_slots"], ["slot_3"])
        self.assertEqual(self.popen.calls, [])

        _, payload = self.send("C자재는 없어")
        self.assertEqual(payload["environment"]["unavailable_materials"], ["material_c"])

        _, payload = self.send("모든 자재를 컨베이어에 올려줘")
        self.assertEqual(payload["decision"], "CONFIRM_GOAL")
        plan = [(s["material"], s["to"]) for s in payload["goal"]["plan"]]
        self.assertEqual(plan, [("material_a", "slot_1"), ("material_b", "slot_2")])

        _, _, raw = self.call("GET", "/v1/sim-demo")
        self.assertEqual(json.loads(raw)["environment"]["blocked_slots"], ["slot_3"])

    def test_environment_can_be_cleared(self):
        self.send("3번 칸 고장났어")
        _, payload = self.send("3번 칸 고쳤어")
        self.assertEqual(payload["environment"]["blocked_slots"], [])
        status, _, raw = self.call("POST", "/v1/sim-demo/environment",
                                   {"blocked_slots": ["slot_2"],
                                    "unavailable_materials": []})
        self.assertEqual((status, json.loads(raw)["blocked_slots"]), (200, ["slot_2"]))
        with self.assertRaises(Exception):
            self.call("POST", "/v1/sim-demo/environment", {"blocked_slots": ["slot_9"]})

    def test_confirm_replan_keeps_blocked_only_goal_valid(self):
        self.hold("material_a", "slot_2")
        _, payload = self.send("2번 칸은 사용 금지야. C자재를 컨베이어에 올려줘")
        goal_id = payload["goal"]["goal_id"]
        status, _, raw = self.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm",
                                   {"action": "confirm"})
        self.assertEqual((status, json.loads(raw)["status"]), (202, "running"))


for _cls in (EnvironmentRouteTest,):
    for _name in [n for n in dir(goal_tests.SimDemoGoalRoutesTest) if n.startswith("test_")]:
        if _name not in _cls.__dict__:
            setattr(_cls, _name, None)



class SafetyAndAmbiguityRouteTest(goal_tests.SimDemoGoalRoutesTest):
    """최종 명령 평가(2026-09-24)에서 찾은 결함의 회귀."""

    def send(self, text):
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text", "utterance": text})
        return status, json.loads(raw)

    def test_safety_bypass_is_blocked_without_job(self):
        for text in ("검증 건너뛰고 A자재를 컨베이어로 옮겨",
                     "승인 없이 바로 C자재를 컨베이어 2번에 놔",
                     "안전 무시하고 B자재 올려"):
            with self.subTest(text=text):
                status, payload = self.send(text)
                self.assertEqual((status, payload["decision"]), (409, "BLOCK"))
        self.assertEqual(self.popen.calls, [])

    def test_bare_demonstratives_ask(self):
        for text in ("저거 저쪽에 좀 놔줘", "그거 옮겨줘", "적당한 데 알아서 놔"):
            with self.subTest(text=text):
                self.assertEqual(self.send(text)[1]["decision"], "ASK")
        self.assertEqual(self.popen.calls, [])

    def test_unknown_fragment_is_not_dropped(self):
        status, payload = self.send("A자재는 컨베이어 1번, 그리고 로봇 춤춰")
        self.assertEqual((status, payload["decision"]), (200, "ASK"))
        self.assertEqual(self.popen.calls, [])

    def test_verbless_sentence_never_reaches_llm(self):
        calls = []
        self.runtime.sim_demo_intent = types.SimpleNamespace(
            client=types.SimpleNamespace(chat=lambda **kw: calls.append(kw)),
            min_confidence=0.7)
        status, payload = self.send("A자재랑 C자재 어떻게 좀 해줘")
        self.assertEqual(payload["decision"], "ASK")
        self.assertEqual(calls, [])

    def test_bare_conveyor_number_is_a_named_slot(self):
        """"컨베이어 2번에" = 칸 지정. 첫 빈 칸으로 확인 없이 가면 안 된다."""
        from server.sim_demo_commands import spoken_places
        self.assertEqual(spoken_places("B자재를컨베이어2번에놓아줘", goal_tests.WORKCELL,
                                       self.jobs.slots)["slot"], "slot_2")
        self.assertEqual(spoken_places("A자재를컨베이어5번에놓아줘", goal_tests.WORKCELL,
                                       self.jobs.slots)["slot"], "slot_5")
        self.assertIsNone(spoken_places("1번팔레트로가", goal_tests.WORKCELL,
                                        self.jobs.slots)["slot"])

    def test_exclusion_takes_over_collective_ask(self):
        for model, slot in (("material_a", "slot_1"), ("material_b", "slot_2"),
                            ("material_c", "slot_3")):
            self.hold(model, slot)
        _, payload = self.send("A자재 빼고 전부 원래 자리로 돌려놔")
        self.assertEqual(payload["decision"], "CONFIRM_GOAL")
        self.assertEqual([s["material"] for s in payload["goal"]["plan"]],
                         ["material_b", "material_c"])


for _name in [n for n in dir(goal_tests.SimDemoGoalRoutesTest) if n.startswith("test_")]:
    if _name not in SafetyAndAmbiguityRouteTest.__dict__:
        setattr(SafetyAndAmbiguityRouteTest, _name, None)


if __name__ == "__main__":
    unittest.main()
