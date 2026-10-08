"""명령 해석 개선(2026-10-08) — 표현별 정상 해석 · 되묻기 · 오해석 방지.

모델·Gazebo를 부르지 않는다. 해석 결과(`Interpretation`)는 직접 만들어 서버 판정(`decide_intent`)만 본다 —
모델이 무엇을 내든 서버가 원문·등록 정보·상태로 다시 보는지가 핵심이다.
실제 Qwen으로 본 결과는 `scripts/command_interp_eval.py`(fixtures/command_interp)에 있다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

from core.reason_codes import ReasonCode  # noqa: E402
from core.task_intent import CONTEXT, REGISTRY, STATE, UTTERANCE  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402
from server.plan_intent import decide_intent, material_positions, merge_pick_place  # noqa: E402
from test_plan_intent import CATALOG, decide, facts, interp, task  # noqa: E402

# 기본 상태(test_plan_intent.facts): A 1번 팔레트, B 2번 팔레트, C 컨베이어. 원래 자리: A 1번, B 2번, C 3번.
HOME = {"mat_c": "loc_pallet_3"}


def plan_of(d):
    return (d.intent.material.resource_id, d.intent.destination.resource_id) if d.intent else None


class GenericNounTest(unittest.TestCase):
    def test_generic_nouns_do_not_require_jajae(self):
        for utterance, mtext in [("주황색 물체 컨베이어로 옮겨", "주황색 물체"), ("주황 부품 컨베이어로", "주황 부품"),
                                 ("주황색 거 컨베이어에 놔", "주황색 거"), ("주황 물건 벨트로", "주황 물건")]:
            with self.subTest(utterance):
                d = decide(utterance, interp("mat_a", mtext, dest="loc_conveyor", dtext="컨베이어로"))
                self.assertEqual(plan_of(d), ("mat_a", "loc_conveyor"), d.detail)

    def test_generic_noun_alone_asks_instead_of_blocking(self):
        for mtext in ("물체 하나", "물체", "이거", "저거"):
            with self.subTest(mtext):
                d = decide(f"{mtext} 컨베이어로", interp(None, mtext, dest="loc_conveyor", dtext="컨베이어로"))
                self.assertEqual(d.kind, "ask", d.detail)
                self.assertEqual(d.draft.get("destination", {}).get("key"), "loc_conveyor")   # 목적지는 남긴다

    def test_shape_words_are_not_resolved_without_registered_shapes(self):
        # 이 셀 설정에는 자재 모양이 등록돼 있지 않다 — 모양 표현으로 자재를 고르지 않는다(계획하지 않는다).
        for mtext in ("네모난 거", "삼각형 물체", "동그란 부품"):
            with self.subTest(mtext):
                d = decide(f"{mtext} 컨베이어로", interp(None, mtext, dest="loc_conveyor", dtext="컨베이어로"))
                self.assertIsNone(d.intent, d.detail)

    def test_unregistered_material_is_still_blocked(self):
        d = decide("보라색 물체 컨베이어로", interp(None, "보라색 물체", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual((d.kind, d.reason_code), ("block", ReasonCode.PLAN_UNKNOWN_RESOURCE))


class KoreanCodeReadingTest(unittest.TestCase):
    def test_standalone_readings_name_the_registered_code(self):
        for utterance, mid in [("에이 컨베이어로", "mat_a"), ("비를 벨트에 올려줘", "mat_b"),
                               ("에이자재 컨베이어로", "mat_a"), ("비, 컨베이어로", "mat_b")]:
            with self.subTest(utterance):
                d = decide(utterance, interp(mid, utterance.split()[0], dest="loc_conveyor", dtext="컨베이어로"))
                self.assertEqual(plan_of(d), (mid, "loc_conveyor"), d.detail)
                self.assertEqual(d.intent.material.evidence.kind, UTTERANCE)
        d = decide("에이 컨베이어로", interp("mat_a", "에이", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertIn("한글 읽기", d.intent.material.evidence.text)          # 근거에 읽기임을 남긴다

    def test_readings_are_never_substrings_or_subject_marked(self):
        f = facts()
        for utterance in ("비가 오네 컨베이어 괜찮아?", "에이씨 이거 왜 안돼", "비닐 컨베이어로", "아저씨 컨베이어로",
                          "시비 걸지마", "비행기 컨베이어로"):
            with self.subTest(utterance):
                self.assertEqual(material_positions(utterance, f, readings=True), {})

    def test_reading_counts_only_for_a_transfer_request(self):
        f = facts()
        self.assertEqual(material_positions("에이 컨베이어로", f, readings=False), {})
        d = decide("에이 컨베이어로", interp("mat_a", "에이", dest="loc_conveyor", dtext="컨베이어로",
                                         action="unknown", confidence=0.4))
        self.assertIsNone(d.intent)

    def test_non_standalone_reading_asks_instead_of_blocking_or_planning(self):
        d = decide("비가 오네 컨베이어 괜찮아?", interp(None, "비", dest="loc_conveyor", dtext="컨베이어"))
        self.assertEqual(d.kind, "ask", d.detail)
        d = decide("에이씨 이거 왜 안돼", interp(None, "에이씨"))
        self.assertIsNone(d.intent)

    def test_code_alone_without_destination_asks_only_the_destination(self):
        d = decide("A 옮겨줘", interp("mat_a", "A", confidence=0.5))
        self.assertEqual(d.kind, "ask")
        self.assertEqual(d.draft["material"]["resource_id"], "mat_a")
        self.assertNotIn("destination", d.draft)
        self.assertIn("어디로", d.detail)


class OmittedNounAndRoleTest(unittest.TestCase):
    def test_bare_place_after_a_color_is_the_destination(self):
        d = decide("초록 컨베이어", interp("mat_c", "초록", source="loc_conveyor", stext="컨베이어"), locations=HOME)
        self.assertEqual(plan_of(d), ("mat_c", "loc_conveyor"), d.detail)
        self.assertEqual(d.intent.source.resource_id, "loc_pallet_3")
        self.assertEqual(d.intent.source.evidence.kind, STATE)          # 출발지는 기록에서

    def test_bare_place_where_the_material_already_is_asks(self):
        d = decide("초록 컨베이어", interp("mat_c", "초록", source="loc_conveyor", stext="컨베이어"))
        self.assertEqual(d.kind, "ask", d.detail)
        self.assertIn("어디로", d.detail)

    def test_marked_source_is_not_forced_to_destination(self):
        d = decide("초록 컨베이어에서 원래 자리로", interp("mat_c", "초록", source="loc_conveyor", stext="컨베이어에서",
                                                    dest="origin", dtext="원래 자리로"), locations=HOME)
        self.assertEqual(d.kind, "block", d.detail)                     # 말한 출발지(컨베이어) ≠ 기록(3번 팔레트)
        d = decide("초록 컨베이어에서 1번 팔레트로", interp("mat_c", "초록", source="loc_conveyor", stext="컨베이어에서",
                                                      dest="loc_pallet_1", dtext="1번 팔레트로"), locations=HOME)
        self.assertEqual(d.kind, "block", d.detail)

    def test_code_then_origin(self):
        d = decide("B 원래 자리로", interp("mat_b", "B", dest="origin", dtext="원래 자리로"),
                   locations={"mat_b": "loc_conveyor"})
        self.assertEqual(plan_of(d), ("mat_b", "loc_pallet_2"))
        self.assertEqual(d.intent.destination.evidence.kind, REGISTRY)


class ReturnTest(unittest.TestCase):
    def test_return_verb_without_destination_means_origin(self):
        for utterance in ("B 다시 돌려놔", "B 되돌려 놔", "B 돌려놓아 줘"):
            with self.subTest(utterance):
                d = decide(utterance, interp("mat_b", "B"), locations={"mat_b": "loc_conveyor"})
                self.assertEqual(plan_of(d), ("mat_b", "loc_pallet_2"), d.detail)
                self.assertEqual(d.intent.destination.evidence.kind, REGISTRY)

    def test_low_confidence_still_asks(self):
        d = decide("B 다시 돌려놔", interp("mat_b", "B", confidence=0.5), locations={"mat_b": "loc_conveyor"})
        self.assertIsNone(d.intent)                                     # 확신 기준은 그대로

    def test_return_verb_at_origin_is_noop(self):
        d = decide("B 다시 돌려놔", interp("mat_b", "B"))
        self.assertEqual(d.kind, "noop", d.detail)

    def test_stated_destination_wins_over_return_verb(self):
        d = decide("B 컨베이어로 돌려놔", interp("mat_b", "B", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(plan_of(d), ("mat_b", "loc_conveyor"))

    def test_bare_origin_word_at_origin_is_noop_not_a_question(self):
        d = decide("파란 자재 원위치", interp("mat_b", "파란 자재", dest="origin", dtext="원위치"))
        self.assertEqual(d.kind, "noop", d.detail)
        d = decide("A 원위치", interp("mat_a", "A", dest="origin", dtext="원위치"), locations={"mat_a": "loc_conveyor"})
        self.assertEqual(plan_of(d), ("mat_a", "loc_pallet_1"))


class LocationDescribedMaterialTest(unittest.TestCase):
    def test_material_named_only_by_where_it_sits(self):
        cases = [("3번 팔레트 위에 놓여 있는 물체 컨베이어로 옮겨", "3번 팔레트 위에 놓여 있는 물체", "mat_c", HOME),
                 ("1번 팔레트에 올려져 있는 거 벨트로", "1번 팔레트에 올려져 있는 거", "mat_a", None),
                 ("2번 팔레트 위에 놓여 있는 물체를 컨베이어로", "물체", "mat_b", None)]
        for utterance, mtext, mid, where in cases:
            with self.subTest(utterance):
                d = decide(utterance, interp(None, mtext, dest="loc_conveyor", dtext="컨베이어로"), locations=where)
                self.assertEqual(plan_of(d), (mid, "loc_conveyor"), d.detail)
                self.assertEqual(d.intent.material.evidence.kind, STATE)    # 그 위치에 기록된 자재
                self.assertEqual(d.intent.source.evidence.kind, UTTERANCE)

    def test_empty_described_location_asks(self):
        # 기본 상태에서 3번 팔레트는 비어 있다(C는 컨베이어).
        d = decide("3번 팔레트 위에 놓여 있는 물체 컨베이어로", interp(None, "물체", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "ask", d.detail)
        self.assertIn("기록된 자재가 없습니다", d.detail)

    def test_crowded_described_location_asks(self):
        d = decide("컨베이어에 있는 거 원래 자리로", interp(None, "거", dest="origin", dtext="원래 자리로"),
                   locations={"mat_a": "loc_conveyor"})
        self.assertEqual(d.kind, "ask", d.detail)
        self.assertIn("여럿", d.detail)

    def test_model_disagreeing_with_the_record_asks(self):
        d = decide("1번 팔레트에 있는 거 컨베이어로", interp("mat_b", "거", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "ask", d.detail)

    def test_unknown_state_does_not_guess(self):
        f = facts()
        f["state_error"] = "기록 없음"
        d = decide_intent(interp(None, "물체", dest="loc_conveyor", dtext="컨베이어로"),
                          "1번 팔레트에 있는 물체 컨베이어로", f, min_confidence=0.7)
        self.assertEqual(d.kind, "ask", d.detail)


class PickThenPlaceTest(unittest.TestCase):
    def test_pick_then_place_is_one_transfer(self):
        d = decide("A 집은 다음 컨베이어에 놔", interp(tasks=(
            task("mat_a", "A"), task(None, None, dest="loc_conveyor", dtext="컨베이어에"))))
        self.assertEqual(plan_of(d), ("mat_a", "loc_conveyor"), d.detail)
        d = decide("A 집은 다음 컨베이어에 놔", interp(tasks=(
            task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어에"),
            task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어에"))))
        self.assertEqual(plan_of(d), ("mat_a", "loc_conveyor"), d.detail)

    def test_source_description_misread_as_a_task_is_dropped(self):
        u = "1번 팔레트에 있는 주황색 자재를 조심해서 집은 다음에 컨베이어로 옮겨 주세요"
        d = decide(u, interp(tasks=(task("mat_a", "주황색 자재", dest="origin", dtext="1번 팔레트에 있는"),
                                    task("mat_a", "주황색 자재", dest="loc_conveyor", dtext="컨베이어로"))))
        self.assertEqual(plan_of(d), ("mat_a", "loc_conveyor"), d.detail)

    def test_real_multiple_tasks_are_never_partly_planned(self):
        cases = [("A는 컨베이어로 옮기고 B도 컨베이어로", (task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로"),
                                                      task("mat_b", "B", dest="loc_conveyor", dtext="컨베이어로"))),
                 ("A 컨베이어로 옮기고 다시 가져와", (task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로"),
                                               task("mat_a", "A", dest="origin", dtext="다시 가져와"))),
                 ("A 컨베이어로 옮겼다가 원래 자리로", (task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로"),
                                                 task("mat_a", "A", dest="origin", dtext="원래 자리로"))),
                 ("A 컨베이어로 옮기고 A 또 컨베이어로", (task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로"),
                                                   task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로")))]
        for utterance, tasks in cases:
            with self.subTest(utterance):
                d = decide(utterance, interp(tasks=tasks))
                self.assertEqual((d.kind, d.intent), ("ask", None), d.detail)

    def test_merge_helper_keeps_two_destinations_apart(self):
        two = (task("mat_a", "A", dest="loc_conveyor", dtext="컨베이어로"), task("mat_a", "A", dest="origin", dtext="원래"))
        self.assertEqual(merge_pick_place(two), two)
        later_pick = (task(None, None, dest="loc_conveyor", dtext="컨베이어로"), task("mat_a", "A"))
        self.assertEqual(merge_pick_place(later_pick), later_pick)    # 놓기 뒤의 집기는 합치지 않는다


class CorrectionAndAmbiguityTest(unittest.TestCase):
    def test_clear_material_correction_with_readings(self):
        d = decide("씨 말고 에이 벨트로", interp("mat_a", "에이", dest="loc_conveyor", dtext="벨트로"))
        self.assertEqual(plan_of(d), ("mat_a", "loc_conveyor"), d.detail)
        self.assertIn("자재 정정", d.intent.material.evidence.text)

    def test_hesitation_without_content_is_not_planned(self):
        d = decide("아니 아니 잠깐만", interp(action="unknown", confidence=0.2))
        self.assertIsNone(d.intent)

    def test_destination_correction_the_model_never_read_asks(self):
        d = decide("A자재를 1번 팔레트에서 컨베이어로, 아니 2번 팔레트로 옮겨줘",
                   interp("mat_a", "A자재", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "ask", d.detail)

    def test_destination_correction_the_model_read_is_used(self):
        d = decide("파란 자재를 컨베이어로, 아니 그냥 원래 자리에",
                   interp("mat_b", "파란 자재", dest="loc_conveyor", dtext="컨베이어로, 아니 그냥 원래 자리에"),
                   locations={"mat_b": "loc_conveyor"})
        self.assertEqual(plan_of(d), ("mat_b", "loc_pallet_2"), d.detail)

    def test_this_to_there_asks_only_what_is_missing(self):
        d = decide("이거 저기로", interp(None, "이거", dtext="저기로"))
        self.assertEqual(d.kind, "ask", d.detail)
        self.assertIsNone(d.intent)

    def test_that_one_needs_same_session_context(self):
        d = decide("그거 컨베이어로", interp(None, "그거", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "ask")
        ctx = {"moved": {"material": "mat_c", "source": "loc_pallet_3", "destination": "loc_conveyor", "at": 10.0}}
        d = decide("아까 옮긴 그거 원래 자리로", interp(None, "아까 옮긴 그거", dest="origin", dtext="원래 자리로"),
                   context=ctx)
        self.assertEqual(plan_of(d), ("mat_c", "loc_pallet_3"), d.detail)
        self.assertEqual(d.intent.material.evidence.kind, CONTEXT)

    def test_color_shared_by_two_materials_asks(self):
        f = facts()
        for m in f["materials"]:
            if m["id"] in ("mat_a", "mat_b"):
                m["tokens"] = sorted({*m["tokens"], "노랑"})
        d = decide_intent(interp("mat_a", "노랑", dest="loc_conveyor", dtext="컨베이어로"), "노랑 컨베이어로", f,
                          min_confidence=0.7)
        self.assertEqual((d.kind, d.intent), ("ask", None), d.detail)


class SafetyKeptTest(unittest.TestCase):
    def test_occupied_destination_and_source_mismatch_still_block(self):
        d = decide("A를 2번 팔레트로", interp("mat_a", "A", dest="loc_pallet_2", dtext="2번 팔레트로"))
        self.assertEqual(d.kind, "block")
        d = decide("2번 팔레트에 있는 A 컨베이어로", interp("mat_a", "A", source="loc_pallet_2", stext="2번 팔레트에 있는",
                                                       dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "block", d.detail)

    def test_no_destination_is_invented_from_the_current_location(self):
        d = decide("A 옮겨줘", interp("mat_a", "A"))
        self.assertIsNone(d.intent)


class StopContractTest(unittest.TestCase):
    """정지 표현 — 기존 계약 그대로: 정지 낱말이 있으면 계획 없이 전체 정지(부정문·인용문도 정지 쪽으로).

    해석 개선이 정지 판정을 바꾸지 않았는지 본다. 부정문('정지하지 마')·인용문도 정지로 잡는 것은 기존 계약(안전 쪽)이다.
    """

    KEYWORDS = ("정지", "멈춰", "스톱", "스탑")

    def hit(self, utterance):
        return extract_slots(utterance, CATALOG, stop_keywords=self.KEYWORDS).stop_keyword_hit

    def test_stop_expressions(self):
        for utterance in ("정지", "멈춰!", "로봇 멈춰", "스톱", "잠깐 스탑", "A 컨베이어로 옮기다가 멈춰"):
            with self.subTest(utterance):
                self.assertTrue(self.hit(utterance))

    def test_negated_and_quoted_stop_words_still_stop(self):
        for utterance in ("정지하지 마", "멈춰 말고 계속해", "'정지'라고 하면 멈춰?",
                          "정지 버튼 어디 있어"):
            with self.subTest(utterance):
                self.assertTrue(self.hit(utterance))

    def test_transfer_without_stop_words_does_not_stop(self):
        for utterance in ("에이 컨베이어로", "B 다시 돌려놔", "3번 팔레트 위에 놓여 있는 물체 컨베이어로", "멈추지 말고 옮겨"):
            with self.subTest(utterance):
                self.assertFalse(self.hit(utterance))


if __name__ == "__main__":
    unittest.main()
