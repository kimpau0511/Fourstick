"""일반 경로(`/v1/plan`)의 서버 검증 근거 · 정지 발화 (2026-10-06).

지키는 것:
- 별칭 목록에 없는 표현("초록색 모형"·"연두 물체")도 **등록된 색이 유일하게 가리키는 자재**면
  확인된 리소스가 된다. 같은 색 자재가 여럿이거나 등록되지 않은 색이면 더하지 않는다.
- 확인된 자재의 **현재 위치**(기록)가 출발지로 확인된다 — 모델이 엉뚱한 곳에서 집지 않게.
- "그거"처럼 근거가 없는 지시어는 아무것도 더하지 않는다(→ 되묻기 또는 일치 검증 차단).
- 모델 입력과 요청↔계획 일치 검증이 **같은 근거**를 본다.
- 정지 발화는 계획·모델 없이 전체 정지다.

실제 작업 셀 카탈로그·설정을 쓴다. 모델·Gazebo를 부르지 않는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from core.constants import TASK_PLAN_SCHEMA_VERSION  # noqa: E402
from core.resource_catalog import ResourceKind  # noqa: E402
from core.task_plan import TaskPlan, TaskStep  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402
from server.plan_grounding import ground_slots, object_facts  # noqa: E402
from validation.request_plan_consistency import (  # noqa: E402
    ConsistencyStatus,
    check_request_plan_consistency,
)

CATALOG = load_resource_catalog(json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json").read_text(encoding="utf-8")))
WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
AT_ORIGIN = {"material_a": "loc_pallet_1", "material_b": "loc_pallet_2",
             "material_c": "loc_pallet_3"}


def grounded(utterance, locations=AT_ORIGIN):
    facts = object_facts(CATALOG, WORKCELL, locations)
    return ground_slots(extract_slots(utterance, CATALOG), catalog=CATALOG, facts=facts), facts


def transfer_plan(obj="mat_c", source="loc_pallet_3", utterance="x"):
    return TaskPlan(
        plan_id="plan_t", robot_id="r", profile_id="p", profile_version="1",
        steps=(TaskStep("move", {"to": source}),
               TaskStep("pick", {"object": obj, "from": source}),
               TaskStep("move", {"to": "loc_conveyor"}),
               TaskStep("place", {"object": obj, "to": "loc_conveyor"}),
               TaskStep("home")),
        created_at=0.0, ttl_sec=60.0, schema_version=TASK_PLAN_SCHEMA_VERSION,
        utterance=utterance)


class GroundingTest(unittest.TestCase):
    def test_registered_color_names_the_only_material_with_it(self):
        for utterance in ("초록색 모형을 컨베이어 벨트로 옮겨줘", "연두 물체를 저쪽 벨트로 보내줘"):
            with self.subTest(utterance=utterance):
                plain = extract_slots(utterance, CATALOG)
                self.assertNotIn("mat_c", plain.resource_ids, "별칭만으로는 확인되지 않아야 시험이 된다")
                slots, _ = grounded(utterance)
                self.assertIn("mat_c", slots.distinct(ResourceKind.OBJECT))
                self.assertIn("loc_conveyor", slots.resource_ids)
                # 출발지 = C자재의 지금 위치(기록), 사용자가 말하지 않았어도.
                self.assertIn("loc_pallet_3", slots.distinct(ResourceKind.LOCATION))

    def test_current_location_follows_the_record(self):
        slots, _ = grounded("초록색 모형을 원래 자리로 돌려놔",
                            {**AT_ORIGIN, "material_c": "slot_2"})
        self.assertIn("mat_c", slots.resource_ids)
        self.assertIn("loc_conveyor", slots.resource_ids)       # 컨베이어 칸 → 카탈로그의 컨베이어
        self.assertNotIn("loc_pallet_3", slots.resource_ids)

    def test_unknown_location_adds_no_source(self):
        slots, _ = grounded("초록색 모형을 컨베이어 벨트로 옮겨줘", {})
        self.assertIn("mat_c", slots.resource_ids)
        self.assertNotIn("loc_pallet_3", slots.resource_ids)

    def test_unregistered_color_and_pointing_words_add_nothing(self):
        for utterance in ("보라색 모형을 컨베이어로 옮겨줘", "그거 옮겨줘", "저쪽 거 옮겨줘"):
            with self.subTest(utterance=utterance):
                slots, _ = grounded(utterance)
                self.assertEqual(slots.distinct(ResourceKind.OBJECT), ())

    def test_a_color_shared_by_two_materials_adds_nothing(self):
        facts = object_facts(CATALOG, WORKCELL, AT_ORIGIN)
        facts["mat_a"] = {**facts["mat_a"], "canonical": [*facts["mat_a"]["canonical"],
                                                          *facts["mat_c"]["canonical"]]}
        slots = ground_slots(extract_slots("초록색 모형을 컨베이어로 옮겨줘", CATALOG),
                             catalog=CATALOG, facts=facts)
        self.assertEqual(slots.distinct(ResourceKind.OBJECT), ())



class ConsistencyWithGroundingTest(unittest.TestCase):
    def test_grounded_transfer_is_consistent(self):
        slots, _ = grounded("초록색 모형을 컨베이어 벨트로 옮겨줘")
        report = check_request_plan_consistency(plan=transfer_plan(), slots=slots, catalog=CATALOG)
        self.assertEqual(report.status, ConsistencyStatus.CONSISTENT, report.detail)

    def test_without_grounding_the_same_plan_is_blocked(self):
        slots = extract_slots("초록색 모형을 컨베이어 벨트로 옮겨줘", CATALOG)
        report = check_request_plan_consistency(plan=transfer_plan(), slots=slots, catalog=CATALOG)
        self.assertNotEqual(report.status, ConsistencyStatus.CONSISTENT)

    def test_model_choosing_another_material_is_still_blocked(self):
        slots, _ = grounded("초록색 모형을 컨베이어 벨트로 옮겨줘")
        report = check_request_plan_consistency(
            plan=transfer_plan(obj="mat_a", source="loc_pallet_1"), slots=slots, catalog=CATALOG)
        self.assertEqual(report.status, ConsistencyStatus.MISMATCH)

    def test_picking_from_somewhere_other_than_the_current_location_is_blocked(self):
        slots, _ = grounded("초록색 모형을 컨베이어 벨트로 옮겨줘")
        report = check_request_plan_consistency(
            plan=transfer_plan(source="loc_pallet_1"), slots=slots, catalog=CATALOG)
        self.assertEqual(report.status, ConsistencyStatus.MISMATCH)

    def test_pointing_word_cannot_justify_any_plan(self):
        slots, _ = grounded("그거 옮겨줘")
        report = check_request_plan_consistency(plan=transfer_plan(), slots=slots, catalog=CATALOG)
        self.assertNotEqual(report.status, ConsistencyStatus.CONSISTENT)


class PipelineTest(unittest.TestCase):
    """모델 입력이 서버 근거를 받는지, 정지는 모델을 부르지 않는지."""

    def setUp(self):
        from tests.integration.test_planning_flow import (
            PROFILE_FILE,
            SKILLS_FILE,
            ScriptedProvider,
            draft,
            load,
        )
        from config.loader import load_capability_profile, load_skill_catalog
        from planning.plan_provider import DraftStep

        self.skill_catalog = load_skill_catalog(load(SKILLS_FILE))
        self.profile = load_capability_profile(load(PROFILE_FILE))
        self.provider = ScriptedProvider(draft([DraftStep("home", {})]))
        facts = object_facts(CATALOG, WORKCELL, AT_ORIGIN)
        self.grounder = lambda slots: ground_slots(slots, catalog=CATALOG, facts=facts)

    def run_once(self, utterance):
        from planning.pipeline import plan_from_utterance

        return plan_from_utterance(
            utterance, catalog=CATALOG, skill_catalog=self.skill_catalog,
            profile=self.profile, provider=self.provider, robot_id="r",
            plan_id_factory=lambda: "plan_x", created_at=0.0, ttl_sec=60.0,
            stop_keywords=("멈춰", "정지", "스톱"), schema_version=TASK_PLAN_SCHEMA_VERSION,
            slot_grounder=self.grounder)

    def test_model_sees_the_grounded_material_and_its_location(self):
        from planning.prompt import _recognized_line

        self.run_once("초록색 모형을 컨베이어 벨트로 옮겨줘")
        context = self.provider.contexts[-1]
        self.assertIn("mat_c", context.slots.resource_ids)
        line = _recognized_line(context, CATALOG)
        # 서버가 다른 근거로 확인한 것은 '표현'=id로, 별칭으로 확인한 것은 id만.
        self.assertIn("'초록색'=mat_c", line)
        self.assertIn("'C자재의 현재 위치'=loc_pallet_3", line)
        self.assertIn("loc_conveyor", line)
        self.assertNotIn("'컨베이어", line)

    def test_alias_only_utterance_keeps_the_old_line(self):
        from planning.prompt import _recognized_line

        self.run_once("1번 팔레트로 이동해줘")
        self.assertEqual(_recognized_line(self.provider.contexts[-1], CATALOG), "loc_pallet_1")

    def test_stop_never_reaches_the_model(self):
        outcome = self.run_once("멈춰")
        self.assertEqual(self.provider.contexts, [])
        self.assertTrue(outcome.bypassed_model)


class PlanRouteStopTest(unittest.IsolatedAsyncioTestCase):
    """`POST /v1/plan` "멈춰" → 계획 없이 전체 정지(모델 호출 0)."""

    async def asyncSetUp(self):
        from tests.integration.test_web_api import WebCase

        self.case = WebCase()
        await WebCase.asyncSetUp(self.case)
        self.addAsyncCleanup(self._cleanup)

    async def _cleanup(self):
        self.case.doCleanups()

    async def test_stop_utterance_stops_without_a_plan_or_model_call(self):
        response = await self.case.plan("멈춰")
        self.assertEqual(response.status, 200)
        body = response.json()
        self.assertEqual(body["decision"], "STOP")
        self.assertTrue(body["stopped"])
        self.assertTrue(body["stop"]["requested"])
        self.assertNotIn("plan", body)
        self.assertEqual(self.case.provider.calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
