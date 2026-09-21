"""place는 같은 물체를 앞에서 pick한 뒤에만 온다 (E-HOLD-001, 초안 단계 적용).

pick/place가 Profile 관문(`gated_skills`)에 막힌 작업 셀에서도 pick 없는 place
초안이 `capability.profile_incomplete`(정상 초안의 차단)로 남지 않고 구조 결함
`safety.hold_invalid`로 차단되는지 본다. 보정·pick 삽입을 하지 않는지도 본다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import (
    load_capability_profile,
    load_resource_catalog,
    load_skill_catalog,
)
from core.constants import TASK_PLAN_SCHEMA_VERSION
from core.reason_codes import ReasonCode
from planning.pipeline import plan_from_utterance
from planning.plan_provider import DraftStep, PlanDraft, PlanningMode
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION
from validation.safety_validator import place_without_matching_pick

WORKCELL = ROOT / "config" / "workcell"
PROFILE = ROOT / "config" / "profiles" / "fr3wms_2f85_workcell_capability.json"
SKILLS = ROOT / "examples" / "config" / "valid_skill_catalog.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Scripted:
    provider_id = "scripted"
    model_name = "scripted-model"

    def __init__(self, steps):
        self.steps = steps
        self.calls = 0

    def generate(self, context):
        self.calls += 1
        return PlanDraft(
            steps=tuple(DraftStep(skill, dict(args)) for skill, args in self.steps),
            mode=PlanningMode.JSON_SCHEMA, provider_id="scripted",
            model_name="scripted-model",
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            latency_sec=0.0, terminal_hold=None, is_mock=True,
        )


MOVE_P1 = ("move", {"to": "loc_pallet_1"})
MOVE_CV = ("move", {"to": "loc_conveyor"})
HOME = ("home", {})


class PlaceRequiresPick(unittest.TestCase):
    def setUp(self):
        self.catalog = load_resource_catalog(
            load(WORKCELL / "fr3_2f85_workcell_resource_catalog.json"))
        self.skills = load_skill_catalog(load(SKILLS))
        self.profile = load_capability_profile(load(PROFILE))
        # 전제: 이 셀은 pick/place를 관문으로 막는다.
        gated = self.profile.extras["gated_skills"]["skills"]
        self.assertEqual(set(gated), {"pick", "place"})

    def run_flow(self, steps, utterance="작업 요청"):
        provider = Scripted(steps)
        out = plan_from_utterance(
            utterance, catalog=self.catalog, skill_catalog=self.skills,
            profile=self.profile, provider=provider, robot_id="fr3wms_2f85_workcell",
            plan_id_factory=lambda: "plan_1", created_at=1_000.0, ttl_sec=60.0,
            stop_keywords=("정지", "멈춰"), schema_version=TASK_PLAN_SCHEMA_VERSION,
        )
        return out, provider

    def assert_hold_blocked(self, steps):
        out, _ = self.run_flow(steps)
        self.assertFalse(out.ok)
        self.assertIsNone(out.plan)
        self.assertIs(out.failure.reason, ReasonCode.SAFETY_HOLD_INVALID)
        self.assertIn("E-HOLD-001", out.failure.detail)
        # 보정하지 않는다: 초안 스텝이 그대로 남고 pick이 끼워지지 않았다.
        self.assertEqual([s.skill for s in out.draft.steps], [s for s, _ in steps])
        return out

    def test_pick_then_place_same_object_passes_structure(self):
        steps = [MOVE_P1, ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                 MOVE_CV, ("place", {"object": "mat_a", "to": "loc_conveyor"}), HOME]
        self.assertEqual(place_without_matching_pick(
            [DraftStep(s, a) for s, a in steps]), [])
        out, _ = self.run_flow(steps)
        # 구조는 통과하고, 기존 관문 차단은 그대로다.
        self.assertFalse(out.ok)
        self.assertIs(out.failure.reason, ReasonCode.CAPABILITY_PROFILE_INCOMPLETE)

    def test_place_only_is_blocked_structurally(self):
        self.assert_hold_blocked(
            [MOVE_CV, ("place", {"object": "mat_a", "to": "loc_conveyor"}), HOME])

    def test_pick_and_place_of_different_objects_is_blocked(self):
        self.assert_hold_blocked(
            [MOVE_P1, ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
             MOVE_CV, ("place", {"object": "mat_b", "to": "loc_conveyor"}), HOME])

    def test_stop_is_unaffected(self):
        out, provider = self.run_flow([HOME], utterance="정지")
        self.assertTrue(out.ok)
        self.assertTrue(out.bypassed_model)
        self.assertEqual(provider.calls, 0)
        self.assertEqual([s.skill for s in out.plan.steps], ["stop"])
        self.assertEqual(place_without_matching_pick(out.plan.steps), [])

    def test_observed_draft_place_without_pick_single(self):
        # 1.7 개발셋 평가에서 관측된 초안 구조(단일 자재, pick 없음).
        self.assert_hold_blocked(
            [MOVE_CV, ("place", {"object": "mat_b", "to": "loc_conveyor"}), HOME])

    def test_observed_draft_place_without_pick_two_objects(self):
        # 1.7 개발셋 평가에서 관측된 초안 구조(두 자재, 둘 다 pick 없음).
        out = self.assert_hold_blocked(
            [MOVE_CV, ("place", {"object": "mat_a", "to": "loc_conveyor"}),
             MOVE_CV, ("place", {"object": "mat_b", "to": "loc_conveyor"}), HOME])
        self.assertEqual(out.failure.detail.count("아무것도 들지 않은 채"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
