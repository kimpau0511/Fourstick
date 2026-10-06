#!/usr/bin/env python3
"""시뮬레이션 전용 Profile 근거 측정 — **Gazebo 물리 상태로 반복 측정한다.**

```
source /opt/ros/lyrical/setup.bash
FORSTICK2_SIM_PROFILE_MEASURE=1 python3 scripts/measure_sim_profile_evidence.py --cycles 5
FORSTICK2_SIM_PROFILE_MEASURE=1 python3 scripts/measure_sim_profile_evidence.py --passive 600
```

실물용 Profile의 `null`(`arm.payload`·`gripper.pad_aperture_closed`·`mounting.rpy.yaw`)을
채우지 않는다. 여기서 재는 값은 **지금 떠 있는 Gazebo 작업 셀 모델**의 사실이고,
결과는 `reports/workcell/sim_profile_evidence.json`에만 쓴다. 시뮬레이션 전용
Profile(`config/profiles/simulation/`)이 이 보고서를 근거로 가리킨다.

## 무엇을 어떻게 재는가

- **장착 yaw**: Gazebo `dynamic_pose/info`의 `wrist3_Link`와 좌·우 손가락 끝 링크
  pose(물리 상태)에서 열림 축을 구하고, URDF에서 장착 회전(`ur_to_robotiq_joint`의
  yaw)만 0으로 둔 기준 축과의 각도를 잰다. 팔 자세가 다른 표본에서 같은 값이 나와야
  장착 고정 회전이다(`--passive`로 이송 중에 표본을 모은다).
- **완전 닫힘 패드 간격**: 물체 없이 공식 닫힘 위치로 닫은 뒤, 같은 손가락 끝 링크
  pose의 간격에서 패드면 오프셋(공식 메시 측정값)을 뺀다. 같은 방법으로 완전 열림
  간격도 재서 공식 최대 개구(0.085 m)와 대조해 **방법 자체를 검증**한다.
- **자재 질량**: 떠 있는 gz 프로세스가 읽은 world SDF의 `inertial/mass`(파일 checksum
  포함). 적재 한계는 이 값과 이송 반복 결과로만 정한다 — 제조사 정격이 아니다.

**팔을 움직이지 않는다.** 능동 측정은 안전 home에서 그리퍼만 열고 닫는다. 실행 중인
작업 셀 작업·붙어 있는 자재가 있으면 시작하지 않는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robots.moveit.kinematics import link_transforms, parse_urdf  # noqa: E402

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
URDF = Path("/tmp/forstick2_workcell/workcell/fr3wms_with_2f85.urdf")
OUT = ROOT / "reports/workcell/sim_profile_evidence.json"
JOBS_DIR = ROOT / "reports/workcell/sim_demo_jobs"

FLANGE_LINK = "wrist3_Link"
LEFT_TIP = "robotiq_85_left_finger_tip_link"
RIGHT_TIP = "robotiq_85_right_finger_tip_link"
#: URDF에서 장착 yaw가 들어가는 조인트(`ur_to_robotiq_adapter.urdf.xacro`의 rotation).
YAW_JOINT = "ur_to_robotiq_joint"
GRIPPER_BASE = "robotiq_85_base_link"
GRIPPER_JOINT = "robotiq_85_left_knuckle_joint"
#: 안전 home 판정 허용치(rad). 어댑터 도달 판정과 같은 근거(ARM_TOLERANCE_RAD).
HOME_TOLERANCE_RAD = 0.05
#: 그리퍼가 멈췄다고 볼 속도(rad/s)와 연속 표본 수.
SETTLED_VELOCITY_RAD_S = 0.01
SETTLED_SAMPLES = 10
GRIPPER_MOVE_SEC = 3.0


# ── 기하 ────────────────────────────────────────────────────────────────
def quat_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    n = math.sqrt(x * x + y * y + z * z + w * w)
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def reference_axes(urdf: Path) -> dict:
    """flange(`wrist3_Link`) 기준 그리퍼 base 축 — 장착 yaw를 0으로 둔 기준과 빌드된 모델."""
    joints, _ = parse_urdf(urdf)
    built = link_transforms(joints, {})
    zeroed_joints = []
    built_yaw = None
    for joint in joints:
        if joint["name"] == YAW_JOINT:
            built_yaw = float(joint["rpy"][2])
            joint = {**joint, "rpy": (joint["rpy"][0], joint["rpy"][1], 0.0)}
        zeroed_joints.append(joint)
    if built_yaw is None:
        raise SystemExit(f"URDF에 {YAW_JOINT}가 없다 — 장착 yaw를 둘 자리를 모른다")
    zeroed = link_transforms(zeroed_joints, {})

    def relative(table) -> np.ndarray:
        r_flange, _ = table[FLANGE_LINK]
        r_base, _ = table[GRIPPER_BASE]
        return r_flange.T @ r_base

    return {"reference": relative(zeroed), "built": relative(built),
            "built_yaw_rad": built_yaw}


def signed_angle(reference: np.ndarray, vector: np.ndarray, axis: np.ndarray) -> float:
    projected = vector - float(vector @ axis) * axis
    projected = projected / np.linalg.norm(projected)
    return math.atan2(float(np.cross(reference, projected) @ axis),
                      float(reference @ projected))


def measure_sample(poses: dict, axes: dict, pad_offset_m: float) -> dict | None:
    """pose 한 장 → 장착 yaw·패드 간격. 링크가 빠졌으면 None."""
    if not all(name in poses for name in (FLANGE_LINK, LEFT_TIP, RIGHT_TIP)):
        return None
    flange = poses[FLANGE_LINK]
    r_flange = quat_matrix(*flange[3:7])
    left, right = np.array(poses[LEFT_TIP][:3]), np.array(poses[RIGHT_TIP][:3])
    opening = r_flange.T @ (left - right)          # flange 기준, 오른쪽 → 왼쪽
    reference = axes["reference"]
    mount_axis = reference[:, 2]
    along = opening - float(opening @ mount_axis) * mount_axis
    separation = float(np.linalg.norm(along))
    yaw = signed_angle(reference[:, 0], opening, mount_axis)
    return {"yaw_rad": yaw, "tip_separation_m": separation,
            "pad_gap_m": separation - 2.0 * pad_offset_m,
            "off_axis_m": float(abs(opening @ mount_axis))}


# ── 관측 ────────────────────────────────────────────────────────────────
class PoseStream:
    """gz `dynamic_pose/info` 구독. 원본만 담고 필요할 때 해석한다."""

    def __init__(self, world: str, partition: str):
        os.environ["GZ_PARTITION"] = partition
        from gz.transport import Node, SubscribeOptions

        self._world = world
        self._node = Node()
        self._lock = threading.Lock()
        self._raw: tuple[bytes, float] | None = None
        if not self._node.subscribe_raw(f"/world/{world}/dynamic_pose/info", self._on,
                                        "gz.msgs.Pose_V", SubscribeOptions()):
            raise SystemExit("dynamic_pose/info 구독 실패")

    def _on(self, data: bytes, _info) -> None:
        with self._lock:
            self._raw = (bytes(data), time.time())

    def latest(self, *, after: float = 0.0, timeout_sec: float = 3.0) -> tuple[dict, float]:
        from gz.msgs.pose_v_pb2 import Pose_V

        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            with self._lock:
                raw = self._raw
            if raw is not None and raw[1] > after:
                message = Pose_V()
                message.ParseFromString(raw[0])
                out = {}
                for pose in message.pose:
                    p, q = pose.position, pose.orientation
                    out[pose.name] = [p.x, p.y, p.z, q.x, q.y, q.z, q.w]
                return out, raw[1]
            time.sleep(0.01)
        raise TimeoutError("dynamic_pose/info 표본이 오지 않았다")

    def close(self) -> None:
        try:
            self._node.unsubscribe(f"/world/{self._world}/dynamic_pose/info")
        except Exception:  # noqa: BLE001
            pass


def loaded_world_sdf() -> Path | None:
    """떠 있는 gz 프로세스가 읽은 작업 셀 world 파일(명령줄 그대로)."""
    out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "gz-sim-main" in line and "fr3_2f85_workcell.sdf" in line:
            for token in line.split():
                if token.endswith("fr3_2f85_workcell.sdf"):
                    return Path(token)
    return None


def material_masses(workcell: dict) -> dict:
    sdf = loaded_world_sdf()
    if sdf is None or not sdf.is_file():
        return {"available": False, "detail": "떠 있는 작업 셀 world SDF를 찾지 못했다"}
    data = sdf.read_bytes()
    root = ET.fromstring(data)
    wanted = {name for name, row in workcell["models"].items()
              if row.get("kind") == "material"}
    masses = {}
    for model in root.iter("model"):
        name = model.get("name")
        if name in wanted:
            values = [float(m.text) for m in model.iter("mass")]
            masses[name] = {"mass_kg": sum(values), "links": len(values),
                            "declared_kg": workcell["models"][name].get("mass_kg")}
    return {"available": True, "world_sdf": str(sdf),
            "sha256": hashlib.sha256(data).hexdigest(), "materials": masses}


def cell_busy() -> str | None:
    active = JOBS_DIR / "active_job.json"
    if active.exists():
        return f"실행 중인 작업 기록이 있다: {active}"
    record = Path(os.environ.get("FORSTICK2_WORKCELL_LOG_DIR", "/tmp/forstick2_workcell"))
    try:
        joints = json.loads((record / "fixture_joints.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        joints = {}
    held = [m for m, row in joints.items() if (row or {}).get("state") == "attached"]
    if held:
        return f"붙어 있는 자재가 있다: {held}"
    return None


def summarize(values: list[float]) -> dict:
    arr = np.array(values, dtype=float)
    return {"n": int(arr.size), "mean": float(arr.mean()), "min": float(arr.min()),
            "max": float(arr.max()), "std": float(arr.std()),
            "spread": float(arr.max() - arr.min())}


#: 이송 실행기 보고서에서 **자재가 도착지에 놓였다**고 판정한 상태(실행기가 정한 값).
TRANSFER_SUCCESS = ("simulation_transfer_completed", "simulation_transfer_resumed_completed",
                    "returned_to_origin", "route_arrived", "slot_moved")


def transfer_history() -> dict:
    """같은 이송 실행기의 지난 보고서 — 자재별 완료·실패 수(자재 질량 범위 근거)."""
    workcell = json.loads(WORKCELL.read_text(encoding="utf-8"))
    # 정방향 이송 보고서는 `model` 대신 자원 id(`object_id`)를 쓴다 — 셀 설정으로 맞춘다.
    model_of = {row.get("resource_id"): name for name, row in workcell["models"].items()
                if row.get("kind") == "material"}
    rows: dict = {}
    for path in sorted(JOBS_DIR.glob("simjob_*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = report.get("model") or model_of.get(report.get("object_id"))
        status = report.get("status")
        if not model or report.get("mode") == "reconcile_state":
            continue
        row = rows.setdefault(model, {"completed": 0, "other": {}})
        if status in TRANSFER_SUCCESS:
            row["completed"] += 1
        else:
            row["other"][str(status)] = row["other"].get(str(status), 0) + 1
    return {"source": str(JOBS_DIR.relative_to(ROOT)), "success_statuses": list(TRANSFER_SUCCESS),
            "by_material": rows}


def model_inertials(urdf: Path) -> dict:
    """떠 있는 URDF의 커플링·그리퍼 링크 질량(Gazebo가 쓰는 값)."""
    root = ET.parse(str(urdf)).getroot()
    out = {}
    for link in root.findall("link"):
        name = link.get("name") or ""
        mass = link.find("inertial/mass")
        if mass is not None and (name.startswith("robotiq_85") or name == "ur_to_robotiq_link"):
            out[name] = float(mass.get("value"))
    return {"coupling_link": "ur_to_robotiq_link",
            "coupling_mass_kg": out.get("ur_to_robotiq_link"),
            "gripper_links_mass_kg": round(sum(v for k, v in out.items()
                                               if k != "ur_to_robotiq_link"), 6),
            "links": out}


# ── 측정 ────────────────────────────────────────────────────────────────
def active_cycles(args, stream: PoseStream, axes: dict, pad_offset: float,
                  mounting: dict) -> dict:
    from robots.fr3_gazebo.ros_transport import RosWorkcellTransport

    workcell = json.loads(WORKCELL.read_text(encoding="utf-8"))
    poses_cfg = json.loads(POSES.read_text(encoding="utf-8"))
    home_name = "safe_home"
    safe = poses_cfg.get("safe_home") or {}
    home = safe.get("joint_rad") if safe.get("status") == "verified" else None
    if not home:
        raise SystemExit("검증된 안전 home 자세를 설정에서 찾지 못했다")
    closed = float(mounting["gripper_official"]["closed_position_rad"])
    transport = RosWorkcellTransport(world_name=workcell["world_name"],
                                     gz_partition=workcell["gz_partition"],
                                     ros_domain_id=int(workcell["ros_domain_id"]),
                                     node_name="forstick2_sim_profile_measure")
    cycles = []
    try:
        status = transport.connect(10.0)
        if not status.world_present:
            raise SystemExit(f"Gazebo world가 없다: {status.detail}")
        obs = transport.joint_observation(5.0)
        if not obs.valid:
            raise SystemExit(f"관절 관측 실패: {obs.detail}")
        off = {j: abs(obs.positions.get(j, float("nan")) - float(v)) for j, v in home.items()}
        if not all(d <= HOME_TOLERANCE_RAD for d in off.values()):
            raise SystemExit(f"팔이 안전 home에 있지 않다(오차 {off}) — 팔을 움직이지 않는다")

        def settle(target: float) -> dict:
            started = time.time()
            still = 0
            last = None
            while time.time() - started < 10.0:
                last = transport.joint_observation(2.0, after=last.observed_at if last else None)
                if not last.valid:
                    continue
                speed = abs(last.velocities.get(GRIPPER_JOINT, 0.0))
                still = still + 1 if speed < SETTLED_VELOCITY_RAD_S else 0
                if still >= SETTLED_SAMPLES:
                    break
            joint = None if last is None else last.positions.get(GRIPPER_JOINT)
            return {"target_rad": target, "observed_rad": joint, "settled": still >= SETTLED_SAMPLES}

        def sample(label: str, count: int = 10) -> dict:
            rows = []
            after = 0.0
            for _ in range(count):
                poses, after = stream.latest(after=after)
                row = measure_sample(poses, axes, pad_offset)
                if row is not None:
                    rows.append(row)
                time.sleep(0.05)
            if not rows:
                return {"label": label, "samples": 0}
            return {"label": label, "samples": len(rows),
                    "yaw_rad": summarize([r["yaw_rad"] for r in rows]),
                    "pad_gap_m": summarize([r["pad_gap_m"] for r in rows]),
                    "tip_separation_m": summarize([r["tip_separation_m"] for r in rows]),
                    "off_axis_m": summarize([r["off_axis_m"] for r in rows])}

        for index in range(args.cycles):
            row = {"cycle": index + 1}
            outcome = transport.send_gripper(0.0, GRIPPER_MOVE_SEC, GRIPPER_MOVE_SEC + 5.0)
            row["open_command"] = {"accepted": outcome.accepted,
                                   "result_received": outcome.result_received,
                                   "error_code": outcome.error_code}
            row["open_joint"] = settle(0.0)
            row["open"] = sample("open")
            outcome = transport.send_gripper(closed, GRIPPER_MOVE_SEC, GRIPPER_MOVE_SEC + 5.0)
            row["close_command"] = {"accepted": outcome.accepted,
                                    "result_received": outcome.result_received,
                                    "error_code": outcome.error_code}
            row["closed_joint"] = settle(closed)
            row["closed"] = sample("closed")
            cycles.append(row)
            print(f"[측정] {index + 1}/{args.cycles} 열림 간격"
                  f" {row['open'].get('pad_gap_m', {}).get('mean', float('nan')):.6f} m ·"
                  f" 닫힘 간격 {row['closed'].get('pad_gap_m', {}).get('mean', float('nan')):.6f} m ·"
                  f" yaw {row['closed'].get('yaw_rad', {}).get('mean', float('nan')):+.6f} rad"
                  f" · 닫힘 관절 {row['closed_joint']['observed_rad']}", flush=True)
        # 측정 전 상태(열림)로 되돌린다.
        transport.send_gripper(0.0, GRIPPER_MOVE_SEC, GRIPPER_MOVE_SEC + 5.0)
        settle(0.0)
    finally:
        transport.disconnect()
    return {"home_pose": home_name, "closed_command_rad": closed, "cycles": cycles}


def passive(seconds: float, stream: PoseStream, axes: dict, pad_offset: float) -> dict:
    """이송 중 표본 — 팔 자세가 바뀌어도 장착 yaw가 같은지 본다(명령 없음)."""
    rows = []
    started = time.time()
    after = 0.0
    while time.time() - started < seconds:
        try:
            poses, after = stream.latest(after=after, timeout_sec=5.0)
        except TimeoutError:
            continue
        row = measure_sample(poses, axes, pad_offset)
        if row is not None:
            flange = poses[FLANGE_LINK]
            row["flange_xyz_m"] = [round(v, 5) for v in flange[:3]]
            rows.append(row)
        time.sleep(0.2)
    if not rows:
        return {"samples": 0}
    # 플랜지 위치가 서로 다른 자세의 수(1 cm 격자) — 같은 자세 표본만 모은 것이 아님을 보인다.
    cells = {tuple(round(v / 0.01) for v in r["flange_xyz_m"]) for r in rows}
    return {"samples": len(rows), "distinct_flange_positions_1cm": len(cells),
            "yaw_rad": summarize([r["yaw_rad"] for r in rows]),
            "tip_separation_m": summarize([r["tip_separation_m"] for r in rows])}


SIM_PROFILE = ROOT / "config/profiles/simulation/fr3wms_2f85_gazebo_sim.json"
GRIPPER_PROFILE = ROOT / "config/profiles/robotiq_2f85_gripper.json"
#: 능동 측정 최소 반복 수. 한 번 잰 값을 근거로 쓰지 않는다.
MIN_CYCLES = 5


def write_profile(report: dict, out: Path) -> dict:
    """보고서 → 시뮬레이션 전용 Profile. **값은 보고서에서만** 온다(손으로 적지 않는다)."""
    active = report.get("active") or {}
    cycles = [c for c in active.get("cycles") or ()
              if c.get("closed", {}).get("samples") and c.get("closed_joint", {}).get("settled")
              and c.get("open", {}).get("samples")]
    if len(cycles) < MIN_CYCLES:
        raise SystemExit(f"능동 측정이 {len(cycles)}회뿐이다 — {MIN_CYCLES}회 이상 필요")
    closed = [c["closed"]["pad_gap_m"]["mean"] for c in cycles]
    opened = [c["open"]["pad_gap_m"]["mean"] for c in cycles]
    yaws = [c[k]["yaw_rad"]["mean"] for c in cycles for k in ("open", "closed")]
    joints = [c["closed_joint"]["observed_rad"] for c in cycles]
    passive_runs = [r for r in report.get("passive_runs") or () if r.get("samples")]
    passive_yaw = [r["yaw_rad"] for r in passive_runs]
    materials = (report.get("materials") or {}).get("materials") or {}
    history = (report.get("transfer_history") or {}).get("by_material") or {}
    verified = {m: row["mass_kg"] for m, row in materials.items()
                if (history.get(m) or {}).get("completed", 0) > 0}
    if not verified:
        raise SystemExit("이송이 확인된 자재 질량이 없다 — 적재 범위를 정할 수 없다")
    inertials = report.get("inertials") or {}
    gripper = json.loads(GRIPPER_PROFILE.read_text(encoding="utf-8"))
    now = time.time()
    rel = str(OUT.relative_to(ROOT))
    sdf = (report.get("materials") or {})
    source_version = f"urdf sha256:{report['urdf_sha256'][:12]}"

    def measured(value, unit, source, note, version=source_version):
        return {"value": value, "unit": unit, "provenance": {
            "source_kind": "simulation_measurement", "source": source,
            "source_version": version, "source_commit": "", "status": "verified",
            "checked_at": now, "note": note}}

    payload = max(verified.values())
    profile = {
        "schema": "forstick2.simulation_profile/1",
        "sim_profile_id": "fr3wms_2f85_gazebo_sim",
        "sim_profile_version": f"0.1.0-gazebo-measured-{time.strftime('%Y%m%d', time.localtime(now))}",
        "environment": "simulation",
        "real_hardware_claim": False,
        "note": ("시뮬레이션 전용 Profile이다. 실물 Profile(config/profiles/*.json)의 null을 채우지"
                 " 않는다. 값은 지금 Gazebo 작업 셀 모델을 잰 것이며 Gazebo 작업 셀 어댑터에만"
                 " 적용된다. 실물 근거로 승격하지 않는다."),
        "base": {"composite": "fr3wms_with_2f85", "arm": "fairino_fr3wms_arm",
                 "gripper": "robotiq_2f85", "mounting": "fr3wms_to_robotiq_2f85"},
        "applies_to": {"adapter_module": "robots.fr3_gazebo", "robot_id": "fr3wms_2f85_workcell",
                       "is_simulated": True},
        "evidence_report": rel,
        "values": {
            "arm.payload": measured(
                payload, "kg",
                f"{rel} materials(떠 있는 world SDF inertial/mass) + transfer_history",
                f"제조사 정격이 아니다. 같은 이송 실행기로 옮긴 것이 확인된 자재 질량의 최댓값이다"
                f" ({json.dumps(verified)}). 이보다 무거운 자재는 막는다.",
                version=f"world sdf sha256:{sdf.get('sha256', '')[:12]}"),
            "gripper.pad_aperture_closed": measured(
                round(sum(closed) / len(closed), 6), "m",
                f"{rel} active.cycles[*].closed (Gazebo dynamic_pose 손가락 끝 링크 간격 − 패드면 오프셋)",
                f"물체 없이 공식 닫힘 명령 후 관측 관절 {min(joints):.5f}~{max(joints):.5f} rad에서"
                f" {len(cycles)}회 잰 패드면 간격 {min(closed):.6f}~{max(closed):.6f} m."
                f" 같은 방법의 완전 열림 간격 {min(opened):.6f}~{max(opened):.6f} m가 공식 최대 개구"
                f" {report['official_open_gap_m']} m와 맞아 방법을 확인했다. 강체 모델 패드다 —"
                " 실물 고무 패드 두께·압축과 무관하다."),
            "mounting.rpy.yaw": measured(
                round(sum(yaws) / len(yaws), 6), "rad",
                f"{rel} active.cycles[*].yaw_rad, passive_runs[*].yaw_rad (wrist3_Link ↔ 손가락 열림 축)",
                f"조립 모델은 {YAW_JOINT} yaw={report['built_yaw_rad']} rad로 빌드됐다"
                f"(FORSTICK2_ASSEMBLY_YAW_RAD). 물리 pose로 잰 열림 축 각도 {min(yaws):+.2e}~{max(yaws):+.2e} rad"
                + (f", 이송 중 팔 자세 {sum(r['distinct_flange_positions_1cm'] for r in passive_runs)}곳 표본"
                   f" {min(r['min'] for r in passive_yaw):+.2e}~{max(r['max'] for r in passive_yaw):+.2e} rad"
                   if passive_yaw else ", 이송 중 표본 없음")
                + ". 실물 장착 방향(커플링 핀 위치)의 근거가 아니다."),
        },
        "payload_scope": {"verified_materials_kg": verified, "max_kg": payload,
                          "rule": "자재 질량이 max_kg 이하이고 질량이 선언된 자재만 집는다"},
        "gripper_max_effort": {**gripper["max_effort"],
                               "note_simulation": "관절 토크 한계(URDF)를 GripperSpec 계약 값으로 옮긴다."
                                                  " 파지력 주장이 아니다 — Gazebo 파지는 고정 장치다."},
        "coupling_model_mass": {"mass_kg": inertials.get("coupling_mass_kg"),
                                "link": inertials.get("coupling_link"),
                                "source": f"{rel} inertials (떠 있는 URDF {report['urdf']})",
                                "note": "Gazebo가 시뮬레이션하는 값이다. 실물 커플링 질량이 아니다."},
        "grasp_observation": {
            "mode": "gazebo_fixture_joint",
            "detail": "이송 실행기가 고정 장치 붙임·해제 알림과 자재 pose 수렴으로 확인한다"
                      " (robots/fr3_gazebo/sim_fixture.py). 실물 파지 감지가 아니다."},
        "revalidation": {
            "executor": "scripts/demo_workcell_pick_place.py (SimDemoJobs.start_transfer)",
            "checks": ["scene_sync", "preflight", "path_check", "scene_hash_per_stage",
                       "attached_object_collision"],
            "detail": "실행 직전 장면 동기화·단계 사전 검증·경로 표본·단계마다 장면 hash를 다시 보고,"
                      " 하나라도 실패하면 로봇을 움직이지 않는다."},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(profile, ensure_ascii=False, indent=1), encoding="utf-8")
    return profile


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--passive", type=float, default=0.0,
                        help="명령 없이 이 시간(s) 동안 yaw 표본을 모은다(이송 중 실행)")
    parser.add_argument("--static", action="store_true",
                        help="명령·구독 없이 파일 근거만 다시 모은다(질량·관성·이송 이력)")
    parser.add_argument("--write-profile", action="store_true",
                        help="보고서에서 시뮬레이션 전용 Profile을 만든다(측정·명령 없음)")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    if args.write_profile:
        report = json.loads(Path(args.out).read_text(encoding="utf-8"))
        profile = write_profile(report, SIM_PROFILE)
        print(f"[측정] Profile 기록: {SIM_PROFILE}")
        for key, item in profile["values"].items():
            print(f"  {key} = {item['value']} {item['unit']}")
        return 0
    if os.environ.get("FORSTICK2_SIM_PROFILE_MEASURE") != "1":
        print("[측정] 거부: FORSTICK2_SIM_PROFILE_MEASURE=1을 명시하지 않았다", file=sys.stderr)
        return 3
    workcell = json.loads(WORKCELL.read_text(encoding="utf-8"))
    mounting = json.loads(MOUNTING.read_text(encoding="utf-8"))
    pad_offset = abs(float(mounting["gripper_official"]["pad_face"]["x_local_m"]))
    if not URDF.is_file():
        print(f"[측정] 떠 있는 작업 셀 URDF가 없다: {URDF}", file=sys.stderr)
        return 2
    axes = reference_axes(URDF)
    out = Path(args.out)
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = {}
    report.update({
        "schema": "forstick2.sim_profile_evidence/1",
        "is_simulated": True, "real_hardware_verified": False,
        "note": "지금 떠 있는 Gazebo 작업 셀 모델의 측정값이다. 실물 근거가 아니다.",
        "urdf": str(URDF), "urdf_sha256": hashlib.sha256(URDF.read_bytes()).hexdigest(),
        "built_yaw_rad": axes["built_yaw_rad"],
        "pad_face_offset_m": pad_offset,
        "pad_face_offset_source": mounting["gripper_official"]["pad_face"]["source"],
        "official_open_gap_m": mounting["gripper_official"]["aperture_at_open_m"],
        "fk_closed_gap_m": mounting["gripper_official"]["aperture_at_closed_m"],
        "materials": material_masses(workcell),
        "inertials": model_inertials(URDF),
        "transfer_history": transfer_history(),
    })
    if args.static:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"[측정] 기록(파일 근거만): {out}")
        return 0
    stream = PoseStream(workcell["world_name"], workcell["gz_partition"])
    try:
        if args.passive > 0:
            report.setdefault("passive_runs", []).append(
                {"at": time.time(), "seconds": args.passive,
                 **passive(args.passive, stream, axes, pad_offset)})
        else:
            busy = cell_busy()
            if busy:
                print(f"[측정] 거부: {busy}", file=sys.stderr)
                return 4
            report["active"] = {"at": time.time(),
                                **active_cycles(args, stream, axes, pad_offset, mounting)}
    finally:
        stream.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[측정] 기록: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
