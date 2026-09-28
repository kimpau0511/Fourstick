"""Restart recovery for the durable sim-demo motion-job journal."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.sim_demo_jobs import ACTIVE_JOB_FILE, SimDemoJobError, SimDemoJobs
from tests.unit.test_sim_demo_web import FakePopen, WORKCELL


IDENTITY = {
    "pid": 4321,
    "boot_id": "boot-a",
    "start_ticks": 9876,
    "cmdline_sha256": "a" * 64,
}


class MutableProbe:
    def __init__(self, state="alive"):
        self.state = state

    def __call__(self, identity):
        return self.state, f"probe says {self.state}"


class PidFakePopen(FakePopen):
    def __call__(self, argv, **kwargs):
        proc = super().__call__(argv, **kwargs)
        proc.pid = IDENTITY["pid"]
        return proc


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.jobs_dir = self.tmp / "jobs"
        self.stop_path = self.tmp / "stop.json"
        self.report_path = self.jobs_dir / "simjob_old.json"
        self.console_path = self.jobs_dir / "simjob_old.console"

    def write_journal(self, **changes):
        record = {
            "version": 1,
            "job_id": "simjob_old",
            "process_identity": IDENTITY,
            "action": "transfer",
            "material": "material_a",
            "slot": "slot_1",
            "checkpoint_id": None,
            "goal_id": None,
            "started_at": 123.5,
            "paths": {"report": str(self.report_path),
                      "console": str(self.console_path)},
            "status": "running",
        }
        record.update(changes)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / ACTIVE_JOB_FILE).write_text(
            json.dumps(record), encoding="utf-8")
        return record

    def jobs(self, probe, **kwargs):
        popen = kwargs.pop("popen", FakePopen())
        return SimDemoJobs(
            workcell=WORKCELL, state_path=self.tmp / "state.json",
            jobs_dir=self.jobs_dir, stop_request=self.stop_path,
            popen=popen, environ={"PATH": "/usr/bin"},
            process_probe=probe, **kwargs)

    def test_alive_process_is_adopted_and_blocks_second_motion_until_terminal(self):
        self.write_journal()
        probe = MutableProbe()
        jobs = self.jobs(probe)

        running = jobs.running()
        self.assertEqual(running["job_id"], "simjob_old")
        self.assertTrue(running["recovered"])
        self.assertIsNone(jobs.recovery_required)
        self.assertEqual(jobs.cell_execution.current().operation_id, "simjob_old")
        with self.assertRaises(SimDemoJobError) as caught:
            jobs.start("transfer", "material_b")
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue((self.jobs_dir / ACTIVE_JOB_FILE).exists())

        probe.state = "dead"
        self.assertIsNone(jobs.running())
        self.assertFalse((self.jobs_dir / ACTIVE_JOB_FILE).exists())
        self.assertIsNone(jobs.cell_execution.current())

    def test_dead_process_cleans_stale_journal(self):
        self.write_journal()
        jobs = self.jobs(MutableProbe("dead"))

        self.assertIsNone(jobs.running())
        self.assertIsNone(jobs.recovery_required)
        self.assertFalse((self.jobs_dir / ACTIVE_JOB_FILE).exists())
        started = jobs.start("transfer", "material_b")
        self.assertEqual(started["status"], "running")

    def test_uncertain_identity_exposes_recovery_required_and_blocks_motion(self):
        self.write_journal(process_identity={"pid": 4321})
        jobs = self.jobs(MutableProbe("uncertain"))

        recovery = jobs.status()["recovery_required"]
        self.assertTrue(recovery["required"])
        self.assertEqual(recovery["job_id"], "simjob_old")
        self.assertTrue((self.jobs_dir / ACTIVE_JOB_FILE).exists())
        with self.assertRaises(SimDemoJobError) as caught:
            jobs.start("return", "material_a")
        self.assertEqual(caught.exception.status, 409)

    def test_no_journal_is_normal_startup(self):
        jobs = self.jobs(MutableProbe("alive"), popen=PidFakePopen(),
                         process_identity=lambda pid: dict(IDENTITY))

        self.assertIsNone(jobs.recovery_required)
        self.assertIsNone(jobs.running())
        self.assertIsNone(jobs.status()["recovery_required"])
        started = jobs.start("transfer", "material_a")
        journal = json.loads(
            (self.jobs_dir / ACTIVE_JOB_FILE).read_text(encoding="utf-8"))
        self.assertEqual(journal["job_id"], started["job_id"])
        self.assertEqual(journal["process_identity"]["pid"], IDENTITY["pid"])
        self.assertEqual(journal["process_identity"]["boot_id"], IDENTITY["boot_id"])
        self.assertEqual(journal["process_identity"]["job_marker"],
                         started["report_path"])
        self.assertEqual(journal["status"], "running")

    def test_stop_remains_usable_for_adopted_job(self):
        self.write_journal()
        jobs = self.jobs(MutableProbe())

        result = jobs.request_stop(reason="restart_operator_stop")

        self.assertTrue(result["requested"])
        self.assertEqual(result["job_id"], "simjob_old")
        persisted = json.loads(self.stop_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["reason"], "restart_operator_stop")
        self.assertEqual(persisted["job_id"], "simjob_old")
        self.assertEqual(jobs.running()["job_id"], "simjob_old")


if __name__ == "__main__":
    unittest.main()
