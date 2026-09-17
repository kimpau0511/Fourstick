#!/usr/bin/env python3
"""FR3(WMS) ArmCapabilityProfile을 **외부 URDF에서** 생성한다 (8-02).

자산은 외부 경로에서 읽는다 — 라이선스가 확인되지 않아 저장소에 복사하지
않는다. 이 스크립트는 값을 읽어 Profile JSON을 만들고, 값마다 출처(파일·
commit·checksum)와 확인 상태·시각을 붙인다.

읽는 값(공식 URDF 근거):
- 관절 이름·순서·종류, 위치 하한·상한, 최대 속도, effort(N·m)
- 링크 질량과 무게중심
- base 프레임과 도구(flange) 프레임 이름

계산하는 값(URDF 기구학 유도 — 방법을 provenance에 적는다):
- 최대 도달거리: 관절 범위를 격자로 훑어 base→tool 거리의 최댓값

없는 값(그대로 unavailable):
- 정격 페이로드(데이터시트), 최대 가속도(URDF에 없음),
  상태 신선도·연결 제한시간(운영 기준 미확정)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path("/home/asd/external/frcobot_ros2")
URDF = "fairino_description/urdf/FR3WMS.urdf"
OUT = Path("config/profiles/fr3wms_arm.json")
#: 도달거리 격자 분해(관절당 표본 수). 크게 하면 정확해지고 느려진다.
GRID = 13


def rpy(roll: float, pitch: float, yaw: float):
    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)
    return [
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ]


def axis_rot(axis, angle):
    n = math.sqrt(sum(a * a for a in axis)) or 1.0
    x, y, z = (a / n for a in axis)
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    return [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]


def mm(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def mv(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def floats(text, default=(0.0, 0.0, 0.0)):
    return default if not text else tuple(float(v) for v in text.split())


def read_urdf(path: Path):
    root = ET.parse(path).getroot()
    joints = []
    for joint in root.findall("joint"):
        origin = joint.find("origin")
        limit = joint.find("limit")
        axis = joint.find("axis")
        joints.append({
            "name": joint.get("name"),
            "type": joint.get("type"),
            "parent": joint.find("parent").get("link"),
            "child": joint.find("child").get("link"),
            "xyz": floats(None if origin is None else origin.get("xyz")),
            "rpy": floats(None if origin is None else origin.get("rpy")),
            "axis": floats(None if axis is None else axis.get("xyz"), (0.0, 0.0, 1.0)),
            "limit": None if limit is None else {
                "lower": float(limit.get("lower")),
                "upper": float(limit.get("upper")),
                "effort": float(limit.get("effort")),
                "velocity": float(limit.get("velocity")),
            },
        })
    masses = {}
    for link in root.findall("link"):
        inertial = link.find("inertial")
        if inertial is None:
            continue
        mass = inertial.find("mass")
        com = inertial.find("origin")
        masses[link.get("name")] = {
            "mass": float(mass.get("value")),
            "com": floats(None if com is None else com.get("xyz")),
        }
    return root, joints, masses


def tool_position(chain, angles, *, link: str | None = None):
    rot = [[1.0, 0, 0], [0, 1.0, 0], [0, 0, 1.0]]
    pos = (0.0, 0.0, 0.0)
    for joint in chain:
        rot_j = rpy(*joint["rpy"])
        pos = tuple(p + q for p, q in zip(pos, mv(rot, joint["xyz"])))
        rot = mm(rot, rot_j)
        if joint["type"] == "revolute":
            rot = mm(rot, axis_rot(joint["axis"], angles.get(joint["name"], 0.0)))
        if link is not None and joint["child"] == link:
            return pos
    return pos


def max_reach(chain, movable, *, link: str | None = None, horizontal: bool = True):
    """관절 범위를 격자로 훑어 최대 도달값을 찾는다.

    `horizontal=True`면 **base z축 기준 수평 반경**(작업 반경의 정의),
    False면 base 원점으로부터의 3D 거리다. base 높이와 tool 오프셋 때문에 두
    값은 크게 다르다 — 정의를 섞지 않는다(`scripts/analyze_fr3_reach.py`).

    j1은 base z축 회전이라 두 값 모두 바꾸지 않으므로 고정한다.
    """
    sweep = [j for j in movable if j["name"] != movable[0]["name"]]
    grids = []
    for joint in sweep:
        low, high = joint["limit"]["lower"], joint["limit"]["upper"]
        grids.append([low + (high - low) * i / (GRID - 1) for i in range(GRID)])
    best = 0.0
    best_angles: dict = {}
    for combo in itertools.product(*grids):
        angles = {joint["name"]: value for joint, value in zip(sweep, combo)}
        x, y, z = tool_position(chain, angles, link=link)
        value = math.hypot(x, y) if horizontal else math.sqrt(x * x + y * y + z * z)
        if value > best:
            best, best_angles = value, angles
    return best, best_angles


def main() -> int:
    repo = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO
    path = repo / URDF
    if not path.is_file():
        print(f"외부 URDF가 없다: {path}", file=sys.stderr)
        return 2
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    blob = subprocess.run(
        ["git", "-C", str(repo), "hash-object", str(path)],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=60,
    ).stdout.strip()
    now = round(time.time(), 3)

    root, joints, masses = read_urdf(path)
    movable = [j for j in joints if j["type"] == "revolute"]
    chain = joints   # base_link부터 tool_Link까지 직렬 사슬이다
    flange_link = next(j for j in joints if j["type"] == "fixed")["parent"]
    # **작업 반경의 정의를 고정한다**: base z축 기준 수평 반경, 플랜지 링크까지,
    # 관절 제한 안에서. 3D 거리(base 높이·tool 오프셋 포함)와 다른 값이다.
    reach, reach_angles = max_reach(chain, movable, link=flange_link, horizontal=True)
    reach_tool, _ = max_reach(chain, movable, link=None, horizontal=True)
    reach_3d, _ = max_reach(chain, movable, link=None, horizontal=False)

    source = f"{URDF} (외부 경로: {repo})"

    def prov(status: str, note: str, kind: str = "urdf") -> dict:
        return {
            "source_kind": kind, "source": source,
            "source_version": f"commit {commit[:12]}",
            "source_commit": commit, "status": status,
            "checked_at": now, "note": note,
        }

    def measured(value, unit, note, status="declared", kind="urdf"):
        return {"value": value, "unit": unit, "provenance": prov(status, note, kind)}

    def unavailable(unit, note):
        return {
            "value": None, "unit": unit,
            "provenance": {
                "source_kind": "", "source": "", "source_version": "",
                "source_commit": "", "status": "unavailable",
                "checked_at": 0.0, "note": note,
            },
        }

    joint_entries = []
    for joint in movable:
        limit = joint["limit"]
        joint_entries.append({
            "name": joint["name"], "kind": "revolute",
            "lower": measured(limit["lower"], "rad", "URDF limit lower"),
            "upper": measured(limit["upper"], "rad", "URDF limit upper"),
            "max_velocity": measured(limit["velocity"], "rad/s", "URDF limit velocity"),
            "max_acceleration": unavailable(
                "rad/s^2", "URDF에 가속도 한계가 없다. 공식 데이터시트 미확보"
            ),
        })

    total_mass = round(sum(m["mass"] for m in masses.values()), 6)
    tool_joint = next(j for j in joints if j["type"] == "fixed")

    payload = {
        "arm_profile_id": "fairino_fr3wms_arm",
        "arm_profile_version": "0.3.0-urdf-reach-defined",
        "display_name": "FAIRINO FR3-WMS",
        "joints": joint_entries,
        "payload": unavailable(
            "kg",
            "정격 페이로드는 데이터시트 값이다. URDF에 없고 공식 문서 미확보 →"
            " pick·place는 지원 스킬에서 제외된다",
        ),
        "reach": measured(
            round(reach, 6), "m",
            "작업 반경 정의: **base z축 기준 최대 수평 반경**(플랜지 링크"
            f" {flange_link}까지), 관절 제한 안에서 {GRID}점 격자 FK로 계산."
            f" 같은 격자에서 tool frame 수평 반경 {round(reach_tool, 6)} m,"
            f" base 원점 기준 3D 거리 {round(reach_3d, 6)} m."
            " 더 촘촘한 17점 격자(scripts/analyze_fr3_reach.py,"
            " reports/profile/fr3_reach_analysis.json)에서는 각각"
            " 0.672505 m·0.809984 m다."
            " 유통사 표기 0.622 m는 링크 오프셋 단순 합(0.28+0.24+0.102)과 같고"
            " 정의가 다르다. 플랜지 수평 반경과 약 8 mm 차이는 공식 도면·"
            "데이터시트 없이 해소되지 않는다(`scripts/analyze_fr3_reach.py`)",
            status="unverified",
        ),
        "frames": {
            "base": joints[0]["parent"],
            "tool": tool_joint["child"],
        },
        # 이 시뮬레이션 경로에서 실제로 검증한 컨트롤러 조합이다.
        # 실기 컨트롤러(FAIRINO v3.9.x) 요구 버전은 여전히 미확보다.
        "controller_requirement": (
            "ros2_control 6.9.0 / gz_ros2_control 3.0.8 / Gazebo Sim 10.5.0"
            " (시뮬레이션 경로에서 검증. 실기 컨트롤러 요구 버전 미확보)"
        ),
        "declared_skills": ["home", "move", "pick", "place", "stop"],
        # 아래 두 값은 **우리 시뮬레이션 설정**에서 온다(로봇 사양이 아니다).
        # 실기 값은 컨트롤러 문서를 확보한 뒤 교체한다.
        "state_max_age": {
            "value": 0.1, "unit": "s",
            "provenance": {
                "source_kind": "config", "source": "config/gazebo/fr3wms_controllers.yaml",
                "source_version": "forstick2", "source_commit": "",
                "status": "declared", "checked_at": now,
                "note": "controller_manager update_rate 200 Hz(주기 0.005s)의 20배."
                        " 시뮬레이션 경로 기준이며 실기 관측 주기로 교체해야 한다",
            },
        },
        "connect_timeout": {
            "value": 60.0, "unit": "s",
            "provenance": {
                "source_kind": "config", "source": "scripts/run_gazebo_fr3.sh",
                "source_version": "forstick2", "source_commit": "",
                "status": "declared", "checked_at": now,
                "note": "controller_manager 대기 제한시간(--controller-manager-timeout)"
                        " 으로 실제 사용한 값. 실기 연결 기준 미확보",
            },
        },
        "environment": "simulation",
        "verified": False,
        "unverified_items": [
            "payload_rating", "joint_acceleration_limits", "reach_definition_vs_datasheet",
            "controller_requirement", "state_freshness", "connect_timeout",
            "urdf_license",
        ],
        "notes": (
            f"공식 URDF에서 읽은 값이다(commit {commit[:12]}, sha256"
            f" {checksum[:12]}, git-blob {blob[:12]}). 링크 질량 합"
            f" {total_mass} kg(URDF inertial). effort는 관절 토크 한계(N·m)이며"
            " 파지력이 아니다. **URDF 라이선스가 확인되지 않아 저장소에 자산을"
            " 복사하지 않고 외부 경로로만 참조한다.**"
        ),
    }

    OUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "written": str(OUT),
        "commit": commit, "sha256": checksum, "git_blob": blob,
        "joints": [j["name"] for j in movable],
        "base_frame": payload["frames"]["base"],
        "tool_frame": payload["frames"]["tool"],
        "reach_horizontal_flange_m": round(reach, 6),
        "reach_horizontal_tool_m": round(reach_tool, 6),
        "reach_3d_base_to_tool_m": round(reach_3d, 6),
        "reach_angles": {k: round(v, 4) for k, v in reach_angles.items()},
        "link_mass_sum_kg": total_mass,
        "effort_Nm": [j["limit"]["effort"] for j in movable],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
