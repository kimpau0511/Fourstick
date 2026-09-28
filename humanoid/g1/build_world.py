"""G1(12자유도 다리) Gazebo 세계 만들기 — FR3 작업 셀과 **별개**다.

    python3 humanoid/g1/build_world.py    # → /tmp/forstick2_humanoid/g1_world.sdf

원본: unitree_rl_gym `resources/robots/g1_description/g1_12dof.urdf`(BSD-3-Clause,
Unitree Robotics). 이 파일을 복사하지 않고 읽어서, 메시 경로를 절대 경로로 바꾸고 Gazebo
플러그인만 덧붙여 SDF로 변환한다(`gz sdf -p`).

- 관절 토크: `ApplyJointForce`(관절마다 `/model/g1_12dof/joint/<관절>/cmd_force`). PD는 제어기가 **물리 한
  스텝마다** 계산한다(학습·MuJoCo 배치와 같은 식: kp(q*-q) − kd·q̇, 토크 한계로 자름).
- 관측: `JointStatePublisher`(관절 위치·속도) · 골반 IMU(각속도·방향) · 발 접촉 센서
  (`/g1/contact/<left|right>` — 발 충돌 구 4개가 지면에 닿는지, 검증 전용).
- 관절 감쇠·마찰은 MJCF 기본값(0.001, 0.1)을 옮긴다. armature(0.01)는 SDF/DART에 대응하는
  값이 없어 옮기지 못한다(차이로 기록).
- 물리 스텝 0.002 s(MuJoCo 배치와 같음). 세계는 멈춘 상태로 뜨고 제어기가 스텝을 민다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

G1_DESC = Path(os.environ.get("FORSTICK2_G1_DESC", "/home/asd/external/unitree_rl_gym/resources/robots/g1_description"))
OUT_DIR = Path(os.environ.get("FORSTICK2_HUMANOID_DIR", "/tmp/forstick2_humanoid"))
LEG_JOINTS = ("left_hip_pitch_joint", "left_hip_roll_joint", "left_hip_yaw_joint",
              "left_knee_joint", "left_ankle_pitch_joint", "left_ankle_roll_joint",
              "right_hip_pitch_joint", "right_hip_roll_joint", "right_hip_yaw_joint",
              "right_knee_joint", "right_ankle_pitch_joint", "right_ankle_roll_joint")
PHYSICS_DT = 0.002
MODEL = "g1_12dof"          # 원본 URDF의 robot 이름 그대로
WORLD = "forstick2_humanoid"
START_Z = 0.793


# 제어 방식: "force" = 관절별 ApplyJointForce(파이썬이 스텝마다 토크, verify_walk용),
# "pd" = G1PdController 플러그인(Gazebo 안 500 Hz PD, 파이썬은 50 Hz 목표 — 연속 제어용).
# 둘은 같은 관절 힘을 쓰므로 한 세계에 함께 두지 않는다.
KPS = "100 100 100 150 40 40 100 100 100 150 40 40"
KDS = "2 2 2 4 2 2 2 2 2 4 2 2"
EFFORTS = "88 139 88 139 50 50 88 139 88 139 50 50"
DEFAULT = "-0.1 0 0 0.3 -0.2 0 -0.1 0 0 0.3 -0.2 0"


def gazebo_tags(control: str = "force") -> str:
    plugins = [
        '<plugin filename="gz-sim-joint-state-publisher-system" '
        'name="gz::sim::systems::JointStatePublisher">'
        '<topic>/g1/joint_state</topic></plugin>']
    if control == "pd":
        plugins.append(
            '<plugin filename="G1PdController" name="forstick2::G1PdController">'
            f'<joints>{" ".join(LEG_JOINTS)}</joints><kp>{KPS}</kp><kd>{KDS}</kd>'
            f'<effort>{EFFORTS}</effort><default>{DEFAULT}</default>'
            '<target_topic>/g1/pd/target</target_topic><ack_topic>/g1/pd/ack</ack_topic>'
            '</plugin>')
    else:
        for joint in LEG_JOINTS:
            plugins.append(
                '<plugin filename="gz-sim-apply-joint-force-system" '
                f'name="gz::sim::systems::ApplyJointForce"><joint_name>{joint}</joint_name>'
                '</plugin>')
    imu = ('<gazebo reference="pelvis"><sensor name="pelvis_imu" type="imu">'
           '<always_on>1</always_on><update_rate>500</update_rate>'
           '<topic>/g1/imu</topic></sensor></gazebo>')
    friction = "".join(
        f'<gazebo reference="{side}_ankle_roll_link"><mu1>1.0</mu1><mu2>1.0</mu2></gazebo>'
        for side in ("left", "right"))
    contact = "".join(
        f'<gazebo reference="{side}_ankle_roll_link"><sensor name="{side}_foot_contact" type="contact">'
        '<always_on>1</always_on><update_rate>500</update_rate><contact>'
        + "".join(f"<collision>{side}_ankle_roll_link_collision{sfx}</collision>"
                  for sfx in ("", "_1", "_2", "_3"))
        + f'</contact><topic>/g1/contact/{side}</topic></sensor></gazebo>'
        for side in ("left", "right"))
    return f"<gazebo>{''.join(plugins)}</gazebo>{imu}{friction}{contact}"


def model_sdf(control: str = "force") -> str:
    urdf = (G1_DESC / "g1_12dof.urdf").read_text(encoding="utf-8")
    urdf = re.sub(r'filename="meshes/', f'filename="file://{G1_DESC}/meshes/', urdf)
    # MJCF 기본 관절 감쇠·마찰(armature는 옮길 수 없다).
    urdf = re.sub(r"(<joint name=\"[^\"]+_joint\" type=\"revolute\">)",
                  r'\1<dynamics damping="0.001" friction="0.1"/>', urdf)
    urdf = urdf.replace("</robot>", gazebo_tags(control) + "</robot>")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src = OUT_DIR / f"g1_12dof_gazebo_{control}.urdf"
    src.write_text(urdf, encoding="utf-8")
    sdf = subprocess.run(["gz", "sdf", "-p", str(src)], capture_output=True, text=True,
                         check=True).stdout
    # 모델 요소만 꺼낸다.
    return sdf[sdf.index("<model"):sdf.rindex("</model>") + len("</model>")]


def conveyor_sdf(site: dict) -> str:
    c = site["conveyor"]
    (x, y, z), (sx, sy, sz) = c["center_m"], c["size_m"]
    box = f"<geometry><box><size>{sx} {sy} {sz}</size></box></geometry>"
    return (f'<model name="conveyor"><static>true</static><pose>{x} {y} {z} 0 0 0</pose>'
            f'<link name="link"><collision name="c">{box}</collision>'
            f'<visual name="v">{box}<material><diffuse>0.3 0.3 0.35 1</diffuse></material></visual>'
            '</link></model>')


def world_sdf(model: str, world: str = WORLD, extra: str = "", spawn_xy=(0.0, 0.0)) -> str:
    model = model.replace(">", f"><pose>{spawn_xy[0]} {spawn_xy[1]} {START_Z} 0 0 0</pose>", 1)
    return f"""<?xml version="1.0"?>
