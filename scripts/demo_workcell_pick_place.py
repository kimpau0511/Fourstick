#!/usr/bin/env python3
"""Gazebo pick/place **시뮬레이션 E2E** 시연 (8-11).

```
FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh pallet_1 mat_a
```

**이것은 시뮬레이터 데모 검증이다.** 실제 로봇 pick/place 가능 판정이 아니다.
물체는 시뮬레이션 고정 장치(`robots/fr3_gazebo/sim_fixture.py`)로 움직인다 —
마찰 파지도, 물체 감지도 아니다. 결과는 `simulation_e2e`로만 기록되고
`real_hardware_ready`는 항상 False다.

## 왜 별도 명령인가

일반 웹 UI의 `실행 시작`이나 일반 `/v1/execute`로는 여기에 들어올 수 없다.
그 경로의 pick/place는 `capability.profile_incomplete`로 계속 막혀 있고, 이
스크립트는 그 관문을 건드리지 않는다. 진입은 환경변수
`FORSTICK2_SIM_PICK_PLACE_DEMO=1`을 **명시했을 때만** 열린다.

## 하는 일

1. 진입 조건: 환경변수 · 정지 래치 해제 · scene snapshot · 물체가 선언된
   팔레트에 있는가 · 컨베이어 배치 구역이 비었는가
2. 12단계 계획을 사전 검증한다(`validation/pick_place_plan.py` 그대로)
3. 12단계를 **순서대로 실행**한다. 매 단계 전에
   - scene hash를 다시 읽어 바뀌었으면 중단(`geometry.snapshot_expired`)
   - 적재 단계는 물체를 붙인 상태로 충돌 검사(`AttachedCollisionObject`)
4. `gripper_close` 뒤 고정, 이동 중 도구를 따라가게 하고, `gripper_open_release`
   뒤 해제해 실제로 컨베이어에 안착시킨다
5. 7개 조건을 관측으로 판정한다(`validation/simulation_e2e.py`)

`--stop-at <단계>`를 주면 그 단계 이동 중 정지를 한 번 요청하고, 정지 뒤
arm·gripper·고정 상태·물체 pose를 기록한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.reason_codes import ReasonCode  # noqa: E402
from core.task_plan import TaskStep  # noqa: E402
from robots.fr3_gazebo.adapter import load_workcell_resources  # noqa: E402
from robots.fr3_gazebo.sim_fixture import (  # noqa: E402
    FIXTURE_KIND,
    FixtureError,
    GazeboObjectFixture,
    declarations_from_config,
)
from robots.moveit.kinematics import link_transforms, parse_urdf  # noqa: E402
from validation.pick_place_plan import (  # noqa: E402
    HOLDING_STAGES,
    STAGE_GRIPPER_CLOSE,
    STAGE_GRIPPER_OPEN_RELEASE,
    STAGE_HOME_END,
    STAGE_RETREAT,
    CellBindings,
    check_stages,
    stage_joint_state,
    validate,
)
from validation.simulation_e2e import (  # noqa: E402
    CRITERIA,
    Criterion,
    SimulationTransferResult,
    in_placement_zone,
    not_started,
    placement_zone,
)

ENV_GATE = "FORSTICK2_SIM_PICK_PLACE_DEMO"
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
GRASP = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"
URDF = Path("/tmp/forstick2_gazebo/workcell/fr3wms_with_2f85.moveit.urdf")
LOG_DIR = Path("/tmp/forstick2_workcell")
LATCH = LOG_DIR / "sim_stop_latch.json"
OUT = ROOT / "reports/workcell/pick_place_sim_e2e.json"

#: 단계 이동 시간(초). **이미 검증된 값을 쓴다** — 팔은 어댑터의 이동 시간
#: (`Fr3GazeboAdapter.move_seconds` 6.0), 그리퍼는 8-08 그리퍼 검증이 쓴 5초다.
#: 더 짧게 주면 경로 허용치를 위반한다(실측: 1.5초에서 컨트롤러 결과 −4
#: PATH_TOLERANCE_VIOLATED).
ARM_SECONDS = 6.0
GRIPPER_SECONDS = 5.0
ACTION_TIMEOUT = 20.0
JOINT_TOLERANCE_RAD = 0.05
#: 안착 뒤 배치 구역 높이 허용치. 자재 높이의 10%.
PLACEMENT_Z_TOLERANCE_M = 0.01
#: 이동 중 물체가 도구를 따라오는지 판정할 허용치(고정 장치 갱신 지연 포함).
FOLLOW_TOLERANCE_M = 0.05


class StopLatchFile:
    """정지 래치. 정지 뒤 새 시뮬레이션은 **해제와 scene 재검증을 요구한다.**

    `core/stop_contract.py`와 같은 규칙이다 — 추적 중인 goal이 남아 있으면
    풀지 않는다. 시연은 별도 프로세스로 돌기 때문에 래치를 파일로 남긴다.
    """

    def __init__(self, path: Path):
        self.path = path

    def latched(self) -> dict | None:
        if not self.path.is_file():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"detail": "래치 파일을 읽지 못했다"}

    def latch(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def release(self, *, live_goals: int, scene_hash: str,
                latched_hash: str | None) -> tuple[bool, str]:
        record = self.latched()
        if record is None:
            return True, "래치가 없다"
        if live_goals:
            return False, f"추적 중인 goal이 {live_goals}개 남아 있어 풀지 않는다"
        if latched_hash is not None and scene_hash != latched_hash:
            # scene이 바뀐 것을 **확인**했다. 바뀐 환경으로 재검증을 다시 한다.
            self.path.unlink(missing_ok=True)
            return True, (f"정지 시점 scene {latched_hash[:12]} →"
                          f" 현재 {scene_hash[:12]} · 재검증 후 해제")
        self.path.unlink(missing_ok=True)
        return True, f"정지 래치 해제 · scene {scene_hash[:12]} 재검증"


def tcp_frame_of(joints, mimics, tcp_link: str, arm: dict, gripper: float):
    """FK로 TCP의 (회전, 위치)를 구한다. world 기준이다."""
    angles = dict(arm)
    angles["robotiq_85_left_knuckle_joint"] = gripper
    for name, (multiplier, offset) in mimics.items():
        angles[name] = gripper * multiplier + offset
    rotation, position = link_transforms(joints, angles)[tcp_link]
    return rotation, position


def settled_observation(transport, *, timeout_sec: float = 8.0,
                       velocity_tolerance: float = 0.01):
    """움직임이 멈춘 **새 표본**을 기다린다.

    명령 결과가 돌아온 것만으로 관측이 최신인 것은 아니다. 고정 장치 스레드가
    돌고 있으면 `/joint_states` 콜백이 밀려 캐시가 과거 표본일 수 있다
    (실측: 그 과거 표본으로 재면 이동 중 위치가 나와 도달 판정이 틀린다).
    그래서 **직전 표본보다 새로운** 표본을 받고, 연속 두 장이 멈춰 있을 때만
    그 표본으로 판정한다.
    """
    deadline = time.monotonic() + timeout_sec
    marker = time.time()
    quiet = 0
    latest = transport.joint_observation(1.0)
    while time.monotonic() < deadline:
        latest = transport.joint_observation(1.0, after=marker)
        if not latest.valid:
            continue
        marker = latest.observed_at
        peak = max((abs(v) for v in latest.velocities.values()), default=0.0)
        quiet = quiet + 1 if peak < velocity_tolerance else 0
        if quiet >= 2:
            return latest
    return latest


def load_mimics() -> dict:
    out = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        mimic = joint.find("mimic")
        if mimic is not None and mimic.get("joint") == "robotiq_85_left_knuckle_joint":
            out[joint.get("name")] = (float(mimic.get("multiplier", "1")),
                                      float(mimic.get("offset", "0")))
    return out


def build_bindings(resources) -> CellBindings:
    declaration = dict(resources.grasp_declaration)
    return CellBindings(
        resources={rid: dict(row) for rid, row in resources.resources.items()},
        object_support=dict(resources.object_support),
        approach_pose=dict(resources.move_pose),
        grasp_pose=dict(resources.grasp_pose),
        place_pose=dict(resources.place_pose),
        home_pose=resources.safe_home_pose,
        poses={name: dict(j) for name, j in resources.poses.items()},
        gripper_joint=str(declaration.get("gripper_joint") or ""),
        gripper_open_rad=declaration.get("gripper_open_rad"),
        gripper_grasp_rad=declaration.get("gripper_grasp_rad"),
        gripper_mimic=dict(declaration.get("gripper_mimic") or {}),
        pad_links=tuple(declaration.get("pad_links") or ()),
        attached={rid: dict(spec) for rid, spec
                  in (declaration.get("attached") or {}).items()},
        pose_evidence={n: dict(v) for n, v in resources.pose_evidence.items()},
        sources={"grasp": str(GRASP)},
    )


def scene_and_transport(resources, profile_limits: dict):
    """planning scene 클라이언트와 컨트롤러 전송 계층."""
    import rclpy
    from rclpy.node import Node

    from robots.fr3_gazebo.ros_transport import RosWorkcellTransport
    from robots.moveit.ros_client import RosPlanningSceneClient

    transport = RosWorkcellTransport(
        world_name=resources.world_name, gz_partition=resources.gz_partition,
        ros_domain_id=resources.ros_domain_id)
    status = transport.connect(30.0)
    if not status.world_present:
        raise SystemExit(f"작업 셀 world가 없다: {status.detail}")
    if not rclpy.ok():
        rclpy.init()
    node = Node("forstick2_sim_pick_place_scene")
    client = RosPlanningSceneClient(
        node, group_name="fr3wms_arm", frame_id="base_link",
        joint_limits=profile_limits,
        model_hash=f"{resources.workcell_id}:{resources.workcell_version}",
        ttl_sec=5.0, source="scripts/demo_workcell_pick_place.py")
    if not client.wait(timeout_sec=30.0):
        node.destroy_node()
        raise SystemExit("MoveIt planning scene 서비스가 없다")
    return client, node, transport


class Follower(threading.Thread):
    """붙어 있는 물체를 도구 위치로 계속 옮기는 스레드. **고정 장치다.**"""

    def __init__(self, *, fixture, model, transport, joints, mimics, tcp_link,
                 offset, gripper_value, hz=15.0):
        super().__init__(daemon=True)
        self.fixture = fixture
        self.model = model
        self.transport = transport
        self.joints = joints
        self.mimics = mimics
        self.tcp_link = tcp_link
        self.offset = np.asarray(offset, dtype=float)
        self.gripper_value = gripper_value
        self.period = 1.0 / hz
        self._stop = threading.Event()
        self.samples: list[dict] = []
        self.errors: list[str] = []

    def target_pose(self) -> tuple | None:
        observation = self.transport.joint_observation(0.05)
        if not observation.valid:
            return None
        arm = {name: value for name, value in observation.positions.items()
               if name.startswith("j")}
        if len(arm) < 6:
            return None
        rotation, position = tcp_frame_of(
            self.joints, self.mimics, self.tcp_link, arm, self.gripper_value)
        return tuple(position + rotation @ self.offset)

    def run(self) -> None:
        while not self._stop.is_set():
            target = self.target_pose()
            if target is not None:
                try:
                    self.fixture.follow(self.model, target)
                except FixtureError as exc:
                    self.errors.append(str(exc))
                    return
            self._stop.wait(self.period)

    def sample(self) -> dict | None:
        """물체가 도구와 함께 있는지 **관측으로** 확인한 한 표본.

        판정 직전에 한 번 따라가게 하고 갱신을 기다린다. 서비스 응답 지연을
        "도구와 떨어졌다"로 잘못 읽지 않기 위해서다 — 재는 것은 고정 장치가
        물체를 도구에 붙여 두는지이고, 요청 왕복 시간이 아니다.
        """
        target = self.target_pose()
        if target is not None:
            try:
                self.fixture.follow(self.model, target)
            except FixtureError as exc:
                self.errors.append(str(exc))
                return None
            time.sleep(0.3)
        observed = self.fixture.pose_of(self.model, timeout_sec=1.0, fresh=True)
        if target is None or observed is None:
            return None
        gap = float(np.linalg.norm(np.asarray(observed) - np.asarray(target)))
        row = {
            "at": time.time(),
            "tool_expected_m": [round(v, 6) for v in target],
            "object_observed_m": [round(v, 6) for v in observed],
            "gap_m": round(gap, 6),
            "with_tool": gap <= FOLLOW_TOLERANCE_M,
        }
        self.samples.append(row)
        return row

    def halt(self) -> None:
        self._stop.set()
        self.join(timeout=2.0)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("support", help="출발 위치 자원 id 또는 Gazebo 모델")
    parser.add_argument("object", help="자재 자원 id 또는 Gazebo 모델")
    parser.add_argument("--target", default="loc_conveyor")
    parser.add_argument("--stop-at", default=None,
                        help="이 단계 이동 중 정지를 한 번 요청한다")
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--restore-only", action="store_true",
                        help="시연하지 않고 자재를 선언된 원래 자리로만"
                             " 되돌린다(정지 뒤 정리에 쓴다)")
    parser.add_argument("--no-restore", action="store_true",
                        help="끝나고 자재를 원래 자리로 되돌리지 않는다")
    # ── 결함 주입 (검증 전용) ─────────────────────────────────────────
    # 차단 경로가 실제로 동작하는지 확인하려면 결함을 **실제로** 만들어야
    # 한다. 아래 두 옵션은 검증 스크립트만 쓰며, 정상 시연에는 쓰지 않는다.
    parser.add_argument("--inject-attach-offset-z", type=float, default=None,
                        help="검증 전용: 붙일 물체의 z 오프셋을 이만큼 밀어"
                             " 적재 상태 충돌 검사를 실제로 위반시킨다")
    parser.add_argument("--inject-attach-failure", action="store_true",
                        help="검증 전용: 고정 단계에서 선언되지 않은 물체를"
                             " 붙이려 해 고정 실패 경로를 실제로 태운다")
    parser.add_argument("--inject-scene-change-at", default=None,
                        help="검증 전용: 이 단계 앞에서 planning scene에 임시"
                             " 물체를 넣어 hash를 실제로 바꾼다")
    args = parser.parse_args()

    if os.environ.get(ENV_GATE) != "1":
        print(f"거부: {ENV_GATE}=1을 명시하지 않았다.", file=sys.stderr)
        print("  이 시연은 시뮬레이터 전용이며 실제 pick/place 가능 판정이"
              " 아니다. 일반 웹 UI·API로는 진입할 수 없다.", file=sys.stderr)
        return 3

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    if args.restore_only:
        objects = declarations_from_config(data["models"], data["frames"],
                                            source=str(WORKCELL))
        fixture = GazeboObjectFixture(
            world_name=data["world_name"], gz_partition=data["gz_partition"],
            objects=objects)
        model = args.object if args.object in objects else None
        if model is None:
            for name, row in zip(objects, objects):
                pass
            table = {r["resource_id"]: r["gazebo_model"]
                     for r in data["resource_map"]}
            model = table.get(args.object)
        if model is None or model not in objects:
            print(f"되돌릴 자재를 찾지 못했다: {args.object}", file=sys.stderr)
            return 1
        event = fixture.restore(model)
        fixture.close()
        print(f"[정리] {model} → {event.pose_m} (확인={event.verified})")
        return 0 if event.verified else 1

    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=0.5,
        mounting_path=MOUNTING, grasp_path=GRASP)
    bindings = build_bindings(resources)
    grasp_file = json.loads(GRASP.read_text(encoding="utf-8"))

    # 자원 id로 정규화한다. Gazebo 모델 이름으로 불러도 같은 것을 가리킨다.
    def resource_id(token: str) -> str:
        if token in resources.resources:
            return token
        for rid, row in resources.resources.items():
            if row["gazebo_model"] == token:
                return rid
        return token

    object_rid = resource_id(args.object)
    support_rid = resource_id(args.support)
    target_rid = resource_id(args.target)
    scenario = f"{support_rid}/{object_rid}->{target_rid}"
    model = (resources.resources.get(object_rid) or {}).get("gazebo_model", "")
    limitations = tuple(resources.grasp_declaration.get("limitations") or ()) + (
        "물체는 시뮬레이션 고정 장치로 움직인다 — 마찰 파지·물체 감지가 아니다."
        " 붙어 있는 동안 물체는 정적이므로 미끄러짐이 일어나지 않는다",
        "grasp.object_held는 simulated_observation이며 실기 관측이 아니다",
    )

    def fail(reason: ReasonCode, detail: str) -> int:
        result = not_started(
            scenario=scenario, object_id=object_rid, support_id=support_rid,
            target_id=target_rid, reason=reason, detail=detail,
            limitations=limitations)
        write(args.out, result)
        closer = globals().get("_active_fixture")
        if closer is not None:
            closer.close()
        print(f"시작하지 않았다: [{reason}] {detail}", file=sys.stderr)
        return 1

    if not model:
        return fail(ReasonCode.PLAN_UNKNOWN_RESOURCE,
                    f"셀 선언에 없는 자재다: {args.object}")

    # ── 계획 초안과 사전 검증 ─────────────────────────────────────────
    steps = (
        TaskStep(skill="pick", args={"object": object_rid, "from": support_rid}),
        TaskStep(skill="move", args={"to": target_rid}),
        TaskStep(skill="place", args={"object": object_rid, "to": target_rid}),
        TaskStep(skill="home"),
    )
    slots = _Slots([support_rid, object_rid, target_rid])

    profile_limits = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        limit = joint.find("limit")
        if limit is not None and joint.get("type") == "revolute":
            profile_limits[joint.get("name")] = (float(limit.get("lower")),
                                                 float(limit.get("upper")))

    client, node, transport = scene_and_transport(resources, profile_limits)
    joints_urdf, _links = parse_urdf(URDF)
    mimics = load_mimics()
    tcp_link = str(grasp_file["declared"]["tcp_link"])

    objects = declarations_from_config(data["models"], data["frames"],
                                       source=str(WORKCELL))
    fixture = GazeboObjectFixture(
        world_name=resources.world_name, gz_partition=resources.gz_partition,
        objects=objects)
    globals()["_active_fixture"] = fixture

    latch = StopLatchFile(LATCH)
    snapshot = client.snapshot()
    record = latch.latched()
    if record is not None:
        released, detail = latch.release(
            live_goals=transport.live_goals(),
            scene_hash=snapshot.content_hash,
            latched_hash=record.get("scene_hash"))
        print(f"[래치] {detail}")
        if not released:
            return fail(ReasonCode.EXEC_STOPPED,
                        f"정지 래치가 걸려 있다 — {detail}")
        # 해제 뒤 **scene을 다시 읽어** 재검증 기준으로 쓴다.
        snapshot = client.snapshot()

    injected: list[str] = []
    if args.inject_attach_offset_z is not None:
        spec = dict(bindings.attached[object_rid])
        offset = list(spec["offset_m"])
        offset[2] = float(offset[2]) + float(args.inject_attach_offset_z)
        spec["offset_m"] = offset
        bindings.attached[object_rid] = spec
        injected.append(f"attach_offset_z+{args.inject_attach_offset_z}")
        print(f"[결함 주입] 붙일 물체 z 오프셋 {offset[2]:.4f} m"
              " — 적재 상태 충돌 검사를 위반시킨다")

    validation = validate(steps, slots=slots, bindings=bindings, client=client,
                          utterance=f"[시뮬레이션 시연] {scenario}")
    if not validation.plan_verified:
        codes = validation.to_dict()["reason_codes"]
        first = validation.findings[0] if validation.findings else None
        return fail(first.reason_code if first else ReasonCode.GEOMETRY_COLLISION,
                    f"계획 사전 검증이 통과하지 않았다: {codes}")
    stages = validation.stages

    # ── 진입 조건: 자재가 선언된 자리에 있는가 ────────────────────────
    declared_home = objects[model].home_pose_m
    observed = fixture.pose_of(model, timeout_sec=5.0, fresh=True)
    if observed is None:
        return fail(ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                    f"{model}의 pose를 관측하지 못했다 — 씬에 없다")
    drift = float(np.linalg.norm(np.asarray(observed) - np.asarray(declared_home)))
    if drift > 0.02:
        return fail(ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                    f"{model}이 선언된 자리에 없다 — 관측"
                    f" {tuple(round(v, 4) for v in observed)}, 선언"
                    f" {tuple(round(v, 4) for v in declared_home)}"
                    f" (차이 {drift:.4f} m)")

    # ── 진입 조건: 컨베이어 배치 구역이 비었는가 ──────────────────────
    conveyor = data["models"][(resources.resources[target_rid])["gazebo_model"]]
    belt = next(part for part in conveyor["parts"] if part["name"] == "belt")
    rails = [part for part in conveyor["parts"] if part["name"].startswith("frame_")]
    target_center = _resolve(data["frames"], conveyor["frame"])
    # 허용 구역은 **선언된 치수에서** 나온다. 벨트 폭과 가드레일 안쪽 면 중
    # 좁은 쪽을 쓴다.
    rail_inner = min(abs(part["center_xyz_m"][1]) - part["size_m"][1] / 2
                     for part in rails) if rails else belt["size_m"][1] / 2
    zone = placement_zone(
        target_center_m=target_center,
        surface_half_extent_m=(belt["size_m"][0] / 2,
                               min(belt["size_m"][1] / 2, rail_inner)),
        object_size_m=objects[model].size_m,
        surface_top_z_m=target_center[2],
        z_tolerance_m=PLACEMENT_Z_TOLERANCE_M)
    for other, item in objects.items():
        if other == model:
            continue
        pose = fixture.pose_of(other, timeout_sec=2.0)
        if pose is None:
            continue
        inside, _ = in_placement_zone(pose, zone)
        if inside:
            return fail(ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                        f"배치 구역이 이미 {other}로 점유됐다"
                        f" (pose {tuple(round(v, 4) for v in pose)})")

    print(f"[시작] {scenario} · 모델 {model} · scene {snapshot.content_hash[:12]}")
    print(f"       배치 허용 구역 x[{zone['x_min_m']:.3f},{zone['x_max_m']:.3f}]"
          f" y[{zone['y_min_m']:.3f},{zone['y_max_m']:.3f}]"
          f" z={zone['z_center_m']:.3f}±{zone['z_tolerance_m']:.3f}")

    # ── 실행 ──────────────────────────────────────────────────────────
    attach_spec = dict(bindings.attached[object_rid])
    offset = attach_spec["offset_m"]
    stage_records: list[dict] = []
    timeline: list[dict] = []
    follower: Follower | None = None
    faults: list[tuple[ReasonCode, str]] = []
    attach_event = None
    detach_event = None
    stop_requested = False
    stop_confirmed: bool | None = None
    follow_samples: list[dict] = []

    def note_pose(label: str) -> None:
        pose = fixture.pose_of(model, timeout_sec=1.5, fresh=True)
        timeline.append({
            "stage": label, "at": time.time(),
            "object_pose_m": None if pose is None else [round(v, 6) for v in pose],
            "held_by_fixture": model in fixture.held(),
        })

    note_pose("before_start")

    probe_added = False
    for stage in stages:
        if args.inject_scene_change_at == stage.stage and not probe_added:
            probe_added = _apply_probe(node, add=True)
            injected.append(f"scene_change_at:{stage.stage}")
            print(f"[결함 주입] {stage.label} 앞에서 planning scene에 임시"
                  f" 물체를 넣었다 (성공={probe_added})")
            time.sleep(1.0)

        # 매 단계 전에 scene을 다시 읽는다. 바뀌었으면 중단한다.
        current = client.snapshot()
        if current.content_hash != snapshot.content_hash:
            faults.append((
                ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
                f"{stage.label} 앞에서 scene이 바뀌었다"
                f" ({snapshot.content_hash[:12]} → {current.content_hash[:12]})"))
            break

        # 적재 단계는 **물체를 붙인 상태로** 충돌 검사를 한다.
        checks, findings = check_stages([stage], bindings=bindings, client=client)
        check = checks[0]
        if not check.passed:
            faults.append((
                findings[0].reason_code if findings else ReasonCode.GEOMETRY_COLLISION,
                f"{stage.label} 검사 미통과: {check.detail or '사유 기록 없음'}"))
            stage_records.append({**_stage_row(stage), "check": check.to_dict(),
                                  "executed": False})
            break

        joints = stage_joint_state(stage, bindings)
        arm_target = {name: value for name, value in joints.items()
                      if name.startswith("j")}
        stop_here = args.stop_at == stage.stage

        if stage.stage in HOLDING_STAGES and stage.kind == "arm_motion":
            follower = Follower(
                fixture=fixture, model=model, transport=transport,
                joints=joints_urdf, mimics=mimics, tcp_link=tcp_link,
                offset=offset, gripper_value=bindings.gripper_grasp_rad)
            follower.start()

        if stop_here:
            # **이동 중**에 정지한다. 결과를 기다리지 않는 전송으로 보내고,
            # 궤적이 진행되는 동안 취소를 요청한다.
            outcome = transport.send_arm_async(
                arm_target, ARM_SECONDS, ACTION_TIMEOUT)
            time.sleep(ARM_SECONDS / 3.0)
        elif stage.kind == "gripper":
            outcome = transport.send_gripper(
                float(stage.gripper_joint_rad), GRIPPER_SECONDS, ACTION_TIMEOUT)
        else:
            outcome = transport.send_arm(arm_target, ARM_SECONDS, ACTION_TIMEOUT)

        if stop_here:
            stop_requested = True
            cancel = transport.cancel_all(5.0)
            time.sleep(0.5)
            observation = settled_observation(transport, timeout_sec=5.0)
            peak = max((abs(v) for v in observation.velocities.values()),
                       default=0.0)
            stop_confirmed = bool(cancel.accepted and observation.valid
                                  and peak < 0.01)
            if follower is not None:
                follower.halt()
                follow_samples.extend(follower.samples)
                follower = None
            note_pose(f"stopped_at:{stage.stage}")
            latch.latch({
                "at": time.time(), "stage": stage.stage,
                "scene_hash": current.content_hash,
                "held_by_fixture": list(fixture.held()),
                "object_pose_m": timeline[-1]["object_pose_m"],
                "arm_peak_rad_s": peak,
                "goals_canceling": cancel.goals_canceling,
                "detail": "정지 뒤 새 시뮬레이션은 래치 해제와 scene 재검증을"
                          " 요구한다",
            })
            stage_records.append({
                **_stage_row(stage), "check": check.to_dict(), "executed": True,
                "stopped": True, "cancel_accepted": cancel.accepted,
                "goals_canceling": cancel.goals_canceling,
                "arm_peak_rad_s": peak, "stop_confirmed": stop_confirmed,
            })
            break

        if follower is not None:
            row = follower.sample()
            if row is not None:
                follow_samples.append(row)
            follower.halt()
            follow_samples.extend(follower.samples)
            follower = None

        observation = settled_observation(transport)
        if stage.kind == "gripper":
            name = bindings.gripper_joint
            error = abs(observation.positions.get(name, float("nan"))
                        - float(stage.gripper_joint_rad))
        else:
            error = max((abs(observation.positions.get(n, float("nan")) - v)
                         for n, v in arm_target.items()), default=float("nan"))
        reached = bool(error == error and error <= JOINT_TOLERANCE_RAD)
        per_joint = {name: round(observation.positions.get(name, float("nan")) - v, 6)
                     for name, v in (arm_target if stage.kind != "gripper"
                                     else {bindings.gripper_joint:
                                           float(stage.gripper_joint_rad)}).items()}
        row = {
            **_stage_row(stage), "check": check.to_dict(), "executed": True,
            "goal_accepted": outcome.accepted,
            "controller_error_code": outcome.error_code,
            "observed_error_rad": None if error != error else round(error, 6),
            "observed_error_per_joint_rad": per_joint,
            "reached": reached,
        }
        stage_records.append(row)
        if not outcome.accepted or outcome.error_code not in (0, None):
            faults.append((ReasonCode.EXEC_GOAL_REJECTED,
                           f"{stage.label} 컨트롤러 결과 {outcome.error_code}"
                           f" ({outcome.detail})"))
            break
        if not reached:
            faults.append((ReasonCode.EXEC_GOAL_NOT_REACHED,
                           f"{stage.label} 목표 미도달 (오차 {error:.5f} rad)"))
            break

        # ── 고정·해제 ─────────────────────────────────────────────────
        if stage.stage == STAGE_GRIPPER_CLOSE:
            pose = fixture.pose_of(model, timeout_sec=2.0, fresh=True)
            attach_model = model
            if args.inject_attach_failure:
                attach_model = f"{model}__undeclared_probe"
                injected.append("attach_failure")
                print(f"[결함 주입] 선언되지 않은 물체 {attach_model}를"
                      " 붙이려 한다")
            try:
                attach_event = fixture.attach(attach_model, pose_m=pose)
            except FixtureError as exc:
                faults.append((exc.reason, str(exc)))
                break
            print(f"  [고정] {model} @ {tuple(round(v, 4) for v in pose)}"
                  f" ({FIXTURE_KIND})")
            note_pose("after_attach")
        elif stage.stage == STAGE_GRIPPER_OPEN_RELEASE:
            pose = fixture.pose_of(model, timeout_sec=2.0, fresh=True)
            try:
                detach_event = fixture.detach(model, pose_m=pose)
            except FixtureError as exc:
                faults.append((exc.reason, str(exc)))
                break
            settled = fixture.settle(model)
            print(f"  [해제] {model} → 안착"
                  f" {tuple(round(v, 4) for v in settled) if settled else None}")
            note_pose("after_detach_settled")
        else:
            note_pose(stage.stage)

        print(f"  [{stage.no:2d}/{len(stages)}] {stage.label:16s}"
              f" 오차 {error:.5f} rad · 도달={reached}")

    if follower is not None:
        follower.halt()
        follow_samples.extend(follower.samples)
    if probe_added:
        _apply_probe(node, add=False)
        time.sleep(1.0)
        print("[결함 주입] 임시 물체를 제거해 scene을 되돌렸다")

    executed = [row for row in stage_records if row.get("executed")]
    final_pose = fixture.pose_of(model, timeout_sec=3.0, fresh=True)
    in_zone, zone_detail = (in_placement_zone(final_pose, zone)
                            if final_pose is not None
                            else (False, "최종 pose를 관측하지 못했다"))

    # ── 안전 자세 복귀 확인 ───────────────────────────────────────────
    returned = False
    return_detail = "복귀를 확인하지 못했다"
    if not stop_requested:
        for name in (STAGE_HOME_END, STAGE_RETREAT):
            stage = next((s for s in stages if s.stage == name), None)
            row = next((r for r in stage_records if r["stage"] == name), None)
            if stage is not None and row is not None and row.get("reached"):
                returned = True
                return_detail = f"{stage.label} 관측 도달 (자세 {stage.pose_name})"
                break

    criteria = [
        Criterion(
            key="stages_completed", label=dict(CRITERIA)["stages_completed"],
            met=len(executed) == len(stages) and not faults,
            detail=f"{len(executed)}/{len(stages)} 단계 실행",
            reason_code=(None if (len(executed) == len(stages) and not faults)
                         else (faults[0][0] if faults
                               else ReasonCode.EXEC_GOAL_NOT_REACHED)),
            evidence={"executed": len(executed), "planned": len(stages)}),
        Criterion(
            key="attached_at_declared_support",
            label=dict(CRITERIA)["attached_at_declared_support"],
            met=bool(attach_event and attach_event.verified
                     and bindings.object_support.get(object_rid) == support_rid),
            detail=(f"{model}을 {support_rid}에서 고정" if attach_event
                    else "고정 기록이 없다"),
            reason_code=(None if (attach_event and attach_event.verified
                                  and bindings.object_support.get(object_rid)
                                  == support_rid)
                         else ReasonCode.EXEC_SIM_FIXTURE_FAILED),
            evidence={"attach": None if attach_event is None
                      else attach_event.to_dict(),
                      "declared_support": bindings.object_support.get(object_rid)}),
        Criterion(
            key="moved_with_tool", label=dict(CRITERIA)["moved_with_tool"],
            met=bool(follow_samples) and all(row["with_tool"]
                                             for row in follow_samples),
            detail=(f"표본 {len(follow_samples)}개, 최대 간격 "
                    f"{max((r['gap_m'] for r in follow_samples), default=0):.4f} m"
                    if follow_samples else "이동 중 표본이 없다"),
            reason_code=(None if (follow_samples
                                  and all(r["with_tool"] for r in follow_samples))
                         else ReasonCode.EXEC_SIM_FIXTURE_FAILED),
            evidence={"samples": follow_samples,
                      "tolerance_m": FOLLOW_TOLERANCE_M}),
        Criterion(
            key="detached_at_target", label=dict(CRITERIA)["detached_at_target"],
            met=bool(detach_event and detach_event.verified),
            detail=("컨베이어 위치에서 해제" if detach_event
                    else "해제 기록이 없다"),
            reason_code=(None if (detach_event and detach_event.verified)
                         else ReasonCode.EXEC_SIM_FIXTURE_FAILED),
            evidence={"detach": None if detach_event is None
                      else detach_event.to_dict()}),
        Criterion(
            key="placement_in_zone", label=dict(CRITERIA)["placement_in_zone"],
            met=bool(in_zone and detach_event is not None),
            detail=zone_detail,
            reason_code=(None if (in_zone and detach_event is not None)
                         else ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE),
            evidence={"zone": zone,
                      "final_pose_m": None if final_pose is None
                      else [round(v, 6) for v in final_pose]}),
        Criterion(
            key="returned_to_safe_pose",
            label=dict(CRITERIA)["returned_to_safe_pose"],
            met=returned, detail=return_detail,
            reason_code=None if returned else ReasonCode.EXEC_GOAL_NOT_REACHED,
            evidence={"gripper_open_rad": bindings.gripper_open_rad}),
        Criterion(
            key="no_fault_during_run", label=dict(CRITERIA)["no_fault_during_run"],
            met=not faults and not stop_requested,
            detail=("오류·정지 없음" if not faults and not stop_requested
                    else " · ".join(f"[{code}] {text}" for code, text in faults)
                         or "정지가 요청됐다"),
            reason_code=(None if (not faults and not stop_requested)
                         else (faults[0][0] if faults else ReasonCode.EXEC_STOPPED)),
            evidence={"faults": [[str(code), text] for code, text in faults],
                      "stop_requested": stop_requested}),
    ]

    result = SimulationTransferResult(
        scenario=scenario, object_id=object_rid, support_id=support_rid,
        target_id=target_rid, criteria=tuple(criteria),
        fixture_events=fixture.event_dicts(),
        object_pose_timeline=tuple(timeline),
        plan_validation=validation.to_dict(),
        stage_records=tuple(stage_records),
        stop_requested=stop_requested, stop_confirmed=stop_confirmed,
        detail=zone_detail,
        limitations=limitations + (
            tuple(f"검증 전용 결함 주입: {item}" for item in injected)))

    if not args.no_restore and not stop_requested:
        fixture.restore(model)
        print(f"  [정리] {model}을 선언된 원래 자리로 되돌렸다")

    write(args.out, result)
    # gz 구독을 먼저 끊는다. 인터프리터 정리 중에 콜백이 들어오면 abort한다.
    fixture.close()
    node.destroy_node()
    import rclpy

    if rclpy.ok():
        rclpy.shutdown()

    print(f"\n판정: {result.status.value}"
          f" · simulation_e2e={result.simulation_e2e}"
          f" · real_hardware_ready={result.real_hardware_ready}"
          f" · grasp={result.grasp_observation_kind}")
    for item in result.criteria:
        print(f"  [{'OK ' if item.met else 'BAD'}] {item.label}: {item.detail}")
    print(f"기록: {args.out}")
    return 0 if result.simulation_e2e else 1


def _apply_probe(node, *, add: bool) -> bool:
    """planning scene에 임시 물체를 넣거나 뺀다(검증 전용 결함 주입)."""
    import rclpy
    from geometry_msgs.msg import Pose
    from moveit_msgs.msg import CollisionObject, PlanningScene
    from moveit_msgs.srv import ApplyPlanningScene
    from shape_msgs.msg import SolidPrimitive

    client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    if not client.wait_for_service(timeout_sec=15.0):
        return False
    obj = CollisionObject()
    obj.header.frame_id = "base_link"
    obj.id = "forstick2_sim_e2e_scene_probe"
    obj.operation = CollisionObject.ADD if add else CollisionObject.REMOVE
    if add:
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.BOX
        primitive.dimensions = [0.02, 0.02, 0.02]
        pose = Pose()
        # 로봇·셀에서 멀리 둔다 — 충돌 판정을 바꾸지 않고 hash만 바꾼다.
        pose.position.x, pose.position.y, pose.position.z = (-1.5, -1.5, 0.05)
        pose.orientation.w = 1.0
        obj.primitives = [primitive]
        obj.primitive_poses = [pose]
    scene = PlanningScene()
    scene.is_diff = True
    scene.world.collision_objects.append(obj)
    request = ApplyPlanningScene.Request()
    request.scene = scene
    future = client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=30.0)
    return bool(future.done() and future.result() and future.result().success)


def _stage_row(stage) -> dict:
    return {"no": stage.no, "stage": stage.stage, "label": stage.label,
            "kind": stage.kind, "pose_name": stage.pose_name,
            "gripper_joint_rad": stage.gripper_joint_rad,
            "holds_object": stage.holds_object}


def _resolve(frames, name):
    out = [0.0, 0.0, 0.0]
    while name is not None:
        out = [a + b for a, b in zip(out, frames[name]["xyz_m"])]
        name = frames[name]["parent"]
    return tuple(out)


class _Slots:
    class Match:
        def __init__(self, resource_id):
            self.resource_id = resource_id
            self.surface = f"[시연] {resource_id}"

    def __init__(self, ids):
        self.matches = [self.Match(rid) for rid in ids]


def write(path, result: SimulationTransferResult) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # gz 구독 콜백은 C++ 스레드에서 파이썬을 호출한다. 인터프리터가 정리되는
    # 중에 콜백이 들어오면 프로세스가 죽는다(실측: 종료 코드 -11 SIGSEGV,
    # 판정은 이미 기록된 뒤였다). 남은 일이 종료뿐이므로 정리를 건너뛰고 바로
    # 끝낸다 — 그래야 종료 코드가 판정을 그대로 말한다.
    os._exit(_code)
