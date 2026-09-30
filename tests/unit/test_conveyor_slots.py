"""컨베이어 다중 슬롯 모델 검증 (`validation/conveyor_slots.py`).

확인하는 것:
 - **검증된 슬롯만** 읽는다. status나 검사가 깨끗하지 않으면 없는 자리다
 - 이송은 **비어 있는 첫 슬롯**을 받고, 셋이 차면 배정하지 않는다(BLOCK)
 - 복귀는 그 자재가 **배정받았던 슬롯**에서 간다
 - 정지·실패로 남은 자재도 **자리를 비우지 않는다**
 - 슬롯이 없던 옛 기록은 `slot_1`로 읽는다(호환)
 - 기록과 Gazebo 관측이 어긋나면 집어낸다(맞추지는 않는다)

ROS·브라우저 없이 돈다. 실제 채택된 좌표도 함께 확인한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.conveyor_slots import (  # noqa: E402
    LEGACY_SLOT,
    ConveyorSlot,
    assign_slot,
    load_slots,
    occupancy,
    occupancy_rows,
    reconcile_occupancy,
    record_on_conveyor,
    record_slot,
    slot_at,
    slot_label,
    slot_number,
    slots_full,
)

GRASP = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"


def clean_checks(n=7):
    return {f"check_{i}": {"clean": True} for i in range(n)}


def config(slots, status="verified"):
    return {"conveyor_slots": {"status": status, "pitch_m": 0.1, "slots": slots}}


def row(center, offset, checks=None):
    return {"object_world_center_m": list(center), "x_offset_m": offset,
            "checks": clean_checks() if checks is None else checks}


class LoadTest(unittest.TestCase):
    def test_reads_only_verified_and_clean_slots(self):
        cfg = config({
            "slot_1": row((0.25, -0.5, 0.75), 0.0),
            "slot_2": row((0.15, -0.5, 0.75), -0.1,
                          {"a": {"clean": True}, "b": {"clean": False}}),
            "slot_3": row((0.05, -0.5, 0.75), -0.2, {}),
        })
        self.assertEqual([s.name for s in load_slots(cfg)], ["slot_1"])

    def test_unverified_block_yields_nothing(self):
        cfg = config({"slot_1": row((0.25, -0.5, 0.75), 0.0)}, status="blocked")
        self.assertEqual(load_slots(cfg), ())
        self.assertEqual(load_slots({}), ())
        self.assertEqual(load_slots({"conveyor_slots": {}}), ())

    def test_slots_come_out_in_number_order(self):
        cfg = config({"slot_3": row((0.05, -0.5, 0.75), -0.2),
                      "slot_1": row((0.25, -0.5, 0.75), 0.0),
                      "slot_2": row((0.15, -0.5, 0.75), -0.1)})
        self.assertEqual([s.name for s in load_slots(cfg)],
                         ["slot_1", "slot_2", "slot_3"])

    def test_labels_and_numbers(self):
        self.assertEqual(slot_label("slot_2"), "컨베이어 2번 위치")
        self.assertEqual(slot_number("slot_3"), 3)
        # 모르는 이름은 만들어 내지 않는다.
        self.assertEqual(slot_label(None), "컨베이어")
        self.assertIsNone(slot_number("elsewhere"))


class AdoptedConfigTest(unittest.TestCase):
    """저장소에 실제로 채택된 슬롯. 검증을 거친 값인지 함께 본다."""

    def setUp(self):
        self.slots = load_slots(json.loads(GRASP.read_text(encoding="utf-8")))

    def test_three_slots_are_adopted(self):
        self.assertEqual([s.name for s in self.slots],
                         ["slot_1", "slot_2", "slot_3"])

    def test_slot_1_keeps_the_original_single_placement(self):
        """기존 단일 배치와 같은 자리여야 한다 — 호환 요구."""
        poses = json.loads(
            (ROOT / "config/workcell/fr3_2f85_workcell_poses.json")
            .read_text(encoding="utf-8"))
        place = poses["poses"]["conveyor_place"]["target_world_xyz_m"]
        self.assertEqual(self.slots[0].center_m[:2], tuple(place[:2]))
        self.assertEqual(self.slots[0].x_offset_m, 0.0)

    def test_slots_extend_only_in_the_verified_direction(self):
        """+x 쪽은 접근 IK가 수렴하지 않았다 — 대칭으로 두지 않는다."""
        self.assertTrue(all(s.x_offset_m <= 0 for s in self.slots),
                        [s.x_offset_m for s in self.slots])

    def test_spacing_is_at_least_the_declared_lower_bound(self):
        path = ROOT / "reports/workcell/conveyor_slots.json"
        # reports/는 저장소에 올리지 않는 측정 산출물이다. MoveIt이 있는 환경에서
        # scripts/derive_conveyor_slots.py를 실행해야 생긴다 — 없으면 실패가 아니라 건너뛴다.
        if not path.is_file():
            self.skipTest(f"측정 산출물이 없다: {path.relative_to(ROOT).as_posix()}")
        report = json.loads(path.read_text(encoding="utf-8"))
        lower = report["pitch_plan"]["lower_bound_m"]
        offsets = sorted(s.x_offset_m for s in self.slots)
        gaps = [round(offsets[i + 1] - offsets[i], 6) for i in range(len(offsets) - 1)]
        self.assertTrue(all(g >= lower for g in gaps), (gaps, lower))

    def test_every_adopted_slot_passed_every_moveit_check(self):
        block = json.loads(GRASP.read_text(encoding="utf-8"))["conveyor_slots"]
        self.assertEqual(block["status"], "verified")
        for name, entry in block["slots"].items():
            with self.subTest(slot=name):
                checks = entry["checks"]
                self.assertTrue(checks, "검사 기록이 없다")
                self.assertTrue(all(c["clean"] for c in checks.values()))
                # 이웃이 찬 상태 검사가 **반드시** 있어야 한다.
                self.assertIn("neighbours_occupied_place", checks)
                self.assertIn("neighbours_occupied_approach", checks)


class AssignmentTest(unittest.TestCase):
    def setUp(self):
        self.slots = load_slots(config({
            "slot_1": row((0.25, -0.5, 0.75), 0.0),
            "slot_2": row((0.15, -0.5, 0.75), -0.1),
            "slot_3": row((0.05, -0.5, 0.75), -0.2)}))

    def held(self, **pairs):
        return {model: {"state": "held_on_target", "slot": slot}
                for model, slot in pairs.items()}

    def test_first_free_slot_in_order(self):
        self.assertEqual(assign_slot(self.slots, {}).name, "slot_1")
        self.assertEqual(assign_slot(self.slots, self.held(a="slot_1")).name, "slot_2")
        self.assertEqual(
            assign_slot(self.slots, self.held(a="slot_1", b="slot_2")).name, "slot_3")

    def test_fourth_transfer_gets_nothing(self):
        full = self.held(a="slot_1", b="slot_2", c="slot_3")
        self.assertIsNone(assign_slot(self.slots, full))
        self.assertTrue(slots_full(self.slots, full))

    def test_a_freed_slot_is_reused_not_appended(self):
        records = self.held(a="slot_1", b="slot_2", c="slot_3")
        del records["b"]
        self.assertEqual(assign_slot(self.slots, records).name, "slot_2")

    def test_stopped_and_failed_records_still_hold_their_slot(self):
        """정지·실패로 남은 자재 자리에 다른 자재를 주면 겹친다."""
        for state in ("stopped_unrestored", "fault_unrestored",
                      "return_stopped", "return_failed"):
            with self.subTest(state=state):
                records = {"a": {"state": state, "slot": "slot_1"}}
                self.assertEqual(occupancy(self.slots, records)["slot_1"], "a")
                self.assertEqual(assign_slot(self.slots, records).name, "slot_2")

    def test_a_returned_material_frees_its_slot(self):
        # 복귀가 끝나면 기록이 사라진다 — 그때 자리가 빈다.
        self.assertEqual(occupancy(self.slots, {})["slot_1"], None)

    def test_legacy_records_without_slot_read_as_slot_1(self):
        records = {"a": {"state": "held_on_target"}}
        self.assertEqual(record_slot(records["a"]), LEGACY_SLOT)
        self.assertEqual(occupancy(self.slots, records)["slot_1"], "a")
        self.assertEqual(assign_slot(self.slots, records).name, "slot_2")

    def test_occupancy_rows_carry_what_the_screen_shows(self):
        records = self.held(material_a="slot_2")
        rows = occupancy_rows(self.slots, records,
                              {"material_a": {"korean": "A자재"}})
        by_slot = {r["slot"]: r for r in rows}
        self.assertFalse(by_slot["slot_1"]["occupied"])
        self.assertTrue(by_slot["slot_2"]["occupied"])
        self.assertEqual(by_slot["slot_2"]["korean"], "A자재")
        self.assertEqual(by_slot["slot_2"]["label"], "컨베이어 2번 위치")
        self.assertEqual(by_slot["slot_3"]["model"], None)


class RecordPoseTest(unittest.TestCase):
    """기록이 남았다는 것과 **그 자리에 놓였다**는 것은 다르다.

    집기 전에 정지하면 `stopped_unrestored` 기록과 슬롯 배정은 남지만 자재는
    원래 자리에 그대로다(실측 2026-09-22). 기록 안의 pose로 그 둘을 가른다.
    """

    def setUp(self):
        self.slots = load_slots(config({
            "slot_1": row((0.25, -0.5, 0.75), 0.0),
            "slot_2": row((0.15, -0.5, 0.75), -0.1),
            "slot_3": row((0.05, -0.5, 0.75), -0.2)}))

    def test_pose_at_the_assigned_slot_counts_as_on_conveyor(self):
        record = {"state": "held_on_target", "slot": "slot_2",
                  "pose_m": [0.1498, -0.4995, 0.75]}
        self.assertTrue(record_on_conveyor(self.slots, record))

    def test_pose_still_at_the_pallet_is_not_on_conveyor(self):
        # 실측값: pre-grasp에서 정지한 A자재의 기록.
        record = {"state": "stopped_unrestored", "slot": "slot_2",
                  "pose_m": [0.500081, 0.200908, 0.839999]}
        self.assertFalse(record_on_conveyor(self.slots, record))

    def test_pose_at_another_slot_is_not_the_assigned_one(self):
        record = {"state": "held_on_target", "slot": "slot_3",
                  "pose_m": [0.25, -0.5, 0.75]}
        self.assertFalse(record_on_conveyor(self.slots, record))

    def test_missing_pose_or_record_is_not_on_conveyor(self):
        self.assertFalse(record_on_conveyor(self.slots, None))
        self.assertFalse(record_on_conveyor(self.slots, {"state": "held_on_target"}))

    def test_legacy_record_without_slot_reads_as_slot_1(self):
        record = {"state": "held_on_target", "pose_m": [0.2493, -0.4998, 0.75]}
        self.assertTrue(record_on_conveyor(self.slots, record))

    def test_slot_stays_reserved_even_when_the_object_is_not_there(self):
        """자리는 계속 잡아 둔다 — 문구만 달라진다. 그 위에 다른 자재를 놓지 않는다."""
        records = {"material_a": {"state": "stopped_unrestored", "slot": "slot_2",
                                  "pose_m": [0.5, 0.2, 0.84]}}
        self.assertEqual(occupancy(self.slots, records)["slot_2"], "material_a")


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.slots = load_slots(config({
            "slot_1": row((0.25, -0.5, 0.75), 0.0),
            "slot_2": row((0.15, -0.5, 0.75), -0.1),
            "slot_3": row((0.05, -0.5, 0.75), -0.2)}))

    def test_slot_at_matches_within_tolerance_only(self):
        self.assertEqual(slot_at(self.slots, (0.151, -0.5, 0.75), 0.02).name, "slot_2")
        self.assertIsNone(slot_at(self.slots, (0.5, 0.2, 0.84), 0.02))
        self.assertIsNone(slot_at(self.slots, None, 0.02))

    def test_reconcile_reports_agreement_and_drift(self):
        records = {"material_a": {"state": "held_on_target", "slot": "slot_1"},
                   "material_b": {"state": "held_on_target", "slot": "slot_2"},
                   "material_c": {"state": "held_on_target", "slot": "slot_3"}}
        observed = {"material_a": (0.25, -0.5, 0.75),      # 맞다
                    "material_b": (0.05, -0.5, 0.75),      # 3번에서 보인다
                    "material_c": (0.5, -0.2, 0.84)}       # 컨베이어에 없다
        verdicts = {f["model"]: f["verdict"]
                    for f in reconcile_occupancy(self.slots, records, observed,
                                                 tolerance_m=0.02)}
        self.assertEqual(verdicts, {"material_a": "agrees",
                                    "material_b": "moved",
                                    "material_c": "record_only"})

    def test_missing_observation_is_not_treated_as_empty(self):
        records = {"material_a": {"state": "held_on_target", "slot": "slot_1"}}
        found = reconcile_occupancy(self.slots, records, {}, tolerance_m=0.02)
        self.assertEqual(found[0]["verdict"], "unobserved")


if __name__ == "__main__":
    unittest.main(verbosity=2)
