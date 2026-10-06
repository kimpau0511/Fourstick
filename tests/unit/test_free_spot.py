"""표면 빈 위치 놓기 — 후보 형상·선택 규칙, 발화 감지, 기록, 실행기 인자, FK 재검사.

Gazebo·MoveIt 없이 결정적으로 본다. IK·FK는 실행 중인 셀의 URDF가 있을 때만 본다(없으면 건너뜀).
실제 놓기·안정성은 E2E(`md/exec-plans/2026-10-02-free-spot-place.md` 시험 A·B)가 본다.
"""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_demo_jobs import SimDemoJobError, SimDemoJobs, build_argv  # noqa: E402
from server.sim_demo_places import SURFACE, declared_places  # noqa: E402
from server.sim_free_spot import detect  # noqa: E402
from tests.unit.test_sim_demo_web import GRASP, WORKCELL, JobsBase  # noqa: E402
from tests.unit.test_simulation_demo_checkpoint import completed_result  # noqa: E402
from validation.free_spot import candidates, still_free, surface_from_config  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    ON_SURFACE,
    POLICY_DEMO_HOLD,
    SURFACE_SPOT,
    SimulationDemoState,
    checkpoint_resumable,
)

SURFACES = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_surfaces.json")
                      .read_text(encoding="utf-8"))
SIZES = {m: WORKCELL["models"][m]["size_m"] for m in ("material_a", "material_b", "material_c")}
#: 정렬 기준 손가락 여유(자재 반폭 + 열린 개구 반폭 + 여유) — 계산기와 같은 식.
GAP = 0.025 + 0.084997 / 2 + 0.01
ORIGINS = {"material_b": (0.25, -0.5, 0.75), "material_c": (0.5, -0.2, 0.84)}


def surface():
    return surface_from_config(WORKCELL, SURFACES, "surface_workbench")


def pick(others):
    found = candidates(surface(), material_size_m=SIZES["material_a"], others=others,
                       other_sizes=SIZES, sufficient_gap_m=GAP)
    return found, (found.candidates[0].x, found.candidates[0].y) if found.candidates else None


class SurfaceGeometryTest(unittest.TestCase):
    def test_workbench_top_and_resting_pallets(self):
        s = surface()
        self.assertAlmostEqual(s.top_z, 0.75)
        self.assertEqual(sorted(o.label for o in s.obstacles),
                         ["pallet_1.tray", "pallet_2.tray", "pallet_3.tray"])
        self.assertIsNotNone(s.region)       # 근거 있는 놓기 가능 띠

    def test_region_needs_evidence(self):
        bad = json.loads(json.dumps(SURFACES))
        bad["surfaces"]["surface_workbench"]["placement_region"].pop("source")
        with self.assertRaises(ValueError):
            surface_from_config(WORKCELL, bad, "surface_workbench")

    def test_unknown_or_not_free_spot_surface_refused(self):
        with self.assertRaises(ValueError):
            surface_from_config(WORKCELL, SURFACES, "surface_floor")
        bad = json.loads(json.dumps(SURFACES))
        bad["surfaces"]["surface_workbench"]["placement"] = "fixed"
        with self.assertRaises(ValueError):
            surface_from_config(WORKCELL, bad, "surface_workbench")

    def test_every_candidate_is_supported_and_clear_of_pallets(self):
        found, _ = pick(ORIGINS)
        s = surface()
        for c in found.candidates:
            self.assertGreaterEqual(c.x - 0.025 - s.area.x0, s.margin_m - 1e-9)
            self.assertLessEqual(c.x + 0.025, 0.42 - s.margin_m + 1e-9)   # 팔레트 앞 가장자리
            self.assertLessEqual(abs(c.y) + 0.025, 0.32 - s.margin_m + 1e-9)


