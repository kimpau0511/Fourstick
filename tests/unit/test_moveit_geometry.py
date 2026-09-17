"""MoveIt planning scene 기하 검사 계약 (md/개발플랜.md 8-07).

**ROS 없이** 검증한다. `MoveItGeometryValidator`는 client 계약만 보므로
여기서는 시험용 대역(fake client)을 넣는다. 실제 Gazebo·MoveIt 관측은
`scripts/verify_fr3_moveit.py`가 따로 남긴다(reports/moveit/verify_moveit.json).

확인하는 것:
- 수치 관절값이 없는 스텝 → ALLOW를 만들지 않고 ASK
- 관절 제한 위반 → BLOCK(workspace_violation), 충돌 → BLOCK(collision)
- scene 좌표계 불일치 → ASK(frame_unknown), 변환해 주지 않는다
- 요청 snapshot과 현재 scene hash 불일치 → ASK
- **검사 중 scene이 바뀌면** ALLOW를 내지 않는다
- scene 내용이 바뀌면 snapshot 버전·hash가 달라진다(승인 재사용 판정의 전제)
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.geometry import GeometryDecision, GeometryRequest
from core.reason_codes import ReasonCode
from core.task_plan import TaskPlan, TaskStep
from robots.moveit.scene import StateValidity, scene_snapshot_from_parts
from robots.moveit.validator import MoveItGeometryValidator, joint_motion

FRAME = "synth_base"
NOW = 20_000.0
JOINTS = {"a1": 0.0, "a2": 0.0}


def snapshot(*, objects=(), pairs=(("l1", "l2"),), frame=FRAME, captured=NOW - 1.0):
    return scene_snapshot_from_parts(
        scene_name="synth_scene", robot_model_hash="modelhash",
        world_objects=list(objects), acm_disabled_pairs=list(pairs),
        frame_id=frame, captured_at=captured, ttl_sec=30.0, source="시험",
    )


class FakeSceneClient:
    """시험용 planning scene 대역. 좌표 계산을 하지 않는다."""

    def __init__(self, snapshots, validity=None):
        self._snapshots = list(snapshots)
        self._validity = validity or {}
        self.checked: list[dict] = []

    def snapshot(self):
        return (self._snapshots.pop(0) if len(self._snapshots) > 1
                else self._snapshots[0])

    def check_state(self, joints):
        self.checked.append(dict(joints))
        return self._validity.get(
            tuple(sorted(joints.items())), StateValidity(valid=True))


def plan(steps=2) -> TaskPlan:
    return TaskPlan(
        "plan_scene", "synth_robot", "synthetic", "test-1.0",
        tuple(TaskStep("move", {"to": f"loc_{i}"}) for i in range(steps)),
        created_at=NOW - 10, ttl_sec=600.0,
    )


def request(*, motion=None, frame=FRAME, environment=None, steps=2) -> GeometryRequest:
    target = plan(steps)
    return GeometryRequest(
        plan=target, plan_hash=target.plan_hash(), robot_id="synth_robot",
        profile_id="synthetic", profile_version="test-1.0", frame_id=frame,
        checked_at=NOW, snapshot=environment or snapshot().to_environment(),
        timeout_sec=1.0, resolved_motion=motion or {},
    )


def motion_for(steps=2, joints=None):
    return {i: joint_motion(joints or JOINTS) for i in range(1, steps + 1)}


class TestSceneSnapshotIdentity(unittest.TestCase):
    def test_content_change_changes_version_and_hash(self):
        empty = snapshot()
        with_box = snapshot(objects=[{
            "id": "box", "pose": (0, 0, 0, 0, 0, 0, 1),
            "shapes": [{"type": "box", "dimensions": (0.1, 0.1, 0.1)}],
        }])
        self.assertNotEqual(empty.content_hash, with_box.content_hash)
        self.assertNotEqual(empty.snapshot_version, with_box.snapshot_version)

    def test_same_content_keeps_the_same_identity(self):
        first = snapshot(captured=NOW - 5.0)
        second = snapshot(captured=NOW - 1.0)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.snapshot_version, second.snapshot_version)

    def test_allowed_collision_pairs_are_part_of_the_fingerprint(self):
        self.assertNotEqual(
            snapshot(pairs=(("l1", "l2"),)).content_hash,
            snapshot(pairs=(("l1", "l2"), ("l2", "l3"))).content_hash,
        )

    def test_summary_carries_no_geometry_body(self):
        scene = snapshot(objects=[{
            "id": "box", "pose": (0, 0, 0, 0, 0, 0, 1),
            "shapes": [{"type": "box", "dimensions": (0.1, 0.1, 0.1)}],
        }])
        self.assertEqual(scene.summary["world_object_ids"], ["box"])
        self.assertNotIn("shapes", scene.summary)
        self.assertNotIn("pose", scene.summary)


class TestValidatorDecisions(unittest.TestCase):
    def test_missing_numeric_motion_asks_instead_of_allowing(self):
        client = FakeSceneClient([snapshot()])
        verdict = MoveItGeometryValidator(client).check(request())
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                      verdict.reason_codes())
        self.assertFalse(verdict.input_complete)
        self.assertEqual(client.checked, [])

    def test_partial_motion_still_asks(self):
        client = FakeSceneClient([snapshot()])
        verdict = MoveItGeometryValidator(client).check(
            request(motion={1: joint_motion(JOINTS)}))
        self.assertIs(verdict.decision, GeometryDecision.ASK)

    def test_complete_motion_on_valid_states_allows_with_scene_identity(self):
        scene = snapshot()
        client = FakeSceneClient([scene])
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(), environment=scene.to_environment()))
        self.assertIs(verdict.decision, GeometryDecision.ALLOW)
        self.assertEqual(verdict.snapshot_hash, scene.content_hash)
        self.assertEqual(verdict.snapshot_version, scene.snapshot_version)
        self.assertEqual(verdict.frame_id, FRAME)
        self.assertEqual(verdict.evidence["checked_steps"], 2)
        self.assertEqual(len(client.checked), 2)

    def test_joint_limit_violation_blocks_with_its_own_reason(self):
        scene = snapshot()
        out = {"a1": 9.0, "a2": 0.0}
        client = FakeSceneClient([scene], {
            tuple(sorted(out.items())): StateValidity(
                valid=False, out_of_bounds=("a1",)),
        })
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(joints=out),
                    environment=scene.to_environment()))
        self.assertIs(verdict.decision, GeometryDecision.BLOCK)
        self.assertIn(ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                      verdict.reason_codes())
        self.assertNotIn(ReasonCode.GEOMETRY_COLLISION, verdict.reason_codes())

    def test_collision_blocks_and_names_the_pair(self):
        scene = snapshot()
        client = FakeSceneClient([scene], {
            tuple(sorted(JOINTS.items())): StateValidity(
                valid=False, contacts=(("link_a", "box"),)),
        })
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(), environment=scene.to_environment()))
        self.assertIs(verdict.decision, GeometryDecision.BLOCK)
        self.assertIn(ReasonCode.GEOMETRY_COLLISION, verdict.reason_codes())
        self.assertIn("link_a", verdict.reasons[0].detail)

    def test_frame_mismatch_asks_and_does_not_convert(self):
        scene = snapshot(frame="other_frame")
        client = FakeSceneClient([scene])
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(),
                    environment=scene.to_environment(), frame=FRAME))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_FRAME_UNKNOWN, verdict.reason_codes())
        self.assertEqual(client.checked, [])

    def test_stale_request_snapshot_asks(self):
        current = snapshot(objects=[{
            "id": "box", "pose": (0, 0, 0, 0, 0, 0, 1),
            "shapes": [{"type": "box", "dimensions": (0.1, 0.1, 0.1)}],
        }])
        client = FakeSceneClient([current])
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(), environment=snapshot().to_environment()))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                      verdict.reason_codes())
        self.assertEqual(client.checked, [])

    def test_scene_change_during_the_check_prevents_allow(self):
        before = snapshot()
        after = snapshot(objects=[{
            "id": "box", "pose": (0, 0, 0, 0, 0, 0, 1),
            "shapes": [{"type": "box", "dimensions": (0.1, 0.1, 0.1)}],
        }])
        client = FakeSceneClient([before, after])
        verdict = MoveItGeometryValidator(client).check(
            request(motion=motion_for(), environment=before.to_environment()))
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertIn(ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                      verdict.reason_codes())

    def test_missing_snapshot_asks_without_touching_the_scene(self):
        client = FakeSceneClient([snapshot()])
        target = plan()
        bare = GeometryRequest(
            plan=target, plan_hash=target.plan_hash(), robot_id="synth_robot",
            profile_id="synthetic", profile_version="test-1.0", frame_id=FRAME,
            checked_at=NOW, snapshot=None, resolved_motion=motion_for(),
        )
        verdict = MoveItGeometryValidator(client).check(bare)
        self.assertIs(verdict.decision, GeometryDecision.ASK)
        self.assertEqual(client.checked, [])

    def test_validator_identity_is_recorded(self):
        scene = snapshot()
        validator = MoveItGeometryValidator(FakeSceneClient([scene]))
        verdict = validator.check(
            request(motion=motion_for(), environment=scene.to_environment()))
        self.assertEqual(verdict.validator_id, "moveit-planning-scene")
        self.assertTrue(verdict.validator_version)
        self.assertEqual(verdict.evidence["method"],
                         "moveit_state_validity_per_step")


class TestObservedVerificationReport(unittest.TestCase):
    """실제 Gazebo·MoveIt 관측 보고서가 계약대로 남아 있는지 본다."""

    @classmethod
    def setUpClass(cls):
        cls.path = ROOT / "reports/moveit/verify_moveit.json"

    def test_report_exists_and_covers_every_required_check(self):
        if not self.path.is_file():
            self.skipTest("MoveIt 관측 보고서가 없다(ROS 환경에서만 생성된다)")
        import json

        report = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertTrue(report["environment"]["is_simulated"])
        self.assertTrue(report["environment"]["arm_only"])
        names = list(report["checks"])
        for needed in ("current_state", "plan_home", "plan_safe_pose",
                       "execute_trajectory", "target_vs_observed",
                       "joint_limit_blocked", "self_collision",
                       "environment_collision_blocked", "planning_timeout",
                       "snapshot_change_during_planning", "stop_during_motion",
                       "revalidation_after_stop"):
            self.assertTrue(any(needed in name for name in names), needed)

    def test_goal_reached_is_judged_by_observation_not_by_controller_result(self):
        if not self.path.is_file():
            self.skipTest("MoveIt 관측 보고서가 없다")
        import json

        checks = json.loads(self.path.read_text(encoding="utf-8"))["checks"]
        observed = checks["05_target_vs_observed"]
        self.assertIn("max_error_rad", observed)
        self.assertLessEqual(observed["max_error_rad"], observed["tolerance_rad"])
        # 명령 수락과 도달을 한 값으로 합치지 않는다.
        self.assertIn("command_accepted", checks["04_execute_trajectory"])
        self.assertIn("note", checks["04_execute_trajectory"])

    def test_out_of_limit_goal_is_blocked_by_our_gate_not_by_moveit(self):
        if not self.path.is_file():
            self.skipTest("MoveIt 관측 보고서가 없다")
        import json

        check = json.loads(self.path.read_text(encoding="utf-8"))[
            "checks"]["06_joint_limit_blocked"]
        # MoveIt은 제한 밖 목표를 잘라서(clamp) 계획했다 — 그 사실이 남아 있어야 한다.
        self.assertTrue(check["moveit_clamped_goal"])
        self.assertEqual(check["geometry"]["decision"], "block")
        self.assertIn("geometry.workspace_violation",
                      check["geometry"]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
