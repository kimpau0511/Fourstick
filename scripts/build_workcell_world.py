#!/usr/bin/env python3
"""작업 셀 world SDF 생성 (md/개발플랜.md 8-08 우선순위 4).

`config/workcell/fr3_2f85_workcell.json` 하나에서 SDF를 만든다. 손으로 쓴 SDF와
설정이 어긋나면 "설정에 있는 팔레트"와 "Gazebo에 있는 팔레트"가 달라진다 —
그 상태로는 자원 매핑을 근거라고 부를 수 없다.

기존 world는 건드리지 않는다:
- `config/gazebo/fr3_cell.sdf` (arm-only 기준선, 체크섬 보호)
- `config/gazebo/fr3_2f85_cell.sdf` (단순 조립 셀)
새 파일 `config/gazebo/fr3_2f85_workcell.sdf`만 쓴다.

모든 형상에 visual과 collision을 **둘 다** 넣는다. 하나만 있으면 화면에는
보이는데 충돌하지 않거나(또는 반대로) 검증이 뜻을 잃는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "config/gazebo/fr3_2f85_workcell.sdf"


def resolve_frame(frames: dict, name: str) -> list[float]:
    """프레임 체인을 따라 world 기준 좌표를 만든다(회전은 모두 0인 셀이다)."""
    out = [0.0, 0.0, 0.0]
    seen = set()
    while name is not None:
        if name in seen:
            raise SystemExit(f"프레임 순환: {name}")
        seen.add(name)
        frame = frames[name]
        if any(frame["rpy_rad"]):
            raise SystemExit(
                f"프레임 {name}에 회전이 있다 — 이 생성기는 회전을 다루지 않는다"
            )
        out = [a + b for a, b in zip(out, frame["xyz_m"])]
        name = frame["parent"]
    return out


def box_link(name: str, size, center, color, *, collision_bitmask: int = 1) -> str:
    sx, sy, sz = size
    cx, cy, cz = center
    r, g, b, a = color
    return f"""    <link name="{name}">
      <pose>{cx} {cy} {cz} 0 0 0</pose>
      <collision name="{name}_collision">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <surface>
          <contact><collide_bitmask>{collision_bitmask}</collide_bitmask></contact>
        </surface>
      </collision>
      <visual name="{name}_visual">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <material>
          <ambient>{r * 0.6} {g * 0.6} {b * 0.6} {a}</ambient>
          <diffuse>{r} {g} {b} {a}</diffuse>
        </material>
      </visual>
    </link>
"""


def box_inertia(mass: float, size) -> str:
    sx, sy, sz = size
    ixx = mass * (sy * sy + sz * sz) / 12.0
    iyy = mass * (sx * sx + sz * sz) / 12.0
    izz = mass * (sx * sx + sy * sy) / 12.0
    return f"""      <inertial>
        <mass>{mass}</mass>
        <inertia>
          <ixx>{ixx:.8f}</ixx><iyy>{iyy:.8f}</iyy><izz>{izz:.8f}</izz>
          <ixy>0</ixy><ixz>0</ixz><iyz>0</iyz>
        </inertia>
      </inertial>
"""


def main() -> int:
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    frames = data["frames"]
    models = data["models"]
    world_name = data["world_name"]

    blocks: list[str] = []

    # 바닥
    floor = models["floor"]
    fx, fy = floor["size_m"]
    r, g, b, a = floor["color_rgba"]
    blocks.append(f"""  <model name="floor">
    <static>true</static>
    <link name="link">
      <collision name="collision">
        <geometry><plane><normal>0 0 1</normal><size>{fx} {fy}</size></plane></geometry>
      </collision>
      <visual name="visual">
        <geometry><plane><normal>0 0 1</normal><size>{fx} {fy}</size></plane></geometry>
        <material>
          <ambient>{r * 0.8} {g * 0.8} {b * 0.8} {a}</ambient>
          <diffuse>{r} {g} {b} {a}</diffuse>
        </material>
      </visual>
    </link>
  </model>
""")

    for name, model in models.items():
        if name == "floor":
            continue
        origin = resolve_frame(frames, model["frame"])
        if model["kind"] == "material":
            size = model["size_m"]
            r, g, b, a = model["color_rgba"]
            mu = model["friction"]
            sx, sy, sz = size
            blocks.append(f"""  <model name="{name}">
    <pose>{origin[0]} {origin[1]} {origin[2]} 0 0 0</pose>
    <link name="body">
{box_inertia(model["mass_kg"], size)}      <collision name="collision">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <surface>
          <friction><ode><mu>{mu}</mu><mu2>{mu}</mu2></ode></friction>
          <contact><collide_bitmask>1</collide_bitmask></contact>
        </surface>
      </collision>
      <visual name="visual">
        <geometry><box><size>{sx} {sy} {sz}</size></box></geometry>
        <material>
          <ambient>{r * 0.6} {g * 0.6} {b * 0.6} {a}</ambient>
          <diffuse>{r} {g} {b} {a}</diffuse>
        </material>
      </visual>
    </link>
  </model>
