"""일반 경로 pick/place(시뮬레이션 셀 전용) — Profile·관문·transfer 묶음·실행 판정.

Gazebo 없이 결정적으로 본다. 실행기는 `SimDemoJobs`에 가짜 Popen을 넣고, 실행기가 쓰는
보고서를 테스트가 직접 써서 결과 판정만 검증한다(실제 이송은 E2E가 본다).
"""

from __future__ import annotations

import copy
import json
import sys
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_capability_profile  # noqa: E402
from core.execution_state import ExecutionState  # noqa: E402
from core.reason_codes import ReasonCode  # noqa: E402
from core.robot_profile import RobotProfileError  # noqa: E402
from core.sim_profile import apply_to_composite, load_simulation_profile  # noqa: E402
from core.task_plan import TaskStep  # noqa: E402
from server.sim_demo_jobs import SimDemoJobs  # noqa: E402
from server.sim_pick_place import (  # noqa: E402
    TransferUnit,
    _base_composite,
    execute_transfer,
    gate_transfer,
    open_simulation_pick_place,
    resolve_transfer,
    transfer_units,
)
from tests.unit.test_sim_demo_web import GRASP, WORKCELL, JobsBase  # noqa: E402
from tests.unit.test_simulation_demo_checkpoint import completed_result  # noqa: E402
from validation import pick_place_gate  # noqa: E402
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)

PROFILES = ROOT / "config/profiles"
SIM_PATH = PROFILES / "simulation/fr3wms_2f85_gazebo_sim.json"
MANIFEST = json.loads((ROOT / "config/workcell/active.json").read_text(encoding="utf-8"))
WORKCELL_PROFILE = load_capability_profile(json.loads(
    (PROFILES / "fr3wms_2f85_workcell_capability.json").read_text(encoding="utf-8")))
# 그리퍼 검증 기록은 작업 셀 PC에서 측정해 만든다(reports/는 Git 밖). 없으면 이 모듈을 건너뛴다 —
# 새로 받은 저장소에서 가져오기 오류로 전체 테스트가 깨지지 않게, 대신 건너뛴 이유를 남긴다.
VERIFICATION_PATH = ROOT / "reports/workcell/gripper_control.json"
if not VERIFICATION_PATH.is_file():
    raise unittest.SkipTest(f"그리퍼 검증 기록이 없다: {VERIFICATION_PATH.relative_to(ROOT)}"
                            " (scripts/verify_workcell_gripper.py가 작업 셀에서 만든다)")
VERIFICATION = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))
SIM = json.loads(SIM_PATH.read_text(encoding="utf-8"))


def opened(manifest=MANIFEST, path=SIM_PATH):
    return open_simulation_pick_place(
        manifest=manifest, sim_profile_path=path, profile_dir=PROFILES,
        workcell_profile=WORKCELL_PROFILE, verification=VERIFICATION)


def steps(*rows):
    return tuple(TaskStep(skill, dict(args)) for skill, args in rows)


TRANSFER_A = steps(("move", {"to": "loc_pallet_1"}),
                   ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                   ("move", {"to": "loc_conveyor"}),
                   ("place", {"object": "mat_a", "to": "loc_conveyor"}),
                   ("home", {}))


