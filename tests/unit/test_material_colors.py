"""색깔로 자재 지정 — 정규화 · 선언/관측 대조 · 되묻기 · 기존 경로 통합."""

from __future__ import annotations

import copy
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.material_colors import (  # noqa: E402
    ColorRegistry,
    ColorResolutionError,
    canonical_color,
    observe_gazebo_colors,
    stray_color_words,
)
from tests.unit import test_sim_demo_goals as goal_tests  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL  # noqa: E402

OBSERVED = {"material_a": (0.9, 0.5, 0.1, 1.0), "material_b": (0.35, 0.75, 0.95, 1.0),
            "material_c": (0.6, 0.9, 0.4, 1.0)}


class NormalizationTest(unittest.TestCase):
    def test_synonyms_share_one_color(self):
        for word in ("빨강", "빨간", "빨간색", "적색", "레드"):
            self.assertEqual(canonical_color(word), "red", word)
        for word in ("파랑", "파란색", "청색", "블루"):
            self.assertEqual(canonical_color(word), "blue", word)
        self.assertEqual(canonical_color("노란색"), "yellow")
        self.assertIsNone(canonical_color("무지개"))

    def test_declared_colors_come_from_workcell(self):
        registry = ColorRegistry.from_workcell(WORKCELL, OBSERVED)
        self.assertEqual(registry.by_color["orange"], ["material_a"])
        self.assertEqual(registry.by_color["blue"], ["material_b"])
        self.assertEqual(registry.by_color["sky"], ["material_b"])
        self.assertEqual(registry.by_color["green"], ["material_c"])
        self.assertTrue(all(i.status == "verified" for i in registry.materials.values()))


class RewriteTest(unittest.TestCase):
    def setUp(self):
        self.registry = ColorRegistry.from_workcell(WORKCELL, OBSERVED)

    def test_color_references_become_material_names(self):
        text, subs = self.registry.rewrite("파란색 자재는 빼고 나머지를 제자리로 돌려놔")
        self.assertEqual(text, "B자재는 빼고 나머지를 제자리로 돌려놔")
        self.assertEqual(subs[0]["material"], "material_b")
        text, _ = self.registry.rewrite("청색 거를 1번 칸으로, 주황 블록은 원래 자리로")
        self.assertEqual(text, "B자재를 1번 칸으로, A자재은 원래 자리로")
        text, _ = self.registry.rewrite("초록 걸 먼저 옮겨")
        self.assertEqual(text, "C자재를 먼저 옮겨")

    def test_pallet_color_is_not_a_material(self):
        # 2026-09-25: 팔레트 색은 자재가 아니라 **팔레트 이름**으로 바뀐다.
        text, subs = self.registry.rewrite("주황 팔레트로 이동")
        self.assertEqual(text, "1번 팔레트로 이동")
        self.assertEqual([r.get("pallet") for r in subs], ["pallet_1"])
        self.assertNotIn("material", subs[0])
        self.assertEqual(stray_color_words(text), [])

    def test_pallet_colors_come_from_tray_matching_its_material(self):
        text, subs = self.registry.rewrite("파란 자재를 초록 팔레트로 옮겨줘")
        self.assertEqual(text, "B자재를 3번 팔레트로 옮겨줘")
        self.assertEqual(self.registry.pallets["pallet_3"].colors, ("green", "lime"))
        # 트레이 색이 자재 색과 다르게 선언되면 색 낱말을 물려받지 않는다(추측 없음).
        workcell = copy.deepcopy(WORKCELL)
        workcell["models"]["pallet_3"]["parts"][0]["color_rgba"] = [0.2, 0.2, 0.2, 1.0]
        registry = ColorRegistry.from_workcell(workcell, OBSERVED)
        with self.assertRaises(ColorResolutionError) as caught:
            registry.rewrite("B자재를 초록 팔레트로 옮겨줘")
        self.assertIn("팔레트 번호로", str(caught.exception))

    def test_pallet_observation_mismatch_asks(self):
        registry = ColorRegistry.from_workcell(
            WORKCELL, dict(OBSERVED, pallet_3=(0.9, 0.1, 0.1, 1.0)))
        self.assertEqual(registry.pallets["pallet_3"].status, "mismatch")
        with self.assertRaises(ColorResolutionError) as caught:
            registry.rewrite("B자재를 초록 팔레트로 옮겨줘")
        self.assertIn("설정과 다릅니다", str(caught.exception))

    def test_unregistered_color_asks(self):
        for text in ("빨간 자재를 1번 칸으로 옮겨줘", "노란 자재 먼저 옮겨줘"):
            with self.subTest(text=text):
                with self.assertRaises(ColorResolutionError) as caught:
                    self.registry.rewrite(text)
                self.assertIn("등록되어 있지 않습니다", str(caught.exception))

    def test_duplicate_color_asks(self):
        workcell = copy.deepcopy(WORKCELL)
        for row in workcell["resource_map"]:
            if row.get("gazebo_model") == "material_c":
                row["korean_colors"] = ["파랑"]
        registry = ColorRegistry.from_workcell(workcell, OBSERVED)
        with self.assertRaises(ColorResolutionError) as caught:
            registry.rewrite("파란 자재를 1번 칸으로")
        self.assertIn("여럿", str(caught.exception))

    def test_observation_mismatch_asks(self):
        observed = dict(OBSERVED, material_b=(0.9, 0.1, 0.1, 1.0))
        registry = ColorRegistry.from_workcell(WORKCELL, observed)
        self.assertEqual(registry.materials["material_b"].status, "mismatch")
        with self.assertRaises(ColorResolutionError) as caught:
            registry.rewrite("파란 자재를 1번 칸으로")
        self.assertIn("설정과 다릅니다", str(caught.exception))

    def test_stray_color_words_are_detected(self):
        self.assertEqual(stray_color_words("빨간색으로 된 걸 옮겨"), ["빨간색"])