<sdf version="1.10">
  <world name="{world}">
    <physics name="dt" type="dart">
      <max_step_size>{PHYSICS_DT}</max_step_size>
      <real_time_factor>1.0</real_time_factor>
    </physics>
    <gravity>0 0 -9.81</gravity>
    <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
    <plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
    <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
    <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
    <plugin filename="gz-sim-contact-system" name="gz::sim::systems::Contact"/>
    <light type="directional" name="sun"><pose>0 0 10 0 0 0</pose><direction>-0.5 0.1 -0.9</direction></light>
    <model name="ground">
      <static>true</static>
      <link name="link">
        <collision name="c"><geometry><plane><normal>0 0 1</normal><size>50 50</size></plane></geometry>
          <surface><friction><ode><mu>1.0</mu><mu2>1.0</mu2></ode></friction></surface></collision>
        <visual name="v"><geometry><plane><normal>0 0 1</normal><size>50 50</size></plane></geometry></visual>
      </link>
    </model>
    {extra}
    {model}
  </world>
</sdf>
"""


SITE = Path(__file__).resolve().parent / "config/site.json"
MANIP_SITE = Path(os.environ.get("FORSTICK2_G1_MANIP_SITE",
                                  Path(__file__).resolve().parent / "config/manip_site.json"))
#: 오른팔 접촉 감지(받침대·물체와 닿는지). 손바닥은 wrist_yaw 링크에 합쳐진다(gz sdf 변환).
RIGHT_ARM_CONTACT_LINKS = (
    "right_elbow_link", "right_wrist_roll_link", "right_wrist_pitch_link", "right_wrist_yaw_link",
    "right_hand_thumb_0_link", "right_hand_thumb_1_link", "right_hand_thumb_2_link",
    "right_hand_middle_0_link", "right_hand_middle_1_link", "right_hand_index_0_link",
    "right_hand_index_1_link")


#: 명시(explicit) PD의 수치 안정 한계. 플러그인은 이전 스텝 상태로 토크를 계산하므로, 관성이 작은
#: 관절(손가락 말단 I≈1.8e-5 kg·m²)은 kd·dt/I가 2를 넘으면 발산한다(실측: 손가락 이득 kd 0.02에서
#: kd·dt/I=2.23 → 관절 속도 37 rad/s, 물리 스텝 멈춤). 여유를 두고 아래 비율로 자른다.
MAX_KD_DT_OVER_I = 0.5
MAX_KP_DT2_OVER_I = 0.1


def stable_gains(name: str, inertia: float) -> tuple[float, float]:
    kp, kd = upper_gains(name)
    return (min(kp, MAX_KP_DT2_OVER_I * inertia / PHYSICS_DT ** 2),
            min(kd, MAX_KD_DT_OVER_I * inertia / PHYSICS_DT))


def upper_gains(name: str) -> tuple[float, float]:
    """상체 PD 이득 기본값(선택값 — 통과하도록 맞춘 값이 아니다). 허리는 사실상 고정.
    실제로 쓰는 값은 `stable_gains`가 관절 유효 관성으로 수치 안정 한계를 적용한 값이다."""
    if name.startswith("waist"):
        return 300.0, 5.0
    if "hand" in name:
        return 2.0, 0.02
    if "wrist_roll" in name:
        return 20.0, 0.5
    if "wrist" in name:
        return 10.0, 0.3
    return 60.0, 1.5                       # shoulder·elbow


def manip_model_sdf(site: dict) -> tuple[str, list[str]]:
    """손 달린 G1(다리 PD + 상체 PD 두 인스턴스 + 오른팔 접촉 센서). 상체 관절 이름 목록도 돌려준다."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from urdf_kin import Robot
    src_urdf = G1_DESC / site["model_urdf"]
    robot = Robot(src_urdf)
    # 하체 PD 묶음: 기본은 다리 12(unitree 보행 정책). 설정에 lower가 있으면 그 관절·이득(예: GR00T 균형 정책 =
    # 다리 12 + 허리 3). 나머지 가동 관절은 상체 PD.
    lower = site.get("lower") or {"joints": list(LEG_JOINTS), "kp": KPS.split(), "kd": KDS.split(),
                                  "default": DEFAULT.split()}
    lower_joints = list(lower["joints"])
    upper = [j.name for j in robot.movable() if j.name not in lower_joints]
    kp, kd, eff, table = [], [], [], {}
    for name in upper:
        inertia = robot.effective_inertia(name)
        p, d = stable_gains(name, inertia)
        kp.append(round(p, 5))
        kd.append(round(d, 6))
        eff.append(robot.joints[name].effort)
        table[name] = {"inertia": inertia, "kp": kp[-1], "kd": kd[-1], "nominal": upper_gains(name),
                       "effort": eff[-1]}
    (OUT_DIR / "g1_manip_upper_gains.json").parent.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "g1_manip_upper_gains.json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    urdf = src_urdf.read_text(encoding="utf-8")
    urdf = re.sub(r'filename="meshes/', f'filename="file://{G1_DESC}/meshes/', urdf)

    # 다리 링크의 관성은 보행 정책이 학습·검증된 모델(g1_12dof.urdf)의 값을 쓴다. 상체 URDF들은 발목
    # 링크 관성이 실제 값(8.4e-6 ~ 1.6e-3 kg·m²)이고 정책 모델은 0.01로 키워져 있다. 실제 값으로는 발목 PD가
    # 수치적으로 발산해 1.3 s 안에 넘어졌다(실측: 발목 roll 관절 속도 한계 37 rad/s에서 진동).
    # 질량·질량 중심은 같고 관성 텐서만 다르다 — 바꾼 링크를 기록한다.
    policy_urdf = (G1_DESC / "g1_12dof.urdf").read_text(encoding="utf-8")
    replaced = []
    for joint in LEG_JOINTS:
        link = robot.joints[joint].child
        block = rf'(<link name="{link}">\s*<inertial>.*?</inertial>)'
        src_block = re.search(block, urdf, flags=re.S)
        pol_block = re.search(block, policy_urdf, flags=re.S)
        if src_block and pol_block and src_block.group(1) != pol_block.group(1):
            urdf = urdf.replace(src_block.group(1), pol_block.group(1), 1)
            replaced.append(link)
    (OUT_DIR / "g1_manip_leg_inertia_from_policy_model.json").write_text(
        json.dumps({"replaced_links": replaced, "source": "g1_12dof.urdf"}), encoding="utf-8")

    # 관절 감쇠·마찰: 보행 모델과 같은 값. 손가락은 링크가 작아(20–86 g) 명시 PD만으로 떨 수 있어
    # 감쇠를 더 준다(손가락 관절만).
    def dyn(m):
        # 손가락: 감쇠 0.05(암묵 — 수치 안정), 마찰 0. 다리 값(마찰 0.1 N·m)을 손가락에 주자 말단 관절이
        # 전혀 움직이지 않았다(실측: 닫기 명령 1.3 rad에 관측 0.0). 손 MJCF에도 마찰이 없다.
        if "_hand_" in m.group(1):
            return m.group(0) + '<dynamics damping="0.05" friction="0.0"/>'
        return m.group(0) + '<dynamics damping="0.001" friction="0.1"/>'

    urdf = re.sub(r'<joint name="([^"]+_joint)" type="revolute">', dyn, urdf)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    probe_path = OUT_DIR / "manip_probe.urdf"
    probe_path.write_text(urdf, encoding="utf-8")
    probe = subprocess.run(["gz", "sdf", "-p", str(probe_path)], capture_output=True, text=True,
                           check=True).stdout
    sensors = []
    for link in RIGHT_ARM_CONTACT_LINKS:
        names = re.findall(rf"<collision name='({link}_collision[^']*)'", probe) or \
            re.findall(rf"<collision name='({link}_fixed_joint_lump__[^']*)'", probe)
        if names:
            sensors.append(
                f'<gazebo reference="{link}"><sensor name="{link}_contact" type="contact">'
                '<always_on>1</always_on><update_rate>50</update_rate><contact>'
                + "".join(f"<collision>{n}</collision>" for n in names)
                + '</contact></sensor></gazebo>')
    upper_plugin = (
        '<plugin filename="G1PdController" name="forstick2::G1PdController">'
        f'<joints>{" ".join(upper)}</joints><kp>{" ".join(map(str, kp))}</kp>'
        f'<kd>{" ".join(map(str, kd))}</kd><effort>{" ".join(map(str, eff))}</effort>'
        f'<default>{" ".join("0" for _ in upper)}</default>'
        '<target_topic>/g1/upper/target</target_topic><ack_topic>/g1/upper/ack</ack_topic>'
        '</plugin>')
    # 조작 세계는 관절 43·링크 44·접촉 센서 13으로 메시지가 많다. 500 Hz 그대로면 파이썬 수신이 밀려
    # 작은 확인 응답까지 버려졌다(실측: PD 확인 응답 유실). 제어 주기(50 Hz)에 맞춰 줄인다.
    lower_eff = [robot.joints[n].effort for n in lower_joints]
    tags = gazebo_tags("pd")
    tags = re.sub(r"<joints>.*?</joints><kp>.*?</kp><kd>.*?</kd><effort>.*?</effort><default>.*?</default>",
                  f"<joints>{' '.join(lower_joints)}</joints><kp>{' '.join(map(str, lower['kp']))}</kp>"
                  f"<kd>{' '.join(map(str, lower['kd']))}</kd><effort>{' '.join(map(str, lower_eff))}</effort>"
                  f"<default>{' '.join(map(str, lower['default']))}</default>", tags, count=1)
    tags = tags.replace(
        "<topic>/g1/joint_state</topic></plugin>",
        "<topic>/g1/joint_state</topic><update_rate>50</update_rate></plugin>", 1)
    tags = tags.replace("<update_rate>500</update_rate>", "<update_rate>50</update_rate>")
    tags = tags.replace("</gazebo>", upper_plugin + "</gazebo>", 1)
    urdf = urdf.replace("</robot>", tags + "".join(sensors) + "</robot>")
    src = OUT_DIR / "g1_manip_gazebo.urdf"
    src.write_text(urdf, encoding="utf-8")
    sdf = subprocess.run(["gz", "sdf", "-p", str(src)], capture_output=True, text=True,
                         check=True).stdout
    return sdf[sdf.index("<model"):sdf.rindex("</model>") + len("</model>")], upper


