"""G1 3D 화면의 **읽기 전용** 데이터 — 모델(시각 메시)·관측 상태.

- 모델: unitree_rl_gym `g1_12dof.urdf`의 시각 메시(충돌·플러그인 제외). 팔·허리는 URDF에서 고정
  관절이다 — 화면에 “다리 12관절 제어 · 팔·허리 고정”으로 적는다. 컨베이어 상자·안전 지점은
  `humanoid/g1/config/site.json`에서만 온다.
- 상태: 관절(12)·골반 world pose는 Gazebo 관측(`G1Bridge`)만. 시뮬레이션 시각과 받은 벽시계
  시각을 따로 준다. 낡았으면 stale — 화면은 멈춘다.
"""

from __future__ import annotations

import os
from pathlib import Path

from server.sim_view import load_robot_model

G1_DESC = Path(os.environ.get("FORSTICK2_G1_DESC",
                              "/home/asd/external/unitree_rl_gym/resources/robots/g1_description"))
URL_PREFIX = "/v1/humanoid/view/mesh"


class HumanoidView:
    def __init__(self, bridge, site: dict):
        self.bridge = bridge
        self.site = site
        self._model = None
        self._meshes: dict[str, Path] = {}

    def model(self) -> dict:
        if self._model is not None:
            return self._model
        urdf = G1_DESC / "g1_12dof.urdf"
        if not urdf.is_file():
            return {"available": False, "detail": f"G1 URDF가 없다: {urdf}"}

        def resolve(filename: str):
            name = filename.split("meshes/", 1)[-1]
            target = (G1_DESC / "meshes" / name).resolve()
            return target if target.is_relative_to((G1_DESC / "meshes").resolve()) else None

        text, meshes, missing = load_robot_model(urdf, resolve, URL_PREFIX)
        self._meshes = meshes
        c = self.site["conveyor"]
        self._model = {
            "available": True, "robot": "unitree_g1", "urdf": text, "missing_meshes": missing,
            "controlled_joints": list(__import__("robots.g1_gazebo.bridge",
                                                 fromlist=["LEG_JOINTS"]).LEG_JOINTS),
            "fixed_note": "다리 12관절 제어 · 팔·허리 고정(URDF 고정 관절)",
            "conveyor": {"center_m": c["center_m"], "size_m": c["size_m"]},
            "safe_points": {n: {"xy_m": p["xy_m"], "yaw_rad": p["yaw_rad"],
                                "label": p.get("label_ko", n)}
                            for n, p in self.site["safe_points"].items()},
            "keep_out_x_m": c["front_face_x_m"] - self.site["keep_out"]["min_pelvis_to_front_face_m"],
            "source": "unitree_rl_gym g1_12dof.urdf (BSD-3-Clause)",
        }
        return self._model

    def mesh(self, key: str, name: str) -> Path | None:
        if not self._meshes:
            self.model()
        target = self._meshes.get(key)
        return target if target is not None and target.name == name else None

    def state(self) -> dict:
        return self.bridge.view_state()
