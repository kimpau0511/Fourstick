"""공통 transfer 결과 기록 — 이어 옮긴 뒤에도 기록이 마지막 이송을 가리키는가."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.simulation_demo_state import (  # noqa: E402
    HELD_ON_TARGET,
    ON_PALLET,
    STOPPED_UNRESTORED,
    SimulationDemoState,
)

P2 = (0.5, 0.0, 0.84)
SLOT1 = (0.25, -0.5, 0.75)


def base(source, destination_rid):
    return {"scenario": f"{source}/mat_a->{destination_rid}", "object_id": "mat_a",
            "support_id": "loc_pallet_1", "target_id": destination_rid,
            "reset_command": "restore"}


class RecordTransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = SimulationDemoState(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def move(self, source, destination, kind, center, rid, **kw):
        args = dict(completed=True, stop_requested=False, attached=True, final_pose_m=center)
        args.update(kw)
        return self.state.record_transfer(
            "material_a", source=source, destination=destination, destination_kind=kind,
            destination_center_m=center, own_origin="loc_pallet_1",
            base=base(source, rid), **args)

    def row(self):
        return (self.state.status().get("objects") or {}).get("material_a")

    def test_pallet_then_slot_points_at_the_conveyor(self):
        # 우회 계획: 1번 팔레트 → 2번 팔레트 → 칸 1. 두 번째 기록은 컨베이어를 가리켜야 한다.
        self.move("loc_pallet_1", "loc_pallet_2", "pallet", P2, "loc_pallet_2")
        self.assertEqual((self.row()["state"], self.row()["pallet"]), (ON_PALLET, "loc_pallet_2"))
        self.move("loc_pallet_2", "slot_1", "conveyor_slot", SLOT1, "loc_conveyor")
        row = self.row()
        self.assertEqual((row["state"], row["slot"]), (HELD_ON_TARGET, "slot_1"))
        self.assertEqual(row["target_id"], "loc_conveyor")
        self.assertEqual(row["scenario"], "loc_pallet_2/mat_a->loc_conveyor")
        self.assertNotIn("pallet", row)

    def test_own_origin_clears_and_stop_binds_destination(self):
        self.move("loc_pallet_1", "slot_1", "conveyor_slot", SLOT1, "loc_conveyor")
        self.move("slot_1", "loc_pallet_2", "pallet", P2, "loc_pallet_2",
                  completed=False, stop_requested=True)
        row = self.row()
        self.assertEqual(row["state"], STOPPED_UNRESTORED)
        self.assertEqual(row["destination"]["id"], "loc_pallet_2")
        self.assertEqual(row["target_id"], "loc_pallet_2")

    def test_not_attached_keeps_the_record(self):
        self.move("loc_pallet_1", "slot_1", "conveyor_slot", SLOT1, "loc_conveyor")
        before = self.row()
        self.move("slot_1", "loc_pallet_2", "pallet", P2, "loc_pallet_2",
                  completed=False, attached=False)
        self.assertEqual(self.row(), before)


if __name__ == "__main__":
    unittest.main()
