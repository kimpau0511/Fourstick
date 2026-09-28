"""관절로 붙이는 고정 장치(DetachableJoint) — Gazebo 없이 볼 수 있는 규칙."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from robots.fr3_gazebo import sim_fixture  # noqa: E402
from robots.fr3_gazebo.sim_fixture import (  # noqa: E402
    FixtureError,
    GazeboObjectFixture,
    ObjectDeclaration,
)

ITEM = ObjectDeclaration(model="material_c", size_m=(0.05, 0.05, 0.1), mass_kg=0.2,
                         friction=0.8, color_rgba=(0.6, 0.9, 0.4, 1.0),
                         home_pose_m=(0.5, -0.2, 0.84))


def fixture(mode: str) -> GazeboObjectFixture:
    with mock.patch.dict(os.environ, {"FORSTICK2_FIXTURE_MODE": mode}):
        return GazeboObjectFixture(world_name="w", gz_partition="p",
                                   objects={"material_c": ITEM})


class ModeTest(unittest.TestCase):
    def test_default_is_joint_and_unknown_falls_back_to_joint(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FORSTICK2_FIXTURE_MODE", None)
            self.assertEqual(GazeboObjectFixture(world_name="w", gz_partition="p",
                                                 objects={}).mode, "joint")
        self.assertEqual(fixture("static").mode, "static")
        self.assertEqual(fixture("nonsense").mode, "joint")

    def test_topics_are_per_material(self):
        topics = GazeboObjectFixture.joint_topics("material_c")
        self.assertEqual(topics["attach"], "/forstick2/fixture/material_c/attach")
        self.assertEqual(topics["state"], "/forstick2/fixture/material_c/state")

    def test_follow_is_physics_in_joint_mode(self):
        fx = fixture("joint")
        fx._held.add("material_c")
        fx._set_pose = mock.Mock()
        fx.follow("material_c", (0, 0, 1))
        fx._set_pose.assert_not_called()                  # 순간 이동하지 않는다
        static = fixture("static")
        static._held.add("material_c")
        static._set_pose = mock.Mock()
        static.follow("material_c", (0, 0, 1))
        static._set_pose.assert_called_once()
        with self.assertRaises(FixtureError):
            fx.follow("material_a", (0, 0, 1))            # 붙지 않은 자재

    def test_restore_releases_the_joint_before_replacing(self):
        fx = fixture("joint")
        order = []
        fx.joint_system_loaded = lambda model: True
        fx._joint_detach = lambda model: (order.append("detach"), (True, ""))[1]
        fx._replace = lambda *a, **k: (order.append("replace"),
                                       mock.Mock(verified=True))[1]
        fx.restore("material_c")
        self.assertEqual(order, ["detach", "replace"])


class ResendTest(unittest.TestCase):
    """첫 발행이 빠져도(새 프로세스의 첫 메시지) 알림이 올 때까지 다시 보낸다 — 2026-09-28 실측 재현 조건."""

    def _fx(self, drop_first: int):
        fx = fixture("joint")
        sent = []

        def publish(topic):
            sent.append(topic)
            if len(sent) > drop_first:            # 앞의 drop_first번은 빠진다
                want = "detached" if topic.endswith("/detach") else "attached"
                fx._joint_state["material_c"] = (want, sim_fixture.time.time())
        fx._publish_empty = publish
        fx._subscribe_joint_state = lambda model: None
        fx.joint_system_loaded = lambda model: True
        return fx, sent

    def test_first_detach_dropped_then_resent(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FORSTICK2_WORKCELL_LOG_DIR": tmp}):
            fx, sent = self._fx(drop_first=1)
            ok, detail = fx._joint_detach("material_c")
        self.assertTrue(ok, detail)
        self.assertEqual(len(sent), 2)

    def test_no_notification_fails_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FORSTICK2_WORKCELL_LOG_DIR": tmp}), \
                mock.patch.object(sim_fixture, "JOINT_STATE_TIMEOUT_SEC", 0.5):
            fx, sent = self._fx(drop_first=10**6)
            ok, _detail = fx._joint_detach("material_c")
        self.assertFalse(ok)
        self.assertGreater(len(sent), 1)


class RecordTest(unittest.TestCase):
    def test_confirmed_state_is_persisted_across_processes(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FORSTICK2_WORKCELL_LOG_DIR": tmp}):
            self.assertEqual(sim_fixture.read_joint_record(), {})
            sim_fixture._write_joint_record("material_c", "attached", "알림")
            sim_fixture._write_joint_record("material_a", "detached", "알림")
            record = sim_fixture.read_joint_record()
            self.assertEqual(record["material_c"]["state"], "attached")
            self.assertEqual(record["material_a"]["state"], "detached")
            self.assertEqual(sim_fixture.joint_record_path().parent, Path(tmp))


if __name__ == "__main__":
    unittest.main()
