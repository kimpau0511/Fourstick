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
from core.task_intent import REGISTRY, STATE, UTTERANCE, TaskIntent  # noqa: E402
from core.task_plan import TaskPlan, TaskStep  # noqa: E402
from planning.intent_interpreter import (  # noqa: E402
    FIELDS,
    IntentInterpreter,
    Interpretation,
    output_json_schema,
)
from server.plan_intent import (  # noqa: E402
    IntentPlanProvider,
    decide_intent,
    load_place_terms,
)
from validation.request_plan_consistency import (  # noqa: E402
    ConsistencyStatus,
    check_request_plan_consistency,
)

CATALOG = load_resource_catalog(json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json").read_text(encoding="utf-8")))
SYMBOLS = load_place_terms(ROOT / "config/workcell/active.json")


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
            "symbols": SYMBOLS}


def interp(material=None, mtext=None, source=None, stext=None, dest=None, dtext=None,
           action="transfer", confidence=0.9, ok=True):
    return Interpretation(ok=ok, action=action, material_id=material, material_text=mtext,
                          source_id=source, source_text=stext, destination=dest,
                          destination_text=dtext, confidence=confidence, model_reason="시험",
                          model_id="fake-qwen", failure=None if ok else "unavailable",
                          reason="" if ok else "해석기를 쓸 수 없습니다: timeout")


def decide(utterance, i, **kw):
    return decide_intent(i, utterance, facts(**kw), min_confidence=0.7)


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

    def test_destination_already_reached_asks(self):
        d = decide("초록자재 컨베이어로", interp("mat_c", "초록자재", dest="loc_conveyor", dtext="컨베이어로"))
        self.assertEqual(d.kind, "ask")
        self.assertIn("이미", d.clarification)

    def test_material_must_be_named_by_the_text(self):
        cases = [("D자재를 컨베이어로 옮겨줘", interp(None, "D자재", dest="loc_conveyor", dtext="컨베이어로")),
                 ("보라색 모형을 컨베이어로", interp("mat_a", "보라색 모형", dest="loc_conveyor", dtext="컨베이어로")),
                 ("초록자재를 컨베이어로", interp("mat_a", "초록자재", dest="loc_conveyor", dtext="컨베이어로")),
                 ("A자재를 컨베이어로", interp("mat_a", "C자재", dest="loc_conveyor", dtext="컨베이어로"))]
        for utterance, i in cases:
            with self.subTest(utterance=utterance):
                self.assertEqual(decide(utterance, i).kind, "ask")

    def test_unknown_location_asks(self):
        d = decide("a자재 컨베이어로", interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로"),
                   locations={"mat_a": None})
        self.assertEqual(d.kind, "ask")
        self.assertIn("현재 위치", d.clarification)

    def test_model_failure_low_confidence_and_unknown_never_make_an_intent(self):
        for i in (interp(ok=False), interp("mat_a", "a자재", dest="loc_conveyor", dtext="컨베이어로",
                                            confidence=0.3),
                  interp(action="unknown")):
            with self.subTest(i=i):
                self.assertEqual(decide("a자재 컨베이어로", i).kind, "ask")

    def test_no_material_expression_goes_to_the_existing_planner(self):
        d = decide("1번 팔레트로 이동해줘", interp(dest="loc_pallet_1", dtext="1번 팔레트로"))
        self.assertEqual(d.kind, "other")


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
        self.assertEqual(list(schema["properties"])[:3], ["material_text", "source_text", "destination_text"])
        self.assertEqual(schema["properties"]["destination"]["enum"],
                         ["loc_conveyor", "origin", "free_slot", None])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(sorted(schema["required"]), sorted(FIELDS))

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
        self.api._interpret_intent = lambda text: decision

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

    async def test_non_transfer_requests_keep_the_existing_planner(self):
        from server.plan_intent import IntentDecision

        self.use(IntentDecision("other", interpretation={}))
        body = (await self.case.plan()).json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(self.case.provider.calls, 1)
        self.assertIsNone(self.case.app.runtime.repository.get_request_intent(body["request_id"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
