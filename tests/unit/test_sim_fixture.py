"""Gazebo 물체 고정 장치 (md/개발플랜.md 8-11).

ROS·Gazebo 없이 확인할 수 있는 것만 본다 — SDF 생성, 선언 읽기, 사건 기록.

확인하는 것:

- 붙어 있는 동안의 모델은 **정적이고 충돌 형상이 없다**(손가락이 물체 안에
  있으므로 접촉력이 팔의 추종을 방해한다 — 실측으로 확인한 문제)
- 떼면 되돌아가는 모델은 **동적이고 선언된 질량·마찰·충돌 형상을 갖는다**
- 치수·질량·마찰·색을 장치가 만들지 않는다(선언에서만 온다)
- 사건 기록에 `fixture_kind: simulation_fixture`가 항상 붙는다 —
  실기 파지와 구분되는 표시다
- 선언되지 않은 물체는 거부한다(`exec.sim_fixture_failed`)
"""

from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode
from robots.fr3_gazebo.sim_fixture import (
    FIXTURE_KIND,
    FixtureError,
    FixtureEvent,
    GazeboObjectFixture,
    ObjectDeclaration,
    declarations_from_config,
    dynamic_sdf,
    static_sdf,
)

DECLARATION = ObjectDeclaration(
    model="part_x", size_m=(0.05, 0.05, 0.1), mass_kg=0.2, friction=0.8,
    color_rgba=(0.9, 0.5, 0.1, 1.0), home_pose_m=(0.5, 0.2, 0.84),
    source="config/test.json")

CONFIG = {
    "part_x": {"kind": "material", "frame": "part_x_frame",
               "size_m": [0.05, 0.05, 0.1], "mass_kg": 0.2, "friction": 0.8,
               "color_rgba": [0.9, 0.5, 0.1, 1.0]},
    "tray_1": {"kind": "pallet", "frame": "tray_1_frame", "parts": []},
}
FRAMES = {
    "world": {"parent": None, "xyz_m": [0.0, 0.0, 0.0]},
    "tray_1_frame": {"parent": "world", "xyz_m": [0.5, 0.2, 0.79]},
    "part_x_frame": {"parent": "tray_1_frame", "xyz_m": [0.0, 0.0, 0.05]},
}


def model_of(sdf: str):
    return ET.fromstring(sdf).find("model")


class TestHeldModelIsStaticAndCollisionFree(unittest.TestCase):
    def test_held_model_is_static(self):
        model = model_of(static_sdf(DECLARATION))
        self.assertEqual(model.findtext("static"), "true")

    def test_held_model_has_no_collision_geometry(self):
        """충돌 형상을 두면 정적 물체와 손가락 사이 접촉력이 팔을 방해한다."""
        model = model_of(static_sdf(DECLARATION))
        self.assertEqual(model.find("link").findall("collision"), [])
        self.assertEqual(len(model.find("link").findall("visual")), 1)

    def test_held_model_keeps_the_declared_size(self):
        model = model_of(static_sdf(DECLARATION))
        size = model.find("link/visual/geometry/box/size").text.split()
        self.assertEqual([float(v) for v in size], [0.05, 0.05, 0.1])

    def test_held_model_has_no_mass(self):
        model = model_of(static_sdf(DECLARATION))
        self.assertIsNone(model.find("link/inertial"))


class TestReleasedModelIsDynamic(unittest.TestCase):
    def test_released_model_is_not_static(self):
        model = model_of(dynamic_sdf(DECLARATION))
        self.assertIsNone(model.find("static"))

    def test_released_model_carries_declared_mass_and_friction(self):
        model = model_of(dynamic_sdf(DECLARATION))
        self.assertAlmostEqual(float(model.findtext("link/inertial/mass")), 0.2)
        mu = model.findtext("link/collision/surface/friction/ode/mu")
        self.assertAlmostEqual(float(mu), 0.8)

    def test_released_model_has_collision_geometry(self):
        model = model_of(dynamic_sdf(DECLARATION))
        self.assertEqual(len(model.find("link").findall("collision")), 1)

    def test_inertia_follows_the_box_formula(self):
        model = model_of(dynamic_sdf(DECLARATION))
        sx, sy, sz = DECLARATION.size_m
        mass = DECLARATION.mass_kg
        self.assertAlmostEqual(
            float(model.findtext("link/inertial/inertia/ixx")),
            mass * (sy * sy + sz * sz) / 12.0)
        self.assertAlmostEqual(
            float(model.findtext("link/inertial/inertia/izz")),
            mass * (sx * sx + sy * sy) / 12.0)


class TestDeclarationsComeFromConfig(unittest.TestCase):
    def test_only_materials_are_declared(self):
        out = declarations_from_config(CONFIG, FRAMES, source="config/test.json")
        self.assertEqual(set(out), {"part_x"})

    def test_home_pose_follows_declared_frame_parentage(self):
        out = declarations_from_config(CONFIG, FRAMES)
        for value, expected in zip(out["part_x"].home_pose_m, (0.5, 0.2, 0.84)):
            self.assertAlmostEqual(value, expected)

    def test_values_are_not_invented(self):
        out = declarations_from_config(CONFIG, FRAMES)
        item = out["part_x"]
        self.assertEqual(item.size_m, (0.05, 0.05, 0.1))
        self.assertEqual(item.mass_kg, 0.2)
        self.assertEqual(item.friction, 0.8)

    def test_material_without_a_size_is_an_error_not_a_default(self):
        broken = {"part_y": {"kind": "material", "frame": "part_x_frame",
                             "mass_kg": 0.2, "friction": 0.8,
                             "color_rgba": [0, 0, 0, 1]}}
        with self.assertRaises(KeyError):
            declarations_from_config(broken, FRAMES)


class TestEventsAreMarkedAsFixture(unittest.TestCase):
    def test_every_event_says_it_is_a_simulation_fixture(self):
        event = FixtureEvent(kind="attach", model="part_x", at=1.0,
                             pose_m=(0.5, 0.2, 0.84), verified=True)
        payload = event.to_dict()
        self.assertEqual(payload["fixture_kind"], FIXTURE_KIND)
        self.assertEqual(FIXTURE_KIND, "simulation_fixture")

    def test_unverified_event_keeps_its_detail(self):
        event = FixtureEvent(kind="detach", model="part_x", at=1.0,
                             pose_m=None, verified=False, detail="확인 실패")
        payload = event.to_dict()
        self.assertFalse(payload["verified"])
        self.assertIsNone(payload["pose_m"])
        self.assertEqual(payload["detail"], "확인 실패")


class TestUndeclaredObjectsAreRefused(unittest.TestCase):
    def fixture(self) -> GazeboObjectFixture:
        return GazeboObjectFixture(
            world_name="w", gz_partition="p",
            objects={"part_x": DECLARATION})

    def test_declaration_lookup_refuses_unknown_models(self):
        with self.assertRaises(FixtureError) as caught:
            self.fixture()._declaration("part_zzz")
        self.assertIs(caught.exception.reason,
                      ReasonCode.EXEC_SIM_FIXTURE_FAILED)

    def test_follow_refuses_objects_that_are_not_held(self):
        with self.assertRaises(FixtureError) as caught:
            self.fixture().follow("part_x", (0.0, 0.0, 1.0))
        self.assertIs(caught.exception.reason,
                      ReasonCode.EXEC_SIM_FIXTURE_FAILED)

    def test_nothing_is_held_before_attaching(self):
        self.assertEqual(self.fixture().held(), ())

    def test_close_without_a_node_is_safe(self):
        self.fixture().close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
