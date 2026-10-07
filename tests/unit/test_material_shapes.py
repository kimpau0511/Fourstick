"""자재 형상(2026-10-07: 사각형·삼각형·원형) — Gazebo world·서버 셀 정보·카탈로그가 같은 설정에서 같은 값을 낸다."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.sim_view import cell_boxes  # noqa: E402

CELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json").read_text(encoding="utf-8"))
CATALOG = json.loads((ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json").read_text(encoding="utf-8"))
WANT = {"material_a": ("box", "사각형 자재"), "material_b": ("triangle_prism", "삼각형 자재"),
        "material_c": ("cylinder", "원형 자재")}


class MaterialShapeTest(unittest.TestCase):
    def test_server_cell_info_carries_shape_name_and_id(self):
        rows = {m["model"]: m for m in cell_boxes(CELL)["materials"]}
        for model, (shape, name) in WANT.items():
            with self.subTest(model=model):
                self.assertEqual((rows[model]["shape"], rows[model]["name"]), (shape, name))
                self.assertEqual(rows[model]["resource_id"], CELL["models"][model]["resource_id"])

    def test_catalog_names_match_and_old_names_still_resolve(self):
        entries = {e["resource_id"]: e for e in CATALOG["entries"]}
        for model, (_, name) in WANT.items():
            rid = CELL["models"][model]["resource_id"]
            with self.subTest(rid=rid):
                self.assertEqual(entries[rid]["display_name"], name)
                old = {"mat_a": "A자재", "mat_b": "B자재", "mat_c": "C자재"}[rid]
                self.assertIn(old, entries[rid]["aliases"])          # 예전 이름으로 한 명령도 그대로 통한다
        # '원'처럼 한 글자 별칭은 '원래 자리'에 걸린다 — 넣지 않는다.
        self.assertNotIn("원", entries["mat_c"]["aliases"])

    def test_generated_world_uses_the_same_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "world.sdf"
            env = {**os.environ, "FORSTICK2_WORLD_SDF": str(out)}
            subprocess.run([sys.executable, str(ROOT / "scripts/build_workcell_world.py")], env=env,
                           check=True, capture_output=True)
            root = ET.parse(out).getroot()
            for model, (shape, _) in WANT.items():
                with self.subTest(model=model):
                    node = root.find(f".//model[@name='{model}']/link/collision/geometry")
                    tag = {"box": "box", "cylinder": "cylinder", "triangle_prism": "mesh"}[shape]
                    self.assertIsNotNone(node.find(tag), ET.tostring(node, encoding="unicode"))
                    visual = root.find(f".//model[@name='{model}']/link/visual/geometry")
                    self.assertIsNotNone(visual.find(tag))
            mesh = root.find(".//model[@name='material_b']/link/collision/geometry/mesh/uri").text
            text = Path(mesh.removeprefix("file://")).read_text(encoding="utf-8")
            self.assertEqual(text.count("\nvn "), 8)                # 면마다 법선(물리 엔진 요구)


if __name__ == "__main__":
    unittest.main(verbosity=2)