def manip_scene_sdf(site: dict) -> str:
    c, o = site["conveyor"], site["object"]
    (x, y, z), (sx, sy, sz) = c["center_m"], c["size_m"]
    col = f"<geometry><box><size>{sx} {sy} {sz}</size></box></geometry>"
    (ox, oy, oz), (osx, osy, osz) = o["pose_m"], o["size_m"]
    ob = f"<geometry><box><size>{osx} {osy} {osz}</size></box></geometry>"
    m = o["mass_kg"]
    ixx, iyy, izz = (m * (osy ** 2 + osz ** 2) / 12, m * (osx ** 2 + osz ** 2) / 12,
                     m * (osx ** 2 + osy ** 2) / 12)
    return (f'<model name="pedestal"><static>true</static><pose>{x} {y} {z} 0 0 0</pose>'
            f'<link name="link"><collision name="c">{col}</collision><visual name="v">{col}'
            '<material><diffuse>0.55 0.5 0.45 1</diffuse></material></visual></link></model>'
            f'<model name="{o["model"]}"><pose>{ox} {oy} {oz} 0 0 0</pose><link name="link">'
            f'<inertial><mass>{m}</mass><inertia><ixx>{ixx}</ixx><iyy>{iyy}</iyy><izz>{izz}</izz>'
            '<ixy>0</ixy><ixz>0</ixz><iyz>0</iyz></inertia></inertial>'
            f'<collision name="c">{ob}<surface><friction><ode><mu>0.8</mu><mu2>0.8</mu2></ode>'
            f'</friction></surface></collision><visual name="v">{ob}'
            '<material><diffuse>0.2 0.6 0.9 1</diffuse></material></visual></link></model>')


