"""중단된 반복의 잠금 해제 전 확인(server/cell_quiet.py) — 실행 종료·로봇 정지·부착 상태. 하나라도 모르면 유지."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.cell_quiet import check_cell_quiet  # noqa: E402

WORKCELL = {"gz_partition": "p", "frames": {"world": {"xyz_m": [0, 0, 0]},
                                            "pal_a": {"parent": "world", "xyz_m": [0.5, 0.0, 0.1]}},
            "models": {"material_a": {"kind": "material", "frame": "pal_a", "size_m": [0.05, 0.05, 0.05]}},
            "resource_map": [{"gazebo_model": "material_a", "korean": "A자재"}]}


class FakeState:
    def __init__(self, samples):
        self.samples = list(samples)

    def sample(self):
        return self.samples.pop(0)


def sample(seq, joint=0.0, pose=(0.5, 0.0, 0.1), age=0.05):
    return {"joints": {"j1": joint}, "joint_seq": seq, "joint_age_sec": age,
            "materials": {"material_a": list(pose) + [0, 0, 0, 1]}}


class Jobs:
    def __init__(self, running=None):
        self._running = running
        self.recovery_required = None
        self.materials = {"material_a": {"korean": "A자재"}}
        self.slots = ()

    def running(self):
        return self._running


class Api:
    def __init__(self):
        self._flag_lock = threading.Lock()
        self._active_executions = {}


def run(*, running=None, goals=0, samples=None, attach=None, view=True):
    view_obj = type("V", (), {"state": FakeState(samples or [sample(1), sample(2)]), "workcell": WORKCELL})()
    runtime = type("R", (), {"sim_demo_jobs": Jobs(running), "sim_view": view_obj if view else None})()
    attach = attach if attach is not None else {"material_a": {"loaded": True, "state": "detached"}}
    return check_cell_quiet(runtime, Api(), goals=lambda: goals,
                            attachments=lambda models, partition: attach, sleep=lambda s: None)


def failed(result):
    return {c["name"] for c in result["checks"] if not c["ok"]}


class CellQuietTest(unittest.TestCase):
    def test_all_quiet(self):
        result = run()
        self.assertTrue(result["ok"], result)

    def test_running_process_keeps_lock(self):
        result = run(running={"job_id": "simjob_1", "action": "transfer"})
        self.assertFalse(result["ok"])
        self.assertEqual(failed(result), {"process"})

    def test_active_controller_goal_or_unknown_controller_keeps_lock(self):
        self.assertEqual(failed(run(goals=1)), {"controller"})
        self.assertEqual(failed(run(goals=None)), {"controller"})

    def test_moving_or_stale_joints_keep_lock(self):
        self.assertEqual(failed(run(samples=[sample(1, 0.0), sample(2, 0.05)])), {"joints"})
        self.assertEqual(failed(run(samples=[sample(1), sample(1)])), {"joints"})        # 새 값이 안 옴
        self.assertEqual(failed(run(samples=[sample(1, age=5), sample(2, age=5)])), {"joints"})
        self.assertIn("joints", failed(run(view=False)))

    def test_attachment(self):
        attached = run(attach={"material_a": {"loaded": True, "state": "attached"}})
        self.assertTrue(attached["ok"])                                    # 막지 않고 알린다(복구하려면 잠금이 풀려야)
        self.assertIn("그리퍼에 붙어", attached["checks"][-1]["detail"])
        unknown_home = run(attach={"material_a": {"loaded": True, "state": "unknown"}})
        self.assertTrue(unknown_home["ok"])                                # 원래 자리에 놓인 것이 관측됨
        self.assertTrue(unknown_home["notes"])
        unknown_away = run(attach={"material_a": {"loaded": True, "state": "unknown"}},
                           samples=[sample(1, pose=(0.2, 0.3, 0.4)), sample(2, pose=(0.2, 0.3, 0.4))])
        self.assertEqual(failed(unknown_away), {"attachment"})
        not_loaded = run(attach={"material_a": {"loaded": False, "state": "attached"}})
        self.assertTrue(not_loaded["ok"])                                  # Gazebo 재기동 — 붙임 시스템이 없다


if __name__ == "__main__":
    unittest.main(verbosity=2)