class SelectionTest(unittest.TestCase):
    def test_different_layouts_choose_different_spots(self):
        _, l1 = pick(ORIGINS)
        _, l2 = pick({**ORIGINS, "material_c": (0.375, 0.0, 0.80)})
        _, l3 = pick({"material_b": (0.375, 0.0, 0.80), "material_c": (l2[0], l2[1], 0.80)})
        self.assertEqual(l1, (0.375, 0.0))
        self.assertNotEqual(l2, l1)
        self.assertNotIn(l3, (l1, l2))
        # 손가락 여유만큼 떨어진다.
        self.assertGreaterEqual(abs(l2[1]) - 0.05, GAP - 1e-9)

    def test_occupied_spots_are_rejected_and_counted(self):
        found, chosen = pick({**ORIGINS, "material_c": (0.375, 0.0, 0.80)})
        self.assertGreater(found.rejected["occupied"], 0)
        self.assertIn("material_c", found.occupants)
        self.assertGreaterEqual(abs(chosen[1]), 0.05 + 0.01)

    def test_materials_not_on_the_surface_do_not_occupy(self):
        found, _ = pick(ORIGINS)
        self.assertEqual(found.occupants, {})

    def test_unobserved_material_means_no_candidates(self):
        found, chosen = pick({**ORIGINS, "material_b": None})
        self.assertIsNone(chosen)
        self.assertIn("관측", found.problem)

    def test_no_space_when_the_band_is_full(self):
        crowd = {f"m{i}": (0.375, y, 0.80) for i, y in enumerate(
            [-0.27 + 0.06 * k for k in range(10)])}
        sizes = {**SIZES, **{m: SIZES["material_a"] for m in crowd}}
        found = candidates(surface(), material_size_m=SIZES["material_a"], others=crowd,
                           other_sizes=sizes, sufficient_gap_m=GAP)
        self.assertEqual(found.candidates, ())
        self.assertIsNotNone(found.problem)

    def test_still_free_detects_a_new_occupant(self):
        ok, _ = still_free(surface(), (0.375, 0.0), material_size_m=SIZES["material_a"],
                           others=ORIGINS, other_sizes=SIZES)
        self.assertTrue(ok)
        ok, why = still_free(surface(), (0.375, 0.0), material_size_m=SIZES["material_a"],
                             others={**ORIGINS, "material_c": (0.38, 0.01, 0.80)},
                             other_sizes=SIZES)
        self.assertFalse(ok)
        self.assertIn("material_c", why)


class DetectTest(unittest.TestCase):
    def detect(self, text):
        return detect(text, WORKCELL, SURFACES)

    def test_surface_without_exact_spot(self):
        for text in ("A자재를 작업대의 빈 곳에 놔", "에이 자재 작업대 남는 자리에 둬",
                     "A자재를 작업대에 올려줘"):
            with self.subTest(text):
                row = self.detect(text)
                self.assertEqual((row["surface_id"], row["material"]),
                                 ("surface_workbench", "material_a"))

    def test_stated_source(self):
        row = self.detect("A자재를 1번 팔레트에서 작업대 빈 곳에 놔")
        self.assertEqual(row["stated_source"], "loc_pallet_1")
        row = self.detect("B자재를 컨베이어에서 작업대 빈 곳에 놔")
        self.assertEqual(row["stated_source"], "loc_conveyor")

    def test_not_this_feature(self):
        for text in ("A자재를 컨베이어로 옮겨줘", "작업대 빈 곳 멈춰", "작업대 보여줘"):
            with self.subTest(text):
                self.assertIsNone(self.detect(text))

    def test_material_missing_or_many(self):
        self.assertIsNone(self.detect("작업대 빈 곳에 놔")["material"])
        self.assertIsNone(self.detect("A자재랑 B자재를 작업대 빈 곳에 놔")["material"])

    def test_surface_is_a_declared_place_for_the_classifier(self):
        places = declared_places(WORKCELL, (), None, SURFACES)
        surface_places = [p for p in places if p.kind == SURFACE]
        self.assertEqual([p.id for p in surface_places], ["surface_workbench"])
        self.assertFalse(surface_places[0].is_material_target)   # 기존 이송 경로 대상이 아니다


class RecordTest(JobsBase):
    SPOT = {"id": "spot:surface_workbench:375:0", "surface_id": "surface_workbench",
            "center_m": [0.375, 0.0, 0.8]}

    def record(self, *, stable, final=(0.3752, 0.0003, 0.8)):
        return SimulationDemoState(self.state_path).record_transfer(
            "material_a", source="loc_pallet_1", destination=self.SPOT["id"],
            destination_kind=SURFACE_SPOT, destination_center_m=self.SPOT["center_m"],
            own_origin="loc_pallet_1", completed=True, stop_requested=False, attached=True,
            final_pose_m=final, spot=self.SPOT, stable=stable)

    def test_arrival_needs_stability(self):
        attempt = self.record(stable=None)
        self.assertFalse(attempt["arrived"])
        attempt = self.record(stable=True)
        self.assertTrue(attempt["arrived"])
        row = SimulationDemoState(self.state_path).status()["objects"]["material_a"]
        self.assertEqual(row["state"], ON_SURFACE)
        self.assertEqual(row["spot"]["id"], self.SPOT["id"])

    def test_spot_location_counts_for_other_transfers(self):
        self.record(stable=True)
        jobs = SimDemoJobs(workcell=WORKCELL, grasp_config=GRASP, state_path=self.state_path,
                           jobs_dir=self.tmp / "jobs", stop_request=self.tmp / "s.json",
                           popen=self.popen, environ={"PATH": "/usr/bin"},
                           surfaces_config=SURFACES)
        world = jobs.transfer_world()
        self.assertEqual(world.location_of["material_a"], self.SPOT["id"])
        # 표면에 놓인 자재는 아직 다시 집지 않는다(계약이 자리 모름으로 막는다).
        _plan, found = jobs.check_transfer("material_a", self.SPOT["id"], "slot_1")
        self.assertTrue(found)

    def test_spot_checkpoint_is_not_resumable(self):
        ok, why = checkpoint_resumable({"stop_confirmed": True, "object_state": "held",
                                        "stopped_stage": "place_approach", "mode": "spot"})
        self.assertFalse(ok)
        self.assertIn("restore", why)


