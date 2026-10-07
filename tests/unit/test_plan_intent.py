"""일반 경로(`/v1/plan`) 이송: 해석 → 해소·검증 → 검증된 작업 의도 → 계획 → 요청–계획 검증 (2026-10-06).

지키는 것:
- 해석(Qwen)이 고른 id는 **원문 표현이 가리키는 것과 같을 때만** 쓴다. 근거마다 종류를 남긴다.
- "원래 자리"는 등록 정보(원래 팔레트), "빈자리"는 상태(컨베이어 빈 칸) — 둘 다 **등록 표현**이
  있을 때만. 등록되지 않은 장소를 상징 목적지로 바꿔 읽지 않는다.
- 근거 없는 출발지 주장은 버리고 상태 기록(현재 위치)을 쓴다. 근거 있는 출발지가 기록과 다르면 차단.
- 계획은 의도로 만들고, 요청–계획 검증도 **같은 의도**로 역할까지 대조한다. 계획을 근거로 요청
  리소스를 더하지 않는다 — 다른 자재·다른 목적지·뒤바뀐 역할은 불일치다.
- 의도는 요청마다 저장되고 실행 직전 재검증이 같은 의도를 읽는다.

모델·Gazebo를 부르지 않는다. 해석 결과는 `Interpretation`을 직접 만든다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from core.constants import TASK_PLAN_SCHEMA_VERSION  # noqa: E402
from core.reason_codes import ReasonCode  # noqa: E402
from core.resource_catalog import normalize  # noqa: E402
from core.task_intent import CONTEXT, REGISTRY, STATE, UTTERANCE, TaskIntent  # noqa: E402
from core.task_plan import TaskPlan, TaskStep  # noqa: E402
from planning.intent_interpreter import (  # noqa: E402
    FIELDS,
    TASK_FIELDS,
    IntentInterpreter,
    Interpretation,
    TaskMention,
    output_json_schema,
)
from server.plan_intent import (  # noqa: E402
    IntentPlanProvider,
    decide_intent,
    load_language_terms,
    load_place_terms,
)
from validation.request_plan_consistency import (  # noqa: E402
    ConsistencyStatus,
    check_request_plan_consistency,
)

CATALOG = load_resource_catalog(json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json").read_text(encoding="utf-8")))
SYMBOLS = load_place_terms(ROOT / "config/workcell/active.json")
TERMS = load_language_terms(ROOT / "config/workcell/active.json")


def facts(locations=None, free=("slot_2", "slot_3")):
    """실제 카탈로그·등록 색으로 만든 셀 사실(상태만 주입)."""
    workcell = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
    where = {"mat_a": "loc_pallet_1", "mat_b": "loc_pallet_2", "mat_c": "loc_conveyor"}
    where.update(locations or {})
    origin = {"mat_a": "loc_pallet_1", "mat_b": "loc_pallet_2", "mat_c": "loc_pallet_3"}
    materials = []
    for row in workcell["resource_map"]:
        rid = row.get("resource_id")
        if rid not in where:
            continue
        entry = CATALOG.get(rid)
        colors = list(row.get("korean_colors") or ())
        materials.append({"id": rid, "model": row["gazebo_model"], "name": entry.display_name,
                          "colors": colors[:4],
                          "tokens": sorted({normalize(t) for t in (entry.display_name, *entry.aliases, *colors)}),
                          "location": where[rid], "origin": origin[rid]})
    locs = []
    from core.resource_catalog import ResourceKind
    for rid in CATALOG.ids_of_kind(ResourceKind.LOCATION):
        entry = CATALOG.get(rid)
        locs.append({"id": rid, "name": entry.display_name, "aliases": [],
                     "tokens": sorted({normalize(t) for t in (entry.display_name, *entry.aliases)})})
    return {"materials": materials, "locations": locs, "free_conveyor_slots": list(free),
            "symbols": SYMBOLS, "terms": TERMS, "state_error": None}


def task(material=None, mtext=None, source=None, stext=None, dest=None, dtext=None):
    return TaskMention(material_id=material, material_text=mtext, source_id=source, source_text=stext,
                       destination=dest, destination_text=dtext)


def interp(material=None, mtext=None, source=None, stext=None, dest=None, dtext=None,
           action="transfer", confidence=0.9, ok=True, tasks=None):
    tasks = tasks if tasks is not None else (task(material, mtext, source, stext, dest, dtext),)
    return Interpretation(ok=ok, action=action, tasks=tuple(tasks), confidence=confidence,
                          model_reason="시험", model_id="fake-qwen",
                          failure=None if ok else "unavailable",
                          reason="" if ok else "해석기를 쓸 수 없습니다: timeout")


def decide(utterance, i, context=None, **kw):
    return decide_intent(i, utterance, facts(**kw), min_confidence=0.7, context=context)


class ResolveTest(unittest.TestCase):
    def test_abbreviated_and_glued_utterances_resolve_with_evidence(self):
        cases = [("b자재컨베이어로옮겨", interp("mat_b", "b자재", dest="loc_conveyor", dtext="컨베이어로")),
                 ("주황모형 벨트로", interp("mat_a", "주황", dest="loc_conveyor", dtext="벨트로")),
                 ("파란자재컨베이어로", interp("mat_b", "파란자재", dest="loc_conveyor", dtext="컨베이어로"))]
        for utterance, i in cases:
            with self.subTest(utterance=utterance):
                d = decide(utterance, i)
                self.assertEqual(d.kind, "intent", d.detail)
                self.assertEqual(d.intent.material.evidence.kind, UTTERANCE)
                self.assertEqual(d.intent.source.evidence.kind, STATE)      # 말하지 않은 출발지 = 기록
                self.assertEqual(d.intent.destination.resource_id, "loc_conveyor")

    def test_origin_resolves_from_the_registry(self):
        d = decide("초록자재 원래자리로", interp("mat_c", "초록자재", dest="origin", dtext="원래자리로"))
        self.assertEqual(d.kind, "intent", d.detail)
        self.assertEqual(d.intent.destination.resource_id, "loc_pallet_3")
        self.assertEqual(d.intent.destination.evidence.kind, REGISTRY)
        self.assertEqual(d.intent.source.resource_id, "loc_conveyor")

    def test_free_slot_needs_the_conveyor_and_a_free_slot(self):
        ok = decide("A자재 컨베이어 빈자리로", interp("mat_a", "A자재", dest="free_slot", dtext="빈자리로"))
        self.assertEqual((ok.kind, ok.intent.destination.resource_id), ("intent", "loc_conveyor"))
        self.assertEqual(ok.intent.destination.evidence.kind, STATE)
        other = decide("작업대 빈자리에 A자재 놔", interp("mat_a", "A자재", dest="free_slot", dtext="작업대 빈자리에"))
        self.assertEqual(other.kind, "ask")
        full = decide("A자재 컨베이어 빈자리로", interp("mat_a", "A자재", dest="free_slot", dtext="빈자리로"),
                      free=())
        self.assertEqual(full.kind, "block")

    def test_unregistered_place_is_not_read_as_a_symbol(self):
        """실측: '창고로'를 free_slot으로 읽어 컨베이어로 해소했다 — 등록 표현이 없으면 받지 않는다."""
        for dest in ("free_slot", "origin", "loc_conveyor"):
            with self.subTest(dest=dest):
                d = decide("A자재를 창고로 옮겨줘", interp("mat_a", "A자재", dest=dest, dtext="창고로"))
                self.assertEqual(d.kind, "ask")

    def test_stated_source_must_match_the_record(self):
        d = decide("A자재를 3번 팔레트에서 컨베이어로 옮겨줘",
                   interp("mat_a", "A자재", "loc_pallet_3", "3번 팔레트", "loc_conveyor", "컨베이어로"))
        self.assertEqual(d.kind, "block")
        self.assertEqual(d.reason_code, ReasonCode.PLAN_RESOURCE_MISMATCH)
        ok = decide("A자재를 1번 팔레트에서 컨베이어로 옮겨줘",
                    interp("mat_a", "A자재", "loc_pallet_1", "1번 팔레트", "loc_conveyor", "컨베이어로"))
        self.assertEqual((ok.kind, ok.intent.source.evidence.kind), ("intent", UTTERANCE))

    def test_source_evidence_comes_from_the_text_not_the_model_id(self):
        """모델 id가 틀려도 원문이 3번 팔레트를 말했으면 그것이 말한 출발지다 → 기록과 달라 차단."""
        d = decide("A자재를 3번 팔레트에서 컨베이어로 옮겨줘",
                   interp("mat_a", "A자재", "loc_pallet_1", "3번 팔레트", "loc_conveyor", "컨베이어로"))
        self.assertEqual(d.kind, "block")

    def test_ungrounded_or_overlapping_source_claims_are_ignored(self):
        """실측: 모델이 목적지 표현('원래자리')을 출발지에도 적었다 — 근거 없는 주장은 버린다."""
        cases = [("초록자재 원래자리로", interp("mat_c", "초록자재", "loc_pallet_1", "원래자리", "origin", "원래자리로")),
                 ("a자재 컨베이어로", interp("mat_a", "a자재", "loc_pallet_2", "2번 팔레트", "loc_conveyor", "컨베이어로"))]
        for utterance, i in cases:
            with self.subTest(utterance=utterance):
                d = decide(utterance, i)
                self.assertEqual(d.kind, "intent", d.detail)
                self.assertEqual(d.intent.source.evidence.kind, STATE)

    def test_destination_already_reached_is_a_noop_not_a_plan(self):
        d = decide("초록자재 컨베이어로", interp("mat_c", "초록자재", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "noop")
        self.assertIn("이미", d.clarification)

    def test_unregistered_material_is_blocked_and_mismatches_ask(self):
        for utterance, i in (("D자재를 컨베이어로 옮겨줘", interp(None, "D자재", dest="loc_conveyor", dtext="컨베이어로")),
                             ("보라색 모형을 컨베이어로", interp("mat_a", "보라색 모형", dest="loc_conveyor", dtext="컨베이어로"))):
            with self.subTest(utterance=utterance):
                d = decide(utterance, i)
                self.assertEqual((d.kind, d.reason_code), ("block", ReasonCode.PLAN_UNKNOWN_RESOURCE))
        # 원문이 가리킨 자재와 모델 id가 다르면 되묻는다.
        self.assertEqual(decide("초록자재를 컨베이어로", interp("mat_a", "초록자재", dest="loc_conveyor",
                                                          dtext="컨베이어로")).kind, "ask")
        # 일반 명사뿐이면 '없는 자재'가 아니라 '어느 자재인지'를 묻는다.
        self.assertEqual(decide("물건 컨베이어로", interp(None, "물건", dest="loc_conveyor",
                                                    dtext="컨베이어로")).kind, "ask")

    def test_material_evidence_is_the_text_even_if_the_model_text_is_ungrounded(self):
        """모델이 원문에 없는 표현을 적어도, 원문이 그 자재를 가리키고 id가 같으면 원문을 근거로 쓴다."""
        d = decide("A자재를 컨베이어로", interp("mat_a", "C자재", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual((d.kind, d.intent.material.evidence.text), ("intent", "a자재"))

    def test_unknown_location_asks(self):
        d = decide("a자재 컨베이어로", interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로"),
                   locations={"mat_a": None})
        self.assertEqual(d.kind, "ask")
        self.assertIn("현재 위치", d.clarification)

    def test_model_failure_low_confidence_and_unknown_never_make_an_intent(self):
        for i in (interp(ok=False), interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로",
                                            confidence=0.3),
                  interp(action="unknown"), interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로",
                                                   action="unknown")):
            with self.subTest(i=i):
                self.assertEqual(decide("a자재 컨베이어로", i).kind, "ask")

    def test_no_material_expression_goes_to_the_existing_planner(self):
        # 실측 Qwen 출력: action=other (2026-10-06).
        d = decide("1번 팔레트로 이동해줘", interp(dest="loc_pallet_1", dtext="1번 팔레트로", action="other"))
        self.assertEqual(d.kind, "other")
        # 모델이 이송이라 읽으면 실행 경로로 가지 않고 무엇을 옮길지 묻는다.
        self.assertEqual(decide("1번 팔레트로 이동해줘", interp(dest="loc_pallet_1", dtext="1번 팔레트로")).kind, "ask")


class SecondPassTest(unittest.TestCase):
    """2차(2026-10-06): 복귀 버튼·긴 문장·맥락·여러 작업. 모델 출력은 실측(Qwen3-8B) 그대로다."""

    ON_CONVEYOR = {"mat_a": "loc_conveyor", "mat_b": "loc_conveyor", "mat_c": "loc_conveyor"}
    AT_ORIGIN = {"mat_a": "loc_pallet_1", "mat_b": "loc_pallet_2", "mat_c": "loc_pallet_3"}
    MOVED_A = {"moved": {"material": "mat_a", "source": "loc_pallet_1", "destination": "loc_conveyor",
                         "at": 10.0}}

    def test_return_button_sentence_resolves_even_when_the_model_puts_origin_in_the_source(self):
        """복귀 버튼의 실제 원인: 모델이 '원래 자리'를 출발지 칸에 적고 목적지를 비웠다(실측)."""
        for mid, name, origin in (("mat_a", "A자재", "loc_pallet_1"), ("mat_b", "B자재", "loc_pallet_2"),
                                  ("mat_c", "C자재", "loc_pallet_3")):
            with self.subTest(material=name):
                d = decide(f"{name}를 원래 자리로 돌려놔",
                           interp(mid, name, origin, "원래 자리", "origin", None),
                           locations=self.ON_CONVEYOR)
                self.assertEqual(d.kind, "intent", d.detail)
                self.assertEqual(d.intent.destination.resource_id, origin)
                self.assertEqual(d.intent.destination.evidence.kind, REGISTRY)
                self.assertEqual((d.intent.source.resource_id, d.intent.source.evidence.kind),
                                 ("loc_conveyor", STATE))

    def test_return_when_already_home_occupied_or_unknown(self):
        noop = decide("A자재를 원래 자리로 돌려놔", interp("mat_a", "A자재", dest="origin", dtext="원래 자리로"),
                      locations=self.AT_ORIGIN)
        self.assertEqual(noop.kind, "noop")
        self.assertIn("이미 원래 자리(1번 팔레트)", noop.detail)
        taken = decide("A자재를 원래 자리로 돌려놔", interp("mat_a", "A자재", dest="origin", dtext="원래 자리로"),
                       locations={"mat_a": "loc_conveyor", "mat_b": "loc_pallet_1"})
        self.assertEqual(taken.kind, "block")
        self.assertIn("삼각형 자재", taken.detail)
        lost = decide("A자재를 원래 자리로 돌려놔", interp("mat_a", "A자재", dest="origin", dtext="원래 자리로"),
                      locations={"mat_a": None})
        self.assertEqual(lost.kind, "ask")
        self.assertIn("현재 위치를 확인할 수 없습니다", lost.detail)
        f = facts(locations={"mat_a": None})
        f["state_error"] = "자재 상태 기록을 읽지 못했습니다(시험)"
        failed = decide_intent(interp("mat_a", "A자재", dest="origin", dtext="원래 자리로"),
                               "A자재를 원래 자리로 돌려놔", f, min_confidence=0.7)
        self.assertEqual(failed.kind, "ask")
        self.assertIn("읽지 못했습니다", failed.detail)

    def test_long_sentence_with_a_category_source_and_free_spot(self):
        utterance = "지금 팔레트에 있는 초록색 물건 있잖아, 그거 컨베이어 빈 곳으로 좀 옮겨줘"
        i = interp("mat_c", "초록색 물건", "loc_pallet_1", "지금 팔레트에 있는", "free_slot", "컨베이어 빈 곳으로")
        d = decide(utterance, i, locations=self.AT_ORIGIN)
        self.assertEqual(d.kind, "intent", d.detail)
        self.assertEqual(d.intent.material.resource_id, "mat_c")
        self.assertEqual(d.intent.source.resource_id, "loc_pallet_3")    # '팔레트' 중 기록된 곳
        self.assertEqual((d.intent.destination.resource_id, d.intent.destination.evidence.kind),
                         ("loc_conveyor", STATE))
        # 같은 말인데 자재가 팔레트에 없으면(기록: 컨베이어) 말한 출발지 불일치로 차단한다.
        moved = decide(utterance, i, locations={"mat_c": "loc_conveyor"})
        self.assertEqual(moved.kind, "block")

    def test_filler_is_not_a_stated_source_but_an_unknown_place_with_a_source_marker_asks(self):
        d = decide("음 그러니까 저기 있는 그 A자재 말이야 그거를 혹시 가능하면 컨베이어 쪽으로 좀 옮겨 줄래요",
                   interp("mat_a", "그 A자재", None, "저기 있는", "free_slot", "컨베이어 쪽으로"))
        self.assertEqual(d.kind, "intent", d.detail)
        self.assertEqual(d.intent.source.evidence.kind, STATE)
        self.assertEqual(d.intent.destination.resource_id, "loc_conveyor")
        unknown = decide("A자재를 창고에서 컨베이어로", interp("mat_a", "A자재", None, "창고에서", "loc_conveyor", "컨베이어로"))
        self.assertEqual(unknown.kind, "ask")

    def test_pointing_word_uses_only_same_session_context(self):
        i = interp("mat_a", "그거", "loc_conveyor", "컨베이어에서", "origin", "원래 자리로")
        d = decide("아까 옮긴 그거 원래 자리로 돌려놔", i, context=self.MOVED_A, locations={"mat_a": "loc_conveyor"})
        self.assertEqual(d.kind, "intent", d.detail)
        self.assertEqual((d.intent.material.resource_id, d.intent.material.evidence.kind), ("mat_a", CONTEXT))
        self.assertEqual(d.intent.destination.resource_id, "loc_pallet_1")
        # 맥락이 없으면 모델이 id를 골라도 쓰지 않고 구체적으로 묻는다.
        none = decide("아까 옮긴 그거 원래 자리로 돌려놔", i, locations={"mat_a": "loc_conveyor"})
        self.assertEqual(none.kind, "ask")
        self.assertIn("어느 자재인지 알 수 없습니다", none.detail)
        bare = decide("그거 옮겨줘", interp(None, "그거", action="unknown", confidence=0.5))
        self.assertEqual(bare.kind, "ask")
        self.assertIn("'그거'", bare.detail)
        # 맥락과 모델이 가리킨 자재가 다르면 되묻는다.
        clash = decide("그거 원래 자리로", interp("mat_b", "그거", dest="origin", dtext="원래 자리로"),
                       context=self.MOVED_A, locations={"mat_a": "loc_conveyor"})
        self.assertEqual(clash.kind, "ask")

    def test_missing_part_is_asked_and_the_confirmed_part_is_kept(self):
        first = decide("A자재 옮겨줘", interp("mat_a", "A자재", action="unknown", confidence=0.5))
        self.assertEqual(first.kind, "ask")
        self.assertIn("어디로", first.detail)
        self.assertEqual(first.draft["material"]["resource_id"], "mat_a")
        context = {"draft": {**first.draft, "at": 20.0}}
        # 답 "컨베이어로" — 모델이 other라 해도 앞 질문의 답으로 잇는다.
        answer = decide("컨베이어로", interp(dest="loc_conveyor", dtext="컨베이어로", action="other"),
                        context=context)
        self.assertEqual(answer.kind, "intent", answer.detail)
        self.assertEqual((answer.intent.material.resource_id, answer.intent.material.evidence.kind),
                         ("mat_a", CONTEXT))
        self.assertEqual(answer.draft, {})                             # 확정되면 지운다
        # 맥락 없는 같은 답은 기존 경로(위치로 이동)다.
        self.assertEqual(decide("컨베이어로", interp(dest="loc_conveyor", dtext="컨베이어로",
                                                  action="other")).kind, "other")
        # 목적지만 말했으면 목적지를 남기고 자재를 묻는다.
        where = decide("컨베이어로 옮겨줘", interp(dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(where.kind, "ask")
        self.assertEqual(where.draft["destination"]["key"], "loc_conveyor")
        then = decide("B자재", interp("mat_b", "B자재"), context={"draft": {**where.draft, "at": 30.0}})
        self.assertEqual(then.kind, "intent", then.detail)
        self.assertEqual((then.intent.destination.resource_id, then.intent.destination.evidence.kind),
                         ("loc_conveyor", CONTEXT))

    def test_several_tasks_materials_or_corrections_are_never_merged(self):
        two = decide("A자재 컨베이어로 옮기고 B자재도 컨베이어로 옮겨줘", interp(tasks=(
            task("mat_a", "A자재", "loc_pallet_1", "1번 팔레트에서", "loc_conveyor", "컨베이어로"),
            task("mat_b", "B자재", "loc_pallet_2", "2번 팔레트에서", "loc_conveyor", "컨베이어로"))))
        self.assertEqual((two.kind, two.reason_code), ("ask", ReasonCode.PLAN_AMBIGUOUS))
        self.assertEqual(two.tasks, ("사각형 자재 → 컨베이어", "삼각형 자재 → 컨베이어"))
        seq = decide("A자재를 컨베이어로 옮긴 다음에 원래 자리로 돌려놔", interp(tasks=(
            task("mat_a", "A자재", None, None, "loc_conveyor", "컨베이어로"),
            task("mat_a", "A자재", None, None, "origin", "원래 자리로"))))
        self.assertEqual(seq.tasks, ("사각형 자재 → 컨베이어", "사각형 자재 → 원래 자리"))
        self.assertIsNone(seq.intent)
        # 모델이 하나로 뭉개도 서버가 원문에서 자재 둘을 찾으면 실행 의도를 만들지 않는다.
        merged = decide("초록색이랑 주황색 컨베이어로", interp("mat_c", "초록색", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual((merged.kind, merged.intent), ("ask", None))
        # 정정 + 목적지 둘 — 모델 출력(작업 둘, 둘 다 컨베이어)과 상관없이 어느 쪽인지 묻는다.
        fix = decide("A자재를 1번 팔레트에서 컨베이어로, 아니 2번 팔레트로 옮겨줘", interp(tasks=(
            task("mat_a", "A자재", "loc_pallet_1", "1번 팔레트에서", "loc_conveyor", "컨베이어로"),
            task("mat_a", "A자재", "loc_pallet_2", "2번 팔레트에서", "loc_conveyor", "컨베이어로"))))
        self.assertEqual(fix.kind, "ask")
        self.assertIn("컨베이어, 2번 팔레트", fix.detail)
        # 정정 없이 목적지 표현이 둘이면(한 작업) 역시 되묻는다.
        both = decide("A자재 컨베이어로 2번 팔레트로", interp("mat_a", "A자재", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(both.kind, "ask")

    def test_role_markers_override_model_roles(self):
        """'X에서'는 출발지, 'X로'는 목적지 — 모델이 바꿔 적어도 원문 조사를 따른다."""
        d = decide("컨베이어에서 A자재 1번 팔레트로", interp("mat_a", "A자재", "loc_pallet_1", "1번 팔레트로",
                                                      "loc_conveyor", "컨베이어에서"),
                   locations={"mat_a": "loc_conveyor"})
        self.assertEqual(d.kind, "ask")    # 모델 목적지(컨베이어)가 원문 목적지(1번 팔레트)와 다르다 — 추측하지 않는다
        ok = decide("컨베이어에서 A자재 1번 팔레트로", interp("mat_a", "A자재", None, None, "loc_pallet_1", "1번 팔레트로"),
                    locations={"mat_a": "loc_conveyor"})
        self.assertEqual(ok.kind, "intent", ok.detail)
        self.assertEqual((ok.intent.source.resource_id, ok.intent.source.evidence.kind),
                         ("loc_conveyor", UTTERANCE))

    def test_registered_terms_file_declares_markers_and_references(self):
        self.assertIn("에서", TERMS["source"])
        self.assertLess(TERMS["source"].index("에서부터"), TERMS["source"].index("에서"))
        self.assertIn("그거", TERMS["context"])
        self.assertIn("아니", TERMS["correction"])
        self.assertIn(normalize("빈 곳"), SYMBOLS["free_slot"]["terms"])


class IntentPlanAndConsistencyTest(unittest.TestCase):
    def intent(self, utterance="a자재 컨베이어로"):
        return decide(utterance, interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로")).intent

    def plan(self, steps):
        return TaskPlan(plan_id="plan_t", robot_id="r", profile_id="p", profile_version="1",
                        steps=tuple(TaskStep(s, a) for s, a in steps), created_at=0.0, ttl_sec=60.0,
                        schema_version=TASK_PLAN_SCHEMA_VERSION)

    def test_composed_plan_follows_the_intent_and_is_consistent(self):
        draft = IntentPlanProvider(self.intent(), final_skill="home", latency_sec=0.5).generate(None)
        steps = [(s.skill, dict(s.args)) for s in draft.steps]
        self.assertEqual(steps, [("move", {"to": "loc_pallet_1"}),
                                 ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                                 ("move", {"to": "loc_conveyor"}),
                                 ("place", {"object": "mat_a", "to": "loc_conveyor"}),
                                 ("home", {})])
        report = check_request_plan_consistency(plan=self.plan(steps), slots=None, catalog=CATALOG,
                                                intent=self.intent())
        self.assertEqual(report.status, ConsistencyStatus.CONSISTENT, report.detail)
        # 요청 리소스는 의도의 근거에서 온다.
        self.assertTrue(any("[state]" in r.surface for r in report.request_resources))

    def test_wrong_material_destination_or_swapped_roles_are_mismatches(self):
        wrong = {
            "다른 자재": [("move", {"to": "loc_pallet_2"}), ("pick", {"object": "mat_b", "from": "loc_pallet_2"}),
                      ("move", {"to": "loc_conveyor"}), ("place", {"object": "mat_b", "to": "loc_conveyor"}),
                      ("home", {})],
            "다른 목적지": [("move", {"to": "loc_pallet_1"}), ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                       ("move", {"to": "loc_pallet_2"}), ("place", {"object": "mat_a", "to": "loc_pallet_2"}),
                       ("home", {})],
            "뒤바뀐 역할": [("move", {"to": "loc_conveyor"}), ("pick", {"object": "mat_a", "from": "loc_conveyor"}),
                       ("move", {"to": "loc_pallet_1"}), ("place", {"object": "mat_a", "to": "loc_pallet_1"}),
                       ("home", {})],
            "놓기 없음": [("move", {"to": "loc_pallet_1"}), ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                      ("home", {})],
        }
        for name, steps in wrong.items():
            with self.subTest(name=name):
                report = check_request_plan_consistency(plan=self.plan(steps), slots=None, catalog=CATALOG,
                                                        intent=self.intent())
                self.assertEqual(report.status, ConsistencyStatus.MISMATCH, name)

    def test_intent_roundtrips_through_a_dict(self):
        intent = self.intent()
        self.assertEqual(TaskIntent.from_dict(json.loads(json.dumps(intent.to_dict()))), intent)


class InterpreterContractTest(unittest.TestCase):
    def test_schema_limits_values_and_extracts_text_first(self):
        schema = output_json_schema(["mat_a"], ["loc_conveyor"])
        item = schema["properties"]["tasks"]["items"]
        self.assertEqual(list(schema["properties"]), list(FIELDS))
        self.assertEqual(list(item["properties"])[:3], ["material_text", "source_text", "destination_text"])
        self.assertEqual(item["properties"]["destination"]["enum"],
                         ["loc_conveyor", "origin", "free_slot", None])
        self.assertIs(schema["additionalProperties"], False)
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(sorted(item["required"]), sorted(TASK_FIELDS))

    def test_prompt_does_not_leak_the_current_location(self):
        from planning.intent_interpreter import user_prompt

        text = user_prompt("a자재 컨베이어로", [{"id": "mat_a", "name": "A자재", "colors": ["주황"],
                                               "location": "loc_pallet_1"}],
                           [{"id": "loc_conveyor", "name": "컨베이어", "aliases": ["벨트"]}])
        self.assertNotIn("loc_pallet_1", text)

    def test_server_failure_and_bad_output_are_failures(self):
        class Client:
            config = None

            def __init__(self, content=None, error=None):
                self.content, self.error = content, error

            def chat(self, **kw):
                if self.error:
                    raise self.error
                import types
                return types.SimpleNamespace(content=self.content, latency_sec=0.1)

        mats = [{"id": "mat_a", "name": "A자재", "colors": []}]
        locs = [{"id": "loc_conveyor", "name": "컨베이어", "aliases": []}]
        for client in (Client(error=TimeoutError("timed out")), Client(content="A자재를 옮깁니다"),
                       Client(content=json.dumps({"action": "transfer"}))):
            with self.subTest(client=client):
                self.assertFalse(IntentInterpreter(client=client).interpret("x", mats, locs).ok)


class IntentStorageTest(unittest.TestCase):
    def test_intent_is_stored_once_per_request_and_cannot_change(self):
        from storage.records import RequestRecord
        from storage.repository import IntegrityViolation
        from storage.sqlite.repository import SqliteRepository

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = SqliteRepository(str(Path(tmp.name) / "t.sqlite3"), now=time.time())
        self.addCleanup(repo.close)
        repo.save_request(RequestRecord(request_id="req_1", utterance="a자재 컨베이어로",
                                        schema_version=TASK_PLAN_SCHEMA_VERSION, created_at=1.0))
        intent = decide("a자재 컨베이어로", interp("mat_a", "a자재", dest="loc_conveyor",
                                                dtext="컨베이어로")).intent.to_dict()
        repo.save_request_intent("req_1", intent, interpreter="fake", created_at=2.0)
        repo.save_request_intent("req_1", intent, interpreter="fake", created_at=2.0)   # 멱등
        self.assertEqual(repo.get_request_intent("req_1"), json.loads(json.dumps(intent)))
        changed = {**intent, "destination": {**intent["destination"], "resource_id": "loc_pallet_2"}}
        with self.assertRaises(IntegrityViolation):
            repo.save_request_intent("req_1", changed, interpreter="fake", created_at=3.0)
        self.assertIsNone(repo.get_request_intent("req_x"))


class ApiIntentTest(unittest.IsolatedAsyncioTestCase):
    """`POST /v1/plan`이 의도로 계획을 만들고, 같은 의도를 저장·재검증에 쓰는지."""

    async def asyncSetUp(self):
        from tests.integration.test_web_api import WebCase

        self.case = WebCase()
        await WebCase.asyncSetUp(self.case)
        self.addAsyncCleanup(self._cleanup)
        self.api = self.case.app.api

    async def _cleanup(self):
        self.case.doCleanups()

    def use(self, decision):
        self.api._interpret_intent = lambda text, session_id=None: decision

    async def test_intent_plan_is_stored_and_rechecked_with_the_same_intent(self):
        from server.plan_intent import IntentDecision
        from core.task_intent import Evidence, ResolvedRef

        intent = TaskIntent(action="transfer",
                            material=ResolvedRef("obj_a", Evidence(UTTERANCE, "A자재")),
                            source=ResolvedRef("loc_pallet_1", Evidence(STATE, "A자재의 현재 위치(기록)")),
                            destination=ResolvedRef("loc_conveyor", Evidence(UTTERANCE, "컨베이어로")),
                            interpreter="fake")
        self.use(IntentDecision("intent", intent=intent, interpretation={"latency_sec": 0.2}))
        body = (await self.case.plan("에이자재 컨베이어로")).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.case.provider.calls, 0, "의도가 있으면 계획 모델을 부르지 않는다")
        self.assertEqual(body["consistency"]["status"], "consistent")
        stored = self.case.app.runtime.repository.get_request_intent(body["request_id"])
        self.assertEqual(TaskIntent.from_dict(stored), intent)
        # 실행 직전과 같은 경로(bundle_for)도 저장된 의도로 대조한다.
        bundle = self.api.bundle_for(session_id=self.case.session, request_id=body["request_id"],
                                     plan_id=body["plan"]["plan_id"])
        self.assertEqual(bundle.consistency["status"], "consistent")

    async def test_ask_and_block_end_without_a_plan(self):
        from server.plan_intent import IntentDecision

        for kind, code in (("ask", ReasonCode.PLAN_CLARIFICATION_REQUIRED),
                           ("block", ReasonCode.PLAN_RESOURCE_MISMATCH)):
            with self.subTest(kind=kind):
                self.use(IntentDecision(kind, reason_code=code, detail="시험", clarification="시험",
                                        interpretation={}))
                response = await self.case.plan("A자재를 3번 팔레트에서 컨베이어로")
                body = response.json()
                self.assertEqual(response.status, 422)
                self.assertEqual(body["decision"], kind.upper())
                self.assertNotIn("plan", body)
        self.assertEqual(self.case.provider.calls, 0)

    async def test_place_word_with_a_verified_intent_is_not_asked_again(self):
        """'원래 자리로 돌려놔'(놓기 낱말 '놔' + 상징 목적지): 검증된 의도가 물체·목적지를 확인했으면
        놓기 되묻기를 하지 않는다. 같은 계획을 의도 없이 판정하면 예전처럼 되묻는다(2026-10-07 복귀 버튼)."""
        from server.plan_intent import IntentDecision
        from core.task_intent import Evidence, ResolvedRef

        intent = TaskIntent(action="transfer",
                            material=ResolvedRef("obj_a", Evidence(UTTERANCE, "에이자재")),
                            source=ResolvedRef("loc_conveyor", Evidence(STATE, "A자재의 현재 위치(기록)")),
                            destination=ResolvedRef("loc_pallet_1", Evidence(REGISTRY, "A자재의 원래 자리(등록: 1번 팔레트)")),
                            interpreter="fake")
        self.use(IntentDecision("intent", intent=intent, interpretation={}))
        body = (await self.case.plan("에이자재 원래 자리로 돌려놔")).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["consistency"]["status"], "consistent")
        from core.task_plan import TaskPlan
        bundle = self.api.bundle_for(session_id=self.case.session, request_id=body["request_id"],
                                     plan_id=body["plan"]["plan_id"])
        from planning.slot_extractor import extract_slots
        slots = extract_slots("에이자재 원래 자리로 돌려놔", self.case.app.runtime.resource_catalog)
        # 이 시험 셀의 안전 규칙은 다른 이유로 먼저 차단한다 — 놓기 판정 갈래만 보려고 앞 단계 합산을 ALLOW로 둔다.
        from unittest import mock
        from server.api import ValidationDecision
        with mock.patch("server.api._aggregate_gate", return_value=(ValidationDecision.ALLOW, None, "")), \
                mock.patch("server.api.gate_transfer", return_value=None):
            with_intent = self.api.gate_for(bundle.plan, slots, intent)
            without = self.api.gate_for(bundle.plan, slots, None)
        self.assertIn("놓기 요청인데", without.detail or "")          # 의도 없으면 예전 규칙 그대로
        self.assertEqual(without.decision, ValidationDecision.ASK)
        self.assertNotIn("놓기 요청인데", with_intent.detail or "")
        self.assertEqual(with_intent.decision, ValidationDecision.ALLOW)
        self.assertIsInstance(bundle.plan, TaskPlan)

    async def test_noop_and_tasks_are_reported_without_a_plan(self):
        from server.plan_intent import IntentDecision

        self.use(IntentDecision("noop", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                detail="A자재은(는) 이미 원래 자리(1번 팔레트)에 있습니다", interpretation={}))
        body = (await self.case.plan("A자재를 원래 자리로 돌려놔")).json()
        self.assertEqual((body["decision"], body["ok"]), ("NOOP", False))
        self.assertIn("이미 원래 자리", body["detail"])
        self.assertNotIn("plan", body)
        self.use(IntentDecision("ask", reason_code=ReasonCode.PLAN_AMBIGUOUS, detail="작업 2개",
                                tasks=("A자재 → 컨베이어", "B자재 → 컨베이어"), interpretation={}))
        body = (await self.case.plan("A자재 컨베이어로 옮기고 B자재도")).json()
        self.assertEqual(body["intent_tasks"], ["A자재 → 컨베이어", "B자재 → 컨베이어"])
        self.assertEqual(self.case.provider.calls, 0)

    async def test_follow_up_draft_is_per_session_expires_and_stop_clears_it(self):
        from server.plan_intent import DRAFT_TTL_SEC, IntentDecision

        now = [1000.0]
        self.api.now = lambda: now[0]
        draft = {"material": {"resource_id": "obj_a", "evidence": {"kind": "utterance", "text": "A자재"}}}
        self.use(IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                detail="A자재을(를) 어디로 옮길까요?", draft=draft, interpretation={}))
        await self.case.plan("A자재 옮겨줘")
        self.assertEqual(self.api._intent_context(self.case.session)["draft"]["material"]["resource_id"], "obj_a")
        other = (await self.case.client.post("/v1/sessions", {"origin": "test"})).json()["session_id"]
        self.assertNotIn("draft", self.api._intent_context(other))          # 다른 세션에는 없다
        # 해석 실패(draft=None)는 앞 질문을 지우지 않는다.
        self.use(IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED, detail="?",
                                interpretation={}))
        await self.case.plan("음")
        self.assertIn("draft", self.api._intent_context(self.case.session))
        now[0] += DRAFT_TTL_SEC + 1
        self.assertNotIn("draft", self.api._intent_context(self.case.session))
        self.use(IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                detail="?", draft=draft, interpretation={}))
        await self.case.plan("A자재 옮겨줘")
        body = (await self.case.plan("멈춰")).json()
        self.assertTrue(body.get("stopped"), body)
        self.assertNotIn("draft", self.api._intent_context(self.case.session))

    async def test_moved_context_comes_only_from_a_succeeded_execution_in_the_same_session(self):
        """'아까 옮긴 그거'의 맥락 = 이 세션에서 **성공한** 마지막 이송의 의도(저장소). 실행 실패·
        계획만 만든 것·다른 세션은 맥락이 아니다. (실제 실행 경로는 격리 복제 셀에서 따로 본다.)"""
        import types

        ok = types.SimpleNamespace(task_succeeded=True)
        failed = types.SimpleNamespace(task_succeeded=False)
        executions = {self.case.session: [types.SimpleNamespace(execution_id="e1", request_id="r1"),
                                          types.SimpleNamespace(execution_id="e2", request_id="r2")]}
        finals = {"e1": types.SimpleNamespace(result=ok, recorded_at=5.0),
                  "e2": types.SimpleNamespace(result=failed, recorded_at=6.0)}
        intents = {"r1": {"material": {"resource_id": "obj_a"}, "source": {"resource_id": "loc_pallet_1"},
                          "destination": {"resource_id": "loc_conveyor"}},
                   "r2": {"material": {"resource_id": "obj_b"}, "source": {"resource_id": "loc_pallet_2"},
                          "destination": {"resource_id": "loc_conveyor"}}}
        repo = self.case.app.runtime.repository
        repo_stub = types.SimpleNamespace(
            executions_for_session=lambda sid: executions.get(sid, []),
            final_result=lambda eid: finals.get(eid), get_request_intent=lambda rid: intents.get(rid))
        self.api.runtime.repository = repo_stub
        self.addCleanup(setattr, self.api.runtime, "repository", repo)
        moved = self.api._intent_context(self.case.session)["moved"]
        self.assertEqual((moved["material"], moved["destination"], moved["at"]), ("obj_a", "loc_conveyor", 5.0))
        self.assertNotIn("moved", self.api._intent_context("sess_other"))
        finals["e1"] = types.SimpleNamespace(result=failed, recorded_at=5.0)
        self.assertNotIn("moved", self.api._intent_context(self.case.session))

    async def test_non_transfer_requests_keep_the_existing_planner(self):
        from server.plan_intent import IntentDecision

        self.use(IntentDecision("other", interpretation={}))
        body = (await self.case.plan()).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.case.provider.calls, 1)
        self.assertIsNone(self.case.app.runtime.repository.get_request_intent(body["request_id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
