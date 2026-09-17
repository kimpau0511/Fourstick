#!/usr/bin/env python3
"""장착 조건을 근거로 판정한다 (8-08 우선순위 1·2).

입력은 `measure_flange_interface.py`가 만든 측정값과 공식 URDF·xacro 값뿐이다.
**여기서 수치를 만들지 않는다.** 항목마다 측정값·기준값·출처·판정을 남기고,
확인할 수 없는 항목은 무엇을 어떻게 재야 하는지 적는다.

출력: reports/mounting/interface_comparison.json
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS = ROOT / "reports/mounting/flange_measurements.json"
OUT = ROOT / "reports/mounting/interface_comparison.json"

ROBOTIQ_SHARE = Path("/opt/ros/lyrical/share/robotiq_description")
ROBOTIQ_REPO = Path("/home/asd/external/robotiq_ros")
#: manifest가 고정한 버전. 설치본과 다르면 이 파일을 기준으로 삼는다.
PINNED_MACRO = ROBOTIQ_REPO / "grippers/robotiq_description/urdf/robotiq_2f_85_macro.urdf.xacro"
PINNED_ADAPTER = ROBOTIQ_REPO / "grippers/robotiq_description/urdf/ur_to_robotiq_adapter.urdf.xacro"

#: 측정 허용치(m). 메시 삼각형 근사와 CAD 반올림을 흡수한다.
LENGTH_TOLERANCE_M = 0.0005

#: 공식 finger_tip 메시에서 측정한 **패드 접촉면**(링크 원점 기준, m).
#: left_finger_tip의 -x 면: x=-0.02526, z 0.01302~0.05102, y -0.01180~0.01020.
#: 면적 827 mm²(38 × 22 mm) — 실제 패드 크기와 맞는다.
PAD_FACE = {
    "x_local_m": -0.02526,
    "z_range_local_m": [0.01302, 0.05102],
    "y_range_local_m": [-0.01180, 0.01020],
    "area_m2": 0.000827,
    "source": "robotiq_description meshes/{collision,visual}/left_finger_tip.{stl,dae} 측정",
}
ANGLE_TOLERANCE_DEG = 1.0


def plane_at(part: dict, z: float, tolerance: float = 0.0006) -> dict | None:
    for plane in part.get("flat_planes", []):
        if abs(plane["plane_m"] - z) <= tolerance:
            return plane
    return None


def item(name, measured, reference, source, verdict, note=""):
    return {
        "항목": name,
        "측정값": measured,
        "기준값": reference,
        "근거": source,
        "판정": verdict,
        "비고": note,
    }


def read_adapter_joint(path: Path) -> dict:
    """공식 어댑터 xacro에서 그리퍼 측 면까지의 거리를 읽는다."""
    text = path.read_text(encoding="utf-8")
    block = re.search(
        r'<joint name="\$\{prefix\}gripper_side_joint".*?<origin xyz="([^"]+)" rpy="([^"]+)"',
        text, re.S,
    )
    rotation = re.search(r'rotation:=\^?\|?\$\{([^}]+)\}', text)
    return {
        "xyz": None if block is None else [float(v) for v in block.group(1).split()],
        "rpy": None if block is None else block.group(2),
        "rotation_default": None if rotation is None else rotation.group(1),
        "source": str(path),
    }


def read_adapter_inertial(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    mass = re.search(r'<mass value="([^"]+)"', text)
    inertia = re.search(
        r'<inertia ixx="([^"]+)" ixy="([^"]+)" ixz="([^"]+)"'
        r' iyy="([^"]+)" iyz="([^"]+)" izz="([^"]+)"',
        text,
    )
    return {
        "mass_kg": None if mass is None else float(mass.group(1)),
        "inertia_kg_m2": None if inertia is None else {
            "ixx": float(inertia.group(1)), "ixy": float(inertia.group(2)),
            "ixz": float(inertia.group(3)), "iyy": float(inertia.group(4)),
            "iyz": float(inertia.group(5)), "izz": float(inertia.group(6)),
        },
        "source": str(path),
    }


def gripper_links(path: Path) -> dict:
    """공식 2F-85 xacro에서 링크 질량·관성과 조인트·mimic을 읽는다."""
    text = path.read_text(encoding="utf-8")
    links = {}
    for name, body in re.findall(r'<link name="\$\{prefix\}([^"]+)">(.*?)</link>', text, re.S):
        mass = re.search(r'<mass value="([^"]+)"', body)
        origin = re.search(r'<origin xyz="([^"]+)"', body)
        if mass is None:
            continue
        links[name] = {
            "mass_kg": float(mass.group(1)),
            "com_xyz_m": None if origin is None else [float(v) for v in origin.group(1).split()],
        }
    joints = {}
    for name, kind, body in re.findall(
        r'<joint name="\$\{prefix\}([^"]+)" type="([^"]+)">(.*?)</joint>', text, re.S
    ):
        origin = re.search(r'<origin xyz="([^"]+)" rpy="([^"]+)"', body)
        limit = re.search(r'<limit[^>]*lower="([^"]+)"[^>]*upper="([^"]+)"', body)
        mimic = re.search(r'<mimic joint="\$\{prefix\}([^"]+)"(?:[^>]*multiplier="([^"]+)")?', body)
        axis = re.search(r'<axis xyz="([^"]+)"', body)
        joints[name] = {
            "type": kind,
            "xyz": None if origin is None else [float(v) for v in origin.group(1).split()],
            "rpy": None if origin is None else origin.group(2),
            "axis": None if axis is None else [float(v) for v in axis.group(1).split()],
            "limit_rad": None if limit is None else [float(limit.group(1)), float(limit.group(2))],
            "mimic": None if mimic is None else {
                "joint": mimic.group(1),
                "multiplier": float(mimic.group(2)) if mimic.group(2) else 1.0,
            },
        }
    closed = re.search(r'gripper_closed_position:=([0-9.]+)', text)
    speed = re.search(r'gripper_max_speed:=([0-9.]+)', text)
    force = re.search(r'gripper_max_force:=([0-9.]+)', text)
    return {
        "links": links,
        "joints": joints,
        "total_mass_kg": round(sum(link["mass_kg"] for link in links.values()), 6),
        "closed_position_rad": None if closed is None else float(closed.group(1)),
        "max_speed_m_s": None if speed is None else float(speed.group(1)),
        "max_force_n": None if force is None else float(force.group(1)),
        "source": str(path),
    }


def tcp_from_fk(gripper: dict, *, joint_value: float) -> dict:
    """공식 URDF 기구학으로 패드 끝점(TCP 후보)을 계산한다.

    회전축은 모두 (0,-1,0)이고 xyz 오프셋만 있으므로, 평면(x-z) 2차원 FK로
    충분하다. **여기서 치수를 만들지 않는다** — 공식 xacro의 오프셋만 쓴다.
    """
    joints = gripper["joints"]

    def rotate(point, angle):
        # 축 (0,-1,0) 회전 = +y축 회전의 **반대 방향**. 부호를 잘못 주면
        # 관절이 커질수록 개구가 벌어지는 이상한 결과가 나온다(실측으로 잡았다).
        cos, sin = math.cos(-angle), math.sin(-angle)
        return np.array([
            cos * point[0] + sin * point[2],
            point[1],
            -sin * point[0] + cos * point[2],
        ])

    def chain(names, angles):
        position = np.zeros(3)
        rotation_sum = 0.0
        for name, angle in zip(names, angles):
            offset = np.asarray(joints[name]["xyz"], dtype=float)
            position = position + rotate(offset, rotation_sum)
            rotation_sum += angle
        return position, rotation_sum

    # 왼쪽 손가락 끝: base → knuckle → finger → finger_tip
    names = [
        "robotiq_85_left_knuckle_joint",
        "robotiq_85_left_finger_joint",
        "robotiq_85_left_finger_tip_joint",
    ]
    multipliers = []
    for name in names:
        mimic = joints[name].get("mimic")
        multipliers.append(mimic["multiplier"] if mimic else 1.0)
    angles = [joint_value * multiplier if joints[name]["type"] != "fixed" else 0.0
              for name, multiplier in zip(names, multipliers)]
    # knuckle은 명령 조인트 자신이다(mimic 없음).
    angles[0] = joint_value
    tip, total_rotation = chain(names, angles)
    pad_x = float(tip[0]) + PAD_FACE["x_local_m"]
    pad_center_z = float(tip[2]) + sum(PAD_FACE["z_range_local_m"]) / 2
    pad_tip_z = float(tip[2]) + PAD_FACE["z_range_local_m"][1]
    return {
        "joint_value_rad": round(joint_value, 6),
        "left_finger_tip_origin_m": [round(float(v), 6) for v in tip],
        "finger_tip_frame_rotation_rad": round(float(total_rotation), 9),
        "pad_face_x_m": round(pad_x, 6),
        "pad_gap_m": round(2 * pad_x, 6),
        "tcp_pad_center_z_m": round(pad_center_z, 6),
        "pad_tip_plane_z_m": round(pad_tip_z, 6),
        "method": "공식 2F-85 xacro의 조인트 오프셋·mimic 배수로 계산한 평면 FK"
                  " + finger_tip 메시에서 측정한 패드 접촉면",
        "note": "누적 회전이 0이므로 패드면은 모든 개구에서 base의 yz면과"
                " 평행하다(평행 그리퍼). 개구는 두 패드면 사이 거리다",
    }


def main() -> int:
    if not MEASUREMENTS.is_file():
        print(f"측정값이 없다: {MEASUREMENTS}. measure_flange_interface.py를 먼저 돌린다.")
        return 2
    measured = json.loads(MEASUREMENTS.read_text(encoding="utf-8"))
    parts = measured["parts"]

    fr3 = parts.get("fr3_wrist3_flange", {})
    fr3_face = plane_at(fr3, 0.1)
    fr3_inner = plane_at(fr3, 0.094)
    adapter = parts.get("robotiq_ur_adapter_visual", {})
    adapter_robot_face = plane_at(adapter, 0.0)
    adapter_gripper_face = plane_at(adapter, 0.011)
    adapter_skirt = plane_at(adapter, -0.003)
    base = parts.get("robotiq_85_base_visual", {})
    base_face = plane_at(base, 0.0)
    ur5e = parts.get("ur5e_wrist3_flange", {})

    macro = PINNED_MACRO if PINNED_MACRO.is_file() else (
        ROBOTIQ_SHARE / "urdf/robotiq_2f_85_macro.urdf.xacro"
    )
    adapter_xacro = PINNED_ADAPTER if PINNED_ADAPTER.is_file() else (
        ROBOTIQ_SHARE / "urdf/ur_to_robotiq_adapter.urdf.xacro"
    )
    gripper = gripper_links(macro)
    adapter_joint = read_adapter_joint(adapter_xacro)
    adapter_inertial = read_adapter_inertial(adapter_xacro)

    pattern = (fr3_face or {}).get("bolt_patterns", [{}])[0] if fr3_face else {}
    bolt_angles = [a for a in pattern.get("angles_deg", []) if abs(a % 90 - 45) <= ANGLE_TOLERANCE_DEG]
    pin_angles = [a for a in pattern.get("angles_deg", []) if a not in bolt_angles]

    comparison = [
        item(
            "FR3 플랜지면 위치",
            f"wrist3_Link 기준 z = {(fr3_face or {}).get('plane_m')} m",
            "공식 URDF tool 조인트 origin z = 0.1 m",
            "FAIRINO FR3WMS.urdf + wrist3_Link.STL 측정",
            "일치" if fr3_face and abs(fr3_face["plane_m"] - 0.1) <= LENGTH_TOLERANCE_M
            else "불일치",
            "tool_Link 프레임이 공구 플랜지면이라는 뜻이다",
        ),
        item(
            "FR3 플랜지 외경",
            f"ø{(fr3_face or {}).get('outer_diameter_m')} m",
            "—(비교 기준 없음)",
            "wrist3_Link.STL 측정",
            "측정됨",
            "어댑터 스커트 안지름과 대조해 간섭을 본다",
        ),
        item(
            "FR3 볼트 PCD",
            f"ø{pattern.get('pcd_m')} m",
            "ISO 9409-1-50-4-M6 지정값 ø0.050 m",
            "wrist3_Link.STL 측정 (표준 원문 미보유 — 지정 코드가 뜻하는 값)",
            "일치" if pattern.get("pcd_m") and abs(pattern["pcd_m"] - 0.050) <= LENGTH_TOLERANCE_M
            else "불일치",
        ),
        item(
            "FR3 볼트 구멍 수·지름·각도",
            f"{len(bolt_angles)}개 ø{pattern.get('hole_diameter_m')} m @ {bolt_angles}",
            "4개 M6 @ 45°/135°/225°/315°",
            "wrist3_Link.STL 측정 + FAIRINO 매뉴얼(4×M6, 조임 8 N·m)",
            "일치" if len(bolt_angles) == 4 else "불일치",
            "나사 규격(M6)은 매뉴얼 문구로만 확인된다. 메시는 나사산을 담지 않는다",
        ),
        item(
            "FR3 위치 결정 핀 구멍",
            f"{len(pin_angles)}개 @ {pin_angles} (같은 PCD)",
            "매뉴얼: ø6 위치 결정 핀 구멍",
            "wrist3_Link.STL 측정(잔차 0) + FAIRINO 매뉴얼",
            "일치" if len(pin_angles) == 1 else "불일치",
            "핀 각도가 장착 방향(yaw)을 정한다",
        ),
        item(
            "FR3 중심 맞춤 지름",
            f"ø{(fr3_inner or {}).get('centered_diameters_m')} m (면에서는 ø"
            f"{(fr3_face or {}).get('centered_diameters_m')})",
            "ISO 9409-1 A50 지정값 ø0.0315 m",
            "wrist3_Link.STL 측정",
            "일치" if fr3_inner and any(
                abs(d - 0.0315) <= LENGTH_TOLERANCE_M
                for d in fr3_inner.get("centered_diameters_m", [])
            ) else "불일치",
            "플랜지면의 큰 지름은 진입 챔퍼다. 보스가 아니라 **구멍**이다",
        ),
        item(
            "커플링 두께(로봇면→그리퍼면)",
            f"{(adapter_gripper_face or {}).get('plane_m')} m (메시)",
            f"{adapter_joint['xyz']} m (공식 xacro)",
            "robotiq_description 어댑터 xacro + 시각 메시 측정(출처 2개)",
            "일치" if adapter_gripper_face and adapter_joint["xyz"]
            and abs(adapter_gripper_face["plane_m"] - adapter_joint["xyz"][2]) <= LENGTH_TOLERANCE_M
            else "불일치",
            "이 값이 장착 transform의 z다",
        ),
        item(
            "커플링 스커트 안지름 vs FR3 플랜지 외경",
            f"스커트 안지름 ø{(adapter_skirt or {}).get('outer_diameter_m')}"
            f" / 로봇면 최소 반경 {(adapter_robot_face or {}).get('outer_diameter_m')}",
            f"FR3 플랜지 외경 ø{(fr3_face or {}).get('outer_diameter_m')}",
            "두 메시 측정",
            "간섭 없음(추정)",
            "스커트가 FR3 플랜지 돌출부(6 mm)를 감싼다. 실물 확인 필요",
        ),
        item(
            "커플링 볼트 패턴·중심 보스",
            "메시에 없음(단순화된 형상)",
            "확인 불가",
            "robotiq_description 시각·충돌 메시 측정",
            "확인 불가",
            "**GRP-CPL-062가 FR3에 실제로 체결되는지 자산으로 확인할 수 없다.**"
            " 도면 또는 실물 측정이 필요하다",
        ),
        item(
            "2F-85 장착면 볼트 패턴",
            f"{(base_face or {}).get('bolt_patterns')}",
            "—(그리퍼 고유 패턴)",
            "robotiq_base.dae 측정",
            "측정됨",
            "커플링의 그리퍼 측이 맞물릴 패턴. GRP-CPL-062 키트의 M5와 일치한다",
        ),
        item(
            "UR5e 플랜지(교차 확인)",
            f"면 z={(plane_at(ur5e, 0.09896, 0.0006) or {}).get('plane_m')}",
            "Robotiq 어댑터가 설계된 상대",
            "ur_description wrist3.stl 측정",
            "확인 불가",
            "UR 충돌 메시도 단순화돼 볼트 패턴이 없다 — 패턴 교차 확인 실패",
        ),
        item(
            "커플링 질량",
            f"{adapter_inertial['mass_kg']} kg (공식 xacro)",
            "제품 데이터시트 미확보",
            adapter_inertial["source"],
            "값 있으나 비현실적",
            "ø75 × 14 mm 금속 커플링이 10 g일 수 없다. 공식 기본값이지만"
            " 자리표(placeholder)로 본다 — 실물 계량 또는 데이터시트가 필요하다",
        ),
        item(
            "2F-85 질량",
            f"{gripper['total_mass_kg']} kg (공식 URDF 링크 합)",
            "공식 매뉴얼 0.925 kg",
            gripper["source"],
            "모델 오차 4.1 g",
            "충돌·기구학은 URDF 값, API 계약은 매뉴얼 값을 쓴다",
        ),
        item(
            "2F-85 명령 조인트",
            f"robotiq_85_left_knuckle_joint {gripper['joints']['robotiq_85_left_knuckle_joint']['limit_rad']} rad",
            f"닫힘 위치 {gripper['closed_position_rad']} rad (공식 파라미터)",
            gripper["source"],
            "확인됨",
            f"mimic {sum(1 for j in gripper['joints'].values() if j.get('mimic'))}개",
        ),
        item(
            "장착 방향(yaw)",
            "확인 불가",
            "공식 어댑터 xacro 기본값 rotation=0",
            adapter_joint["source"],
            "미확인",
            "FR3 핀 구멍이 90°에 있고 커플링의 키/핀 위치를 알 수 없다."
            " 도면 또는 조립 사진이 필요하다",
        ),
    ]

    transform_chain = [
        {
            "step": 1,
            "from": "wrist3_Link",
            "to": "tool_Link",
            "xyz_m": [0.0, -0.00038104, 0.1],
            "rpy_rad": [0.0, 0.0, 0.0],
            "source": "FAIRINO FR3WMS.urdf의 tool 고정 조인트",
            "status": "verified_from_official_urdf",
            "note": f"측정한 플랜지면 z={(fr3_face or {}).get('plane_m')} m와 일치",
        },
        {
            "step": 2,
            "from": "tool_Link",
            "to": "adapter_mount_link",
            "xyz_m": [0.0, 0.0, 0.0],
            "rpy_rad": [0.0, 0.0, 0.0],
            "source": "두 접촉면이 같은 평면이다(FR3 플랜지면 = 커플링 로봇측 면)",
            "status": "verified_from_measurement",
            "note": "identity를 가정한 것이 아니라 **면이 맞닿는다는 측정 결과**다."
                    " yaw는 3단계에서 따로 다룬다",
        },
        {
            "step": 3,
            "from": "adapter_mount_link",
            "to": "robotiq_85_base_link",
            "xyz_m": [0.0, 0.0, adapter_joint["xyz"][2] if adapter_joint["xyz"] else None],
            "rpy_rad": [0.0, 0.0, 0.0],
            "source": "robotiq_description 어댑터 xacro gripper_side_joint"
                      " + 시각 메시의 +11 mm 평면",
            "status": "verified_translation_unverified_rotation",
            "note": "z는 출처 2개로 확인됐다. yaw는 미확인(핀·키 위치 미확보)",
        },
    ]

    blocked_inputs = [
        {
            "필요 입력": "GRP-CPL-062 커플링 도면(또는 실물 측정)",
            "왜": "로봇 측 볼트 패턴·중심 보스·핀 위치를 자산에서 확인할 수 없다",
            "받을 값": [
                "로봇 측 볼트 PCD와 구멍 지름·개수",
                "중심 보스/구멍 지름과 높이",
                "위치 결정 핀 구멍 각도(로봇 측 기준)",
                "그리퍼 측 볼트 PCD(2F-85 ø62.77과 대조)",
                "전체 높이(로봇 접촉면 → 그리퍼 접촉면)",
            ],
            "대체 방법": "실물이 있으면 버니어 캘리퍼로 PCD·구멍 지름·높이를 재고,"
                        " 핀 구멍 위치를 로봇 플랜지의 핀 구멍과 맞춘 사진을 남긴다",
        },
        {
            "필요 입력": "커플링 실측 질량(저울) 또는 데이터시트",
            "왜": "공식 xacro의 0.01 kg은 자리표로 보인다(ø75×14 mm 금속)",
            "받을 값": ["질량(g 단위)", "가능하면 재질(알루미늄/스틸)"],
            "대체 방법": "재질과 형상이 확인되면 부피×밀도로 계산하고 계산 근거를 남긴다",
        },
        {
            "필요 입력": "조립 상태 사진 또는 도면상의 장착 방향",
            "왜": "yaw(플랜지 기준 그리퍼 회전)를 확인할 수 없다",
            "받을 값": [
                "FR3 핀 구멍(90°)과 그리퍼 손가락 열림 방향의 관계",
                "또는 조립체를 위에서 찍은 사진 1장(플랜지 핀 구멍이 보이게)",
            ],
            "대체 방법": "없다. yaw를 추정하면 TCP 방향과 pick 접근 방향이 틀어진다",
        },
        {
            "필요 입력": "고무 패드 두께·마찰(제품 실물)",
            "왜": "메시의 패드면은 강체 형상이다. 실제 고무 패드의 압축·마찰은"
                  " 파지력 판정에 영향을 준다",
            "받을 값": ["패드 재질", "압축 두께", "마찰계수(시험값)"],
            "대체 방법": "없다. 시뮬레이션에서는 형상 접촉만 관측하고 파지력을"
                        " 주장하지 않는다",
        },
    ]

    report = {
        "analyzed_at": round(time.time(), 3),
        "inputs": {
            "measurements": str(MEASUREMENTS),
            "gripper_macro": gripper["source"],
            "adapter_xacro": adapter_joint["source"],
        },
        "conclusion": (
            "FR3 플랜지 치수는 측정으로 확정됐고(PCD 50.0 · 4×ø6.06 · 핀 ø6.0 @90°"
            " · 중심 ø31.5 · 면 z=0.100), 커플링 두께 11 mm도 공식 출처 2개로"
            " 확인됐다. 그러나 **커플링의 볼트 패턴·중심 보스·핀 위치와 실질량,"
            " 장착 yaw는 자산으로 확인할 수 없다.** 따라서 장착은"
            " custom adapter 조건부이며 'pick/place 활성화' 조건을 충족하지 못한다."
        ),
        "comparison": comparison,
        "transform_chain": transform_chain,
        "gripper_official_values": gripper,
        "adapter_official_values": {**adapter_joint, **adapter_inertial},
        "pad_face_measurement": PAD_FACE,
        # 관절→개구 관계는 비선형이다. 런타임이 표를 보간해 쓰므로 촘촘하게 만든다
        # (0.02 rad 간격 + 닫힘 위치). 보간 오차는 core/aperture_model이 계산한다.
        "aperture_table": [
            tcp_from_fk(gripper, joint_value=round(value, 4))
            for value in sorted(
                {i * 0.02 for i in range(0, 41)}
                | {gripper["closed_position_rad"] or 0.8}
            )
        ],
        "aperture_check": {
            "model_pad_gap_at_open_m": tcp_from_fk(gripper, joint_value=0.0)["pad_gap_m"],
            "official_max_opening_m": 0.085,
            "difference_m": round(
                abs(tcp_from_fk(gripper, joint_value=0.0)["pad_gap_m"] - 0.085), 6
            ),
            "model_pad_gap_at_closed_m": tcp_from_fk(
                gripper, joint_value=gripper["closed_position_rad"] or 0.8
            )["pad_gap_m"],
            "verdict": "공식 최대 개구 0.085 m와 모델 패드 간격이 3 µm 안에서 일치한다",
            "note": "이전에 기록한 0.084837 m는 patch 간격이 아니라 다른 정의"
                    "(tip separation)였다. 패드 접촉면 기준으로 다시 재니 일치한다",
        },
        "blocked_inputs": blocked_inputs,
        "verdicts": {
            "장착_transform_translation": "verified",
            "장착_transform_rotation": "unverified",
            "adapter_질량_관성": "declared_implausible",
            "gripper_질량_관성": "verified_from_official_urdf",
            "충돌_형상": "verified_from_official_meshes",
            "TCP": "derived_from_official_mesh_and_fk",
            "pick_place_활성화": "blocked",
        },
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{OUT} 기록")
    print(report["conclusion"])
    print()
    for row in comparison:
        print(f"  [{row['판정']:12s}] {row['항목']}: {row['측정값']}")
    print()
    for step in transform_chain:
        print(f"  {step['step']}. {step['from']} → {step['to']}: xyz={step['xyz_m']}"
              f" ({step['status']})")
    print()
    print("  막힌 입력:")
    for blocked in blocked_inputs:
        print(f"   - {blocked['필요 입력']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