def main() -> int:
    import json
    import sys
    if "--manip" in sys.argv:
        # 팔·손 조작 실험: 손 달린 G1 + 받침대·물체. 보행·왕복 세계와 따로 둔다.
        site = json.loads(MANIP_SITE.read_text(encoding="utf-8"))
        model, upper = manip_model_sdf(site)
        out = OUT_DIR / site.get("world_file", "g1_manip_world.sdf")
        out.write_text(world_sdf(model, site["world"], manip_scene_sdf(site),
                                 site.get("spawn_xy", (0.0, 0.0))), encoding="utf-8")
        (OUT_DIR / (site.get("world_file", "g1_manip_world.sdf").replace(".sdf", "_upper_joints.json"))
         ).write_text(json.dumps(upper), encoding="utf-8")
    elif "--nav" in sys.argv:
        # 연속 제어·왕복용: PD 플러그인 + 컨베이어. 보행 검증 세계(g1_world.sdf)와 따로 둔다.
        site = json.loads(SITE.read_text(encoding="utf-8"))
        out = OUT_DIR / "g1_nav_world.sdf"
        out.write_text(world_sdf(model_sdf("pd"), site["world"], conveyor_sdf(site)),
                       encoding="utf-8")
    else:
        out = OUT_DIR / "g1_world.sdf"
        out.write_text(world_sdf(model_sdf()), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