class SpotJobTest(JobsBase):
    def setUp(self):
        super().setUp()
        self.jobs = SimDemoJobs(workcell=WORKCELL, grasp_config=GRASP,
                                state_path=self.state_path, jobs_dir=self.tmp / "jobs",
                                stop_request=self.tmp / "s.json", popen=self.popen,
                                environ={"PATH": "/usr/bin"}, surfaces_config=SURFACES)

    SPEC = {"spot_id": "spot:surface_workbench:375:0", "surface_id": "surface_workbench",
            "center_m": [0.375, 0.0, 0.8], "source": "loc_pallet_1"}

    def test_argv_carries_the_spot_file(self):
        job = self.jobs.start("spot", "material_a", spot=self.SPEC)
        argv = self.popen.calls[-1]["argv"]
        self.assertIn("--route-to-spot", argv)
        path = Path(argv[argv.index("--route-to-spot") + 1])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["spot_id"],
                         self.SPEC["spot_id"])
        self.assertEqual(argv[argv.index("--route-from") + 1], "loc_pallet_1")
        self.assertEqual(job["spot"]["spot_id"], self.SPEC["spot_id"])

    def test_source_changed_since_confirmation_refuses(self):
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_a", result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None, slot="slot_1")
        with self.assertRaises(SimDemoJobError):
            self.jobs.start("spot", "material_a", spot=self.SPEC)
        self.assertEqual(self.popen.calls, [])

    def test_build_argv_requires_spot_and_source(self):
        with self.assertRaises(ValueError):
            build_argv("spot", {"support_model": "pallet_1", "model": "material_a"}, None)


URDF = Path("/tmp/forstick2_workcell/workcell/fr3wms_with_2f85.moveit.urdf")


@unittest.skipUnless(URDF.is_file(), "실행 중인 작업 셀 URDF가 없다")
class PlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from robots.fr3_gazebo import build_free_spot_planner

        cls.planner = build_free_spot_planner()

    def test_plan_poses_land_on_the_spot(self):
        plan = self.planner.plan(material="material_a", source="loc_pallet_1",
                                 surface_id="surface_workbench", surface_top_z=0.75,
                                 center_xy=(0.375, 0.0))
        self.assertTrue(hasattr(plan, "route"), getattr(plan, "detail", ""))
        self.assertEqual(self.planner.fk_check(plan.to_dict(), surface_top_z=0.75), [])
        self.assertEqual(plan.route.place.location, plan.spot_id)

    def test_tampered_joints_fail_fk(self):
        plan = self.planner.plan(material="material_a", source="loc_pallet_1",
                                 surface_id="surface_workbench", surface_top_z=0.75,
                                 center_xy=(0.375, 0.0)).to_dict()
        plan["place"]["joint_rad"] = dict(plan["place"]["joint_rad"], j1=plan["place"][
            "joint_rad"]["j1"] + 0.1)
        self.assertTrue(self.planner.fk_check(plan, surface_top_z=0.75))

    def test_unreachable_spot_is_refused(self):
        plan = self.planner.plan(material="material_a", source="loc_pallet_1",
                                 surface_id="surface_workbench", surface_top_z=0.75,
                                 center_xy=(1.1, 0.0))
        self.assertFalse(hasattr(plan, "route"))


if __name__ == "__main__":
    unittest.main()


import asyncio  # noqa: E402
import types  # noqa: E402

from server.sim_demo_confirm import ConfirmStore  # noqa: E402
from server.sim_demo_context import DialogueContexts  # noqa: E402


class FakeService:
    """경로 배선만 본다. 계산·검증은 위 테스트와 E2E가 본다."""

    def __init__(self, recheck_ok=True):
        self.recheck_ok = recheck_ok
        self.plans = []

    def plan(self, *, material, surface_id, stated_source):
        self.plans.append((material, surface_id, stated_source))
        return {"decision": "CONFIRM", "summary": "A자재를 1번 팔레트에서 집어 작업대 빈 위치",
                "why": ["후보 1곳"], "selection": {"count": 1}, "source": "loc_pallet_1",
                "spot": {**SpotJobTest.SPEC, "material": material}}

    def recheck(self, spec):
        return (self.recheck_ok, "fixture recheck", {})


