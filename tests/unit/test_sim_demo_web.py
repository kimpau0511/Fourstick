"""웹 시뮬레이션 시연 작업(`server/sim_demo_jobs.py`, `/v1/sim-demo*`) 단위 검증.

- 기본은 꺼짐. 설정 + 시뮬레이션 작업 셀일 때만 실행기가 붙는다
- 작업은 검증된 시연 스크립트를 별도 프로세스로 **하나만** 띄운다
- 시연 정지·전체 정지(`/v1/stop`)는 정지 요청 파일을 쓴다(프로세스를 죽이지 않음)
- 가능 동작 안내는 시연 상태 기록에서 온다
- 일반 pick/place 차단은 그대로

**프로세스를 실제로 띄우지 않는다.** Popen은 기록용 대역이다.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.api import ApiError  # noqa: E402
from server.cell_execution import CellExecutionManager  # noqa: E402
from server.sim_demo_jobs import (  # noqa: E402
    SimDemoJobError,
    SimDemoJobs,
    available_actions,
    materials_from_workcell,
    parse_progress,
)
from validation.simulation_demo_state import (  # noqa: E402
    POLICY_DEMO_HOLD,
    SimulationDemoState,
)
from tests.unit.test_simulation_demo_checkpoint import (  # noqa: E402
    build,
    completed_result,
    stopped_result,
)

WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json")
                      .read_text(encoding="utf-8"))
GRASP = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_grasp.json")
                   .read_text(encoding="utf-8"))


class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


class FakePopen:
    def __init__(self):
        self.calls: list[dict] = []
        self.procs: list[FakeProc] = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), **kwargs})
        proc = FakeProc()
        self.procs.append(proc)
        return proc


class WaitableProc(FakeProc):
    def __init__(self):
        super().__init__()
        self.exited = threading.Event()

    def finish(self, code=0):
        self.code = code
        self.exited.set()

    def wait(self):
        self.exited.wait()
        return self.code


class JobsBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.popen = FakePopen()
        self.state_path = self.tmp / "state.json"
        self.jobs = SimDemoJobs(workcell=WORKCELL, state_path=self.state_path,
                                jobs_dir=self.tmp / "jobs",
                                stop_request=self.tmp / "stop.json",
                                popen=self.popen, environ={"PATH": "/usr/bin"})


class CellExecutionManagerTest(unittest.TestCase):
    def test_one_holder_and_token_scoped_release(self):
        manager = CellExecutionManager(clock=lambda: 123.0)
        first = manager.try_acquire(owner="general_execute", operation_id="plan_1")
        self.assertIsNotNone(first)
        self.assertEqual(first.to_dict(), {
            "owner": "general_execute", "operation_id": "plan_1",
            "started_at": 123.0,
        })
        self.assertIsNone(manager.try_acquire(
            owner="sim_demo", operation_id="simjob_1"))

        impostor = dataclasses.replace(first, token="not-the-holder")
        self.assertFalse(manager.release(impostor))
        self.assertIs(manager.current(), first)
        self.assertTrue(manager.release(first))
        self.assertIsNone(manager.current())


class JobRunnerTest(JobsBase):
    def test_materials_and_supports_come_from_workcell(self):
        materials = materials_from_workcell(WORKCELL)
        self.assertEqual({m: r["support_model"] for m, r in materials.items()},
                         {"material_a": "pallet_1", "material_b": "pallet_2",
                          "material_c": "pallet_3"})

    def test_transfer_launches_demo_with_hold_policy_and_gate(self):
        job = self.jobs.start("transfer", "material_b")
        call = self.popen.calls[0]
        argv = call["argv"]
        self.assertTrue(argv[0].endswith("scripts/demo_workcell_pick_place.sh"))
        self.assertEqual(argv[1:5], ["pallet_2", "material_b", "--cell-policy",
                                     "simulation_demo_hold"])
        self.assertEqual(argv[argv.index("--out") + 1], job["report_path"])
        self.assertEqual(call["env"]["FORSTICK2_SIM_PICK_PLACE_DEMO"], "1")
        self.assertTrue(call["start_new_session"])
        self.assertEqual(job["status"], "running")
        self.assertIs(job["is_simulated"], True)

    def test_argv_for_each_action(self):
        expected = {
            "return": ["pallet_1", "material_a", "--return-held-to-origin"],
            "resume_preflight": ["-", "material_a", "--resume-preflight"],
            "resume": ["-", "material_a", "--resume-checkpoint", "simckpt_x"],
            "restore": ["pallet_1", "material_a", "--restore-only"],
        }
        for action, argv in expected.items():
            with self.subTest(action=action):
                self.jobs.start(action, "material_a", checkpoint_id="simckpt_x")
                self.assertEqual(self.popen.calls[-1]["argv"][1:1 + len(argv)], argv)
                self.popen.procs[-1].code = 0

    def test_only_one_job_at_a_time(self):
        self.jobs.start("transfer", "material_a")
        with self.assertRaises(SimDemoJobError) as caught:
            self.jobs.start("transfer", "material_b")
        self.assertEqual(caught.exception.status, 409)
        self.popen.procs[0].code = 1
        self.jobs.start("restore", "material_a")   # 끝나면 다음 작업 가능

    def test_general_execution_lease_blocks_motion_job(self):
        lease = self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_busy")
        self.assertIsNotNone(lease)
        with self.assertRaises(SimDemoJobError) as caught:
            self.jobs.start("transfer", "material_a")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("general_execute", str(caught.exception))
        self.assertEqual(self.popen.calls, [])
        self.jobs.cell_execution.release(lease)

    def test_motion_job_holds_lease_until_process_finishes(self):
        job = self.jobs.start("transfer", "material_a")
        active = self.jobs.cell_execution.current()
        self.assertIsNotNone(active)
        self.assertEqual(active.owner, "sim_demo")
        self.assertEqual(active.operation_id, job["job_id"])
        self.assertIsNone(self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_waiting"))

        self.popen.procs[0].code = 0
        self.jobs.job(job["job_id"])
        self.assertIsNone(self.jobs.cell_execution.current())
        next_lease = self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_next")
        self.assertIsNotNone(next_lease)
        self.jobs.cell_execution.release(next_lease)

    def test_process_watcher_releases_without_status_poll(self):
        proc = WaitableProc()
        manager = CellExecutionManager()
        jobs = SimDemoJobs(
            workcell=WORKCELL, state_path=self.state_path,
            jobs_dir=self.tmp / "watched-jobs", stop_request=self.tmp / "stop.json",
            popen=lambda *args, **kwargs: proc, environ={"PATH": "/usr/bin"},
            cell_execution=manager)
        jobs.start("transfer", "material_a")
        self.assertIsNotNone(manager.current())

        proc.finish()
        deadline = time.monotonic() + 1.0
        while manager.current() is not None and time.monotonic() < deadline:
            proc.exited.wait(0.01)
            time.sleep(0.001)
        self.assertIsNone(manager.current())

    def test_reconcile_does_not_hold_motion_lease(self):
        self.jobs.start("reconcile")
        lease = self.jobs.cell_execution.try_acquire(
            owner="general_execute", operation_id="plan_during_reconcile")
        self.assertIsNotNone(lease)
        self.jobs.cell_execution.release(lease)

    def test_spawn_failure_releases_motion_lease(self):
        manager = CellExecutionManager()

        def broken_popen(*args, **kwargs):
            raise OSError("spawn failed")

        jobs = SimDemoJobs(
            workcell=WORKCELL, state_path=self.state_path,
            jobs_dir=self.tmp / "broken-jobs", stop_request=self.tmp / "stop.json",
            popen=broken_popen, environ={"PATH": "/usr/bin"},
            cell_execution=manager)
        with self.assertRaises(OSError):
            jobs.start("transfer", "material_a")
        self.assertIsNone(manager.current())

    def test_invalid_requests_are_rejected(self):
        for action, material, checkpoint in (("fly", "material_a", None),
                                             ("transfer", "material_z", None),
                                             ("resume", "material_a", None)):
            with self.subTest(action=action, material=material):
                with self.assertRaises(SimDemoJobError) as caught:
                    self.jobs.start(action, material, checkpoint_id=checkpoint)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.popen.calls, [])

    def test_finished_job_reads_report_and_progress(self):
        job = self.jobs.start("transfer", "material_a")
        Path(job["console_path"]).write_text(
            "[INFO] noise\n  [ 1/12] 안전 home          오차 0.00000 rad · 도달=True\n"
            "  [ 2/12] 팔레트 접근           오차 0.00038 rad · 도달=True\n"
            "판정: simulation_transfer_completed\n", encoding="utf-8")
        Path(job["report_path"]).write_text(json.dumps(
            {"status": "simulation_transfer_completed", "is_simulated": True}),
            encoding="utf-8")
        self.popen.procs[0].code = 0
        detail = self.jobs.job(job["job_id"])
        self.assertEqual(detail["status"], "finished")
        self.assertEqual(detail["exit_code"], 0)
        self.assertEqual(detail["report"]["status"], "simulation_transfer_completed")
        self.assertEqual([p["label"] for p in detail["progress"]],
                         ["안전 home", "팔레트 접근"])
        self.assertNotIn("[INFO] noise", detail["console_tail"])
        self.assertIn("판정: simulation_transfer_completed", detail["console_tail"])
        self.assertIsNone(self.jobs.running())

    def test_parse_progress(self):
        rows = parse_progress("  [10/12] 그리퍼 열기(해제)       오차 0.00009 rad · 도달=False")
        self.assertEqual(rows, [{"no": 10, "of": 12, "label": "그리퍼 열기(해제)",
                                 "error_rad": 0.00009, "reached": False}])


class AtomicSlotAssignmentTest(JobsBase):
    """All transfer callers converge on the lease-protected runner decision."""

    def setUp(self):
        super().setUp()
        self.jobs = SimDemoJobs(
            workcell=WORKCELL, grasp_config=GRASP, state_path=self.state_path,
            jobs_dir=self.tmp / "slot-jobs", stop_request=self.tmp / "stop.json",
            popen=self.popen, environ={"PATH": "/usr/bin"})

    def hold(self, model, slot):
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model=model, result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None, slot=slot)

    def test_transfer_ignores_legacy_caller_slot_and_persists_first_free(self):
        self.hold("material_a", "slot_1")
        job = self.jobs.start("transfer", "material_b", slot="slot_3")
        self.assertEqual(job["slot"], "slot_2")
        self.assertEqual(job["slot_label"], "컨베이어 2번 위치")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_2")
        self.assertEqual(self.jobs.status()["running_job"]["slot"], "slot_2")

    def test_competing_transfers_cannot_both_allocate(self):
        gate = threading.Barrier(3)

        def attempt(model):
            gate.wait()
            try:
                return self.jobs.start("transfer", model)
            except SimDemoJobError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt, model)
                       for model in ("material_a", "material_b")]
            gate.wait()
            results = [future.result() for future in futures]
        started = [row for row in results if isinstance(row, dict)]
        rejected = [row for row in results if isinstance(row, SimDemoJobError)]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["slot"], "slot_1")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].status, 409)
        self.assertEqual(len(self.popen.calls), 1)

    def test_full_conveyor_is_rejected_after_lease_acquisition(self):
        for model, slot in (("material_a", "slot_1"),
                            ("material_b", "slot_2"),
                            ("material_c", "slot_3")):
            self.hold(model, slot)
        with self.assertRaises(SimDemoJobError) as caught:
            self.jobs.start("transfer", "material_a")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("모두 찼습니다", str(caught.exception))
        self.assertEqual(self.popen.calls, [])
        self.assertIsNone(self.jobs.cell_execution.current())

    def test_return_uses_recorded_assignment_not_caller_slot(self):
        self.hold("material_b", "slot_2")
        job = self.jobs.start("return", "material_b", slot="slot_3")
        self.assertEqual(job["slot"], "slot_2")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_2")


class StopRequestTest(JobsBase):
    def test_stop_writes_request_for_running_job_without_killing(self):
        job = self.jobs.start("transfer", "material_a")
        result = self.jobs.request_stop(reason="sim_demo_stop")
        self.assertTrue(result["requested"])
        record = json.loads((self.tmp / "stop.json").read_text(encoding="utf-8"))
        self.assertEqual(record["job_id"], job["job_id"])
        self.assertTrue(record["request_id"].startswith("simstopreq_"))
        self.assertGreater(record["requested_at"], 0)
        self.assertIsNone(self.popen.procs[0].code)   # 죽이지 않았다
        self.assertEqual(self.jobs.job(job["job_id"])["stop_requested"]["request_id"],
                         record["request_id"])

    def test_no_running_job_writes_nothing(self):
        result = self.jobs.request_stop(reason="global_stop")
        self.assertFalse(result["requested"])
        self.assertFalse((self.tmp / "stop.json").exists())


class AvailableActionsTest(JobsBase):
    def status(self):
        return SimulationDemoState(self.state_path).status()

    def test_clean_cell_allows_transfer_only(self):
        actions = available_actions(self.status(), "material_a")
        self.assertEqual(actions, {"transfer": True, "return": False,
                                   "resume_preflight": False, "resume": False,
                                   "restore": False})

    def test_held_on_target_allows_return(self):
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_a", result=completed_result(),
            final_pose_m=(0.25, -0.5, 0.75), restored=None)
        actions = available_actions(self.status(), "material_a")
        self.assertTrue(actions["return"])
        self.assertFalse(actions["transfer"])
        self.assertFalse(available_actions(self.status(), "material_b")["transfer"])

    def test_held_checkpoint_allows_resume_and_restore(self):
        state = SimulationDemoState(self.state_path)
        state.record_run(policy=POLICY_DEMO_HOLD, model="material_a",
                         result=stopped_result(), final_pose_m=(0.4, 0, 1),
                         restored=None)
        state.record_checkpoint(build()[0])
        actions = available_actions(self.status(), "material_a")
        self.assertTrue(actions["resume"] and actions["resume_preflight"])
        self.assertTrue(actions["restore"])
        self.assertFalse(actions["return"])

    def test_running_job_disables_all_actions(self):
        self.jobs.start("transfer", "material_a")
        for row in self.jobs.status()["materials"]:
            self.assertFalse(any(row["actions"].values()))
        self.assertIsNotNone(self.jobs.status()["running_job"])


def body(payload):
    async def read(receive):
        return payload
    return read


class RoutesTest(JobsBase):
    def setUp(self):
        super().setUp()
        from server.config import ServerConfig
        from server.runtime import build_runtime

        config = dataclasses.replace(
            ServerConfig.from_env(), db_path=self.tmp / "web.sqlite3",
            enable_stt=False, llm_config_name="__absent__.json")
        self.runtime = build_runtime(config)
        self.addCleanup(self.runtime.repository.close)
        self.runtime.simulation_demo_state_path = self.state_path

    def call(self, method, path, payload=None, module=None):
        from server.routes import sim_demo

        ctx = types.SimpleNamespace(
            runtime=self.runtime, read_body=body(payload or {}),
            api=types.SimpleNamespace(stop=lambda session_id=None: {"ok": True}))
        return asyncio.run((module or sim_demo).handle(ctx, method, path, None, {}))

    def test_closed_when_the_cell_is_not_a_simulation_workcell(self):
        """작업 셀 Adapter가 없으면(=시뮬레이터에 붙지 않았으면) 열지 않는다."""
        self.assertIsNone(self.runtime.sim_demo_jobs)
        status, _, raw = self.call("GET", "/v1/sim-demo")
        payload = json.loads(raw)
        self.assertFalse(payload["enabled"])
        self.assertIn("작업 셀 Adapter", payload["reason"])
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/v1/sim-demo/jobs",
                      {"action": "transfer", "material": "material_a"})
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.popen.calls, [])

    def test_enabled_flow_start_detail_stop(self):
        self.runtime.sim_demo_jobs = self.jobs
        status, _, raw = self.call("POST", "/v1/sim-demo/jobs",
                                   {"action": "transfer", "material": "material_c"})
        self.assertEqual(status, 202)
        job = json.loads(raw)
        _, _, raw = self.call("GET", f"/v1/sim-demo/jobs/{job['job_id']}")
        self.assertEqual(json.loads(raw)["status"], "running")
        _, _, raw = self.call("POST", "/v1/sim-demo/stop")
        self.assertTrue(json.loads(raw)["requested"])
        _, _, raw = self.call("GET", "/v1/sim-demo")
        status_payload = json.loads(raw)
        self.assertTrue(status_payload["enabled"])
        self.assertIs(status_payload["is_simulated"], True)
        self.assertEqual(status_payload["running_job"]["job_id"], job["job_id"])
        with self.assertRaises(ApiError) as caught:
            self.call("POST", "/v1/sim-demo/jobs",
                      {"action": "transfer", "material": "material_a"})
        self.assertEqual(caught.exception.status, 409)

    def test_direct_card_cannot_choose_a_transfer_slot(self):
        self.runtime.sim_demo_jobs = SimDemoJobs(
            workcell=WORKCELL, grasp_config=GRASP, state_path=self.state_path,
            jobs_dir=self.tmp / "direct-card-jobs",
            stop_request=self.tmp / "stop.json", popen=self.popen,
            environ={"PATH": "/usr/bin"})
        status, _, raw = self.call(
            "POST", "/v1/sim-demo/jobs",
            {"action": "transfer", "material": "material_a", "slot": "slot_3"})
        job = json.loads(raw)
        self.assertEqual(status, 202)
        self.assertEqual(job["slot"], "slot_1")
        argv = self.popen.calls[-1]["argv"]
        self.assertEqual(argv[argv.index("--slot") + 1], "slot_1")

    def test_global_stop_also_requests_sim_demo_stop(self):
        from server.routes import execution

        self.runtime.sim_demo_jobs = self.jobs
        self.jobs.start("transfer", "material_a")
        _, _, raw = self.call("POST", "/v1/stop", {}, module=execution)
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["simulation_demo_stop"]["requested"])
        self.assertEqual(json.loads((self.tmp / "stop.json").read_text())["reason"],
                         "global_stop")

    def test_enabled_by_default_for_a_simulation_workcell(self):
        """환경변수 없이도 시뮬레이션 작업 셀이면 켜진다."""
        from server.config import ServerConfig

        self.assertTrue(ServerConfig.from_env().enable_sim_demo_web)
        self.assertTrue(ServerConfig().enable_sim_demo_web)

    def test_attach_gate(self):
        from server.runtime import _attach_sim_demo_jobs

        manifest = json.loads((ROOT / "config/workcell/active.json").read_text())

        def path(key):
            return (ROOT / "config/workcell" / manifest[key]).resolve()

        cases = (
            (dict(enable_sim_demo_web=False, enable_workcell_robot=True), manifest,
             "FORSTICK2_SIM_DEMO_WEB=0"),
            (dict(enable_sim_demo_web=True, enable_workcell_robot=False), manifest,
             "Adapter"),
            (dict(enable_sim_demo_web=True, enable_workcell_robot=True),
             {**manifest, "is_simulated": False}, "시뮬레이션이 아니다"),
        )
        for flags, man, text in cases:
            with self.subTest(flags=flags):
                runtime = types.SimpleNamespace(sim_demo_jobs=None,
                                                sim_demo_disabled_reason=None,
                                                simulation_demo_state_path=None)
                _attach_sim_demo_jobs(runtime, types.SimpleNamespace(**flags), man, path)
                self.assertIsNone(runtime.sim_demo_jobs)
                self.assertIn(text, runtime.sim_demo_disabled_reason)
        runtime = types.SimpleNamespace(sim_demo_jobs=None, sim_demo_disabled_reason="x",
                                        simulation_demo_state_path=self.state_path,
                                        cell_execution=CellExecutionManager())
        _attach_sim_demo_jobs(runtime, types.SimpleNamespace(
            enable_sim_demo_web=True, enable_workcell_robot=True), manifest, path)
        self.assertIsNotNone(runtime.sim_demo_jobs)
        self.assertIsNone(runtime.sim_demo_disabled_reason)
        self.assertIs(runtime.sim_demo_jobs.cell_execution, runtime.cell_execution)

    def test_general_pick_place_stays_blocked(self):
        from tests.unit.test_fr3_gazebo_adapter import build as build_adapter

        adapter, _, _, _ = build_adapter()
        adapter.connect(1.0)
        result = adapter.pick("mat_a", "loc_pallet_1", 1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "capability.profile_incomplete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
