"""3D 작업 셀 화면 서버 쪽 — 화면 모델 · 메시 허용 목록 · 관측 표본(낡음)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server import sim_view  # noqa: E402

WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
URDF = """<robot name="r">
  <link name="a"><visual><geometry><mesh filename="../meshes/x/a.dae"/></geometry></visual>
    <collision><geometry><mesh filename="../meshes/x/a_col.stl"/></geometry></collision></link>
  <link name="b"><visual><geometry><mesh filename="package://robotiq_description/meshes/b.stl"/></geometry></visual></link>
  <link name="c"><visual><geometry><mesh filename="../meshes/x/missing.STL"/></geometry></visual></link>
  <link name="d"><visual><geometry><box size="1 1 1"/></geometry></visual></link>
  <gazebo><plugin filename="x"/></gazebo>
</robot>"""


class ModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        (base / "fr3/urdf").mkdir(parents=True)
        (base / "fr3/meshes/x").mkdir(parents=True)
        (base / "fr3/meshes/x/a.dae").write_text("<COLLADA/>")
        (base / "robotiq/meshes").mkdir(parents=True)
        (base / "robotiq/meshes/b.stl").write_bytes(b"solid")
        self.urdf = base / "robot.urdf"
        self.urdf.write_text(URDF)

        def resolve(name):
            if name.startswith("package://robotiq_description/"):
                return base / "robotiq" / name[len("package://robotiq_description/"):]
            if name.startswith("../"):
                return (base / "fr3/urdf" / name).resolve()
            return None
        self.resolve = resolve
        self.source = {"urdf_path": self.urdf, "resolve_mesh": resolve,
                       "arm_joints": ("j1",), "gripper_joint": "g", "robot_model": "r"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_visual_meshes_only_with_allowlisted_urls(self):
        text, meshes, missing = sim_view.load_robot_model(self.urdf, self.resolve)
        self.assertNotIn("<collision", text)
        self.assertNotIn("<gazebo", text)
        self.assertIn('filename="/v1/sim-view/mesh/0/a.dae"', text)
        self.assertIn('filename="/v1/sim-view/mesh/1/b.stl"', text)
        self.assertIn('<box size="1 1 1"/>', text)          # 기본 도형은 그대로
        self.assertEqual(missing, ["../meshes/x/missing.STL"])  # 없는 파일은 빼고 알린다
        self.assertEqual(sorted(meshes), ["0", "1"])

    def test_mesh_lookup_requires_key_and_name(self):
        with mock.patch.object(sim_view.SimViewState, "start", return_value=True):
            view = sim_view.SimView(ROOT / "config/workcell/fr3_2f85_workcell.json",
                                    self.source)
            self.assertTrue(view.model()["available"])
            self.assertIsNotNone(view.mesh("0", "a.dae"))
            self.assertIsNone(view.mesh("0", "b.stl"))
            self.assertIsNone(view.mesh("9", "a.dae"))
            self.assertIsNone(view.mesh("0", "../../etc/passwd"))

    def test_cell_boxes_come_from_config(self):
        cell = sim_view.cell_boxes(WORKCELL)
        self.assertEqual([m["model"] for m in cell["materials"]],
                         ["material_a", "material_b", "material_c"])
        tray = next(b for b in cell["fixed"] if b["id"] == "pallet_1__tray")
        self.assertEqual(tray["center_xyz_m"], [0.5, 0.2, 0.77])


class AdapterSourceTest(unittest.TestCase):
    def test_adapter_supplies_robot_specific_parts(self):
        from robots.fr3_gazebo import build_view_source
        source = build_view_source()
        self.assertEqual(set(source), {"urdf_path", "resolve_mesh", "arm_joints",
                                       "gripper_joint", "robot_model"})
        self.assertIsNone(source["resolve_mesh"]("http://example.com/x.dae"))


class SampleTest(unittest.TestCase):
    def state(self, now):
        s = sim_view.SimViewState(world="w", partition="p", robot_model="r",
                                  materials=["material_a"], clock=lambda: now[0])
        return s

    def test_fresh_and_stale(self):
        now = [100.0]
        s = self.state(now)
        self.assertTrue(s.sample()["stale"])                   # 관측 없음
        s._joints = ({"j1": 0.1}, 100.0, 1)
        s._decode = lambda data: {"materials": {"material_a": [0] * 7},
                                  "robot": [0] * 7, "sim_time": 1.0}
        s._pose_raw = (b"x", 100.0, 1)
        now[0] = 100.2
        sample = s.sample()
        self.assertFalse(sample["stale"])
        self.assertAlmostEqual(sample["joint_age_sec"], 0.2)
        now[0] = 100.0 + sim_view.STALE_AFTER_SEC + 0.1
        self.assertTrue(s.sample()["stale"])                   # 오래된 관측

    def test_missing_material_is_stale(self):
        now = [100.0]
        s = self.state(now)
        s._joints = ({"j1": 0.1}, 100.0, 1)
        s._decode = lambda data: {"materials": {}, "robot": None, "sim_time": 1.0}
        s._pose_raw = (b"x", 100.0, 1)
        self.assertTrue(s.sample()["stale"])


if __name__ == "__main__":
    unittest.main()
