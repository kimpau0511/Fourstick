"""Gazebo 재시작 뒤 기록↔관측 정합 검증.

Gazebo를 다시 띄우면 world는 선언된 초기 자리로 돌아가지만 시연 기록
(`sim_demo_state.json`)은 파일에 남는다. 그 어긋남을 **관측으로** 맞춘다.

확인하는 것:

- 판정(`reconcile_findings`)은 원래 슬롯·컨베이어 측정 위치·그 밖·관측 불가를
  기존 `ORIGIN_TOLERANCE_M`으로만 가른다 — 새 허용치를 만들지 않는다
- 적용(`record_reconcile`)은 `at_origin`인 기록만 지운다. 관측하지 못했거나
  어느 자리도 아니면 **그대로 둔다**
- 작업 실행기는 자재를 고르지 않는 셀 전체 작업으로 읽기 전용 모드를 띄우고,
  기록이 없으면 기동 시 아무것도 띄우지 않는다

**프로세스를 띄우지 않는다.** Popen은 기록용 대역이다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from validation.simulation_demo_state import (  # noqa: E402
    HELD_ON_TARGET,
    ORIGIN_TOLERANCE_M,
    RECONCILE_AT_ORIGIN,
    RECONCILE_ELSEWHERE,
    RECONCILE_ON_CONVEYOR,
    RECONCILE_UNOBSERVED,
    SCHEMA,
    SimulationDemoState,
    reconcile_findings,
)
from tests.unit.test_sim_demo_web import JobsBase  # noqa: E402

ORIGIN = (0.5, 0.2, 0.84)
CONVEYOR = (0.25, -0.5, 0.75)


def records(state=HELD_ON_TARGET):
    return {"material_a": {"state": state, "pose_m": list(CONVEYOR)}}


class FindingsTest(unittest.TestCase):
    def find(self, observed, **kwargs):
        return reconcile_findings(
            records=kwargs.get("records", records()),
            observed={"material_a": observed},
            origins={"material_a": ORIGIN},
            conveyor_centers={"material_a": CONVEYOR})[0]

    def test_material_back_at_its_origin_is_a_stale_record(self):
        row = self.find(ORIGIN)
        self.assertEqual(row["verdict"], RECONCILE_AT_ORIGIN)
        self.assertEqual(row["origin_gap_m"], 0.0)

    def test_tolerance_is_the_existing_origin_tolerance(self):
        inside = (ORIGIN[0] + ORIGIN_TOLERANCE_M * 0.9, ORIGIN[1], ORIGIN[2])
        outside = (ORIGIN[0] + ORIGIN_TOLERANCE_M * 1.5, ORIGIN[1], ORIGIN[2])
        self.assertEqual(self.find(inside)["verdict"], RECONCILE_AT_ORIGIN)
        self.assertEqual(self.find(outside)["verdict"], RECONCILE_ELSEWHERE)

    def test_material_still_on_the_conveyor_keeps_the_record(self):
        row = self.find(CONVEYOR)
        self.assertEqual(row["verdict"], RECONCILE_ON_CONVEYOR)

    def test_somewhere_else_is_not_decided(self):
        row = self.find((1.2, 1.2, 1.2))
        self.assertEqual(row["verdict"], RECONCILE_ELSEWHERE)
        self.assertIn("원래 슬롯", row["detail"])

    def test_unobserved_pose_is_not_called_clean(self):
        row = self.find(None)
        self.assertEqual(row["verdict"], RECONCILE_UNOBSERVED)
        self.assertIsNone(row["observed_pose_m"])

    def test_findings_are_sorted_and_cover_every_record(self):
        rows = reconcile_findings(
            records={"material_b": {"state": HELD_ON_TARGET},
                     "material_a": {"state": HELD_ON_TARGET}},
            observed={"material_a": ORIGIN, "material_b": None},
            origins={"material_a": ORIGIN, "material_b": ORIGIN},
            conveyor_centers={})
        self.assertEqual([r["model"] for r in rows], ["material_a", "material_b"])


class ApplyTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"
        self.state = SimulationDemoState(self.path)

    def write(self, payload):
        # 실제 기록과 같은 시뮬레이터 축 불변식을 갖춘다(없으면 읽기가 거부한다).
        self.path.write_text(json.dumps(
            {**payload, "schema": SCHEMA, "is_simulated": True,
             "real_hardware_ready": False, "real_hardware_verified": False},
            ensure_ascii=False), encoding="utf-8")

    def test_only_at_origin_records_are_removed(self):
        self.write({"objects": {
            "material_a": {"state": HELD_ON_TARGET},
            "material_b": {"state": HELD_ON_TARGET},
            "material_c": {"state": HELD_ON_TARGET}}})
        applied = self.state.record_reconcile([
            {"model": "material_a", "verdict": RECONCILE_AT_ORIGIN},
            {"model": "material_b", "verdict": RECONCILE_ON_CONVEYOR},
            {"model": "material_c", "verdict": RECONCILE_UNOBSERVED},
        ], source="test")
        self.assertEqual(applied["cleared"], ["material_a"])
        self.assertEqual(sorted(self.state.status()["objects"]),
                         ["material_b", "material_c"])

    def test_checkpoint_of_a_stale_record_goes_with_it(self):
        self.write({"objects": {"material_a": {"state": HELD_ON_TARGET}},
                    "checkpoints": {"material_a": {"checkpoint_id": "simckpt_1",
                                                   "model": "material_a"}},
                    "resume_preflight": {"material_a": {"allowed": True}}})
        self.state.record_reconcile(
            [{"model": "material_a", "verdict": RECONCILE_AT_ORIGIN}],
            source="test")
        status = self.state.status()
        self.assertEqual(status["objects"], {})
        self.assertEqual(status["checkpoints"], [])

    def test_nothing_is_removed_when_nothing_is_at_origin(self):
        self.write({"objects": {"material_a": {"state": HELD_ON_TARGET}}})
        applied = self.state.record_reconcile(
            [{"model": "material_a", "verdict": RECONCILE_ELSEWHERE,
              "detail": "어느 자리도 아니다"}], source="test")
        self.assertEqual(applied["cleared"], [])
        self.assertIn("material_a", self.state.status()["objects"])

    def test_the_finding_is_kept_as_evidence(self):
        self.write({"objects": {}})
        applied = self.state.record_reconcile(
            [{"model": "material_a", "verdict": RECONCILE_UNOBSERVED,
              "detail": "관측 실패"}], source="scripts/x --reconcile-state")
        self.assertEqual(applied["source"], "scripts/x --reconcile-state")
        self.assertEqual(applied["findings"][0]["detail"], "관측 실패")
        self.assertIs(applied["is_simulated"], True)
        self.assertEqual(self.state.status()["last_reconcile"]["cleared"], [])


class JobTest(JobsBase):
    def write_state(self, payload):
        self.state_path.write_text(json.dumps(
            {**payload, "schema": SCHEMA, "is_simulated": True,
             "real_hardware_ready": False, "real_hardware_verified": False},
            ensure_ascii=False), encoding="utf-8")

    def test_reconcile_is_a_cell_action_without_a_material(self):
        job = self.jobs.start("reconcile")
        argv = self.popen.calls[0]["argv"]
        self.assertEqual(argv[1:4], ["-", "-", "--reconcile-state"])
        self.assertIsNone(job["material"])
        self.assertEqual(self.popen.calls[0]["env"]["FORSTICK2_SIM_PICK_PLACE_DEMO"],
                         "1")

    def test_startup_does_not_launch_anything_without_records(self):
        result = self.jobs.start_reconcile_if_needed()
        self.assertIs(result["started"], False)
        self.assertEqual(self.popen.calls, [])

    def test_startup_launches_once_when_records_remain(self):
        self.write_state({"objects": {"material_a": {"state": HELD_ON_TARGET}}})
        result = self.jobs.start_reconcile_if_needed()
        self.assertIs(result["started"], True)
        self.assertEqual(len(self.popen.calls), 1)
        self.assertIn("--reconcile-state", self.popen.calls[0]["argv"])

    def test_status_reports_whether_reconcile_is_needed(self):
        self.assertIs(self.jobs.status()["reconcile"]["needed"], False)
        self.write_state({"objects": {"material_a": {"state": HELD_ON_TARGET}}})
        self.assertIs(self.jobs.status()["reconcile"]["needed"], True)

    def test_a_running_job_blocks_a_second_reconcile(self):
        self.jobs.start("reconcile")
        with self.assertRaises(Exception):
            self.jobs.start("reconcile")


if __name__ == "__main__":
    unittest.main()
