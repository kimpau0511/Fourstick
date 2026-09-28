"""공통 transfer — 12단계 빌더 · 계획기의 직접/우회 선택 · 서버 진입."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.transfer_skill import TransferRequest, WorldView, validate_transfer  # noqa: E402
from robots.fr3_gazebo import build_transfer_capability  # noqa: E402
from server.sim_demo_arrangement import (  # noqa: E402
    ORIGIN,
    ArrangementError,
    ArrangementRequest,
    plan_arrangement,
)
from server.sim_demo_goals import GOAL_ARRANGE  # noqa: E402
from server.sim_demo_jobs import SimDemoJobError, materials_from_workcell  # noqa: E402
from tests.unit.test_sim_demo_goals import GoalsBase  # noqa: E402
from tests.unit.test_sim_demo_web import WORKCELL  # noqa: E402
from validation.transfer_stages import bind_route, build_route_stages  # noqa: E402

CAP = build_transfer_capability()
GRASP = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json").read_text(encoding="utf-8"))
HOME = {"material_a": "loc_pallet_1", "material_b": "loc_pallet_2", "material_c": "loc_pallet_3"}
SLOTS = ["slot_1", "slot_2", "slot_3"]
MATERIALS = materials_from_workcell(WORKCELL)


def plan_for(material, source, destination, **where):
    plan, findings = validate_transfer(
        TransferRequest(CAP.robot_id, material, source, destination), CAP,
        WorldView(location_of={**HOME, **where}))
    assert plan is not None, findings
    return plan


class StageBuilderTest(unittest.TestCase):
    def bindings(self):
        from robots.fr3_gazebo.adapter import load_workcell_resources
        from validation.simulation_demo_state import return_bindings
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "demo_for_stages", ROOT / "scripts/demo_workcell_pick_place.py")
        demo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(demo)
        resources = load_workcell_resources(
            ROOT / "config/workcell/fr3_2f85_workcell.json",
            ROOT / "config/workcell/fr3_2f85_workcell_poses.json", state_max_age_sec=0.5,
            grasp_path=ROOT / "config/workcell/fr3_2f85_workcell_grasp.json")
        base = demo.build_bindings(resources)
        return return_bindings(base, object_id="mat_b", target_id="loc_conveyor",
                               grasp_config=GRASP, origin_id="loc_pallet_2")

    def test_slot_to_slot_stages_use_only_contract_poses(self):
        plan = plan_for("material_b", "slot_2", "slot_1", material_b="slot_2")
        bindings = bind_route(self.bindings(), plan.poses)
        stages, findings = build_route_stages(
            bindings, object_id="mat_b", route=plan.poses,
            source_resource_id="loc_conveyor", destination_resource_id="loc_conveyor")
        self.assertEqual(findings, [])
        by = {s.stage: s for s in stages}
        slots = GRASP["conveyor_slots"]["slots"]
        self.assertEqual(by["grasp_approach"].pose_name, "material_b_conveyor_grasp__slot_2")
        self.assertEqual(by["grasp_approach"].joint_rad, slots["slot_2"]["grasp_joint_rad_arm"])
        self.assertEqual(by["place_descend"].pose_name, "conveyor_place__slot_1")
        self.assertEqual(by["place_descend"].joint_rad, slots["slot_1"]["place_joint_rad"])
        self.assertEqual(by["lift"].joint_rad, slots["slot_2"]["approach_joint_rad"])
        self.assertEqual(by["place_approach"].joint_rad, slots["slot_1"]["approach_joint_rad"])
        self.assertEqual(by["place_descend"].holds_object, "mat_b")
        self.assertNotEqual(by["place_descend"].joint_rad, by["grasp_approach"].joint_rad)
        # 든 물체 선언은 컨베이어 파지에서 온다(칸에서 집었으므로).
        self.assertEqual(bindings.attached["mat_b"]["offset_m"],
                         GRASP["conveyor_grasp_poses"]["material_b_conveyor_grasp"]
                         ["attached_object"]["offset_m"])

    def test_existing_routes_resolve_to_the_same_poses_the_old_builders_use(self):
        slots = GRASP["conveyor_slots"]["slots"]
        forward = plan_for("material_a", "loc_pallet_1", "slot_3")
        self.assertEqual(dict(forward.poses.place.joint_rad), slots["slot_3"]["place_joint_rad"])
        self.assertEqual(dict(forward.poses.grasp.joint_rad),
                         GRASP["poses"]["material_a_grasp"]["joint_rad"])
        back = plan_for("material_c", "slot_2", "loc_pallet_3", material_c="slot_2")
        self.assertEqual(dict(back.poses.grasp.joint_rad), slots["slot_2"]["grasp_joint_rad_arm"])
        self.assertEqual(dict(back.poses.place.joint_rad),
                         GRASP["pallet_place_poses"]["material_c_pallet_place"]["joint_rad"])

    def test_bind_route_refuses_to_overwrite_a_pose_with_other_values(self):
        plan = plan_for("material_b", "slot_2", "slot_1", material_b="slot_2")
        bindings = self.bindings()
        poses = dict(bindings.poses)
        poses[plan.poses.place.name] = {"j1": 9.9}
        import dataclasses
        with self.assertRaises(ValueError):
            bind_route(dataclasses.replace(bindings, poses=poses), plan.poses)


def route_ok(material, here, there):
    from core.transfer_skill import route_supported
    origin = CAP.origin_of(material)
    return route_supported(CAP, material, origin if here == ORIGIN else here,
                           origin if there == ORIGIN else there)


def plan(targets, objects, **kw):
    request = ArrangementRequest(targets=targets, priority=tuple(targets))
    return plan_arrangement(request, materials=MATERIALS, slot_names=SLOTS,
                            objects={m: {"state": "held_on_target", "slot": s}
                                     for m, s in objects.items()},
                            route_ok=kw.get("route_ok", route_ok))


def steps(result):
    return [(s["action"], s["material"], s["from"], s["to"]) for s in result["steps"]]


class PlannerRouteTest(unittest.TestCase):
    def test_single_direct_move(self):
        result = plan({"material_a": "slot_3"}, {"material_a": "slot_1"})
        self.assertEqual(steps(result), [("move", "material_a", "slot_1", "slot_3")])

    def test_chain_orders_the_vacating_move_first(self):
        result = plan({"material_a": "slot_2", "material_b": "slot_3"},
                      {"material_a": "slot_1", "material_b": "slot_2"})
        self.assertEqual(steps(result), [("move", "material_b", "slot_2", "slot_3"),
                                         ("move", "material_a", "slot_1", "slot_2")])
        self.assertEqual(result["steps"][1]["depends_on"], [1])

    def test_swap_cycle_detours_one_material(self):
        result = plan({"material_a": "slot_2", "material_b": "slot_1"},
                      {"material_a": "slot_1", "material_b": "slot_2"})
        self.assertEqual(steps(result), [
            ("return", "material_b", "slot_2", ORIGIN),
            ("move", "material_a", "slot_1", "slot_2"),
            ("transfer", "material_b", ORIGIN, "slot_1")])
        self.assertEqual(result["steps"][1]["depends_on"], [1])
        self.assertEqual(result["steps"][2]["depends_on"], [1, 2])   # 자기 복귀 + A가 1번을 비움
        self.assertTrue(any("순환" in n for n in result["notes"]))

    def test_three_way_rotation_needs_one_detour(self):
        result = plan({"material_a": "slot_2", "material_b": "slot_3", "material_c": "slot_1"},
                      {"material_a": "slot_1", "material_b": "slot_2", "material_c": "slot_3"})
        self.assertEqual([s["action"] for s in result["steps"]].count("move"), 2)
        self.assertEqual(len(result["steps"]), 4)
        self.assertEqual(result["expected"], {"material_a": "slot_2", "material_b": "slot_3",
                                              "material_c": "slot_1"})

    def test_unverified_direct_route_falls_back_to_detour(self):
        def no_direct(material, here, there):
            if here != ORIGIN and there != ORIGIN:
                return False, "자세 없음"
            return route_ok(material, here, there)
        result = plan({"material_a": "slot_3"}, {"material_a": "slot_1"}, route_ok=no_direct)
        self.assertEqual(steps(result), [("return", "material_a", "slot_1", ORIGIN),
                                         ("transfer", "material_a", ORIGIN, "slot_3")])
        self.assertIn("직접 경로가 없어", result["steps"][0]["reason"])

    def test_no_verified_route_at_all_blocks(self):
        def nothing(material, here, there):
            return False, "자세 없음"
        with self.assertRaises(ArrangementError) as caught:
            plan({"material_a": "slot_3"}, {"material_a": "slot_1"}, route_ok=nothing)
        self.assertEqual(caught.exception.decision, "BLOCK")


ORIGINS = {m: CAP.origin_of(m) for m in MATERIALS}


def route_path(material, here, there):
    from core.transfer_skill import path_obstructions
    origin = CAP.origin_of(material)
    return path_obstructions(CAP, material, origin if here == ORIGIN else here,
                             origin if there == ORIGIN else there)


def pplan(targets, records, blocked=(), paths=False):
    """records: 자재 → 'slot_N' 또는 'loc_pallet_N'(다른 팔레트)."""
    objects = {m: ({"state": "on_pallet", "pallet": w} if w.startswith("loc_")
                   else {"state": "held_on_target", "slot": w}) for m, w in records.items()}
    request = ArrangementRequest(targets=targets, priority=tuple(targets),
                                 blocked_slots=tuple(blocked))
    return plan_arrangement(request, materials=MATERIALS, slot_names=SLOTS, objects=objects,
                            route_ok=route_ok, origins=ORIGINS,
                            route_path=route_path if paths else None)


class PalletPlannerTest(unittest.TestCase):
    def test_occupied_pallet_is_vacated_to_a_verified_free_slot_first(self):
        # "파란 자재를 초록 팔레트로": C가 3번 팔레트를 차지 → C를 빈 칸으로 먼저 뺀다.
        result = pplan({"material_b": "loc_pallet_3"}, {})
        self.assertEqual(steps(result), [
            ("transfer", "material_c", ORIGIN, "slot_1"),
            ("relocate", "material_b", ORIGIN, "loc_pallet_3")])
        self.assertEqual(result["steps"][1]["depends_on"], [1])
        self.assertIn("먼저 옮긴다", result["steps"][0]["reason"])
        self.assertIn("먼저 빠져야", result["steps"][1]["reason"])
        self.assertEqual(result["expected"]["material_b"], "loc_pallet_3")

    def test_no_free_place_to_vacate_asks(self):
        with self.assertRaises(ArrangementError) as caught:
            pplan({"material_b": "loc_pallet_3"}, {"material_a": "slot_1"},
                  blocked=("slot_2", "slot_3"))
        self.assertEqual(caught.exception.decision, "ASK")
        self.assertIn("빈 자리가 없습니다", str(caught.exception))

    def test_occupant_prefers_its_own_origin(self):
        # C가 1번 팔레트에 있고 자기 3번 팔레트가 비었다 → C는 원래 자리로.
        result = pplan({"material_b": "loc_pallet_1"},
                       {"material_a": "slot_1", "material_c": "loc_pallet_1"})
        self.assertEqual(steps(result), [
            ("return", "material_c", "loc_pallet_1", ORIGIN),
            ("relocate", "material_b", ORIGIN, "loc_pallet_1")])

    def test_material_on_other_pallet_can_be_picked_again(self):
        back = pplan({"material_b": ORIGIN},
                     {"material_b": "loc_pallet_3", "material_c": "slot_1"})
        self.assertEqual(steps(back), [("return", "material_b", "loc_pallet_3", ORIGIN)])
        onward = pplan({"material_b": "slot_2"},
                       {"material_b": "loc_pallet_3", "material_c": "slot_1"})
        self.assertEqual(steps(onward), [("relocate", "material_b", "loc_pallet_3", "slot_2")])

    def test_pallet_swap_breaks_cycle_through_a_temporary_slot(self):
        result = pplan({"material_b": "loc_pallet_3", "material_c": "loc_pallet_2"}, {})
        self.assertEqual(len(result["steps"]), 3)
        self.assertEqual(result["expected"], {"material_a": ORIGIN,
                                              "material_b": "loc_pallet_3",
                                              "material_c": "loc_pallet_2"})
        temp = result["steps"][0]["to"]
        self.assertTrue(temp.startswith("slot_"))

    def test_same_pallet_for_two_materials_blocks(self):
        with self.assertRaises(ArrangementError) as caught:
            pplan({"material_b": "loc_pallet_1", "material_a": ORIGIN}, {})
        self.assertEqual(caught.exception.decision, "BLOCK")


class PathAwarePlannerTest(unittest.TestCase):
    """측정된 경로 여유(`fr3_2f85_workcell_path_clearance.json`)를 쓰는 계획."""

    def test_move_before_a_neighbour_arrives(self):
        result = pplan({"material_b": "slot_2", "material_a": "slot_1"}, {}, paths=True)
        # A(1번 팔레트 → 칸 1)는 칸 2를 스친다 → B가 칸 2에 오기 전에 A부터.
        self.assertEqual([s["material"] for s in result["steps"]],
                         ["material_a", "material_b"])
        self.assertEqual(result["steps"][1]["depends_on"], [1])
        self.assertIn("오기 전에", result["steps"][0]["reason"])

    def test_return_after_the_neighbour_leaves(self):
        result = pplan({"material_a": ORIGIN, "material_b": ORIGIN},
                       {"material_a": "slot_1", "material_b": "slot_2"}, paths=True)
        self.assertEqual(steps(result), [("return", "material_b", "slot_2", ORIGIN),
                                         ("return", "material_a", "slot_1", ORIGIN)])
        self.assertIn("빠진 뒤", result["steps"][1]["reason"])

    def test_stationary_material_in_the_way_forces_a_detour(self):
        result = pplan({"material_a": "slot_3"},
                       {"material_a": "slot_1", "material_b": "slot_2"}, paths=True)
        self.assertEqual(steps(result), [
            ("relocate", "material_a", "slot_1", "loc_pallet_2"),
            ("relocate", "material_a", "loc_pallet_2", "slot_3")])
        self.assertIn("B자재가 경로를 막는다", result["steps"][0]["reason"])

    def test_no_clear_detour_blocks(self):
        # 모든 경로가 칸 2를 스친다고 가정한 판정기 — 우회할 길이 없으면 BLOCK.
        objects = {"material_a": {"state": "held_on_target", "slot": "slot_1"},
                   "material_b": {"state": "held_on_target", "slot": "slot_2"}}
        with self.assertRaises(ArrangementError) as caught:
            plan_arrangement(ArrangementRequest(targets={"material_a": "slot_3"},
                                                priority=("material_a",)),
                             materials=MATERIALS, slot_names=SLOTS, objects=objects,
                             route_ok=route_ok, origins=ORIGINS,
                             route_path=lambda m, h, t: frozenset({"slot_2"}))
        self.assertEqual(caught.exception.decision, "BLOCK")
        self.assertIn("경로를 막는다", str(caught.exception))


class StartTransferTest(GoalsBase):
    def test_routes_dispatch_to_the_right_runner(self):
        self.hold("material_a", "slot_1")
        job = self.jobs.start_transfer("material_a", "slot_1", "slot_3")
        argv = self.popen.calls[-1]["argv"]
        self.assertIn("--move-from-slot", argv)
        self.assertEqual(argv[argv.index("--move-to-slot") + 1], "slot_3")
        self.assertEqual(job["transfer"]["route"], "conveyor_slot->conveyor_slot")
        self.assertEqual(job["slot_label"], "컨베이어 1번 위치 → 컨베이어 3번 위치")

    def test_contract_refusals(self):
        self.hold("material_a", "slot_1")
        cases = (
            (("material_b", "loc_pallet_2", "loc_pallet_3"), 409, "destination_occupied"),
            (("material_a", "slot_2", "slot_3"), 400, "source_mismatch"),
            (("material_b", "loc_pallet_2", "slot_1"), 409, "destination_occupied"),
            (("material_a", "slot_1", "slot_1"), 409, "same_location"),
        )
        for args, status, code in cases:
            with self.subTest(args=args):
                with self.assertRaises(SimDemoJobError) as caught:
                    self.jobs.start_transfer(*args)
                self.assertEqual(caught.exception.status, status)
                self.assertIn(code, str(caught.exception))
        with self.assertRaises(SimDemoJobError) as caught:
            self.jobs.start_transfer("material_a", "slot_1", "slot_2", blocked=("slot_2",))
        self.assertIn("destination_blocked", str(caught.exception))
        self.assertEqual(len(self.popen.calls), 0)

    def test_pallet_routes_dispatch_to_route_runner(self):
        self.hold("material_a", "slot_1")
        job = self.jobs.start_transfer("material_b", "loc_pallet_2", "loc_pallet_1")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--route-from") + 1], "loc_pallet_2")
        self.assertEqual(argv[argv.index("--route-to") + 1], "loc_pallet_1")
        self.assertEqual(job["transfer"]["route"], "pallet->pallet")

    def test_goal_uses_direct_move_and_verifies_destination_slot(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_ARRANGE, {"targets": {"material_a": "slot_3"}})
        self.assertEqual([s["action"] for s in goal["plan"]], ["move"])
        self.goals.confirm(goal["goal_id"], "confirm")

        def lands(_seconds):
            self.hold("material_a", "slot_3")
            self.popen.procs[-1].code = 0
        self.goals._sleep = lands
        self.goals.run(goal["goal_id"])
        self.assertEqual(self.goals.get(goal["goal_id"])["status"], "completed")

    def test_goal_step_fails_if_move_lands_elsewhere(self):
        self.hold("material_a", "slot_1")
        goal = self.goals.create(GOAL_ARRANGE, {"targets": {"material_a": "slot_3"}})
        self.goals.confirm(goal["goal_id"], "confirm")

        def lands_wrong(_seconds):
            self.hold("material_a", "slot_2")
            self.popen.procs[-1].code = 0
        self.goals._sleep = lands_wrong
        self.goals.run(goal["goal_id"])
        self.assertEqual(self.goals.get(goal["goal_id"])["status"], "failed")


class TransferRouteCommandTest(GoalsBase):
    """텍스트 → 칸 이동 계획(경로 경유)."""

    def send(self, text):
        import asyncio, types
        from server.routes import sim_demo

        async def read_body(_receive):
            return {"mode": "simulation_demo", "source": "text", "utterance": text}
        ctx = types.SimpleNamespace(runtime=types.SimpleNamespace(
            sim_demo_jobs=self.jobs, sim_demo_goals=self.goals,
            sim_demo_disabled_reason=None), read_body=read_body)
        status, _, raw = asyncio.run(sim_demo.handle(ctx, "POST", "/v1/sim-demo/command",
                                                     None, {}))
        return status, json.loads(raw)

    def test_single_material_slot_move_becomes_direct_move_goal(self):
        self.hold("material_a", "slot_1")
        for text in ("A자재를 컨베이어 3번으로 옮겨줘",
                     "컨베이어 1번에 있는 A자재를 3번 칸으로 옮겨"):
            with self.subTest(text=text):
                status, payload = self.send(text)
                self.assertEqual(payload["decision"], "CONFIRM_GOAL")
                self.assertEqual([(s["action"], s["from"], s["to"])
                                  for s in payload["goal"]["plan"]],
                                 [("move", "slot_1", "slot_3")])
                self.goals.confirm(payload["goal"]["goal_id"], "cancel")
        self.assertEqual(self.popen.calls, [])

    def test_stated_source_mismatch_asks(self):
        self.hold("material_a", "slot_1")
        status, payload = self.send("A자재를 컨베이어 2번에서 3번으로 옮겨줘")
        self.assertEqual(payload["decision"], "ASK")
        self.assertIn("관측상", payload["reason"])

    def test_occupied_destination_plans_prerequisite(self):
        self.hold("material_a", "slot_1")
        status, payload = self.send("B자재를 컨베이어 1번 위치에 놓아줘")
        self.assertEqual(payload["decision"], "CONFIRM_GOAL")
        plan = payload["goal"]["plan"]
        self.assertEqual([(s["action"], s["material"]) for s in plan],
                         [("return", "material_a"), ("transfer", "material_b")])
        self.assertEqual(plan[1]["depends_on"], [1])

    def test_other_pallet_is_planned_when_verified_and_free(self):
        # 2026-09-25 요구 변경: 비어 있는 다른 팔레트는 검증된 자세로 목적지가 된다.
        self.hold("material_b", "slot_2")
        self.hold("material_a", "slot_1")
        status, payload = self.send("B자재를 컨베이어 2번에서 1번 팔레트로 옮겨줘")
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM_GOAL"))
        self.assertEqual([(s["action"], s["from"], s["to"]) for s in payload["goal"]["plan"]],
                         [("relocate", "slot_2", "loc_pallet_1")])
        self.goals.confirm(payload["goal"]["goal_id"], "cancel")
        status, payload = self.send("B자재를 컨베이어 2번에서 2번 팔레트로 옮겨줘")
        self.assertEqual([s["action"] for s in payload["goal"]["plan"]], ["return"])


if __name__ == "__main__":
    unittest.main()
