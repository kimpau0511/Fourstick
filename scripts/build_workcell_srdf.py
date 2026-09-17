#!/usr/bin/env python3
"""작업 셀 MoveIt2 기본 SRDF 생성 (8-08 우선순위 3·4).

안전 home의 관절값을 **손으로 옮겨 적지 않는다.**
`config/workcell/fr3_2f85_workcell_poses.json`에서 읽는다. 그 파일의
`safe_home.status`가 `verified`가 아니면 생성하지 않는다 — 검증되지 않은
자세를 MoveIt의 명명 자세로 만들면 나중에 그것이 근거처럼 쓰인다.

arm-only SRDF와 단순 조립 SRDF는 건드리지 않는다. 새 파일만 쓴다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
OUT = ROOT / "config/moveit/fr3_2f85_workcell.base.srdf"
ARM_JOINTS = ["j1", "j2", "j3", "j4", "j5", "j6"]

#: 2F-85의 **닫힌 4절 링크**를 URDF가 열린 트리로 모델링해 생긴 쌍이다.
#: 실물에서는 핀으로 연결돼 있고(그래서 조인트가 없는데도 메시가 겹친다),
#: MoveIt은 그리퍼 전 구간(0.0~0.7929 rad, 9점)에서 **똑같이 이 2쌍만**
#: 접촉으로 보고한다 — 자세에 따라 달라지지 않는다.
#: 근거: reports/workcell/loop_closure_pairs.json
#: **계획을 통과시키려고 실제 충돌을 끄는 것이 아니다.** 모델 결함을 명시한다.
LOOP_CLOSURE_PAIRS = (
    ("robotiq_85_left_inner_knuckle_link", "robotiq_85_left_finger_tip_link"),
    ("robotiq_85_right_inner_knuckle_link", "robotiq_85_right_finger_tip_link"),
)


def group_state(name: str, joints: dict) -> str:
    rows = "\n".join(
        f'    <joint name="{key}" value="{joints[key]}"/>' for key in ARM_JOINTS)
    return f'  <group_state name="{name}" group="fr3wms_arm">\n{rows}\n  </group_state>\n'


def main() -> int:
    if not POSES.is_file():
        print(f"유도된 자세 파일이 없다: {POSES}\n"
              "먼저 실행한다: python3 scripts/derive_workcell_poses.py",
              file=sys.stderr)
        return 2
    data = json.loads(POSES.read_text(encoding="utf-8"))
    home = data["safe_home"]
    if home.get("status") != "verified":
        print(f"안전 home이 verified가 아니다({home.get('status')}) —"
              " 검증되지 않은 자세를 명명 자세로 만들지 않는다", file=sys.stderr)
        return 3

    states = [group_state("workcell_safe_home", home["joint_rad"])]
    skipped = []
    for name, pose in data["poses"].items():
        if pose.get("status") == "verified":
            states.append(group_state(name, pose["joint_rad"]))
        else:
            skipped.append(f"{name}({pose.get('reason_code', pose.get('status'))})")

    loop_rows = "".join(
        f'  <disable_collisions link1="{a}" link2="{b}"'
        f' reason="mechanism_loop_closure"/>\n'
        for a, b in LOOP_CLOSURE_PAIRS)
    srdf = f"""<?xml version="1.0"?>
<!-- **생성 파일이다. 손으로 고치지 않는다.**
     출처: config/workcell/fr3_2f85_workcell_poses.json
     생성: python3 scripts/build_workcell_srdf.py

     FR3-WMS + GRP-CPL-062 + 2F-85 **작업 셀** MoveIt2 기본 SRDF.
     기존 SRDF는 건드리지 않는다:
      - config/moveit/fr3wms_arm.base.srdf            (arm-only)
      - config/moveit/fr3wms_with_2f85.base.srdf      (단순 조립 셀)

     명명 자세는 모두 scripts/derive_workcell_poses.py가 수치 IK로 풀고
     FK·충돌 메시 간극으로 검증한 값이다. status가 verified가 아닌 자세는
     여기 넣지 않는다{"" if not skipped else " — 제외: " + ", ".join(skipped)}.

     disable_collisions는 여기 없다. collisions_updater로 생성하고
     scripts/prune_self_collision_srdf.py로 근거 없는 제외를 걷어낸다. -->
<robot name="fr3wms_with_2f85">
  <group name="fr3wms_arm">
    <chain base_link="base_link" tip_link="robotiq_85_tcp"/>
  </group>

  <group name="fr3wms_gripper">
    <joint name="robotiq_85_left_knuckle_joint"/>
  </group>

  <end_effector name="robotiq_2f85" parent_link="tool_Link"
                group="fr3wms_gripper" parent_group="fr3wms_arm"/>

{"".join(states)}
  <group_state name="open" group="fr3wms_gripper">
    <joint name="robotiq_85_left_knuckle_joint" value="0.0"/>
  </group_state>

  <group_state name="close" group="fr3wms_gripper">
    <joint name="robotiq_85_left_knuckle_joint" value="0.7929"/>
  </group_state>

  <!-- 2F-85 닫힌 4절 링크의 루프 폐쇄 쌍. URDF가 루프를 끊어 놓아 이 링크들
       사이에 조인트가 없지만 실물은 핀으로 연결돼 있고 메시가 겹친다.
       MoveIt이 그리퍼 전 구간에서 동일하게 이 2쌍만 접촉으로 보고한다.
       근거: reports/workcell/loop_closure_pairs.json -->
{loop_rows}</robot>
"""
    OUT.write_text(srdf, encoding="utf-8")
    import xml.etree.ElementTree as ET
    root = ET.parse(OUT).getroot()
    names = [s.get("name") for s in root.findall("group_state")]
    loops = root.findall("disable_collisions")
    print(f"{OUT.relative_to(ROOT)} 생성 — 명명 자세 {len(names)}개")
    print(f"  {', '.join(names)}")
    print(f"  루프 폐쇄 제외 {len(loops)}쌍"
          f" ({', '.join(e.get('reason') for e in loops)})")
    if skipped:
        print(f"  제외(미검증): {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
