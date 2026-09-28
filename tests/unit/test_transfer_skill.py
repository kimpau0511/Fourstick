"""공통 transfer 계약 + FR3 Capability."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.transfer_skill import (  # noqa: E402
    PHASES,
    Location,
    LocationKind,
    PoseRef,
    PoseRole,
    RoutePoses,
    TransferRequest,
    WorldView,
    validate_transfer,
)
from robots.fr3_gazebo.transfer_capability import ROBOT_ID, Fr3TransferCapability  # noqa: E402

CAP = Fr3TransferCapability.from_files()
HOME = {"material_a": "loc_pallet_1", "material_b": "loc_pallet_2",
        "material_c": "loc_pallet_3"}


def req(material, source, destination, robot=ROBOT_ID):
    return TransferRequest(robot, material, source, destination)


def world(**where):
    return WorldView(location_of={**HOME, **where})


class Fr3RoutesTest(unittest.TestCase):
    def ok(self, request, view):
        plan, findings = validate_transfer(request, CAP, view)
        self.assertEqual(findings, [])
        return plan

    def test_supported_routes_resolve_verified_poses(self):
        forward = self.ok(req("material_a", "loc_pallet_1", "slot_2"), world())
        self.assertEqual(forward.route, "pallet->conveyor_slot")
        self.assertEqual(forward.poses.grasp.name, "material_a_grasp")
        self.assertEqual(forward.poses.place.name, "conveyor_place__slot_2")
        back = self.ok(req("material_a", "slot_2", "loc_pallet_1"), world(material_a="slot_2"))
        self.assertEqual(back.poses.grasp.name, "material_a_conveyor_grasp__slot_2")
        self.assertEqual(back.poses.place.name, "material_a_pallet_place")
        direct = self.ok(req("material_b", "slot_1", "slot_3"), world(material_b="slot_1"))
        self.assertEqual(direct.route, "conveyor_slot->conveyor_slot")
        self.assertEqual((direct.poses.grasp.location, direct.poses.place.location),
                         ("slot_1", "slot_3"))
        self.assertEqual(direct.phases, PHASES)

    def test_pallet_routes_use_location_grasp_and_matching_place(self):
        # 파란(B) → 초록(C) 팔레트: C가 비킨 상태에서 위치 파지·출처가 맞는 놓기.
        plan = self.ok(req("material_b", "loc_pallet_2", "loc_pallet_3"),
                       world(material_c="slot_1"))
        self.assertEqual(plan.route, "pallet->pallet")
        self.assertEqual(plan.poses.grasp.name, "material_b_grasp")
        self.assertEqual(plan.poses.place.held_object_source, "poses.material_b_grasp")
        back = self.ok(req("material_b", "loc_pallet_3", "loc_pallet_2"),
                       world(material_b="loc_pallet_3", material_c="slot_1"))
        self.assertEqual(back.poses.grasp.name, "material_b_grasp_at_loc_pallet_3")
        self.assertEqual(back.poses.place.held_object_source,
                         "pallet_grasp_poses.material_b_grasp_at_loc_pallet_3")
        again = self.ok(req("material_b", "loc_pallet_3", "slot_2"),
                        world(material_b="loc_pallet_3", material_c="slot_1"))
        self.assertEqual(again.poses.place.name, "conveyor_place__slot_2")

    def test_measured_path_obstruction_blocks(self):
        # 측정: 칸 1 → 칸 3은 칸 2를 스친다. 칸 2에 자재가 있으면 계약이 막는다.
        self.assertEqual(CAP.path_obstructions("material_a", "slot_1", "slot_3"),
                         frozenset({"slot_2"}))
        plan, findings = validate_transfer(req("material_a", "slot_1", "slot_3"), CAP,
                                           world(material_a="slot_1", material_b="slot_2"))
        self.assertIsNone(plan)
        self.assertEqual(findings[0].code, "geometry.path_obstructed")
        self.assertIn("slot_2의 material_b", findings[0].detail)
        # 칸 2가 비면 같은 경로가 통과한다.
        self.ok(req("material_a", "slot_1", "slot_3"), world(material_a="slot_1"))

    def test_unmeasured_route_is_left_to_the_runtime_check(self):
        from core.transfer_skill import path_obstructions
        cap = Fr3TransferCapability(workcell=CAP._workcell, grasp=CAP._grasp,
                                    poses={"poses": CAP._derived})
        self.assertIsNone(path_obstructions(cap, "material_a", "slot_1", "slot_3"))
        plan, findings = validate_transfer(req("material_a", "slot_1", "slot_3"), cap,
                                           world(material_a="slot_1", material_b="slot_2"))
        self.assertEqual(findings, [])

    def test_missing_pose_blocks_without_substitution(self):
        import copy
        import json
        base = ROOT / "config/workcell"
        load = lambda n: json.loads((base / n).read_text(encoding="utf-8"))  # noqa: E731
        grasp = copy.deepcopy(load("fr3_2f85_workcell_grasp.json"))
        places = grasp["pallet_place_poses"]
        for name in [n for n, row in places.items()
                     if row.get("support_resource_id") == "loc_pallet_3"
                     and row.get("held_object_source") == "poses.material_b_grasp"]:
            del places[name]
        del grasp["pallet_grasp_poses"]["material_a_grasp_at_loc_pallet_2"]
        cap = Fr3TransferCapability(workcell=load("fr3_2f85_workcell.json"), grasp=grasp,
                                    poses=load("fr3_2f85_workcell_poses.json"))
        plan, findings = validate_transfer(req("material_b", "loc_pallet_2", "loc_pallet_3"),
                                           cap, world(material_c="slot_1"))
        self.assertIsNone(plan)
        self.assertEqual(findings[0].code, "geometry.grasp_pose_unavailable")
        self.assertIn("대신하지 않는다", findings[0].detail)
        plan, findings = validate_transfer(req("material_a", "loc_pallet_2", "slot_1"), cap,
                                           world(material_a="loc_pallet_2",
                                                 material_b="slot_3"))
        self.assertEqual(findings[0].code, "geometry.grasp_pose_unavailable")
        self.assertIn("집는", findings[0].detail)

    def test_state_and_occupancy_rules(self):
        cases = (
            (req("material_a", "slot_2", "slot_1"), world(material_a="slot_3"),
             "ASK", "state.source_mismatch"),
            (req("material_a", "slot_1", "slot_2"),
             WorldView({**HOME, "material_a": "slot_1"}, blocked=frozenset({"slot_2"})),
             "BLOCK", "plan.destination_blocked"),
            (req("material_a", "slot_1", "slot_2"), world(material_a="slot_1",
                                                         material_b="slot_2"),
             "BLOCK", "plan.destination_occupied"),
            (req("material_a", "slot_1", "slot_1"), world(material_a="slot_1"),
             "BLOCK", "plan.same_location"),
            (req("material_z", "slot_1", "slot_2"), world(), "BLOCK", "plan.unknown_resource"),
            (req("material_a", "slot_1", "slot_9"), world(material_a="slot_1"),
             "BLOCK", "plan.unknown_resource"),
            (req("material_a", "slot_1", "slot_2", robot="humanoid_x"), world(),
             "BLOCK", "robot.unknown"),
            (req("material_c", "slot_1", "slot_2"),
             WorldView({**HOME, "material_c": "slot_1"},
                       unavailable=frozenset({"material_c"})),
             "BLOCK", "plan.material_unavailable"),
        )
        for request, view, decision, code in cases:
            with self.subTest(code=code):
                plan, findings = validate_transfer(request, CAP, view)
                self.assertIsNone(plan)
                self.assertEqual((findings[0].decision, findings[0].code), (decision, code))


class FakeCapability:
    """자세 계약 위반을 일부러 돌려주는 대역."""

    robot_id = "fake"

    def __init__(self, poses):
        self._poses = poses

    def locations(self):
        return {"p": Location("p", LocationKind.PALLET),
                "s": Location("s", LocationKind.CONVEYOR_SLOT)}

    def materials(self):
        return ["m"]

    def routes(self):
        return frozenset({(LocationKind.PALLET, LocationKind.CONVEYOR_SLOT)})

    def resolve(self, material, source, destination):
        return self._poses


J1 = {"j1": 0.1}
J2 = {"j1": 0.2}


def poses(grasp=None, place=None):
    return RoutePoses(
        source_approach=PoseRef("pa", PoseRole.APPROACH, "p", J1),
        grasp=grasp or PoseRef("g", PoseRole.GRASP, "p", J1),
        destination_approach=PoseRef("sa", PoseRole.APPROACH, "s", J2),
        place=place or PoseRef("pl", PoseRole.PLACE, "s", J2))


class PoseContractTest(unittest.TestCase):
    def check(self, route_poses):
        return validate_transfer(TransferRequest("fake", "m", "p", "s"),
                                 FakeCapability(route_poses), WorldView({"m": "p"}))

    def test_valid(self):
        plan, findings = self.check(poses())
        self.assertEqual(findings, [])

    def test_source_grasp_reused_as_place_is_blocked(self):
        for place in (PoseRef("g", PoseRole.PLACE, "s", J2),       # 같은 이름
                      PoseRef("pl", PoseRole.PLACE, "s", J1)):     # 같은 관절값
            with self.subTest(place=place.name):
                _, findings = self.check(poses(place=place))
                self.assertIn("geometry.grasp_reused_as_place", [f.code for f in findings])

    def test_role_and_location_must_match(self):
        _, findings = self.check(poses(place=PoseRef("pl", PoseRole.GRASP, "s", J2)))
        self.assertIn("geometry.pose_role_mismatch", [f.code for f in findings])
        _, findings = self.check(poses(place=PoseRef("pl", PoseRole.PLACE, "p", J2)))
        self.assertIn("geometry.pose_role_mismatch", [f.code for f in findings])

    def test_unverified_pose_is_blocked(self):
        _, findings = self.check(poses(place=PoseRef("pl", PoseRole.PLACE, "s", J2,
                                                     verified=False)))
        self.assertIn("geometry.pose_unverified", [f.code for f in findings])

    def test_route_kind_must_be_supported(self):
        plan, findings = validate_transfer(TransferRequest("fake", "m", "s", "p"),
                                           FakeCapability(poses()), WorldView({"m": "s"}))
        self.assertEqual(findings[0].code, "capability.route_unsupported")


if __name__ == "__main__":
    unittest.main()