class SimulationProfileTest(unittest.TestCase):
    def test_profile_fills_only_a_copy_real_profile_stays_blocked(self):
        sim = load_simulation_profile(SIM)
        real = _base_composite(PROFILES, "fr3wms_with_2f85")
        self.assertEqual(set(real.blocking_items),
                         {"arm.payload", "gripper.pad_aperture_closed", "mounting.rpy.yaw"})
        copy_ = apply_to_composite(real, sim)
        self.assertEqual(copy_.blocking_items, ())
        self.assertFalse(copy_.verified)
        self.assertEqual(copy_.environment.value, "simulation")
        self.assertIn("+sim", copy_.arm.arm_profile_version)
        # 원본(실물) 구성은 그대로 막혀 있다.
        self.assertIsNone(real.arm.payload.value)
        self.assertIsNone(real.mounting.rpy[2].value)
        on_disk = json.loads((PROFILES / "fr3wms_arm.json").read_text(encoding="utf-8"))
        self.assertIsNone(on_disk["payload"]["value"])

    def test_values_carry_simulation_provenance(self):
        sim = load_simulation_profile(SIM)
        for key, item in sim.values.items():
            self.assertEqual(item.provenance.source_kind, "simulation_measurement", key)
            self.assertIn("sim_profile_evidence.json", item.provenance.source, key)
        self.assertEqual(sim.max_payload_kg, max(sim.verified_materials_kg.values()))

    def test_refuses_promotion_or_foreign_evidence(self):
        cases = {
            "real environment": lambda d: d.update(environment="real"),
            "hardware claim": lambda d: d.update(real_hardware_claim=True),
            "not simulated": lambda d: d["applies_to"].update(is_simulated=False),
            "datasheet source": lambda d: d["values"]["arm.payload"]["provenance"].update(
                source_kind="datasheet"),
            "unknown item": lambda d: d["values"].update(
                {"arm.reach": copy.deepcopy(d["values"]["arm.payload"])}),
            "payload above verified": lambda d: d["payload_scope"].update(max_kg=5.0),
            "no verified materials": lambda d: d["payload_scope"].update(
                verified_materials_kg={}),
        }
        for label, mutate in cases.items():
            payload = copy.deepcopy(SIM)
            mutate(payload)
            with self.subTest(label), self.assertRaises(RobotProfileError):
                load_simulation_profile(payload)

    def test_payload_allows_only_verified_range(self):
        sim = load_simulation_profile(SIM)
        self.assertTrue(sim.payload_allows("material_a", 0.2)[0])
        self.assertFalse(sim.payload_allows("heavy", 0.25)[0])
        self.assertFalse(sim.payload_allows("unknown", None)[0])


class OpeningTest(unittest.TestCase):
    def test_simulation_cell_opens_pick_place_with_new_version(self):
        result = opened()
        self.assertTrue(result.enabled, result.detail)
        profile = result.profile
        self.assertIn("pick", profile.supported_skills)
        self.assertIn("place", profile.supported_skills)
        self.assertNotEqual(profile.profile_version, WORKCELL_PROFILE.profile_version)
        self.assertEqual(profile.payload_kg, 0.2)
        self.assertEqual(profile.gripper.grasp_aperture_m, SIM["values"][
            "gripper.pad_aperture_closed"]["value"])
        self.assertNotIn("gated_skills", profile.extras)
        self.assertEqual(result.gate.environment, "simulation")
        self.assertTrue(result.gate.enabled)

    def test_not_opened_off_simulation(self):
        for label, manifest in (
                ("real cell", {**MANIFEST, "is_simulated": False}),
                ("hardware verified", {**MANIFEST, "real_hardware_verified": True}),
                ("other adapter", {**MANIFEST, "adapter_module": "robots.hardware"})):
            with self.subTest(label):
                result = opened(manifest)
                self.assertFalse(result.enabled)
                self.assertIsNone(result.profile)

    def test_missing_profile_file_keeps_it_closed(self):
        result = opened(path=ROOT / "config/profiles/simulation/missing.json")
        self.assertFalse(result.enabled)
        self.assertIsNone(result.profile)

    def test_real_gate_is_unchanged(self):
        real = _base_composite(PROFILES, "fr3wms_with_2f85")
        gate = pick_place_gate.evaluate(mounting=real.mounting, verification=VERIFICATION)
        self.assertFalse(gate.enabled)
        self.assertIn("mounting_transform", [c.key for c in gate.blocking])

    def test_simulation_gate_needs_each_simulation_fact(self):
        sim = load_simulation_profile(SIM)
        mounting = apply_to_composite(_base_composite(PROFILES, "fr3wms_with_2f85"),
                                      sim).mounting
        base = sim.to_dict()
        for label, change, key in (
                ("grasp", {"grasp_observation": {}}, "aperture_or_grasp_observation"),
                ("revalidation", {"revalidation": {}}, "plan_collision_revalidation"),
                ("coupling", {"coupling_model_mass_kg": 0.5}, "masses_and_inertia")):
            with self.subTest(label):
                gate = pick_place_gate.evaluate_simulation(
                    mounting=mounting, verification=VERIFICATION,
                    simulation={**base, **change})
                self.assertFalse(gate.enabled)
                self.assertIn(key, [c.key for c in gate.blocking])
        with self.assertRaises(ValueError):
            pick_place_gate.evaluate_simulation(
                mounting=mounting, verification=VERIFICATION,
                simulation={**base, "real_hardware_claim": True})


