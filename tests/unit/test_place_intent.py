"""물체가 확인되지 않은 놓기 요청은 ASK (8-15).

개발셋만 근거로 한다. holdout/sealed2/sealed3 발화는 쓰지 않는다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from core.grasp_observation import measured, unavailable  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402
from validation.place_intent import (  # noqa: E402
    PLACE_MARKERS, evaluate_place_intent, has_place_intent,
)

CONFIG = ROOT / "examples" / "config"


def catalog():
    return load_resource_catalog(json.loads(
        (ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json")
        .read_text(encoding="utf-8")))


class TestPlaceIntent(unittest.TestCase):
    def setUp(self):
        self.cat = catalog()

    def _ev(self, utterance, held=None):
        return evaluate_place_intent(
            utterance=utterance, slots=extract_slots(utterance, self.cat),
            catalog=self.cat, held_object_id=held)

    def test_place_markers_are_detected(self):
        for u in ("내려놔", "놓아", "거기 놔", "컨베이어에 놓아", "저기 얹어놔"):
            self.assertTrue(has_place_intent(u), u)

    def test_move_is_not_a_place_request(self):
        for u in ("1번 팔레트로 가", "컨베이어로 이동해", "홈으로 복귀"):
            self.assertFalse(has_place_intent(u), u)

    def test_place_without_object_is_underspecified(self):
        # 개발셋 dev_098 유형과 그 모호한 변형.
        for u in ("1번 팔레트에 내려놔", "내려놔", "놓아", "거기 놔", "컨베이어에 놓아"):
            self.assertTrue(self._ev(u).underspecified, u)

    def test_place_with_object_and_destination_is_not_underspecified(self):
        # 명확한 놓기는 기존 자원·계획 검증을 거친다(여기서 되묻지 않는다).
        r = self._ev("A자재를 컨베이어에 내려놔")
        self.assertTrue(r.is_place_request)
        self.assertTrue(r.object_confirmed)
        self.assertTrue(r.destination_confirmed)
        self.assertFalse(r.underspecified)

    def test_full_transfer_is_not_underspecified(self):
        r = self._ev("1번 팔레트에서 A자재를 집어서 컨베이어에 내려놔")
        self.assertFalse(r.underspecified)

    def test_pure_move_is_never_underspecified(self):
        self.assertFalse(self._ev("1번 팔레트로 가").underspecified)

    def test_held_object_counts_only_when_measured_and_named(self):
        # 실제 측정 관측이 물체를 지목하면 그 물체는 확인된 것으로 본다.
        r = self._ev("컨베이어에 내려놔", held="mat_a")
        self.assertTrue(r.object_confirmed)
        self.assertFalse(r.underspecified)  # 물체(측정) + 목적지(컨베이어) 모두 확인
        # 목적지가 없으면 여전히 되묻는다.
        r2 = self._ev("내려놔", held="mat_a")
        self.assertTrue(r2.underspecified)

    def test_simulated_or_missing_grasp_never_confirms_object(self):
        # held_object_id는 측정값일 때만 넘긴다. None이면 물체는 발화로만 확인된다.
        r = self._ev("1번 팔레트에 내려놔", held=None)
        self.assertFalse(r.object_confirmed)
        self.assertTrue(r.underspecified)


if __name__ == "__main__":
    unittest.main()
