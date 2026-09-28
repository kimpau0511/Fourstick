"""자연어 pick/place — **선언된 자리만** 출발·도착이 된다.

확인하는 것:
 - 자리 어휘는 셀 설정과 **검증된 슬롯**에서만 만들어진다(코드가 짓지 않는다)
 - 발화가 자리를 콕 집으면 그 자리로 간다 — 서버가 다른 자리로 바꾸지 않는다
 - 찬 자리·없는 자리·엉뚱한 팔레트·지원 밖 목적지는 BLOCK이고 작업이 0건이다
 - 자리를 말하지 않으면 예전처럼 **빈 첫 자리**를 서버가 고른다
 - 모델 출력에 관절값·좌표를 담을 칸이 없다(스키마 시험은 intent 쪽에 있다)

ROS·브라우저·LLM 없이 돈다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_commands import (  # noqa: E402
    BLOCK,
    RUN,
    decide,
    parse_command,
    spoken_places,
)
from server.sim_demo_places import (  # noqa: E402
    CONVEYOR,
    CONVEYOR_SLOT,
    PALLET,
    SAFE,
    declared_places,
    resolve,
)
from validation.conveyor_slots import load_slots  # noqa: E402

WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json")
                      .read_text(encoding="utf-8"))
GRASP = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json")
                   .read_text(encoding="utf-8"))
POSES = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_poses.json")
                   .read_text(encoding="utf-8"))
SLOTS = load_slots(GRASP)
PLACES = declared_places(WORKCELL, SLOTS, POSES)

MATERIALS = {
    "material_a": {"model": "material_a", "korean": "A자재",
                   "support_model": "pallet_1"},
    "material_b": {"model": "material_b", "korean": "B자재",
                   "support_model": "pallet_2"},
    "material_c": {"model": "material_c", "korean": "C자재",
                   "support_model": "pallet_3"},
}


def status(objects=None, checkpoints=()):
    return {"state": {"available": True, "objects": dict(objects or {}),
                      "checkpoints": list(checkpoints)},
            "running_job": None}


def held(model, slot, pose=(0.25, -0.5, 0.75)):
    return {model: {"state": "held_on_target", "slot": slot,
                    "pose_m": list(pose)}}


def run(parsed, state=None):
    return decide(parsed, state or status(), MATERIALS, SLOTS, PLACES)


class VocabularyTest(unittest.TestCase):
    """어휘는 **선언에서만** 온다."""

    def test_places_are_pallets_slots_conveyor_and_safe(self):
        kinds = {place.id: place.kind for place in PLACES}
        self.assertEqual(kinds["loc_pallet_1"], PALLET)
        self.assertEqual(kinds["slot_2"], CONVEYOR_SLOT)
        self.assertEqual(kinds["loc_conveyor"], CONVEYOR)
        self.assertEqual(kinds["loc_safe_home"], SAFE)

    def test_only_verified_slots_enter_the_vocabulary(self):
        """검증되지 않은 슬롯 설정이면 자리도 생기지 않는다."""
        blocked = {"conveyor_slots": {"status": "blocked", "slots": {}}}
        places = declared_places(WORKCELL, load_slots(blocked), POSES)
        self.assertEqual([p.id for p in places if p.kind == CONVEYOR_SLOT], [])

    def test_an_unknown_place_is_never_invented(self):
        self.assertIsNone(resolve(PLACES, "slot_9"))
        self.assertIsNone(resolve(PLACES, "loc_workbench"))
        self.assertIsNone(resolve(PLACES, None))


class SpokenPlaceTest(unittest.TestCase):
    """발화 → 자리. 해석만 한다(상태는 보지 않는다)."""

    def test_slot_phrasings(self):
        for text, expected in (("컨베이어 2번 위치에 놓아줘", "slot_2"),
                               ("컨베이어 3번 자리에 올려줘", "slot_3"),
                               ("2번 슬롯에 놔줘", "slot_2"),
                               ("슬롯1에 올려줘", "slot_1")):
            with self.subTest(text=text):
                self.assertEqual(spoken_places(text, WORKCELL, SLOTS)["slot"],
                                 expected)

    def test_a_pallet_is_not_read_as_a_slot(self):
        found = spoken_places("1번 팔레트에서 집어줘", WORKCELL, SLOTS)
        self.assertIsNone(found["slot"])
        self.assertEqual(found["pallet"], "loc_pallet_1")

    def test_full_sentence_gives_source_and_destination(self):
        parsed = parse_command("A자재를 1번 팔레트에서 컨베이어 2번 위치에 놓아줘",
                               WORKCELL, SLOTS)
        self.assertEqual(parsed["intent"], "transfer")
        self.assertEqual(parsed["material"], "material_a")
        self.assertEqual(parsed["source"], "loc_pallet_1")
        self.assertEqual(parsed["destination"], "slot_2")


class NamedSlotTest(unittest.TestCase):
    """자리를 말하면 **그 자리**다."""

    def transfer(self, material="material_a", source=None, destination=None,
                 state=None):
        return run({"intent": "transfer", "decision": RUN, "material": material,
                    "source": source, "destination": destination}, state)

    def test_each_verified_slot_can_be_named(self):
        for name in ("slot_1", "slot_2", "slot_3"):
            with self.subTest(slot=name):
                out = self.transfer(destination=name)
                self.assertEqual(out["decision"], RUN)
                self.assertEqual(out["slot"], name)
                self.assertEqual(out["job_spec"]["slot"], name)

    def test_an_unspoken_slot_is_chosen_by_the_server(self):
        out = self.transfer(destination="loc_conveyor")
        self.assertEqual(out["slot"], "slot_1")
        out = self.transfer(material="material_b", destination="loc_conveyor",
                            state=status(held("material_a", "slot_1")))
        self.assertEqual(out["slot"], "slot_2")

    def test_a_named_slot_is_not_quietly_swapped(self):
        """1번이 차 있어도 3번을 말했으면 3번이다."""
        out = self.transfer(material="material_b", destination="slot_3",
                            state=status(held("material_a", "slot_1")))
        self.assertEqual((out["decision"], out["slot"]), (RUN, "slot_3"))

    def test_an_occupied_named_slot_is_blocked(self):
        out = self.transfer(material="material_b", destination="slot_1",
                            state=status(held("material_a", "slot_1")))
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("이미 A자재", out["reason"])
        self.assertIsNone(out.get("job_spec"))

    def test_a_slot_that_does_not_exist_is_blocked(self):
        out = self.transfer(destination="slot_9")
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("없는 도착 위치", out["reason"])


class UnsupportedTargetTest(unittest.TestCase):
    """지원하지 않는 자리는 **조용히 바꾸지 않고** 막는다."""

    def test_safe_pose_is_not_a_place_for_material(self):
        out = run({"intent": "transfer", "decision": RUN, "material": "material_a",
                   "source": None, "destination": "loc_safe_home"})
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("안전 위치", out["reason"])

    def test_pallet_to_pallet_transfer_is_not_supported(self):
        out = run({"intent": "transfer", "decision": RUN, "material": "material_a",
                   "source": "loc_pallet_1", "destination": "loc_pallet_2"})
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("지원하지 않습니다", out["reason"])

    def test_a_wrong_source_pallet_is_blocked(self):
        out = run({"intent": "transfer", "decision": RUN, "material": "material_a",
                   "source": "loc_pallet_3", "destination": "loc_conveyor"})
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("3번 팔레트에 있지 않습니다", out["reason"])


class ReturnPlaceTest(unittest.TestCase):
    """복귀는 컨베이어 → **그 자재의 원래 팔레트**뿐이다."""

    def state(self):
        return status(held("material_a", "slot_2", (0.1498, -0.4995, 0.75)))

    def test_return_to_the_declared_origin_pallet(self):
        out = run({"intent": "return", "decision": RUN, "material": "material_a",
                   "source": "slot_2", "destination": "loc_pallet_1"},
                  self.state())
        self.assertEqual(out["decision"], RUN)
        self.assertEqual(out["slot"], "slot_2")
        self.assertEqual(out["job_spec"]["action"], "return")

    def test_return_to_another_pallet_is_blocked(self):
        out = run({"intent": "return", "decision": RUN, "material": "material_a",
                   "source": "slot_2", "destination": "loc_pallet_2"},
                  self.state())
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("원래 자리", out["reason"])

    def test_a_source_slot_the_material_is_not_in_is_blocked(self):
        out = run({"intent": "return", "decision": RUN, "material": "material_a",
                   "source": "slot_3", "destination": "loc_pallet_1"},
                  self.state())
        self.assertEqual(out["decision"], BLOCK)
        self.assertIn("컨베이어 3번 위치에 있지 않습니다", out["reason"])

    def test_return_to_the_conveyor_is_blocked(self):
        out = run({"intent": "return", "decision": RUN, "material": "material_a",
                   "source": "slot_2", "destination": "slot_1"}, self.state())
        self.assertEqual(out["decision"], BLOCK)


class JobSpecTest(unittest.TestCase):
    """만들어지는 것은 **선언형 job spec** 하나다."""

    def test_spec_has_no_room_for_coordinates(self):
        out = run({"intent": "transfer", "decision": RUN, "material": "material_a",
                   "source": "loc_pallet_1", "destination": "slot_2"})
        self.assertEqual(sorted(out["job_spec"]),
                         ["action", "checkpoint_id", "destination", "material",
                          "slot", "source"])
        for banned in ("joint_rad", "pose", "trajectory", "xyz"):
            self.assertNotIn(banned, out["job_spec"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
