"""생략·지시·맥락 표현 해석(`server/sim_demo_context.py`)과 경로 통합."""

from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_confirm import ConfirmStore  # noqa: E402
from server.api import ApiError  # noqa: E402
from server.sim_demo_context import (  # noqa: E402
    CellFacts,
    DialogueContext,
    DialogueContexts,
    resolve,
)
from tests.unit.test_sim_demo_goals import GoalsBase  # noqa: E402

NAMES = {"material_a": "A자재", "material_b": "B자재", "material_c": "C자재"}
ALIASES = {"material_a": ("a자재", "에이자재", "주황자재"),
           "material_b": ("b자재", "비자재", "파랑자재"),
           "material_c": ("c자재", "씨자재", "초록자재")}


def facts(blocked=(), **where):
    return CellFacts(names=NAMES, aliases=ALIASES,
                     origin_pallet_no={"material_a": "1", "material_b": "2", "material_c": "3"},
                     slots=("slot_1", "slot_2", "slot_3"),
                     location={m: where.get(m, "origin") for m in NAMES},
                     blocked=frozenset(blocked))


class ResolveTest(unittest.TestCase):
    def test_unsupported_actions_are_blocked_not_converted(self):
        for text in ("A자재 뒤집어서 놔줘", "B자재 컨베이어 1번 칸에 쌓아줘", "A자재 들고 기다려",
                     "C자재를 컨베이어 2번과 3번 사이에 놔", "A자재 컨베이어 끝까지 밀어줘",
                     "초록 거 90도 돌려서 놔"):
            with self.subTest(text=text):
                self.assertEqual(resolve(text, facts=facts(), context=None).decision, "BLOCK")
        # "돌려놔"(복귀)는 회전이 아니다.
        self.assertIsNone(resolve("A자재를 원래 자리로 돌려놔", facts=facts(material_a="slot_1"),
                                  context=None).decision)

    def test_drop_midway_question_and_quantity_do_not_run(self):
        # 규칙만으로 RUN이 되던 문장(2026-09-30 실측).
        self.assertEqual(resolve("A자재 컨베이어로 옮기다가 중간에 놔버려", facts=facts(),
                                 context=None).decision, "BLOCK")
        for text in ("A자재를 컨베이어에 올려놓을 수 있을까", "C자재 컨베이어에 놔줄 수 있지?",
                     "A자재 두 개 컨베이어에 올려", "C자재 3개 컨베이어로"):
            with self.subTest(text=text):
                self.assertEqual(resolve(text, facts=facts(), context=None).decision, "ASK")
        # 요청형 "~줄래"는 되묻지 않는다.
        self.assertIsNone(resolve("B자재를 컨베이어로 옮겨줄래", facts=facts(),
                                  context=None).decision)

    def test_stt_uncertainty_asks(self):
        low = resolve("B 자재를 컨베이어 2번에 놓아줘", facts=facts(), context=None,
                      source="stt_final", stt_confidence=0.42)
        self.assertEqual(low.decision, "ASK")
        for text in ("비 자재를 컨베이어 이번에 놓아줘", "C 자재를 컨베이어 일번 칸에",
                     "에이 자재 컨베이어 삼 번"):
            with self.subTest(text=text):
                self.assertEqual(resolve(text, facts=facts(), context=None,
                                         source="stt_final", stt_confidence=0.7).decision, "ASK")
        ok = resolve("B 자재를 컨베이어 2번에 놓아줘", facts=facts(), context=None,
                     source="stt_final", stt_confidence=0.81)
        self.assertIsNone(ok.decision)

    def test_letters_and_bare_number(self):
        res = resolve("B는 3번 칸, C는 2번 칸", facts=facts(), context=None)
        self.assertEqual(res.text, "B자재는 3번 칸, C자재는 2번 칸")
        res = resolve("B자재 2번으로", facts=facts(), context=None)
        self.assertIn("컨베이어 2번 칸", res.text)
        self.assertTrue(res.evidence)
        both = resolve("B자재 2번으로", facts=facts(material_b="slot_1"), context=DialogueContext())
        self.assertEqual(both.decision, "ASK")
        self.assertIn("2번 팔레트", both.reason)
        res = resolve("A자재 3번에", facts=facts(), context=None)
        self.assertIn("컨베이어 3번 칸", res.text)       # 3번 팔레트는 A 자리가 아니다

    def test_pronoun_from_dialogue_then_observation(self):
        ctx = DialogueContext()
        ctx.set_focus(["material_b"])
        res = resolve("그다음 그거 제자리로", facts=facts(material_b="slot_1"), context=ctx)
        self.assertIn("B자재", res.text)
        self.assertIn("직전 대화", res.evidence[0])
        ctx.set_focus(["material_a", "material_b"])
        self.assertEqual(resolve("그거 제자리로", facts=facts(material_a="slot_1",
                                                         material_b="slot_2"),
                                 context=ctx).decision, "ASK")
        # 맥락이 없으면 관측상 후보가 하나여도 추측하지 않고 묻는다(2026-09-25 세션 분리).
        only = resolve("그거 제자리로", facts=facts(material_a="slot_2"), context=None)
        self.assertEqual(only.decision, "ASK")
        self.assertIn("맥락", only.reason)
        self.assertEqual(resolve("그거 컨베이어로 옮겨줘", facts=facts(), context=None).decision,
                         "ASK")
        self.assertEqual(resolve("그다음엔 저것도 옮겨줘", facts=facts(), context=ctx).decision,
                         "ASK")
        self.assertEqual(resolve("방금 옮긴 거 제자리로", facts=facts(material_a="slot_1"),
                                 context=DialogueContext()).decision, "ASK")

    def test_focus_expires(self):
        now = [0.0]
        ctx = DialogueContext(clock=lambda: now[0])
        ctx.set_focus(["material_b"])
        now[0] = 601.0
        self.assertEqual(ctx.current_focus(), ())

    def test_free_slot_unique_multiple_blocked(self):
        res = resolve("B자재를 빈 칸에 옮겨줘", facts=facts(material_a="slot_1", material_c="slot_3"),
                      context=None)
        self.assertIn("컨베이어 2번 칸", res.text)
        many = resolve("B자재를 빈 칸에 옮겨줘", facts=facts(material_a="slot_1"),
                       context=DialogueContext())
        self.assertEqual(many.decision, "ASK")
        self.assertIn("2번·3번", many.reason)
        res = resolve("B자재를 빈 칸에 옮겨줘", facts=facts(("slot_3",), material_a="slot_1"),
                      context=None)
        self.assertIn("컨베이어 2번 칸", res.text)
        self.assertIn("사용 금지", res.evidence[-1])

    def test_location_references(self):
        self.assertIn("C자재", resolve("3번 칸에 있는 거 1번 칸으로",
                                       facts=facts(material_c="slot_3"), context=None).text)
        self.assertEqual(resolve("2번 칸에 있는 거 제자리로", facts=facts(material_a="slot_1"),
                                 context=None).decision, "BLOCK")
        self.assertIn("A자재", resolve("1번 칸 비워", facts=facts(material_a="slot_1"),
                                       context=None).text)
        res = resolve("컨베이어 비워줘", facts=facts(material_a="slot_1", material_c="slot_3"),
                      context=None)
        self.assertEqual(res.text, "A자재랑 C자재는 원래 자리로 돌려놔")
        self.assertEqual(resolve("남은 거 컨베이어로 올려", facts=facts(material_a="slot_1"),
                                 context=None).decision, "ASK")

    def test_swap(self):
        res = resolve("A자재랑 B자재 자리 바꿔", facts=facts(material_a="slot_1",
                                                     material_b="slot_2"), context=None)
        self.assertIn("A자재는 컨베이어 2번 칸에", res.text)
        self.assertIn("B자재는 컨베이어 1번 칸에", res.text)
        self.assertEqual(resolve("A자재랑 B자재 자리 바꿔", facts=facts(), context=None).decision,
                         "BLOCK")

    def test_pending_answers(self):
        ctx = DialogueContext()
        self.assertEqual(resolve("B자재를 빈 칸에 옮겨줘", facts=facts(material_a="slot_1"),
                                 context=ctx).decision, "ASK")
        self.assertEqual(resolve("5번", facts=facts(material_a="slot_1"), context=ctx).decision,
                         "ASK")                                  # 선택지에 없음 → 다시 묻는다
        res = resolve("3번", facts=facts(material_a="slot_1"), context=ctx)
        self.assertIn("컨베이어 3번 칸", res.text)
        self.assertIn("직전 질문", res.evidence[0])
        self.assertIsNone(ctx.current_pending())
        resolve("그거 컨베이어로 옮겨줘", facts=facts(), context=ctx)
        self.assertIn("B자재", resolve("B", facts=facts(), context=ctx).text)
        resolve("B자재 2번으로", facts=facts(material_b="slot_1"), context=ctx)
        self.assertIn("컨베이어 2번 칸", resolve("아니 2번 팔레트 말고 칸",
                                              facts=facts(material_b="slot_1"), context=ctx).text)
        resolve("B자재를 빈 칸에 옮겨줘", facts=facts(material_a="slot_1"), context=ctx)
        cancelled = resolve("취소", facts=facts(material_a="slot_1"), context=ctx)
        self.assertEqual(cancelled.decision, "ASK")
        self.assertIsNone(ctx.current_pending())


