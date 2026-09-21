"""작업 셀 Adapter 단위 검증 (md/개발플랜.md 8-08 우선순위 6).

**ROS 없이** Adapter 로직을 검증한다. 전송 계층을 결정적 스텁으로 바꿔
차단 조건과 정지 판정을 확인한다 — 플랫폼 문제와 Adapter 문제를 분리한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_capability_profile
from robots.fr3_gazebo.adapter import (
    ARM_JOINTS,
    CONSECUTIVE_EXCEED_FOR_MOTION,
    GRIPPER_JOINT,
    REQUIRED_CONTROLLERS,
    STOP_DISPLACEMENT_RAD,
    Fr3GazeboAdapter,
    load_workcell_resources,
)
from robots.fr3_gazebo.transport import (
    GoalOutcome,
    JointObservation,
    SceneCheck,
    WorldStatus,
)

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
PROFILE = ROOT / "config/profiles/fr3wms_2f85_workcell_capability.json"
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"


class StubTransport:
    """결정적 전송 스텁. 테스트가 관측값을 직접 만든다."""

    def __init__(self, *, world: str, partition: str = "p", domain: int = 44):
        self.world_name = world
        self.partition = partition
        self.domain = domain
        self.world_present = True
        self.controllers = dict.fromkeys(REQUIRED_CONTROLLERS, "active")
        self.observation_valid = True
        self.observed_at = 100.0
        self.positions: dict[str, float] = {name: 0.0 for name in ARM_JOINTS}
        self.velocities: dict[str, float] = {name: 0.0 for name in ARM_JOINTS}
        self.positions[GRIPPER_JOINT] = 0.0
        self.velocities[GRIPPER_JOINT] = 0.0
        self.scene = SceneCheck(available=True, valid=True, snapshot_id="s",
                                content_hash="h")
        self.arm_outcome = GoalOutcome(accepted=True, result_received=True,
                                       error_code=0)
        self.cancel_outcome = GoalOutcome(accepted=True, cancel_ack=True,
                                          goals_canceling=1)
        #: 관측을 호출할 때마다 쓸 표본 목록. 비면 현재 값을 반복한다.
        self.samples: list[JointObservation] = []
        self.sent: list[dict] = []
        self.snap_target_on_send = True

    def _status(self) -> WorldStatus:
        return WorldStatus(
            world_present=self.world_present, world_name=self.world_name,
            gz_partition=self.partition, ros_domain_id=self.domain,
            controllers=dict(self.controllers), detail="없음" )

    def connect(self, timeout_sec):
        return self._status()

    def status(self, timeout_sec):
        return self._status()

    def joint_observation(self, timeout_sec, *, after=None):
        if self.samples:
            return self.samples.pop(0)
        return JointObservation(
            positions=dict(self.positions), velocities=dict(self.velocities),
            observed_at=self.observed_at, valid=self.observation_valid)

    def send_arm(self, joints, seconds, timeout_sec):
        self.sent.append({"kind": "arm", "joints": dict(joints)})
        if self.snap_target_on_send:
            self.positions.update(joints)
        return self.arm_outcome

    def send_gripper(self, value, seconds, timeout_sec):
        self.sent.append({"kind": "gripper", "value": value})
        return GoalOutcome(accepted=True, result_received=True, error_code=0)

    def cancel_all(self, timeout_sec):
        return self.cancel_outcome

    def check_state(self, joints, timeout_sec):
        return self.scene

    def disconnect(self):
        pass


def build(transport=None, *, now=None, state_max_age_sec=1.0):
    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=state_max_age_sec,
        mounting_path=MOUNTING)
    profile = load_capability_profile(
        json.loads(PROFILE.read_text(encoding="utf-8")))
    transport = transport or StubTransport(world=resources.world_name)
    clock = {"t": 100.0}
    adapter = Fr3GazeboAdapter(
        "robot", profile, transport=transport, resources=resources,
        now=now or (lambda: clock["t"]))
    return adapter, transport, resources, clock


class TestConnectAndCheck(unittest.TestCase):
    def test_connect_requires_the_expected_world(self):
        adapter, transport, resources, _ = build()
        self.assertTrue(adapter.connect(1.0).request_accepted)
        other = StubTransport(world="another_world")
        adapter2, _, _, _ = build(other)
        result = adapter2.connect(1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "config.version_mismatch")

    def test_missing_world_is_connection_lost(self):
        adapter, transport, _, _ = build()
        transport.world_present = False
        result = adapter.connect(1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "robot.connection_lost")

    def test_check_blocks_when_a_required_controller_is_not_active(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.controllers["arm_trajectory_controller"] = "inactive"
        result = adapter.check()
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "robot.state_unavailable")
        self.assertIn("arm_trajectory_controller",
                      result.evidence["inactive_controllers"])

    def test_check_blocks_on_stale_state(self):
        clock = {"t": 100.0}
        adapter, transport, _, _ = build(now=lambda: clock["t"],
                                         state_max_age_sec=0.5)
        adapter.connect(1.0)
        clock["t"] = 101.0          # 관측이 1초 지났다 (상한 0.5초)
        result = adapter.check()
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "robot.state_stale")

    def test_check_blocks_when_state_is_unavailable(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.observation_valid = False
        self.assertEqual(adapter.check().reason.value, "robot.state_unavailable")

    def test_check_reports_gripper_aperture_from_the_official_table(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        evidence = adapter.check().evidence
        self.assertTrue(evidence["gripper"]["observed"])
        # 열림(0 rad)에서 공식 최대 개구에 해당한다.
        self.assertAlmostEqual(evidence["gripper"]["aperture_m"], 0.084997, places=5)
        self.assertEqual(list(evidence["required_controllers"]),
                         list(REQUIRED_CONTROLLERS))


class TestMoveResolution(unittest.TestCase):
    def test_unknown_resource_is_refused(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        result = adapter.move("loc_nowhere", 1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "plan.unknown_resource")

    def test_known_resource_maps_to_a_verified_pose(self):
        adapter, transport, resources, _ = build()
        adapter.connect(1.0)
        pose, detail = adapter.resolve_move("loc_pallet_1")
        self.assertEqual(pose, "pallet_1_approach")
        self.assertEqual(detail["gazebo_model"], "pallet_1")
        self.assertEqual(detail["frame"], "pallet_1_frame")
        self.assertEqual(detail["pose_evidence"]["status"], "verified")
        result = adapter.move("loc_pallet_1", 1.0)
        self.assertTrue(result.task_succeeded)
        self.assertEqual(transport.sent[-1]["kind"], "arm")

    def test_collision_at_target_blocks_execution(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.scene = SceneCheck(available=True, valid=False,
                                     contacts=(("a", "b"),), snapshot_id="s")
        result = adapter.move("loc_pallet_1", 1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "geometry.collision")
        self.assertEqual(result.evidence["contacts"], [["a", "b"]])

    def test_missing_validator_blocks_execution(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.scene = SceneCheck(available=False, detail="서비스 없음")
        result = adapter.move("loc_pallet_1", 1.0)
        self.assertFalse(result.request_accepted)
        self.assertEqual(result.reason.value, "geometry.validator_unavailable")

    def test_goal_not_reached_is_reported_as_failure_not_success(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.snap_target_on_send = False     # 관측이 목표에 가지 않는다
        result = adapter.move("loc_pallet_1", 1.0)
        self.assertTrue(result.request_accepted)
        self.assertFalse(result.task_succeeded)
        self.assertEqual(result.reason.value, "exec.goal_not_reached")

    def test_result_timeout_is_unverifiable_not_failure(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.arm_outcome = GoalOutcome(accepted=True, result_received=False,
                                            detail="결과 대기 초과")
        result = adapter.move("loc_pallet_1", 1.0)
        # 계측하지 못했다 — 성공도 실패도 주장하지 않는다.
        self.assertIsNot(result.task_succeeded, True)
        self.assertFalse(result.verified)
        self.assertEqual(result.reason.value, "exec.result_timeout")


class TestPickPlaceStaysBlocked(unittest.TestCase):
    def test_pick_and_place_resolve_resources_but_refuse(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        for result, skill in ((adapter.pick("mat_a", "loc_pallet_1", 1.0), "pick"),
                              (adapter.place("mat_a", "loc_conveyor", 1.0), "place")):
            with self.subTest(skill=skill):
                self.assertFalse(result.request_accepted)
                self.assertEqual(result.reason.value,
                                 "capability.profile_incomplete")
                # 자원 해석까지는 한다 — 무엇을 하려 했는지 남는다.
                self.assertTrue(result.evidence["object_scene"])
                self.assertIn("2F-85 장착 근거", result.evidence["message"])
                self.assertTrue(result.evidence["unmet"])

    def test_profile_does_not_declare_pick_or_place(self):
        adapter, _, _, _ = build()
        self.assertEqual(list(adapter.profile.supported_skills),
                         ["home", "move", "stop"])
        self.assertFalse(adapter.supports("pick"))
        self.assertFalse(adapter.supports("place"))


def ticking(start=0.0, step=0.05):
    """호출할 때마다 흐르는 시계. 정지 확인은 timeout까지 안정 창을 찾으므로
    멈춘 시계를 쓰면 끝나지 않는다."""
    clock = {"t": start - step}

    def now():
        clock["t"] += step
        return clock["t"]
    return now


class TestStopIsObserved(unittest.TestCase):
    def _samples(self, count, *, gripper_velocities=None, gripper_positions=None,
                 arm_velocity=0.0, start=200.0, step=0.01):
        out = []
        for index in range(count):
            velocities = {name: arm_velocity for name in ARM_JOINTS}
            velocities[GRIPPER_JOINT] = (
                gripper_velocities[index] if gripper_velocities else 0.0)
            positions = {name: 0.0 for name in ARM_JOINTS}
            positions[GRIPPER_JOINT] = (
                gripper_positions[index] if gripper_positions else 0.0)
            out.append(JointObservation(positions=positions, velocities=velocities,
                                        observed_at=start + index * step))
        return out

    def test_stop_request_does_not_claim_stopped(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        result = adapter.stop(1.0)
        # 요청 접수만 알린다. **STOPPED로 주장하지 않는다.**
        self.assertEqual(result.state.value, "stopping")
        self.assertIsNot(result.task_succeeded, True)
        self.assertNotEqual(result.state.value, "stopped")

    def test_isolated_velocity_spike_with_static_position_is_stopped(self):
        """고립 1표본 스파이크는 잡음이다(reports/workcell/stop_observation.json)."""
        adapter, transport, _, _ = build(now=ticking())
        velocities = [0.0] * 10
        velocities[4] = 0.05        # 고립 스파이크 1표본
        transport.samples = self._samples(10, gripper_velocities=velocities)
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.state.value, "stopped")
        self.assertIsNone(result.reason)
        self.assertEqual(
            result.evidence["gripper_channel"]["longest_consecutive_exceed_samples"],
            1)

    def test_consecutive_velocity_exceed_is_not_stopped(self):
        adapter, transport, _, _ = build(now=ticking())
        velocities = [0.0] * 10
        velocities[4] = velocities[5] = 0.05   # 연속 2표본 → 운동
        transport.samples = self._samples(10, gripper_velocities=velocities)
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertTrue(result.evidence["gripper_channel"]["moving"])

    def test_displacement_beyond_tolerance_is_not_stopped(self):
        """속도가 조용해도 위치가 움직였으면 정지가 아니다."""
        adapter, transport, _, _ = build(now=ticking())
        positions = [index * STOP_DISPLACEMENT_RAD for index in range(10)]
        transport.samples = self._samples(10, gripper_positions=positions)
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertGreater(result.evidence["gripper_channel"]["displacement_rad"],
                           STOP_DISPLACEMENT_RAD)

    def test_missing_gripper_observation_is_unconfirmed_not_stopped(self):
        adapter, transport, _, _ = build(now=ticking())
        samples = self._samples(10)
        for sample in samples:
            del sample.velocities[GRIPPER_JOINT]
        transport.samples = samples
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertFalse(result.evidence["gripper_observed"])

    def test_too_few_samples_is_unconfirmed(self):
        adapter, transport, _, _ = build(now=ticking())
        transport.samples = self._samples(3)
        transport.observation_valid = False
        result = adapter.confirm_stopped(1.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertLess(result.evidence["samples"], 10)

    def test_cancel_without_ack_is_unverifiable(self):
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        transport.cancel_outcome = GoalOutcome(accepted=True, cancel_ack=False,
                                               detail="ACK 없음")
        result = adapter.cancel(1.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")

    def test_arm_motion_keeps_stop_unconfirmed(self):
        adapter, transport, _, _ = build(now=ticking())
        transport.samples = self._samples(10, arm_velocity=0.5)
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertTrue(result.evidence["arm"]["moving"])


class TestStopWaitsForSettling(unittest.TestCase):
    """취소 ACK 직후 첫 창만 보고 결론 내리지 않는다. timeout 안에서 안정 창을 찾는다."""

    def _obs(self, at, *, arm_velocity=0.0, arm_position=0.0):
        velocities = {name: arm_velocity for name in ARM_JOINTS}
        velocities[GRIPPER_JOINT] = 0.0
        positions = {name: arm_position for name in ARM_JOINTS}
        positions[GRIPPER_JOINT] = 0.0
        return JointObservation(positions=positions, velocities=velocities,
                                observed_at=at)

    def test_settling_samples_then_stable_window_is_confirmed(self):
        adapter, transport, _, _ = build(now=ticking(start=199.0))
        adapter.connect(1.0)
        adapter.stop(1.0)                       # 취소 ACK 시각을 남긴다
        moving = [self._obs(200.0 + i * 0.005, arm_velocity=0.04,
                            arm_position=i * 0.0002) for i in range(15)]
        still = [self._obs(200.075 + i * 0.005, arm_position=15 * 0.0002)
                 for i in range(10)]
        transport.samples = moving + still
        result = adapter.confirm_stopped(10.0)
        self.assertEqual(result.state.value, "stopped")
        self.assertIsNone(result.reason)
        self.assertTrue(result.verified)
        ev = result.evidence
        self.assertEqual(ev["stop_verdict"], "stop_confirmed")
        # 첫 창(이동 표본)은 탈락했고 사유가 남는다.
        self.assertGreater(ev["rejected_windows"], 0)
        self.assertIn("arm_velocity_consecutive_exceed", ev["rejected_window_runs"][0]["reason"])
        # 첫 안정 창: 마지막 이동 표본 1개(고립 1표본 초과는 잡음 규칙상 운동 아님,
        # 변위도 허용치 안) + 정지 표본 9개.
        self.assertEqual(ev["stable_window_start_at"], round(moving[-1].observed_at, 6))
        self.assertEqual(ev["stable_window_end_at"], round(still[8].observed_at, 6))
        self.assertIsNotNone(ev["cancel_ack_at"])
        self.assertEqual(ev["seconds_to_confirm_from"], "cancel_ack")
        self.assertAlmostEqual(ev["seconds_to_confirm"],
                               still[8].observed_at - ev["cancel_ack_at"], places=3)
        self.assertEqual(ev["arm"]["moving"], False)

    def test_motion_until_timeout_is_unconfirmed(self):
        adapter, transport, _, _ = build(now=ticking(start=0.0, step=0.05))
        transport.samples = [self._obs(200.0 + i * 0.005, arm_velocity=0.04,
                                       arm_position=i * 0.0002) for i in range(400)]
        result = adapter.confirm_stopped(2.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertNotEqual(result.state.value, "stopped")
        ev = result.evidence
        self.assertEqual(ev["stop_verdict"], "stop_unconfirmed")
        self.assertGreater(ev["rejected_windows"], 0)
        self.assertNotIn("stable_window_start_at", ev)
        self.assertGreaterEqual(ev["searched_until"], ev["search_started_at"] + 2.0)

    def test_stale_samples_cannot_complete_a_stable_window(self):
        adapter, transport, _, _ = build(now=ticking())
        fresh = [self._obs(300.0 + i * 0.005) for i in range(9)]
        repeated = [self._obs(fresh[-1].observed_at) for _ in range(5)]   # 새 표본 아님
        transport.samples = fresh + repeated
        result = adapter.confirm_stopped(2.0)
        self.assertEqual(result.reason.value, "exec.stop_unconfirmed")
        self.assertEqual(result.evidence["fresh_samples"], 9)
        self.assertGreaterEqual(result.evidence["stale_samples_skipped"], 5)

    def test_gap_resets_the_window(self):
        adapter, transport, _, _ = build(now=ticking())
        before = [self._obs(400.0 + i * 0.005) for i in range(9)]
        after = [self._obs(401.0 + i * 0.005) for i in range(10)]        # 간격 약 1초
        transport.samples = before + after
        result = adapter.confirm_stopped(5.0)
        self.assertEqual(result.state.value, "stopped")
        # 간격 앞의 표본은 안정 창에 들어가지 않는다.
        self.assertEqual(result.evidence["stable_window_start_at"], round(after[0].observed_at, 6))
        self.assertEqual(result.evidence["rejected_window_runs"][0]["reason"], "sample_gap_exceeded")

    def test_stop_criteria_values_are_unchanged(self):
        self.assertEqual(STOP_DISPLACEMENT_RAD, 0.001)
        self.assertEqual(CONSECUTIVE_EXCEED_FOR_MOTION, 2)
        adapter, _, _, _ = build()
        self.assertEqual(adapter._stop_velocity, 0.01)
        self.assertEqual(adapter._stop_samples, 10)
        self.assertEqual(adapter._max_sample_gap_sec, 0.2)
        # 작업 셀 서버가 실제로 넘기는 값: 속도 0.01(정책에 arm 키가 없어 기본값),
        # 표본 간격은 정지 정책 값, 확인 timeout은 API 상수.
        policy = json.loads((ROOT / "examples/config/valid_stop_policy.json")
                            .read_text(encoding="utf-8"))
        self.assertNotIn("arm", policy["position_tolerance"])
        self.assertEqual(policy["max_sample_gap_sec"], 0.5)
        runtime_source = (ROOT / "server/runtime.py").read_text(encoding="utf-8")
        self.assertIn('stop_policy.position_tolerance.get("arm", 0.01)', runtime_source)
        self.assertIn("max_sample_gap_sec=stop_policy.max_sample_gap_sec", runtime_source)
        source = (ROOT / "server/api.py").read_text(encoding="utf-8")
        self.assertIn("ADAPTER_TIMEOUT_SEC = 10.0", source)


class TestEverythingIsMarkedSimulated(unittest.TestCase):
    def test_evidence_always_says_simulated(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        for result in (adapter.check(), adapter.home(1.0),
                       adapter.move("loc_pallet_1", 1.0),
                       adapter.pick("mat_a", "loc_pallet_1", 1.0),
                       adapter.stop(1.0)):
            with self.subTest(state=result.state.value):
                self.assertTrue(result.evidence.get("is_simulated"))

    def test_hold_is_never_claimed_without_observation(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        snapshot = adapter.state()
        # 파지 관측 수단이 없다 — "쥔 것이 없다"고 주장하지 않는다.
        self.assertFalse(snapshot.hold_observed)
        self.assertIsNone(snapshot.held_object)


if __name__ == "__main__":
    unittest.main()


class TestStopLatch(unittest.TestCase):
    """정지 래치는 계약(core/stop_contract)의 해제 규칙을 따른다."""

    def test_stop_latches_and_state_reports_stopped(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        self.assertFalse(adapter.tracker.stopped)
        adapter.stop(1.0)
        self.assertTrue(adapter.tracker.stopped)
        self.assertEqual(adapter.state().state.value, "stopped")

    def test_reset_for_new_plan_clears_the_latch(self):
        adapter, _, _, _ = build()
        adapter.connect(1.0)
        adapter.stop(1.0)
        adapter.tracker.reset_for_new_plan()
        self.assertFalse(adapter.tracker.stopped)
        self.assertEqual(adapter.state().state.value, "idle")

    def test_reset_is_refused_while_goals_are_tracked(self):
        """이전 goal이 살아 있는데 새 계획을 시작하지 않는다."""
        adapter, transport, _, _ = build()
        adapter.connect(1.0)
        adapter.stop(1.0)
        transport._handles = [object()]     # 추적 중인 goal이 있다
        with self.assertRaises(RuntimeError):
            adapter.tracker.reset_for_new_plan()
        self.assertTrue(adapter.tracker.stopped)

    def test_runtime_hook_shape_is_provided(self):
        """공통 코드는 adapter.tracker.reset_for_new_plan만 안다."""
        adapter, _, _, _ = build()
        self.assertTrue(callable(getattr(adapter.tracker, "reset_for_new_plan")))