class ObserverTest(unittest.TestCase):
    def test_parses_scene_info_and_caches(self):
        scene = ('model { name: "material_b" link { visual { material { diffuse {\n'
                 ' r: 0.35\n g: 0.75\n b: 0.95\n a: 1\n } } } } }')
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return types.SimpleNamespace(stdout=scene)

        colors, detail = observe_gazebo_colors("w_test", "p_test", ["material_b"],
                                               runner=runner, clock=lambda: 1.0)
        self.assertEqual(colors["material_b"][:3], (0.35, 0.75, 0.95))
        observe_gazebo_colors("w_test", "p_test", ["material_b"], runner=runner,
                              clock=lambda: 2.0)
        self.assertEqual(len(calls), 1)

    def test_failure_is_no_observation(self):
        def runner(argv, **kwargs):
            raise FileNotFoundError("gz")
        colors, detail = observe_gazebo_colors("w_fail", "p_fail", ["material_a"],
                                               runner=runner)
        self.assertEqual(colors, {})
        self.assertIn("관측 실패", detail)


class ColorRouteTest(goal_tests.SimDemoGoalRoutesTest):
    def send(self, text):
        status, _, raw = self.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "text", "utterance": text})
        return status, json.loads(raw)

    def observe(self, observed):
        self.runtime.sim_demo_color_observer = lambda models: (observed, "test")

    def llm(self):
        calls = []
        client = types.SimpleNamespace(chat=lambda **kw: calls.append(kw))
        self.runtime.sim_demo_intent = types.SimpleNamespace(
            client=client, min_confidence=0.7, classify=lambda *a, **k: calls.append(a))
        return calls

    def test_color_arrangement_goes_through_existing_goal_flow(self):
        self.observe(OBSERVED)
        self.hold("material_a", "slot_1")
        self.hold("material_c", "slot_2")
        status, payload = self.send("파란색 자재는 빼고 나머지를 제자리로 돌려놔")
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM_GOAL"))
        self.assertEqual([(s["action"], s["material"]) for s in payload["goal"]["plan"]],
                         [("return", "material_c"), ("return", "material_a")])
        # (A 1번의 복귀 경로가 2번 칸을 스친다(측정) → 2번의 C가 먼저 빠진다.)
        subs = payload["color_resolution"]["substitutions"]
        self.assertEqual((subs[0]["material"], subs[0]["check"]),
                         ("material_b", "verified"))
        self.assertEqual(self.popen.calls, [])

    def test_color_with_environment_and_order(self):
        self.observe(OBSERVED)
        self.send("3번 칸 고장났어")
        _, payload = self.send("초록색 자재를 먼저 컨베이어에 올리고 청색 자재는 컨베이어 1번에")
        plan = [(s["material"], s["to"]) for s in payload["goal"]["plan"]]
        self.assertEqual(plan, [("material_c", "slot_2"), ("material_b", "slot_1")])

    def test_unregistered_color_asks_without_llm(self):
        calls = self.llm()
        for text in ("빨간 자재를 1번 칸으로 옮겨줘", "노란 자재 먼저 옮겨줘",
                     "빨간색으로 된 걸 컨베이어로 옮겨"):
            with self.subTest(text=text):
                status, payload = self.send(text)
                self.assertEqual((status, payload["decision"]), (200, "ASK"))
        self.assertEqual(calls, [])
        self.assertEqual(self.popen.calls, [])

    def test_observation_mismatch_asks(self):
        self.observe(dict(OBSERVED, material_a=(0.1, 0.1, 0.9, 1.0)))
        status, payload = self.send("주황 자재를 컨베이어 1번에 올려줘")
        self.assertEqual((status, payload["decision"]), (200, "ASK"))
        self.assertIn("설정과 다릅니다", payload["reason"])

    def test_blue_to_green_pallet_plans_vacating_first(self):
        observed = dict(OBSERVED, pallet_1=(0.9, 0.5, 0.1, 1.0),
                        pallet_2=(0.35, 0.75, 0.95, 1.0), pallet_3=(0.6, 0.9, 0.4, 1.0))
        self.observe(observed)
        status, payload = self.send("파란 자재를 초록 팔레트로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM_GOAL"),
                         payload.get("reason"))
        plan = payload["goal"]["plan"]
        self.assertEqual([(s["material"], s["from"], s["to"]) for s in plan],
                         [("material_c", "origin", "slot_1"),
                          ("material_b", "origin", "loc_pallet_3")])
        self.assertIn("3번 팔레트", plan[0]["reason"])
        self.assertEqual(plan[1]["depends_on"], [1])
        checks = {r.get("pallet") or r.get("material"): r["check"]
                  for r in payload["color_resolution"]["substitutions"]}
        self.assertEqual(checks, {"pallet_3": "verified", "material_b": "verified"})
        self.assertEqual(self.popen.calls, [])

    def test_single_color_command_needs_confirmation(self):
        """"B자재를 컨베이어로"는 바로 실행되지만, 색으로 말하면 확인 카드를 거친다."""
        self.observe(OBSERVED)
        status, payload = self.send("하늘색 자재를 컨베이어로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (409, "BLOCK"))   # 확인 기능 없음
        from server.sim_demo_confirm import ConfirmStore
        self.runtime.sim_demo_confirm = ConfirmStore(ttl_sec=60)
        status, payload = self.send("하늘색 자재를 컨베이어로 옮겨줘")
        self.assertEqual(payload["decision"], "CONFIRM")
        self.assertEqual(payload["material"], "material_b")
        self.assertEqual(payload["confirmation"]["evidence"]["material_korean"], "B자재")
        self.assertIsNone(payload["job"])
        self.assertEqual(self.popen.calls, [])


for _name in [n for n in dir(goal_tests.SimDemoGoalRoutesTest) if n.startswith("test_")]:
    if _name not in ColorRouteTest.__dict__:
        setattr(ColorRouteTest, _name, None)


if __name__ == "__main__":
    unittest.main()