class TransferShapeTest(unittest.TestCase):
    def test_standard_transfer_covers_moves_pick_and_place(self):
        shape = transfer_units(TRANSFER_A)
        self.assertIsNone(shape.problem)
        unit = shape.units[0]
        self.assertEqual((unit.material, unit.source, unit.destination),
                         ("mat_a", "loc_pallet_1", "loc_conveyor"))
        self.assertEqual(unit.steps, (1, 2, 3, 4))
        self.assertEqual(shape.others, (5,))

    def test_leading_home_is_allowed_moves_elsewhere_are_not(self):
        ok = transfer_units((TaskStep("home"), *TRANSFER_A))
        self.assertIsNone(ok.problem)
        bad = transfer_units((TaskStep("move", {"to": "loc_pallet_3"}), *TRANSFER_A[1:]))
        self.assertIsNotNone(bad.problem)

    def test_shapes_that_are_not_one_transfer_are_refused(self):
        cases = {
            "pick only": steps(("move", {"to": "loc_pallet_1"}),
                               ("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                               ("home", {})),
            "different object": steps(("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                                      ("place", {"object": "mat_b", "to": "loc_conveyor"})),
            "two transfers": (*TRANSFER_A[:4], *steps(
                ("pick", {"object": "mat_b", "from": "loc_pallet_2"}),
                ("place", {"object": "mat_b", "to": "loc_conveyor"}))),
            "wrong move between": steps(("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                                        ("move", {"to": "loc_pallet_3"}),
                                        ("place", {"object": "mat_a", "to": "loc_conveyor"})),
            "home while holding": steps(("pick", {"object": "mat_a", "from": "loc_pallet_1"}),
                                        ("home", {}),
                                        ("place", {"object": "mat_a", "to": "loc_conveyor"})),
        }
        for label, plan_steps in cases.items():
            with self.subTest(label):
                shape = transfer_units(plan_steps)
                self.assertFalse(shape.units)
                self.assertIsNotNone(shape.problem)

    def test_plans_without_pick_place_are_untouched(self):
        shape = transfer_units(steps(("move", {"to": "loc_pallet_1"}), ("home", {})))
        self.assertIsNone(shape.problem)
        self.assertEqual(shape.units, ())


class ContractBase(JobsBase):
    def setUp(self):
        super().setUp()
        self.jobs = SimDemoJobs(workcell=WORKCELL, grasp_config=GRASP,
                                state_path=self.state_path, jobs_dir=self.tmp / "jobs",
                                stop_request=self.tmp / "stop.json", popen=self.popen,
                                environ={"PATH": "/usr/bin"})
        self.state = SimulationDemoState(self.state_path)

    def hold(self, model, slot):
        self.state.record_run(policy=POLICY_DEMO_HOLD, model=model, result=completed_result(),
                              final_pose_m=(0.25, -0.5, 0.75), restored=None, slot=slot)


class ResolveTransferTest(ContractBase):
    def test_conveyor_destination_takes_first_free_verified_slot(self):
        self.hold("material_b", "slot_1")
        result = resolve_transfer(self.jobs, TransferUnit("mat_a", "loc_pallet_1",
                                                          "loc_conveyor", (1, 2, 3, 4)))
        self.assertEqual(result["decision"], "ALLOW", result["detail"])
        self.assertEqual((result["material_model"], result["source"], result["destination"]),
                         ("material_a", "loc_pallet_1", "slot_2"))

    def test_conveyor_source_uses_the_recorded_slot(self):
        self.hold("material_a", "slot_3")
        result = resolve_transfer(self.jobs, TransferUnit("mat_a", "loc_conveyor",
                                                          "loc_pallet_1", (1, 2, 3, 4)))
        self.assertEqual(result["decision"], "ALLOW", result["detail"])
        self.assertEqual(result["source"], "slot_3")

    def test_wrong_source_asks_instead_of_fixing_the_plan(self):
        for unit in (TransferUnit("mat_a", "loc_conveyor", "loc_pallet_1", (1,)),
                     TransferUnit("mat_a", "loc_pallet_2", "loc_conveyor", (1,))):
            with self.subTest(unit.source):
                result = resolve_transfer(self.jobs, unit)
                self.assertEqual(result["decision"], "ASK")

    def test_blocks(self):
        self.hold("material_b", "slot_1")
        self.hold("material_c", "slot_2")
        cases = {
            "unknown material": TransferUnit("mat_x", "loc_pallet_1", "loc_conveyor", (1,)),
            "same location": TransferUnit("mat_a", "loc_pallet_1", "loc_pallet_1", (1,)),
            "unknown location": TransferUnit("mat_a", "loc_pallet_1", "loc_pallet_9", (1,)),
        }
        for label, unit in cases.items():
            with self.subTest(label):
                self.assertEqual(resolve_transfer(self.jobs, unit)["decision"], "BLOCK")
        self.state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                              result=completed_result(), final_pose_m=(0.05, -0.5, 0.75),
                              restored=None, slot="slot_3")
        full = resolve_transfer(self.jobs, TransferUnit("mat_a", "loc_conveyor", "loc_conveyor",
                                                        (1,)))
        self.assertEqual(full["decision"], "BLOCK")


class GateTransferTest(ContractBase):
    def runtime(self, **sim_changes):
        result = opened()
        if sim_changes:
            payload = copy.deepcopy(SIM)
            payload["payload_scope"].update(sim_changes)
            payload["values"]["arm.payload"]["value"] = sim_changes["max_kg"]
            result = SimpleNamespace(enabled=True, detail="", profile=result.profile,
                                     sim=load_simulation_profile(payload))
        return SimpleNamespace(sim_pick_place=result, sim_demo_jobs=self.jobs,
                               profile=result.profile)

    def plan(self, plan_steps=TRANSFER_A):
        return SimpleNamespace(steps=plan_steps)

    def test_allows_registered_transfer(self):
        result = gate_transfer(self.runtime(), self.plan())
        self.assertEqual(result["decision"], "ALLOW", result["detail"])
        self.assertIn("0.2 kg", result["payload"])

    def test_payload_outside_verified_range_blocks(self):
        light = self.runtime(max_kg=0.1, verified_materials_kg={"material_a": 0.1})
        result = gate_transfer(light, self.plan())
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIs(result["reason"], ReasonCode.CAPABILITY_LIMIT_EXCEEDED)

    def test_other_registered_profile_blocks(self):
        runtime = self.runtime()
        runtime.profile = WORKCELL_PROFILE          # 예: 열기 전 Profile·Fake로 떨어진 상태
        result = gate_transfer(runtime, self.plan())
        self.assertEqual(result["decision"], "BLOCK")
        self.assertIs(result["reason"], ReasonCode.ROBOT_PROFILE_MISMATCH)

    def test_not_a_simulation_cell_leaves_existing_judgement(self):
        self.assertIsNone(gate_transfer(SimpleNamespace(sim_pick_place=None), self.plan()))

    def test_closed_or_bad_shape_blocks(self):
        closed = SimpleNamespace(sim_pick_place=SimpleNamespace(enabled=False, detail="x"),
                                 sim_demo_jobs=self.jobs)
        self.assertEqual(gate_transfer(closed, self.plan())["decision"], "BLOCK")
        pick_only = self.plan(TRANSFER_A[:2])
        self.assertEqual(gate_transfer(self.runtime(), pick_only)["decision"], "BLOCK")

    def test_running_job_asks(self):
        self.jobs.start("transfer", "material_c")
        result = gate_transfer(self.runtime(), self.plan())
        self.assertEqual(result["decision"], "ASK")


class FakeView:
    def __init__(self, materials):
        self.materials = materials

    def sample(self):
        return {"materials": self.materials, "pose_time": time.time() + 1.0,
                "pose_age_sec": 0.0, "stale": False}


class ExecuteTransferTest(ContractBase):
    UNIT = TransferUnit("mat_a", "loc_pallet_1", "loc_conveyor", (1, 2, 3, 4))

    def setUp(self):
        super().setUp()
        self.assertTrue(self.jobs.reserve_goal("genexec_t", owner="general_execute"))
        self.addCleanup(lambda: self.jobs.release_goal("genexec_t"))

    def finish(self, status, *, code=0, slot="slot_1", hold=True, **report):
        """실행기가 끝난 것처럼 보고서를 쓴다. 작업이 아직 없으면 아무것도 하지 않는다."""
        job = self.jobs.running()
        if job is None:
            return
        Path(job["report_path"]).write_text(json.dumps(
            {"status": status, "detached": True, **report}), encoding="utf-8")
        if hold:
            self.hold("material_a", slot)
        self.popen.procs[-1].code = code

    def run_transfer(self, on_poll, view):
        runtime = SimpleNamespace(sim_demo_jobs=self.jobs, sim_view=view)
        return execute_transfer(runtime, self.UNIT, goal_id="genexec_t", interruption=on_poll)

    def test_general_transfer_uses_execution_snapshot_even_after_setting_zero(self):
        from core.motion_speed import MotionSpeedPolicy
        from server.sim_demo_motion import MotionSettings
        settings = MotionSettings(MotionSpeedPolicy.from_config(json.loads(
            (ROOT / "config/workcell/fr3_2f85_workcell_motion.json").read_text())), self.tmp / "speed.json")
        self.jobs.motion = settings
        snapshot = settings.snapshot()
        settings.set_percent(0)
        def poll():
            self.finish("simulation_transfer_completed")
            return None
        runtime = SimpleNamespace(sim_demo_jobs=self.jobs,
            sim_view=FakeView({"material_a": [0.251, -0.5, 0.75, 0, 0, 0, 1]}))
        result = execute_transfer(runtime, self.UNIT, goal_id="genexec_t", interruption=poll,
                                  speed_percent=snapshot)
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--speed-percent") + 1], "50")
        self.assertEqual(result.state, ExecutionState.COMPLETED, result.evidence)

    # 실측 콘솔(2026-10-07 복제 셀 simjob_5919645b587e)의 단계 줄.
    STAGES = ["안전 home", "팔레트 접근", "pre-grasp", "그리퍼 열기", "pick 접근", "그리퍼 닫기", "lift",
              "컨베이어 접근", "place 접근", "그리퍼 열기(해제)", "retreat", "안전 home 복귀"]

    def stage_line(self, n):
        return f"  [{n:2d}/12] {self.STAGES[n - 1]:<16} 오차 0.00010 rad · 도달=True\n"

    def test_progress_moves_through_plan_steps_in_order_while_running(self):
        """진행 표시: 이송 한 번이 끝나기 전에 계획 스텝 1→2→3→4를 차례로 알린다(끝에 한꺼번에가 아니다)."""
        seen, lines = [], iter(range(1, 13))

        def poll():
            job = self.jobs.running()
            n = next(lines, None)
            if job is not None and n is not None:
                with open(job["console_path"], "a", encoding="utf-8") as fh:
                    fh.write(self.stage_line(n))
            elif n is None:
                self.finish("simulation_transfer_completed")
            return None

        runtime = SimpleNamespace(sim_demo_jobs=self.jobs,
                                  sim_view=FakeView({"material_a": [0.251, -0.5, 0.75, 0, 0, 0, 1]}))
        with mock.patch("server.sim_pick_place.POLL_SEC", 0):
            result = execute_transfer(runtime, self.UNIT, goal_id="genexec_t", interruption=poll,
                                      on_progress=lambda done, stage: seen.append((done, stage and stage["label"])))
        self.assertEqual(result.state, ExecutionState.COMPLETED, result.evidence)
        done = [d for d, _ in seen]
        self.assertEqual(done, sorted(done))                       # 되돌아가지 않는다
        self.assertEqual(sorted(set(done)), [0, 1, 2, 3, 4])       # 0~4를 모두 지난다
        self.assertIn((2, "lift"), seen)
        self.assertIn((3, "그리퍼 열기(해제)"), seen)

    def test_progress_mapping_for_transfer_and_return_consoles(self):
        from server.sim_pick_place import transfer_progress
        rows = lambda labels: [{"label": x, "reached": True} for x in labels]  # noqa: E731
        self.assertEqual(transfer_progress([]), 0)
        self.assertEqual(transfer_progress(rows(self.STAGES[:3])), 0)       # 출발지 접근 중
        self.assertEqual(transfer_progress(rows(self.STAGES[:4])), 1)       # 그리퍼 열기 = 출발지 도착
        self.assertEqual(transfer_progress(rows(self.STAGES[:6])), 1)       # 닫았지만 아직 들지 않음
        self.assertEqual(transfer_progress(rows(self.STAGES[:7])), 2)
        self.assertEqual(transfer_progress(rows(self.STAGES[:10])), 3)
        self.assertEqual(transfer_progress(rows(self.STAGES[:11])), 4)
        back = ["안전 home", "컨베이어 접근", "pre-grasp(컨베이어)", "그리퍼 열기", "컨베이어 pick 접근", "그리퍼 닫기",
                "lift", "원래 팔레트 접근", "원래 슬롯 접근", "그리퍼 열기(해제)", "retreat", "안전 home 복귀"]
        self.assertEqual([transfer_progress(rows(back[:n])) for n in range(13)],
                         [0, 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 4, 4])
        # 도달하지 못한 단계는 세지 않는다.
        self.assertEqual(transfer_progress([{"label": "그리퍼 열기", "reached": False}]), 0)

    def test_success_needs_executor_record_and_fresh_pose(self):
        def poll():
            self.finish("simulation_transfer_completed")
            return None
        result = self.run_transfer(poll, FakeView({"material_a": [0.251, -0.5, 0.75, 0, 0, 0, 1]}))
        self.assertEqual(result.state, ExecutionState.COMPLETED, result.evidence)
        self.assertTrue(result.task_succeeded)
        self.assertEqual(result.evidence["final_position"]["destination"], "slot_1")
        self.assertTrue(self.popen.calls[-1]["argv"][1:3] == ["pallet_1", "material_a"])

    def test_pose_away_from_destination_fails(self):
        def poll():
            self.finish("simulation_transfer_completed")
            return None
        result = self.run_transfer(poll, FakeView({"material_a": [0.5, 0.2, 0.84, 0, 0, 0, 1]}))
        self.assertEqual(result.state, ExecutionState.FAILED)
        self.assertIs(result.reason, ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE)

    def test_no_pose_is_unverifiable_not_success(self):
        def poll():
            self.finish("simulation_transfer_completed")
            return None
        result = self.run_transfer(poll, None)
        self.assertEqual(result.state, ExecutionState.UNKNOWN)
        self.assertFalse(result.task_succeeded)

    def test_executor_failure_keeps_its_reason(self):
        def poll():
            self.finish("simulation_transfer_not_started", code=1, hold=False,
                        reason_codes=["exec.sim_fixture_failed"])
            return None
        result = self.run_transfer(poll, FakeView({}))
        self.assertEqual(result.state, ExecutionState.FAILED)
        self.assertIs(result.reason, ReasonCode.EXEC_SIM_FIXTURE_FAILED)

    def test_stop_reaches_the_executor_and_is_recorded(self):
        calls = {"n": 0}

        def poll():
            if self.jobs.running() is None:
                return None                      # 시작 전 확인
            calls["n"] += 1
            if calls["n"] >= 3:                  # 실행기가 정지로 끝난다
                self.finish("simulation_transfer_stopped", hold=False, detached=False)
            return ReasonCode.EXEC_STOPPED
        result = self.run_transfer(poll, FakeView({}))
        self.assertEqual(result.state, ExecutionState.STOPPED)
        self.assertTrue(result.evidence["stop_confirmed"])
        request = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        self.assertTrue(request["reason"].startswith("general_execute"))
        # 실행기가 늦게 뜬 경우의 재전송은 `SimDemoJobs.request_stop`이 맡는다
        # (tests/unit/test_free_spot.py StopReassertTest).

    def test_stop_before_start_does_not_launch(self):
        result = self.run_transfer(lambda: ReasonCode.EXEC_STOPPED, FakeView({}))
        self.assertFalse(result.request_accepted)
        self.assertIs(result.reason, ReasonCode.EXEC_STOPPED)
        self.assertEqual(self.popen.calls, [])

    def test_failure_after_stop_request_is_failure_not_stop(self):
        calls = {"n": 0}

        def poll():
            if self.jobs.running() is None:
                return None
            calls["n"] += 1
            if calls["n"] >= 2:                  # 정지 요청을 받은 뒤 실행기가 실패로 끝난다
                self.finish("simulation_transfer_incomplete", code=1, hold=False)
            return ReasonCode.EXEC_STOPPED
        result = self.run_transfer(poll, FakeView({}))
        self.assertEqual(result.state, ExecutionState.FAILED)
        self.assertTrue(result.evidence["stop_requested_before_end"])

    def test_success_needs_release_observation(self):
        def poll():
            self.finish("simulation_transfer_completed", detached=None)
            return None
        result = self.run_transfer(poll, FakeView({"material_a": [0.25, -0.5, 0.75, 0, 0, 0, 1]}))
        self.assertEqual(result.state, ExecutionState.UNKNOWN)
        self.assertFalse(result.task_succeeded)

    def test_forward_report_release_comes_from_criteria(self):
        from server.sim_pick_place import report_detached

        self.assertTrue(report_detached({"criteria": [{"key": "detached_at_target", "met": True}]}))
        self.assertFalse(report_detached({"detached": False}))
        self.assertIsNone(report_detached({"status": "x"}))

    def test_contract_rechecked_before_start(self):
        self.hold("material_a", "slot_2")      # 승인 뒤 자재가 이미 옮겨졌다
        result = self.run_transfer(lambda: None, FakeView({}))
        self.assertFalse(result.request_accepted)
        self.assertEqual(self.popen.calls, [])


if __name__ == "__main__":
    unittest.main()


class ResumeAvailabilityTest(unittest.TestCase):
    """복귀 이송 중 정지는 재개가 아니라 복구 대상이다(독립 검증 MAJOR-2)."""

    BASE = {"stop_confirmed": True, "object_state": "held", "stopped_stage": "place_approach",
            "mode": "forward", "model": "material_a"}

    def test_rules_match_the_executor(self):
        from validation.simulation_demo_state import checkpoint_resumable

        self.assertTrue(checkpoint_resumable(self.BASE)[0])
        for label, change in (("return mode", {"mode": "return_held_to_origin"}),
                              ("lift stage", {"stopped_stage": "lift"}),
                              ("unconfirmed", {"stop_confirmed": False}),
                              ("not held", {"object_state": "pallet"})):
            with self.subTest(label):
                self.assertFalse(checkpoint_resumable({**self.BASE, **change})[0])

    def test_screen_does_not_offer_resume_for_a_return_stop(self):
        from server.sim_demo_jobs import available_actions

        status = {"available": True, "checkpoint": {**self.BASE, "mode": "return_held_to_origin",
                                                    "stopped_stage": "lift"},
                  "checkpoints": ["material_a"], "objects": {}}
        actions = available_actions(status, "material_a")
        self.assertFalse(actions["resume"])
        self.assertFalse(actions["resume_preflight"])
        self.assertTrue(actions["restore"])
