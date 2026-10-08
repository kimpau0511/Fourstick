"""2026-10-08 리뷰 11번: 이송 시작 전 서버 기록과 실제 관측(위치·부착)을 함께 본다.

기록만으로 위치를 확정하지 않고 관측을 기록으로 대신하지 않는다. 불일치·부착 불명·이미 목적지를 구분한다.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "unit"))

import test_sim_demo_web as base  # noqa: E402
from server.material_check import (  # noqa: E402
    ATTACH_UNKNOWN, MISMATCH, NOOP, OBSERVATION_MISSING, OK, RECORD_NOT_READY, check_for_action,
)
from server.sim_demo_jobs import SimDemoJobs  # noqa: E402
from validation.simulation_demo_state import POLICY_DEMO_HOLD, SimulationDemoState  # noqa: E402

HOME = {"material_a": (0.5, 0.2, 0.84), "material_b": (0.5, 0.0, 0.84), "material_c": (0.5, -0.2, 0.84)}
SLOT_1 = (0.25, -0.5, 0.75)


class View:
    def __init__(self, poses, stale=False):
        self.poses, self.stale = dict(poses), stale

    def sample(self):
        return {"materials": {k: list(v) + [0, 0, 0, 1] for k, v in self.poses.items()},
                "stale": self.stale, "pose_age_sec": 9.0 if self.stale else 0.1}


class MaterialCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.state_path = self.tmp / "state.json"
        self.jobs = SimDemoJobs(workcell=base.WORKCELL, grasp_config=base.GRASP, state_path=self.state_path,
                                jobs_dir=self.tmp / "jobs", stop_request=self.tmp / "stop.json",
                                popen=base.FakePopen(), environ={"PATH": "/usr/bin"})
        (self.tmp / "gz_server.pid").write_text("1")                 # 현재 Gazebo가 뜬 시각 = 지금
        self.view = View(HOME)

    def joints(self, **states):
        (self.tmp / "fixture_joints.json").write_text(json.dumps(
            {m: {"state": s, "at": time.time() + 1, "confirmed_by": "시험"} for m, s in states.items()}))

    def hold_on_conveyor(self, model="material_a"):
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model=model, result=base.completed_result(),
            final_pose_m=SLOT_1, restored=None)

    def check(self, action, model="material_a"):
        return check_for_action(self.jobs, self.view, model, action, log_dir=self.tmp)

    def test_record_and_observation_agree_at_source(self):
        self.joints(material_a="detached")
        self.assertEqual(self.check("transfer")["kind"], OK)

    def test_observation_differs_from_record_blocks(self):
        self.view.poses["material_a"] = (0.5, 0.0, 0.84)                # 기록은 1번 팔레트, 관측은 20 cm 옆
        got = self.check("transfer")
        self.assertEqual(got["kind"], MISMATCH, got)
        self.assertIn("정합", got["guidance"])

    def test_missing_or_stale_observation_is_not_replaced_by_the_record(self):
        self.view.stale = True
        self.assertEqual(self.check("transfer")["kind"], OBSERVATION_MISSING)
        self.view = View({})
        self.assertEqual(self.check("transfer")["kind"], OBSERVATION_MISSING)
        self.view = None
        self.assertEqual(self.check("transfer")["kind"], OBSERVATION_MISSING)

    def test_attach_state_unknown_or_attached_blocks(self):
        for state in ("unknown", "attached"):
            with self.subTest(state):
                self.joints(material_a=state)
                got = self.check("transfer")
                self.assertEqual(got["kind"], ATTACH_UNKNOWN, got)

    def test_unknown_record_is_settled_only_by_observing_the_joint(self):
        """기록 'unknown'(알림 없는 detach)은 월드 상태에 붙임 관절이 없다고 관측될 때만 통과한다."""
        self.joints(material_a="unknown")
        for observed, want in ((set(), OK), ({"material_a"}, ATTACH_UNKNOWN), (None, ATTACH_UNKNOWN),
                               ({"material_c"}, OK)):
            with self.subTest(observed=observed):
                self.view.attached_models = lambda: (observed, "시험 관측")
                got = self.check("transfer")
                self.assertEqual(got["kind"], want, got)
                self.assertEqual(got["attachment"]["observed_attached"],
                                 None if observed is None else "material_a" in observed)

    def test_attach_record_from_before_this_gazebo_is_not_used(self):
        (self.tmp / "fixture_joints.json").write_text(json.dumps(
            {"material_a": {"state": "unknown", "at": time.time() - 3600, "confirmed_by": "옛 Gazebo"}}))
        os.utime(self.tmp / "gz_server.pid", (time.time(), time.time()))
        self.assertEqual(self.check("transfer")["kind"], OK)

    def test_already_at_destination_by_record_and_observation_is_noop(self):
        self.hold_on_conveyor()
        self.view.poses["material_a"] = SLOT_1
        got = self.check("transfer")
        self.assertEqual(got["kind"], NOOP, got)
        self.assertEqual(self.check("return", "material_b")["kind"], NOOP)    # 원래 자리에 있는 B의 복귀

    def test_record_at_destination_but_observed_elsewhere_is_a_mismatch(self):
        self.hold_on_conveyor()
        got = self.check("transfer")                                         # 관측은 아직 1번 팔레트
        self.assertEqual(got["kind"], MISMATCH, got)

    def test_record_not_at_source_is_not_ready(self):
        SimulationDemoState(self.state_path).record_run(
            policy=POLICY_DEMO_HOLD, model="material_a", result=base.stopped_result(),
            final_pose_m=(0.4, 0.0, 1.0), restored=None)
        got = self.check("transfer")
        self.assertEqual(got["kind"], RECORD_NOT_READY, got)

    def test_plan_noop_needs_the_observation_to_agree(self):
        """일반 계획의 '이미 목적지'(의도 단계, 기록)는 관측이 같을 때만 할 일 없음이다."""
        from server.api import _confirm_noop_by_observation
        from server.plan_intent import IntentDecision

        self.hold_on_conveyor()
        decision = IntentDecision("noop", detail="이미 있음", clarification="이미 있음",
                                  interpretation={"noop_at": {"model": "material_a", "location": "loc_conveyor"}})
        runtime = types.SimpleNamespace(sim_view=self.view, workcell_log_dir=self.tmp)
        self.view.poses["material_a"] = SLOT_1
        self.assertEqual(_confirm_noop_by_observation(runtime, self.jobs, decision).kind, "noop")
        self.view.poses["material_a"] = (0.5, 0.2, 0.84)                     # 기록은 컨베이어, 관측은 팔레트
        got = _confirm_noop_by_observation(runtime, self.jobs, decision)
        self.assertEqual(got.kind, "block")
        self.assertEqual(got.interpretation["material_check"]["kind"], MISMATCH)
        self.assertIn("정합", got.clarification)
        runtime.sim_view = None
        self.assertEqual(_confirm_noop_by_observation(runtime, self.jobs, decision).kind, "ask")

    def test_return_checks_the_conveyor_slot(self):
        self.hold_on_conveyor()
        self.view.poses["material_a"] = SLOT_1
        self.assertEqual(self.check("return")["kind"], OK)
        self.view.poses["material_a"] = (0.15, -0.5, 0.75)                  # 기록은 1번 칸, 관측은 2번 칸
        self.assertEqual(self.check("return")["kind"], MISMATCH)


import test_direct_job_approval as direct  # noqa: E402


class MaterialCheckRouteTest(direct.DirectJobApprovalTest):
    """직접 요청·명령·확인 세 길 모두 카드·작업 전에 기록과 관측을 함께 본다."""
    for _name in [n for n in dir(direct.DirectJobApprovalTest) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def offer(self, material="material_c"):
        return self.post("/v1/sim-demo/jobs", {"action": "transfer", "material": material,
                                               "session_id": self.session})

    def moved_view(self, model="material_c", pose=(0.5, 0.0, 0.9)):
        sample = self.view.sample()
        sample["materials"][model] = list(pose) + [0, 0, 0, 1]
        self.runtime.sim_view = types.SimpleNamespace(sample=lambda: sample)

    def test_direct_offer_blocks_when_observation_differs(self):
        self.moved_view()
        status, payload = self.offer()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["material_check"]["kind"], MISMATCH)
        self.assertIsNone(payload.get("confirmation"))
        self.assertEqual(self.popen.calls, [])

    def test_direct_offer_blocks_when_attachment_is_unknown(self):
        (self.tmp / "fixture_joints.json").write_text(json.dumps(
            {"material_c": {"state": "unknown", "at": time.time(), "confirmed_by": "시험"}}))
        status, payload = self.offer()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["material_check"]["kind"], ATTACH_UNKNOWN)
        self.assertIn("관리자", payload["reason"])
        self.assertEqual(self.popen.calls, [])

    def test_direct_offer_without_observation_is_not_replaced_by_the_record(self):
        self.runtime.sim_view = None
        status, payload = self.offer()
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["material_check"]["kind"], OBSERVATION_MISSING)

    def test_confirm_rechecks_and_starts_nothing_when_the_material_moved(self):
        status, offer = self.offer()
        self.assertEqual(status, 200, offer)
        self.moved_view()                                            # 카드가 뜬 뒤 자재가 밀렸다
        status, payload = self.post("/v1/sim-demo/confirm", {"token": offer["confirmation"]["token"],
                                                             "action": "confirm", "session_id": self.session})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["confirm_rejection"], "material_check")
        self.assertEqual(self.popen.calls, [])

    def test_command_route_does_not_offer_a_card_on_mismatch(self):
        from server.sim_demo_confirm import ConfirmStore

        self.runtime.sim_demo_confirm = ConfirmStore(ttl_sec=60)
        self.moved_view()
        status, payload = self.post("/v1/sim-demo/command", {"utterance": "C 자재를 컨베이어로 옮겨줘",
                                                             "source": "text", "mode": "simulation_demo",
                                                             "session_id": self.session})
        self.assertEqual(status, 409, payload)
        self.assertEqual(payload["decision"], "BLOCK")
        self.assertEqual(payload["material_check"]["kind"], MISMATCH)
        self.assertIsNone(payload.get("confirmation"))

    def test_normal_offer_still_works(self):
        status, payload = self.offer()
        self.assertEqual((status, payload["decision"]), (200, "CONFIRM"))


if __name__ == "__main__":
    unittest.main()