class RouteTest(JobsBase):
    def setUp(self):
        super().setUp()
        self.jobs = SimDemoJobs(workcell=WORKCELL, grasp_config=GRASP,
                                state_path=self.state_path, jobs_dir=self.tmp / "jobs",
                                stop_request=self.tmp / "s.json", popen=self.popen,
                                environ={"PATH": "/usr/bin"}, surfaces_config=SURFACES)
        self.service = FakeService()
        self.runtime = types.SimpleNamespace(
            sim_demo_jobs=self.jobs, sim_demo_goals=None, sim_demo_disabled_reason=None,
            sim_demo_confirm=ConfirmStore(ttl_sec=60), sim_free_spot=self.service,
            sim_demo_contexts=DialogueContexts(clock=lambda: 1000.0))

    def call(self, path, body):
        from server.routes import sim_demo

        async def read_body(_receive):
            return body
        ctx = types.SimpleNamespace(runtime=self.runtime, read_body=read_body,
                                    api=types.SimpleNamespace(require_session=lambda s: None))
        status, _, raw = asyncio.run(sim_demo.handle(ctx, "POST", path, None, {}))
        return status, json.loads(raw)

    def command(self, text):
        return self.call("/v1/sim-demo/command", {"mode": "simulation_demo", "source": "text",
                                                  "utterance": text, "session_id": "s1"})

    def test_rule_detection_gives_a_card_not_a_job(self):
        status, payload = self.command("A자재를 작업대의 빈 곳에 놔")
        self.assertEqual(payload["decision"], "CONFIRM")
        self.assertEqual(self.service.plans, [("material_a", "surface_workbench", None)])
        self.assertEqual(self.popen.calls, [])

    def test_confirm_rechecks_before_starting(self):
        _, card = self.command("A자재를 작업대의 빈 곳에 놔")
        self.service.recheck_ok = False
        status, payload = self.call("/v1/sim-demo/confirm",
                                    {"token": card["confirmation"]["token"], "action": "confirm"})
        self.assertEqual((status, payload["decision"]), (409, "BLOCK"))
        self.assertEqual(payload["confirm_rejection"], "recheck_failed")
        self.assertEqual(self.popen.calls, [])

    def test_confirm_starts_the_spot_job_after_recheck(self):
        _, card = self.command("A자재를 작업대의 빈 곳에 놔")
        status, payload = self.call("/v1/sim-demo/confirm",
                                    {"token": card["confirmation"]["token"], "action": "confirm"})
        self.assertEqual((status, payload["decision"]), (202, "RUN"))
        self.assertIn("--route-to-spot", self.popen.calls[-1]["argv"])

    def classifier(self, destination):
        return types.SimpleNamespace(
            client=None, min_confidence=0.7,
            classify=lambda *a, **k: types.SimpleNamespace(
                ok=True, intent="transfer", material_id="material_a",
                source_resource=None, destination_resource=destination, confidence=0.9,
                failure=None, reason="", model_id="fake", raw="", latency_sec=0.0))

    def test_classifier_cannot_substitute_an_unsaid_surface(self):
        # 실측: "바닥 빈 곳"을 모델이 작업대로 바꿔 읽었다.
        self.runtime.sim_demo_intent = self.classifier("surface_workbench")
        status, payload = self.command("A자재를 바닥 빈 곳에 놔")
        self.assertEqual(payload["decision"], "ASK")
        self.assertEqual(self.service.plans, [])

    def test_classifier_surface_is_used_when_said(self):
        self.runtime.sim_demo_intent = self.classifier("surface_workbench")
        # 규칙이 못 잡은 어순이라도 표면 이름이 있으면 서버 계산으로 간다.
        status, payload = self.command("작업대 쪽 아무데로 A자재 좀")
        self.assertIn(payload["decision"], ("CONFIRM", "ASK"))
        if payload["decision"] == "CONFIRM":
            self.assertEqual(self.service.plans[-1][1], "surface_workbench")


class StopReassertTest(JobsBase):
    def test_stop_is_rewritten_until_the_executor_is_armed(self):
        import server.sim_demo_jobs as module

        original = module.STOP_REASSERT_SEC
        module.STOP_REASSERT_SEC = 0.01
        self.addCleanup(setattr, module, "STOP_REASSERT_SEC", original)
        job = self.jobs.start("transfer", "material_a")
        first = self.jobs.request_stop(reason="test")
        time.sleep(0.1)
        second = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        self.assertNotEqual(first["request_id"], second["request_id"])   # 다시 썼다
        Path(job["console_path"]).write_text("[원본 로그] x\n", encoding="utf-8")
        time.sleep(0.1)
        settled = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        time.sleep(0.1)
        after = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        self.assertEqual(settled["request_id"], after["request_id"])     # 준비 뒤 멈췄다
        self.assertEqual(after["job_id"], job["job_id"])