""")
            continue

        links = "".join(
            box_link(part["name"], part["size_m"], part["center_xyz_m"],
                     part["color_rgba"])
            for part in model["parts"]
        )
        blocks.append(f"""  <model name="{name}">
    <static>{"true" if model["static"] else "false"}</static>
    <pose>{origin[0]} {origin[1]} {origin[2]} 0 0 0</pose>
{links}  </model>
""")

    # GUI 카메라: 로봇·받침대·작업대·팔레트 3개·자재 3개·컨베이어가 한 화면에.
    camera = data.get("gui_camera_pose", "1.55 -1.25 1.45 0 0.42 2.52")

    # 장면 카메라(선택). **GUI 렌더러와 별개로 서버가 렌더링한다.**
    # GUI가 부하로 캡처 슬롯을 주지 못할 때도 "화면에 무엇이 있는가"를
    # 파일로 확인할 수 있어야 한다. 끄면 센서 시스템도 올리지 않는다.
    scene_camera = data.get("scene_camera") or {}
    camera_block, sensors_plugin = "", ""
    if scene_camera.get("enabled"):
        save_path = scene_camera["save_path"]
        width = int(scene_camera.get("width", 640))
        height = int(scene_camera.get("height", 480))
        rate = float(scene_camera.get("update_rate_hz", 1.0))
        sensors_plugin = (
            '    <plugin filename="gz-sim-sensors-system"\n'
            '            name="gz::sim::systems::Sensors">\n'
            '      <render_engine>ogre2</render_engine>\n'
            '    </plugin>\n')
        camera_block = f"""  <model name="scene_camera">
    <static>true</static>
    <pose>{scene_camera.get("pose", camera)}</pose>
    <link name="link">
      <sensor name="camera" type="camera">
        <update_rate>{rate}</update_rate>
        <always_on>1</always_on>
        <camera>
          <horizontal_fov>{scene_camera.get("horizontal_fov_rad", 1.047)}</horizontal_fov>
          <image><width>{width}</width><height>{height}</height></image>
          <clip><near>0.05</near><far>30</far></clip>
          <!-- 서버가 프레임을 이 디렉터리에 저장한다. GUI와 무관하다. -->
          <save enabled="true"><path>{save_path}</path></save>
        </camera>
      </sensor>
    </link>
  </model>
"""

    sdf = f"""<?xml version="1.0" ?>
<!-- **생성 파일이다. 손으로 고치지 않는다.**
     출처: config/workcell/fr3_2f85_workcell.json
     생성: python3 scripts/build_workcell_world.py

     forstick2 8-08 FR3-WMS + GRP-CPL-062 + 2F-85 **작업 셀**.
     기존 world를 건드리지 않는다:
      - config/gazebo/fr3_cell.sdf       (arm-only 기준선, 체크섬 보호)
      - config/gazebo/fr3_2f85_cell.sdf  (단순 조립 셀)

     치수·좌표는 **시뮬레이션 설계값**이다. 실제 설치 현장 실측값이 아니다.
     모든 형상에 visual과 collision을 둘 다 둔다. -->
<sdf version="1.9">
  <world name="{world_name}">
    <physics name="1ms" type="ignored">
      <max_step_size>0.001</max_step_size>
      <real_time_factor>1.0</real_time_factor>
    </physics>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system"
            name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system"
            name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-contact-system"
            name="gz::sim::systems::Contact"/>
{sensors_plugin}
    <light type="directional" name="sun">
      <cast_shadows>false</cast_shadows>
      <pose>0 0 6 0 0 0</pose>
      <diffuse>0.9 0.9 0.9 1</diffuse>
      <specular>0.2 0.2 0.2 1</specular>
      <direction>-0.4 0.3 -0.9</direction>
    </light>

{"".join(blocks)}{camera_block}
    <gui fullscreen="0">
      <plugin filename="MinimalScene" name="3D View">
        <gz-gui>
          <title>forstick2 · FR3-WMS + 2F-85 workcell</title>
          <property type="bool" key="showTitleBar">false</property>
        </gz-gui>
        <!-- 실행 스크립트의 GUI 렌더 엔진 인자와 맞춘다. -->
        <engine>ogre2</engine>
        <scene>scene</scene>
        <ambient_light>0.5 0.5 0.5</ambient_light>
        <background_color>0.18 0.20 0.24</background_color>
        <camera_pose>{camera}</camera_pose>
      </plugin>
      <plugin filename="GzSceneManager" name="Scene Manager"/>
      <!-- 화면에 실제로 무엇이 그려지는지 파일로 확인한다.
           /gui/screenshot 서비스는 인자를 디렉터리로 받는다. -->
      <plugin filename="Screenshot" name="Screenshot"/>
      <plugin filename="InteractiveViewControl" name="Interactive view control"/>
      <plugin filename="CameraTracking" name="Camera Tracking"/>
      <plugin filename="WorldControl" name="World control">
        <gz-gui>
          <anchors target="3D View">
            <line own="left" target="left"/>
            <line own="bottom" target="bottom"/>
          </anchors>
        </gz-gui>
        <use_event>true</use_event>
      </plugin>
      <plugin filename="WorldStats" name="World stats">
        <gz-gui>
          <anchors target="3D View">
            <line own="right" target="right"/>
            <line own="bottom" target="bottom"/>
          </anchors>
        </gz-gui>
      </plugin>
      <plugin filename="ComponentInspector" name="Component inspector"/>
      <plugin filename="EntityTree" name="Entity tree"/>
    </gui>
  </world>
</sdf>
"""
    OUT.write_text(sdf, encoding="utf-8")

    import xml.etree.ElementTree as ET
    root = ET.parse(OUT).getroot()
    model_names = [m.get("name") for m in root.findall(".//world/model")]
    collisions = len(root.findall(".//collision"))
    visuals = len(root.findall(".//visual"))
    print(f"{OUT.relative_to(ROOT)} 생성 — 모델 {len(model_names)}개"
          f" · collision {collisions} · visual {visuals}")
    print(f"  모델: {', '.join(model_names)}")
    if collisions != visuals:
        print(f"  **collision {collisions} != visual {visuals}** — 형상이 짝을 이루지 않는다",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