class ContextRouteTest(GoalsBase):
    def setUp(self):
        super().setUp()
        self.now = [1000.0]
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=self.jobs, sim_demo_goals=self.goals, sim_demo_disabled_reason=None,
            sim_demo_confirm=ConfirmStore(ttl_sec=60),
            sim_demo_contexts=DialogueContexts(clock=lambda: self.now[0]))
        self.ended: set[str] = set()

    def send(self, text, source="text", confidence=None, session="s1"):
        from server.routes import sim_demo

        async def read_body(_receive):
            body = {"mode": "simulation_demo", "source": source, "utterance": text,
                    "session_id": session}
            if source == "stt_final":
                body.update(raw_transcript=text, stt_confidence=confidence)
            return body

        def require_session(session_id):
            if session_id in self.ended:
                raise ApiError(409, None, "세션이 종료됐다")
        ctx = types.SimpleNamespace(runtime=self.runtime, read_body=read_body,
                                    api=types.SimpleNamespace(require_session=require_session))
        status, _, raw = asyncio.run(sim_demo.handle(ctx, "POST", "/v1/sim-demo/command",
                                                     None, {}))
        return status, json.loads(raw)

    def test_contextual_return_needs_confirmation_and_shows_evidence(self):
        self.hold("material_b", "slot_1")
        self.send("B자재를 컨베이어 1번에 놓아줘")        # 이미 거기 — 언급은 기억한다
        status, payload = self.send("그다음 그거 제자리로")
        self.assertIn(payload["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        self.assertNotEqual(payload["decision"], "RUN")
        evidence = payload["confirmation"]["interpretation"]["evidence"]
        self.assertTrue(any("B자재" in row for row in evidence))
        self.assertEqual(self.popen.calls, [])

    def test_ask_then_answer_builds_the_plan(self):
        self.hold("material_a", "slot_1")
        status, payload = self.send("B자재를 빈 칸에 옮겨줘")
        self.assertEqual(payload["decision"], "ASK")
        status, payload = self.send("3번")
        self.assertIn(payload["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        plan = (payload.get("goal") or {}).get("plan") or []
        self.assertEqual([(s["material"], s["to"]) for s in plan], [("material_b", "slot_3")])
        self.assertEqual(self.popen.calls, [])

    def test_voice_low_confidence_repeats_what_was_heard(self):
        status, payload = self.send("B 자재를 컨베이어 2번에 놓아줘", "stt_final", 0.4)
        self.assertEqual(payload["decision"], "ASK")
        self.assertIn("제가 이렇게 들었습니다", payload["reason"])


class SessionIsolationTest(ContextRouteTest):
    """대화 맥락은 브라우저 세션별이다(인증이 아니다)."""

    def test_two_sessions_do_not_share_focus_or_pending(self):
        self.hold("material_b", "slot_1")
        self.send("B자재를 컨베이어 1번에 놓아줘", session="s1")
        _, other = self.send("그거 제자리로", session="s2")
        self.assertEqual(other["decision"], "ASK")          # s2는 B를 말한 적이 없다
        _, mine = self.send("그거 제자리로", session="s1")
        self.assertIn(mine["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        self.goals.confirm(mine["goal"]["goal_id"], "cancel")
        # 되묻기 대기도 세션별이다(빈 칸 2·3 → s1만 질문을 기다린다).
        _, asked = self.send("C자재를 빈 칸에 옮겨줘", session="s1")
        self.assertEqual(asked["decision"], "ASK")
        _, stray = self.send("3번", session="s2")
        self.assertNotIn(stray["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        _, answered = self.send("3번", session="s1")
        self.assertEqual([(s["material"], s["to"]) for s in answered["goal"]["plan"]],
                         [("material_c", "slot_3")])
        self.assertEqual(self.popen.calls, [])

    def test_expiry_asks_instead_of_guessing(self):
        self.hold("material_b", "slot_1")
        self.send("B자재를 컨베이어 1번에 놓아줘")
        self.now[0] += 601
        _, payload = self.send("그거 제자리로")
        self.assertEqual(payload["decision"], "ASK")
        self.assertIn("만료", payload["reason"])
        # 되묻기 대기(2분)도 만료되면 짧은 답이 옛 질문을 채우지 않는다.
        self.hold("material_a", "slot_2")
        self.send("C자재를 빈 칸에 옮겨줘")
        self.now[0] += 121
        _, late = self.send("3번")
        self.assertNotIn(late["decision"], ("CONFIRM", "CONFIRM_GOAL"))

    def test_reconnect_same_session_keeps_context_new_session_does_not(self):
        self.hold("material_b", "slot_1")
        self.send("B자재를 컨베이어 1번에 놓아줘", session="tab-1")
        # STT 스트림을 다시 열어도 브라우저 세션 id가 같으면 맥락이 이어진다.
        _, again = self.send("그거 제자리로", source="stt_final", confidence=0.8,
                             session="tab-1")
        self.assertIn(again["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        self.goals.confirm(again["goal"]["goal_id"], "cancel")
        _, fresh = self.send("그거 제자리로", session="tab-2")   # 새로고침 = 새 세션
        self.assertEqual(fresh["decision"], "ASK")

    def test_ended_or_missing_session_has_no_context(self):
        self.hold("material_b", "slot_1")
        self.send("B자재를 컨베이어 1번에 놓아줘", session="s9")
        self.ended.add("s9")
        _, payload = self.send("그거 제자리로", session="s9")
        self.assertEqual(payload["decision"], "ASK")
        self.assertFalse(payload["dialogue"]["session_context"])
        self.assertFalse(payload["dialogue"]["authentication"])
        _, none = self.send("그거 제자리로", session="")
        self.assertEqual(none["decision"], "ASK")

    def test_cancel_clears_only_this_session(self):
        self.hold("material_a", "slot_2")
        self.send("C자재를 빈 칸에 옮겨줘", session="s1")
        self.send("B자재를 빈 칸에 옮겨줘", session="s2")
        _, cancelled = self.send("취소", session="s1")
        self.assertIn("취소", cancelled["reason"])
        _, answered = self.send("3번", session="s2")
        self.assertIn(answered["decision"], ("CONFIRM", "CONFIRM_GOAL"))
        self.assertEqual([s["material"] for s in answered["goal"]["plan"]], ["material_b"])

    def test_voice_qwen_ask_shows_what_was_heard(self):
        self.runtime.sim_demo_intent = types.SimpleNamespace(
            client=None, min_confidence=0.7,
            classify=lambda *a, **k: types.SimpleNamespace(
                ok=False, intent=None, material_id=None, source_resource=None,
                destination_resource=None, confidence=0.3, failure="low_confidence",
                reason="확신이 낮습니다", model_id="fake", raw="", latency_sec=0.0))
        _, payload = self.send("실자재를 펀베이어 1번에 놓아줘", source="stt_final",
                               confidence=0.66)
        self.assertEqual(payload["decision"], "ASK")
        self.assertTrue(payload["reason"].startswith("제가 이렇게 들었습니다: 실자재를"))


for _name in [n for n in dir(ContextRouteTest) if n.startswith("test_")]:
    if _name not in SessionIsolationTest.__dict__:
        setattr(SessionIsolationTest, _name, None)


class RestoreAndPalletReturnTest(ContextRouteTest):
    """“초록자재다시팔레트로가져다놔” · “원상복귀” (2026-09-26)."""

    def on_pallet(self, model, pallet):
        origin = self.jobs._capability().origin_of(model)
        self.state.record_transfer(model, source=origin, destination=pallet,
                                   destination_kind="pallet",
                                   destination_center_m=(0.5, 0, 0.84), own_origin=origin,
                                   completed=True, stop_requested=False, attached=True,
                                   final_pose_m=(0.5, 0, 0.84), base={"target_id": pallet})

    def test_green_back_to_its_pallet_regardless_of_spacing(self):
        self.hold("material_c", "slot_2")
        for text in ("초록자재다시팔레트로가져다놔", "초록 자재 다시 팔레트로 가져다 놔"):
            with self.subTest(text=text):
                _, payload = self.send(text)
                self.assertEqual((payload["decision"], payload["intent"], payload["material"]),
                                 ("CONFIRM", "return", "material_c"))
        self.assertEqual(self.popen.calls, [])

    def test_green_on_other_pallet_and_already_home(self):
        self.on_pallet("material_c", "loc_pallet_1")
        _, payload = self.send("초록자재다시팔레트로가져다놔")
        self.assertEqual(payload["decision"], "CONFIRM_GOAL")
        self.assertEqual([(s["action"], s["material"], s["to"]) for s in payload["goal"]["plan"]],
                         [("return", "material_c", "origin")])
        self.goals.confirm(payload["goal"]["goal_id"], "cancel")

    def test_green_already_home_does_not_move(self):
        _, payload = self.send("초록자재다시팔레트로가져다놔")
        self.assertNotIn(payload["decision"], ("RUN", "CONFIRM", "CONFIRM_GOAL"))
        self.assertIn("이미 원래 자리(3번 팔레트)", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_restore_all_plans_every_away_material_and_waits_for_approval(self):
        self.hold("material_a", "slot_1")
        self.on_pallet("material_c", "loc_pallet_2")
        _, payload = self.send("원상복귀", session="fresh")
        self.assertEqual(payload["decision"], "CONFIRM_GOAL")
        plan = [(s["material"], s["from"], s["to"]) for s in payload["goal"]["plan"]]
        self.assertEqual(sorted(plan), [("material_a", "slot_1", "origin"),
                                        ("material_c", "loc_pallet_2", "origin")])
        self.assertTrue(all(s["reason"] for s in payload["goal"]["plan"]))
        self.assertIn("원상복귀", " ".join(payload["interpretation"]["evidence"]))
        self.assertEqual(self.popen.calls, [])              # 승인 전에는 움직이지 않는다

    def test_restore_all_when_home_is_noop(self):
        _, payload = self.send("원상복귀")
        self.assertEqual(payload["decision"], "NOOP")
        self.assertIsNone(payload.get("goal"))
        self.assertEqual(self.popen.calls, [])

    def test_recent_action_context_asks_undo_or_all(self):
        self.hold("material_a", "slot_1")
        self.hold("material_c", "slot_2")
        self.send("C자재를 컨베이어 2번에 놓아줘", session="s1")   # 직전에 C를 다뤘다
        _, asked = self.send("원상복귀", session="s1")
        self.assertEqual(asked["decision"], "ASK")
        self.assertIn("C자재만", asked["reason"])
        _, everything = self.send("전부", session="s1")
        self.assertEqual(everything["decision"], "CONFIRM_GOAL")
        self.assertEqual(sorted(s["material"] for s in everything["goal"]["plan"]),
                         ["material_a", "material_c"])
        self.goals.confirm(everything["goal"]["goal_id"], "cancel")
        # '전부'라고 답한 뒤에는 맥락이 모든 자재라 다시 묻지 않는다. 다른 세션에서 C만 고른다.
        self.send("C자재를 컨베이어 2번에 놓아줘", session="s2")
        self.send("원상복귀", session="s2")
        _, only_c = self.send("C자재만", session="s2")
        self.assertEqual(only_c["decision"], "CONFIRM_GOAL")
        self.assertEqual([(s["action"], s["material"]) for s in only_c["goal"]["plan"]],
                         [("return", "material_c")])
        self.assertEqual(self.popen.calls, [])

    def test_undo_marker_asks_even_without_focus(self):
        self.hold("material_a", "slot_1")
        _, asked = self.send("방금 거 원상복귀", session="new")
        self.assertEqual(asked["decision"], "ASK")
        self.assertEqual(self.popen.calls, [])


if __name__ == "__main__":
    unittest.main()
