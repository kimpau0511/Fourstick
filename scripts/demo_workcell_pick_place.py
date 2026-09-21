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

## 끝난 뒤 셀 처리 (`--cell-policy`)

- `e2e_reset`(기본값): E2E 검증과 같다. 끝나면 자재를 원래 자리로 되돌린다.
- `simulation_demo_hold`: 사용자 시연. **이송이 완료됐을 때만** 자재를
  컨베이어에 그대로 두고 `validation/simulation_demo_state.py` 기록에 남긴다.
  STOP·실패·고정 장치 결함은 상태 유지 성공으로 기록하지 않는다. 셀에 자재가
  남아 있으면 `--restore-only`로 되돌려야 다음 시연을 시작할 수 있다.

## resume 사전검증 (`--resume-preflight`)

STOP 체크포인트에서 이어서 계획할 수 있는지 **관측으로만** 판정하고, 통과하면
새 재계획을 기록한다. 로봇 명령·래치 해제·고정 장치 조작을 하지 않는다.
실제 resume 실행은 없다.

```
FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh \
    - material_a --resume-preflight
```

## 원래 슬롯 복귀 (`--return-held-to-origin`)

`simulation_demo_hold`로 컨베이어에 남긴 자재를 로봇으로 **원래 팔레트 슬롯**에
되돌린다. `--restore-only`(물체를 순간 이동)와 달리 12단계를 실제로 실행한다.

```
FORSTICK2_SIM_PICK_PLACE_DEMO=1 ./scripts/demo_workcell_pick_place.sh \
    - material_a --return-held-to-origin
```

복귀 전 조건과 기록 규칙은 `validation/simulation_demo_state.py`가 정한다.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

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
    build_stages,
    STAGE_GRIPPER_CLOSE,
    STAGE_GRIPPER_OPEN_RELEASE,
    STAGE_HOME_END,
    STAGE_RETREAT,
    CellBindings,
    check_stages,
    stage_joint_state,
    validate,
)
from validation.simulation_demo_state import (  # noqa: E402
    CHECKPOINT_UNAVAILABLE,
    OBJECT_HELD,
    ORIGIN_TOLERANCE_M,
    POLICIES,
    POLICY_DEMO_HOLD,
    POLICY_E2E_RESET,
    RESUME_PATH_STEP_RAD,
    RESUME_POSE_TOLERANCE_M,
    SimulationDemoState,
    build_return_stages,
    build_resume_stages,
    build_stop_checkpoint,
    conveyor_grasp_center,
    conveyor_grasp_pose,
    origin_slot,
    pallet_place_pose,
    resume_preflight_findings,
    return_bindings,
    return_preflight,
    should_restore,
    slot_occupant,
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
#: 실행별 stdout/stderr **원본** 로그. 화면 필터와 별개로 전부 남긴다.
RAW_LOG_DIR = ROOT / "reports/workcell/sim_demo_logs"
#: 이번 실행의 원본 로그 경로(`start_raw_log`가 정한다). 보고서에 남긴다.
RAW_LOG: dict = {"path": None, "detail": "원본 로그를 시작하지 않았다"}
#: 이번 시연 실행 id와 STOP 체크포인트 결과. 보고서에 남긴다.
RUN_INFO: dict = {"run_id": None, "stop_checkpoint": None}

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
#: STOP 뒤 관측을 기다리는 상한(초). 정지 확인(`settled_observation`)과 자재
#: 수렴 대기가 **같은 값**을 쓴다 — 새 대기값을 만들지 않는다.
STOP_SETTLE_TIMEOUT_SEC = 5.0
#: 자재 pose 표본 한 장을 기다리는 상한(초). 추종 표본(`Follower.sample`)과 같다.
FOLLOW_SAMPLE_TIMEOUT_SEC = 1.0
#: 수렴으로 인정하는 연속 fresh 표본 수.
CONVERGENCE_CONSECUTIVE = 2


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


#: 웹·서버가 쓰는 **외부 정지 요청** 파일. 시연 프로세스는 이동을 기다리는
#: 동안 이 파일을 보고, 있으면 기존 STOP 절차(취소 → 정지 확인 → 수렴 →
#: 체크포인트 → 래치)로 들어간다.
STOP_REQUEST = LOG_DIR / "sim_demo_stop_request.json"


class ExternalStopRequest:
    """외부 정지 요청. **이 실행이 시작된 뒤** 쓰인 요청만 인정한다."""

    def __init__(self, path: Path, *, since: float):
        self.path = path
        self.since = since
        self.seen: dict | None = None

    def requested(self) -> bool:
        if self.seen is not None:
            return True
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if float(record.get("requested_at") or 0.0) < self.since:
            return False   # 이 실행 전의 오래된 요청은 무시한다
        self.seen = record
        return True

    def consume(self) -> None:
        """처리한 그 요청만 지운다(그 사이 새 요청이면 남긴다)."""
        if self.seen is None:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if current.get("request_id") == self.seen.get("request_id"):
            self.path.unlink(missing_ok=True)


#: 이 실행의 외부 정지 감시기. `main`이 시작할 때 만든다.
EXTERNAL_STOP = ExternalStopRequest(STOP_REQUEST, since=float("inf"))


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

    # rclpy는 **이 스크립트가** 먼저 올린다. transport가 올리면 그 disconnect()가
    # rclpy.shutdown()까지 해서, 종료 순서(disconnect → node·fixture 정리 →
    # shutdown)가 깨진다.
    if not rclpy.ok():
        rclpy.init()
    transport = RosWorkcellTransport(
        world_name=resources.world_name, gz_partition=resources.gz_partition,
        ros_domain_id=resources.ros_domain_id)
    status = transport.connect(30.0)
    if not status.world_present:
        raise SystemExit(f"작업 셀 world가 없다: {status.detail}")
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


def shutdown_ros(*, transport=None, fixture=None, node=None,
                 rclpy_module=None) -> list[str]:
    """ROS 자원을 **정해진 순서로** 정리한다. 한 단계가 실패해도 끝까지 간다.

    1. transport.disconnect() — executor spin 스레드를 먼저 내린다. rclpy가
       먼저 내려가면 그 스레드가 `ExternalShutdownException`을 던진다
       (2026-09-18 A 복귀 실행에서 `Exception in thread Thread-1 (spin)`).
    2. fixture.close() · node.destroy_node()
    3. rclpy.shutdown()

    반환: 정리 중 난 예외 설명(없으면 빈 목록). 판정에는 쓰지 않는다.
    """
    errors: list[str] = []

    def attempt(label: str, action) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — 정리는 끝까지 진행한다
            errors.append(f"{label}: {type(exc).__name__}: {exc}"[:200])

    if transport is not None and callable(getattr(transport, "disconnect", None)):
        attempt("transport.disconnect", transport.disconnect)
    if fixture is not None:
        attempt("fixture.close", fixture.close)
    if node is not None:
        attempt("node.destroy_node", node.destroy_node)
    if rclpy_module is None:
        try:
            import rclpy as rclpy_module
        except ImportError as exc:
            errors.append(f"rclpy import: {exc}"[:200])
            rclpy_module = None
    if rclpy_module is not None:
        def shutdown() -> None:
            if rclpy_module.ok():
                rclpy_module.shutdown()
        attempt("rclpy.shutdown", shutdown)
    for text in errors:
        print(f"[정리] {text}", file=sys.stderr)
    return errors


def start_raw_log(directory: Path = RAW_LOG_DIR, label: str = "demo") -> dict:
    """이 프로세스의 fd 1·2 **원본 출력 전체**를 실행별 파일에 함께 남긴다.

    파이썬 print뿐 아니라 ROS C 로그·스레드 예외처럼 fd에 직접 쓰는 것도
    담는다. 화면(원래 fd)으로도 그대로 나간다 — 화면 쪽 필터와 무관하다.
    `tee`가 없거나 실패하면 원본 로그 없이 진행하고 그 사실을 남긴다.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (f"{time.strftime('%Y%m%dT%H%M%S')}_{os.getpid()}"
                            f"_{label}.log")
        sys.stdout.flush()
        sys.stderr.flush()
        for fd in (2, 1):
            saved = os.dup(fd)
            tee = subprocess.Popen(["tee", "-a", str(path)],
                                   stdin=subprocess.PIPE, stdout=saved)
            os.close(saved)
            os.dup2(tee.stdin.fileno(), fd)
            tee.stdin.close()
        # 원본 로그의 줄 순서가 실제 순서에 가깝도록 줄 단위로 내보낸다.
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
        RAW_LOG.update(path=str(path), detail="stdout·stderr 원본 전체")
    except (OSError, ValueError) as exc:
        RAW_LOG.update(path=None,
                       detail=f"원본 로그를 시작하지 못했다: {type(exc).__name__}: {exc}")
    return dict(RAW_LOG)


def expected_held_pose(*, joints_urdf, mimics, tcp_link: str, positions,
                       gripper_joint: str, gripper_default: float,
                       offset) -> tuple | None:
    """관절 관측의 FK + 파지 offset으로 든 자재가 있어야 할 pose."""
    arm = {n: v for n, v in dict(positions or {}).items() if n.startswith("j")}
    if len(arm) < 6 or offset is None:
        return None
    rotation, position = tcp_frame_of(
        joints_urdf, mimics, tcp_link, arm,
        dict(positions).get(gripper_joint, gripper_default))
    return tuple(float(v) for v in position + rotation @ np.asarray(offset))


def await_object_convergence(*, fixture, model: str, expected_pose_m,
                             tolerance_m: float | None = None,
                             timeout_sec: float = STOP_SETTLE_TIMEOUT_SEC,
                             sample_timeout_sec: float = FOLLOW_SAMPLE_TIMEOUT_SEC,
                             consecutive: int = CONVERGENCE_CONSECUTIVE,
                             clock=time.monotonic) -> dict:
    """든 자재가 도구 위치(기대 pose)에 **수렴**할 때까지 제한 시간 동안 본다.

    표본은 `fresh=True`로만 읽는다(캐시를 비우고 새 pose 메시지를 기다린다).
    새 메시지가 없으면(None) 표본으로 치지 않고 연속 수를 0으로 되돌린다.
    로봇 명령을 보내지 않는다 — 추종기(고정 장치)는 그대로 돌게 둔다.

    허용치는 새 값을 만들지 않고 **두 기존 기준 중 좁은 쪽**을 쓴다 — 추종
    허용치(`FOLLOW_TOLERANCE_M`)와 resume 재검증의 자재 pose 허용치
    (`RESUME_POSE_TOLERANCE_M`). resume이 받아들이지 못할 체크포인트를 만들지
    않기 위해서다.
    """
    if tolerance_m is None:
        tolerance_m = min(FOLLOW_TOLERANCE_M, RESUME_POSE_TOLERANCE_M)
    started = clock()
    samples = 0
    in_a_row = 0
    observed = None
    error = None
    converged = False
    if expected_pose_m is not None:
        while clock() - started < timeout_sec:
            pose = fixture.pose_of(model, timeout_sec=sample_timeout_sec, fresh=True)
            if pose is None:
                in_a_row = 0
                continue
            samples += 1
            observed = tuple(float(v) for v in pose)
            error = float(np.linalg.norm(np.asarray(observed)
                                         - np.asarray(expected_pose_m)))
            in_a_row = in_a_row + 1 if error <= tolerance_m else 0
            if in_a_row >= consecutive:
                converged = True
                break
    return {
        "converged": converged,
        "expected_pose_m": (None if expected_pose_m is None
                            else [round(float(v), 6) for v in expected_pose_m]),
        "observed_pose_m": None if observed is None else [round(v, 6) for v in observed],
        "error_m": None if error is None else round(error, 6),
        "samples": samples, "consecutive_within": in_a_row,
        "required_consecutive": consecutive, "tolerance_m": tolerance_m,
        "applied_tolerance_m": tolerance_m,
        "follow_tolerance_m": FOLLOW_TOLERANCE_M,
        "resume_pose_tolerance_m": RESUME_POSE_TOLERANCE_M,
        "timeout_s": timeout_sec,
        "settle_elapsed_s": round(clock() - started, 3),
        "detail": ("기대 pose를 계산하지 못했다" if expected_pose_m is None
                   else "수렴" if converged else "제한 시간 안에 수렴하지 않았다"),
    }


def settle_held_object(*, follower, fixture, model: str, confirmed: bool,
                       capture: bool, expected_pose_m) -> dict | None:
    """STOP 뒤 추종기 정지 순서. 체크포인트를 남길 때만 수렴을 먼저 확인한다.

    확인된 STOP이고 자재를 든 채 체크포인트를 남길 경로면: 추종기를 켠 채
    수렴을 기다리고 → 그 뒤에 halt. 그 밖에는 기존대로 바로 halt.
    반환: 수렴 기록(대기하지 않았으면 None).
    """
    convergence = None
    if capture and confirmed and model in fixture.held():
        convergence = await_object_convergence(
            fixture=fixture, model=model, expected_pose_m=expected_pose_m)
    if follower is not None:
        follower.halt()
    return convergence


def capture_stop_checkpoint(*, mode: str, model: str, object_id: str,
                            source_id: str, destination_id: str,
                            origin_slot_id: str, stages, stopped_stage: str,
                            stage_records, confirmed: bool | None,
                            stop_execution_id: str, stop_requested_at: float,
                            observation, gripper_joint: str, fixture, client,
                            scene_hash_at_stop: str | None, conveyor_zone,
                            origin_home_m, convergence: dict | None = None,
                            ) -> dict | None:
    """확인된 STOP 뒤 **관측으로** 체크포인트 후보를 만든다. 기록·재개는 하지 않는다.

    든 자재면 저장 pose는 **수렴 관측값**이다(캡처 시점에 다시 읽지 않는다).
    """
    try:
        scene_now = client.snapshot().content_hash
    except Exception:  # noqa: BLE001 — 읽지 못하면 없는 것으로 둔다
        scene_now = None
    held = model in fixture.held()
    object_pose = ((convergence or {}).get("observed_pose_m") if held
                   else fixture.pose_of(model, timeout_sec=2.0, fresh=True))
    checkpoint, reasons = build_stop_checkpoint(
        run_id=str(RUN_INFO.get("run_id") or ""), mode=mode, model=model,
        object_id=object_id, source_id=source_id, destination_id=destination_id,
        origin_slot_id=origin_slot_id,
        stage_names=[stage.stage for stage in stages], stopped_stage=stopped_stage,
        completed_stages=[row["stage"] for row in stage_records
                          if row.get("reached")],
        stop_confirmed=confirmed, stop_execution_id=stop_execution_id,
        stop_requested_at=stop_requested_at,
        joint_observation=None if observation is None else {
            "positions": dict(observation.positions),
            "observed_at": observation.observed_at,
            "valid": observation.valid},
        gripper_joint=gripper_joint, held=held,
        object_pose_m=object_pose, conveyor_zone=conveyor_zone,
        origin_home_m=origin_home_m, scene_hash_at_stop=scene_hash_at_stop,
        scene_hash_now=scene_now, convergence=convergence)
    RUN_INFO["stop_checkpoint"] = (
        {"created": False, "model": model, "reasons": reasons,
         "reason_code": (CHECKPOINT_UNAVAILABLE if held else None),
         "convergence": convergence}
        if checkpoint is None
        else {"created": None, "checkpoint_id": checkpoint["checkpoint_id"],
              "object_state": checkpoint["object_state"],
              "convergence": convergence})
    if checkpoint is None:
        print(f"[체크포인트] 만들지 않았다: {' · '.join(reasons)}")
    return checkpoint


def record_stop_checkpoint(demo_state, checkpoint: dict | None) -> None:
    """판정 기록 **뒤에** 체크포인트를 남긴다. 재개하지 않는다.

    만들지 못했으면(든 자재 미수렴 등) 그 사유·수렴 기록만 남긴다 — 자재 기록과
    래치는 그대로다.
    """
    if checkpoint is None:
        info = RUN_INFO.get("stop_checkpoint") or {}
        if info.get("reason_code") == CHECKPOINT_UNAVAILABLE and info.get("model"):
            demo_state.record_checkpoint_unavailable(info["model"], {
                "reasons": info.get("reasons"),
                "convergence": info.get("convergence")})
        return
    demo_state.record_checkpoint(checkpoint)
    RUN_INFO["stop_checkpoint"] = {"created": True,
                                   "checkpoint_id": checkpoint["checkpoint_id"],
                                   "object_state": checkpoint["object_state"],
                                   "convergence": checkpoint.get("convergence"),
                                   "resume_available": False}
    print(f"[체크포인트] {checkpoint['checkpoint_id']} · 자재 상태"
          f" {checkpoint['object_state']} · 남은 단계"
          f" {len(checkpoint['remaining_stages'])} · 재개 없음")


def _raw_log_fields() -> dict:
    path = RAW_LOG.get("path")
    return {"raw_log_path": (str(Path(path).relative_to(ROOT))
                             if path and Path(path).is_relative_to(ROOT) else path),
            "raw_log_detail": RAW_LOG.get("detail"),
            "simulation_demo_run_id": RUN_INFO.get("run_id"),
            "stop_checkpoint": RUN_INFO.get("stop_checkpoint"),
            # 외부 정지 요청(웹 STOP)으로 멈췄으면 그 요청.
            "external_stop_request": EXTERNAL_STOP.seen}


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
        observed = self.fixture.pose_of(self.model,
                                        timeout_sec=FOLLOW_SAMPLE_TIMEOUT_SEC,
                                        fresh=True)
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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--cell-policy", choices=POLICIES,
                        default=POLICY_E2E_RESET,
                        help="끝난 뒤 셀 처리. 기본 e2e_reset은 되돌린다."
                             " simulation_demo_hold는 이송 완료 시 자재를"
                             " 컨베이어에 남기고 시연 상태로 기록한다")
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
    parser.add_argument("--resume-preflight", action="store_true",
                        help="STOP 체크포인트에서 재계획할 수 있는지 관측으로만"
                             " 사전검증한다(로봇 명령 없음, resume 실행 없음)")
    parser.add_argument("--resume-checkpoint", default=None, metavar="CHECKPOINT_ID",
                        help="이 STOP 체크포인트에서 실행 직전 재검증 뒤 이어서 실행한다"
                             " (새 재계획, 중단 궤적 재생 없음)")
    parser.add_argument("--return-held-to-origin", action="store_true",
                        help="simulation_demo_hold 기록이 있는 자재를 컨베이어에서"
                             " 선언된 원래 팔레트 슬롯으로 이송해 되돌린다")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if os.environ.get(ENV_GATE) != "1":
        print(f"거부: {ENV_GATE}=1을 명시하지 않았다.", file=sys.stderr)
        print("  이 시연은 시뮬레이터 전용이며 실제 pick/place 가능 판정이"
              " 아니다. 일반 웹 UI·API로는 진입할 수 없다.", file=sys.stderr)
        return 3

    label = ("resume" if args.resume_checkpoint
             else "resume_preflight" if args.resume_preflight
             else "return" if args.return_held_to_origin
             else "restore" if args.restore_only else "forward")
    start_raw_log(label=label)
    EXTERNAL_STOP.since = time.time()
    RUN_INFO["run_id"] = f"simdemo_{time.strftime('%Y%m%dT%H%M%S')}_{os.getpid()}"
    print(f"[원본 로그] {RAW_LOG['path'] or RAW_LOG['detail']}")

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
        # 시연 상태 기록은 **확인된 복귀만** 지운다.
        demo_state = SimulationDemoState()
        demo_state.mark_restored(model, verified=event.verified)
        print(f"[정리] {model} → {event.pose_m} (확인={event.verified})")
        if event.verified and StopLatchFile(LATCH).latched() is not None:
            # 복구가 관측으로 확인된 뒤에만, 셀 전체 조건을 다시 보고 래치를 푼다.
            result = clear_latch_after_restore_live(
                data=data, model=model, fixture=fixture, objects=objects,
                demo_state=demo_state)
            print(f"[래치] {'해제' if result['cleared'] else '유지'}: "
                  + (" · ".join(result["reasons"]) or "조건 전부 충족"))
        fixture.close()
        return 0 if event.verified else 1

    if args.resume_checkpoint:
        return resume_checkpoint(args, data)
    if args.resume_preflight:
        return resume_preflight(args, data)
    if args.return_held_to_origin:
        return return_held_to_origin(args, data)

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

    demo_state = SimulationDemoState()

    def fail(reason: ReasonCode, detail: str) -> int:
        left = demo_state.objects()
        if left and reason in (ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                               ReasonCode.EXEC_SIM_TARGET_OCCUPIED):
            detail += (f" — 시뮬레이션 시연 상태가 셀에 남아 있다"
                       f" ({', '.join(sorted(left))}), --restore-only로"
                       " 되돌린 뒤 다시 시작한다")
        result = not_started(
            scenario=scenario, object_id=object_rid, support_id=support_rid,
            target_id=target_rid, reason=reason, detail=detail,
            limitations=limitations)
        write(args.out, result)
        demo_state.record_run(policy=args.cell_policy, model=model,
                              result=result.to_dict(), final_pose_m=None,
                              restored=None)
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
    pending_checkpoint: dict | None = None
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
        # 외부 정지 요청(웹 STOP 등)이 단계 전에 이미 와 있으면 움직이지 않는다.
        external_stop = EXTERNAL_STOP.requested()

        if stage.stage in HOLDING_STAGES and stage.kind == "arm_motion":
            follower = Follower(
                fixture=fixture, model=model, transport=transport,
                joints=joints_urdf, mimics=mimics, tcp_link=tcp_link,
                offset=offset, gripper_value=bindings.gripper_grasp_rad)
            follower.start()

        if external_stop:
            pass
        elif stop_here:
            # **이동 중**에 정지한다. 결과를 기다리지 않는 전송으로 보내고,
            # 궤적이 진행되는 동안 취소를 요청한다.
            outcome = transport.send_arm_async(
                arm_target, ARM_SECONDS, ACTION_TIMEOUT)
            time.sleep(ARM_SECONDS / 3.0)
        elif stage.kind == "gripper":
            outcome = transport.send_gripper(
                float(stage.gripper_joint_rad), GRIPPER_SECONDS, ACTION_TIMEOUT,
                should_stop=EXTERNAL_STOP.requested)
        else:
            outcome = transport.send_arm(arm_target, ARM_SECONDS, ACTION_TIMEOUT,
                                         should_stop=EXTERNAL_STOP.requested)
        external_stop = external_stop or (not stop_here
                                          and EXTERNAL_STOP.requested())
        if external_stop:
            print(f"[외부 정지 요청] {stage.label} 중 — STOP 절차로 들어간다")
            stop_here = True

        if stop_here:
            stop_requested = True
            stop_requested_at = time.time()
            stop_execution_id = f"simstop_{uuid.uuid4().hex[:16]}"
            cancel = transport.cancel_all(5.0)
            time.sleep(0.5)
            observation = settled_observation(transport, timeout_sec=STOP_SETTLE_TIMEOUT_SEC)
            peak = max((abs(v) for v in observation.velocities.values()),
                       default=0.0)
            stop_confirmed = bool(cancel.accepted and observation.valid
                                  and peak < 0.01)
            # 체크포인트를 남길 때만: 추종기를 켠 채 자재 수렴을 확인 → halt.
            convergence = settle_held_object(
                follower=follower, fixture=fixture, model=model,
                confirmed=stop_confirmed,
                capture=args.cell_policy == POLICY_DEMO_HOLD,
                expected_pose_m=expected_held_pose(
                    joints_urdf=joints_urdf, mimics=mimics, tcp_link=tcp_link,
                    positions=observation.positions,
                    gripper_joint=bindings.gripper_joint,
                    gripper_default=bindings.gripper_grasp_rad, offset=offset))
            if follower is not None:
                follow_samples.extend(follower.samples)
                follower = None
            note_pose(f"stopped_at:{stage.stage}")
            if args.cell_policy == POLICY_DEMO_HOLD:
                # 사용자 시연만 남긴다. E2E(e2e_reset)는 기록하지 않는다.
                pending_checkpoint = capture_stop_checkpoint(
                    mode="forward", model=model, object_id=object_rid,
                    source_id=support_rid, destination_id=target_rid,
                    origin_slot_id=support_rid, stages=stages,
                    stopped_stage=stage.stage, stage_records=stage_records,
                    confirmed=stop_confirmed,
                    stop_execution_id=stop_execution_id,
                    stop_requested_at=stop_requested_at,
                    observation=observation,
                    gripper_joint=bindings.gripper_joint, fixture=fixture,
                    client=client, scene_hash_at_stop=current.content_hash,
                    conveyor_zone=zone, origin_home_m=declared_home,
                    convergence=convergence)
            latch.latch({
                "at": time.time(), "stage": stage.stage,
                "stop_execution_id": stop_execution_id,
                "scene_hash": current.content_hash,
                "held_by_fixture": list(fixture.held()),
                "object_pose_m": timeline[-1]["object_pose_m"],
                "arm_peak_rad_s": peak,
                "goals_canceling": cancel.goals_canceling,
                "detail": "정지 뒤 새 시뮬레이션은 래치 해제와 scene 재검증을"
                          " 요구한다",
            })
            EXTERNAL_STOP.consume()   # 처리한 외부 정지 요청만 지운다
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

    restored: bool | None = None
    if should_restore(policy=args.cell_policy,
                      completed=result.simulation_e2e,
                      stop_requested=stop_requested,
                      no_restore=args.no_restore):
        restored = fixture.restore(model).verified
        print(f"  [정리] {model}을 선언된 원래 자리로 되돌렸다 (확인={restored})")
    elif args.cell_policy == POLICY_DEMO_HOLD and result.simulation_e2e:
        print(f"  [시연 상태 유지] {model}을 컨베이어에 둔다 — 다음 시연 전"
              f" --restore-only로 되돌린다")
    demo_state.record_run(policy=args.cell_policy, model=model,
                          result=result.to_dict(), final_pose_m=final_pose,
                          restored=restored)
    record_stop_checkpoint(demo_state, pending_checkpoint)

    write(args.out, result)
    # 종료 순서: transport(spin 스레드) → gz 구독·scene 노드 → rclpy.
    shutdown_ros(transport=transport, fixture=fixture, node=node)

    print(f"\n판정: {result.status.value}"
          f" · simulation_e2e={result.simulation_e2e}"
          f" · real_hardware_ready={result.real_hardware_ready}"
          f" · grasp={result.grasp_observation_kind}")
    for item in result.criteria:
        print(f"  [{'OK ' if item.met else 'BAD'}] {item.label}: {item.detail}")
    print(f"기록: {args.out}")
    return 0 if result.simulation_e2e else 1


RETURN_OUT = ROOT / "reports/workcell/pick_place_sim_demo_return.json"
RESUME_PREFLIGHT_OUT = ROOT / "reports/workcell/sim_demo_resume_preflight.json"
#: 컨트롤러 액션의 **살아 있는** goal 상태(ACCEPTED·EXECUTING·CANCELING).
ACTIVE_GOAL_STATUSES = frozenset({1, 2, 3})
ACTION_STATUS_TOPICS = (
    "/arm_trajectory_controller/follow_joint_trajectory/_action/status",
    "/gripper_trajectory_controller/follow_joint_trajectory/_action/status",
)


def observe_attachment(world: str, partition: str, model: str,
                       *, window_sec: float = 2.0) -> dict:
    """고정 장치 attachment를 **Gazebo 관측으로** 확인한다.

    고정 장치는 붙일 때 물체를 정적 모델로 바꾼다. Gazebo는 정적 모델을
    `pose/info`에는 싣고 `dynamic_pose/info`에는 싣지 않는다(실측 2026-09-18:
    붙은 material_a는 dynamic_pose에 없고 B/C는 있다). 두 토픽에서 모두 관측이
    없으면 판정하지 않는다(None).
    """
    os.environ["GZ_PARTITION"] = partition
    from gz.msgs.pose_v_pb2 import Pose_V
    from gz.transport import Node as GzNode

    counts = {"pose": 0, "dynamic_pose": 0, "dynamic_messages": 0}
    gz = GzNode()

    def on(key):
        def callback(message) -> None:
            if key == "dynamic_pose":
                counts["dynamic_messages"] += 1
            counts[key] += sum(1 for pose in message.pose if pose.name == model)
        return callback

    gz.subscribe(Pose_V, f"/world/{world}/pose/info", on("pose"))
    gz.subscribe(Pose_V, f"/world/{world}/dynamic_pose/info", on("dynamic_pose"))
    time.sleep(window_sec)
    for topic in (f"/world/{world}/pose/info", f"/world/{world}/dynamic_pose/info"):
        gz.unsubscribe(topic)
    static = (None if not counts["pose"] or not counts["dynamic_messages"]
              else counts["dynamic_pose"] == 0)
    return {**counts, "static": static, "window_sec": window_sec}


def observe_active_goals(node, *, timeout_sec: float = 5.0) -> dict:
    """컨트롤러 액션 status 토픽에서 **살아 있는** goal 수를 센다.

    이 프로세스가 보낸 goal만 세는 `transport.live_goals()`로는 다른 프로세스의
    goal을 알 수 없다. status를 받지 못한 토픽이 있으면 None이다.
    """
    import rclpy
    from action_msgs.msg import GoalStatusArray
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                     reliability=ReliabilityPolicy.RELIABLE)
    seen: dict[str, list[int]] = {}
    subs = [node.create_subscription(
        GoalStatusArray, topic,
        lambda msg, t=topic: seen.__setitem__(t, [s.status for s in msg.status_list]),
        qos) for topic in ACTION_STATUS_TOPICS]
    deadline = time.monotonic() + timeout_sec
    while len(seen) < len(ACTION_STATUS_TOPICS) and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    for sub in subs:
        node.destroy_subscription(sub)
    active = (None if len(seen) < len(ACTION_STATUS_TOPICS)
              else sum(1 for statuses in seen.values()
                       for status in statuses if status in ACTIVE_GOAL_STATUSES))
    from collections import Counter

    return {"active": active,
            "status_counts": {topic: dict(Counter(statuses))
                              for topic, statuses in seen.items()}}


RESUME_OUT = ROOT / "reports/workcell/sim_demo_resume.json"
#: resume 완료 판정 이름. 일반 실행 성공 어휘와 섞지 않는다.
RESUMED_COMPLETED = "simulation_transfer_resumed_completed"


class ResumeContext:
    """resume 사전검증·실행에 필요한 것. **주입 가능**해야 ROS 없이 검증된다.

    필드: model · checkpoint · bindings · stages · client · transport · fixture ·
    latch(StopLatchFile) · demo_state · observe_attachment() · observe_goals() ·
    tool_object_gap(observation, pose) · conveyor_zone · origin_home_m ·
    make_follower()(없으면 None) · stage_findings. `commands`는 실제로 보낸 로봇
    명령 기록이다.
    """

    REQUIRED = ("model", "checkpoint", "bindings", "stages", "client", "transport",
                "fixture", "latch", "demo_state", "observe_attachment",
                "observe_goals", "tool_object_gap", "conveyor_zone",
                "origin_home_m")

    def __init__(self, **fields: Any):
        missing = [name for name in self.REQUIRED if name not in fields]
        if missing:
            raise TypeError(f"ResumeContext에 없는 필드: {missing}")
        self.make_follower: Callable[[], Any] | None = None
        #: 관절 위치 → 든 자재 기대 pose(FK + 파지 offset). 없으면 STOP 뒤
        #: 수렴을 확인할 수 없어 새 체크포인트를 만들지 않는다.
        self.expected_object_pose: Callable[[Any], Any] | None = None
        self.stage_findings: tuple = ()
        #: 외부 정지 요청(웹 STOP)을 보는 함수. 기본은 이 실행의 감시기다.
        self.external_stop: Callable[[], bool] = EXTERNAL_STOP.requested
        self.__dict__.update(fields)
        self.commands: list[str] = []


#: 재검증 증거에서 **값이 반드시 있어야 하는** 항목. 하나라도 None이면 BLOCK이다.
REVALIDATION_REQUIRED = (
    "checkpoint_id", "stop_execution_id", "scene.observed", "scene.checkpoint",
    "scene.match", "material_pose.observed_m", "material_pose.error_m",
    "tool_material_error_m", "attachment.static", "attachment.held_in_checkpoint",
    "active_goals.controller", "active_goals.own", "active_goals.total",
    "joints.observed_at", "joints.fresh", "joints.max_error_rad",
    "joints.gripper_rad", "joints.gripper_error_rad",
    "recovery_approach.samples", "recovery_approach.geometry_passed",
    "recovery_approach.remaining_passed", "recovery_approach.scene_stable",
    "revalidated_at")


def _lookup(tree: dict, dotted: str):
    value = tree
    for key in dotted.split("."):
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def revalidation_missing(evidence: dict) -> list[str]:
    """비었거나 확인 불가인 필수 증거 항목."""
    return [key for key in REVALIDATION_REQUIRED if _lookup(evidence, key) is None]


def revalidation_evidence(*, checkpoint: dict, started_at: float, scene_now,
                          pose, attachment: dict, observation, goals: dict,
                          own_goals, gap, gripper_joint: str,
                          geometry: dict | None, samples: int | None) -> dict:
    """실행 직전 재검증의 관측 증거. 계산할 수 없는 값은 None으로 둔다."""
    saved = (checkpoint.get("joint_state") or {}).get("positions") or {}
    positions = dict(getattr(observation, "positions", {}) or {})
    valid = bool(getattr(observation, "valid", False))
    observed_at = getattr(observation, "observed_at", None) if valid else None
    arm_errors = [abs(float(positions[n]) - float(v)) for n, v in saved.items()
                  if n != gripper_joint and n in positions]
    arm_complete = valid and len(arm_errors) == len(
        [n for n in saved if n != gripper_joint])
    gripper = positions.get(gripper_joint) if valid else None
    saved_gripper = (checkpoint.get("gripper") or {}).get("position_rad")
    saved_pose = checkpoint.get("object_pose_m")
    controller = goals.get("active")
    return {
        "checkpoint_id": checkpoint.get("checkpoint_id"),
        "stop_execution_id": checkpoint.get("stop_execution_id"),
        "scene": {"observed": scene_now or None,
                  "checkpoint": checkpoint.get("scene_hash"),
                  "match": (None if not scene_now or not checkpoint.get("scene_hash")
                            else scene_now == checkpoint.get("scene_hash"))},
        "material_pose": {
            "observed_m": None if pose is None else [round(float(v), 6) for v in pose],
            "checkpoint_m": saved_pose,
            "error_m": (None if pose is None or not saved_pose else round(
                float(np.linalg.norm(np.asarray(pose) - np.asarray(saved_pose))), 6))},
        "tool_material_error_m": None if gap is None else round(float(gap), 6),
        "attachment": {"static": attachment.get("static"),
                       "pose_observations": attachment.get("pose"),
                       "dynamic_pose_observations": attachment.get("dynamic_pose"),
                       "held_in_checkpoint": (None if checkpoint.get("object_state")
                                              is None else checkpoint.get(
                                                  "object_state") == OBJECT_HELD)},
        "active_goals": {"controller": controller, "own": own_goals,
                         "total": (None if controller is None or own_goals is None
                                   else int(controller) + int(own_goals))},
        "joints": {
            "observed_at": observed_at,
            "fresh": None if observed_at is None else float(observed_at) >= started_at,
            "max_error_rad": (round(max(arm_errors), 6)
                              if arm_complete and arm_errors else None),
            "gripper_rad": None if gripper is None else round(float(gripper), 6),
            "gripper_error_rad": (None if gripper is None or saved_gripper is None
                                  else round(abs(float(gripper)
                                                 - float(saved_gripper)), 6))},
        "recovery_approach": {
            "samples": samples,
            "geometry_passed": (None if geometry is None else all(
                c["checked"] and c["collision_free"] and c["joint_limits_ok"]
                for c in geometry["recovery_approach"])),
            "remaining_passed": (None if geometry is None else all(
                c["checked"] and c["collision_free"] and c["joint_limits_ok"]
                for c in geometry["remaining"])),
            "scene_stable": None if geometry is None else geometry["scene_stable"]},
        "revalidation_started_at": started_at,
        "revalidated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def verify_resume(ctx: ResumeContext, *, started_at: float) -> dict:
    """사전검증 조건 1~7을 **지금** 관측으로 다시 본다. 명령을 보내지 않는다."""
    cp = ctx.checkpoint
    object_rid = str(cp["object_id"])
    scene_now = ctx.client.snapshot().content_hash
    pose = ctx.fixture.pose_of(ctx.model, timeout_sec=3.0, fresh=True)
    attachment = ctx.observe_attachment()
    observation = ctx.transport.joint_observation(3.0, after=started_at)
    goals = ctx.observe_goals()
    latch = ctx.latch.latched()
    gap = ctx.tool_object_gap(observation, pose)
    own = ctx.transport.live_goals()
    active = None if goals.get("active") is None else int(goals["active"]) + int(own)
    reasons = resume_preflight_findings(
        checkpoint=cp, model=ctx.model, scene_hash_now=scene_now,
        object_pose_m=pose, attachment_static=attachment.get("static"),
        joint_observation={"positions": dict(observation.positions),
                           "observed_at": observation.observed_at,
                           "valid": observation.valid},
        preflight_started_at=started_at,
        gripper_joint=ctx.bindings.gripper_joint, latch=latch,
        live_goals=active, tool_object_gap_m=gap,
        follow_tolerance_m=FOLLOW_TOLERANCE_M)
    reasons.extend(str(getattr(f, "detail", f)) for f in ctx.stage_findings)
    result = {
        "reasons": reasons, "scene_hash": scene_now, "object_pose_m": pose,
        "observation": observation, "latch": latch, "approach": (), "after": (),
        "geometry": None,
        "observations": {
            "scene_hash": scene_now,
            "object_pose_m": None if pose is None else [round(v, 6) for v in pose],
            "attachment": attachment, "joint_observed_at": observation.observed_at,
            "joint_valid": observation.valid, "active_goals": goals,
            "own_live_goals": own, "latch_present": latch is not None,
            "tool_object_gap_m": gap},
    }

    def with_evidence(geometry=None, samples=None) -> dict:
        # 증거는 성공·차단 모두에 싣는다. 비었으면 그 자체가 BLOCK 사유다.
        result["evidence"] = revalidation_evidence(
            checkpoint=cp, started_at=started_at, scene_now=scene_now, pose=pose,
            attachment=attachment, observation=observation, goals=goals,
            own_goals=own, gap=gap, gripper_joint=ctx.bindings.gripper_joint,
            geometry=geometry, samples=samples)
        missing = revalidation_missing(result["evidence"])
        result["evidence"]["missing"] = missing
        if missing and not reasons:
            reasons.append(f"재검증 증거가 비었다(확인 불가): {', '.join(missing)}")
        return result

    if reasons:
        return with_evidence()
    approach, after, build_reasons = build_resume_stages(
        ctx.stages, cp, observation.positions)
    reasons.extend(build_reasons)
    if build_reasons:
        return with_evidence()
    before = ctx.client.snapshot().content_hash
    approach_checks, approach_bad = check_stages(approach, bindings=ctx.bindings,
                                                 client=ctx.client)
    after_checks, after_bad = check_stages(after, bindings=ctx.bindings,
                                           client=ctx.client)
    after_hash = ctx.client.snapshot().content_hash
    for finding in list(approach_bad) + list(after_bad):
        reasons.append(f"[{finding.reason_code}] {finding.detail}")
    stable = before == after_hash == scene_now
    if not stable:
        reasons.append("geometry 검사 중 scene이 바뀌었다")
    result.update(approach=approach, after=after, geometry={
        "recovery_approach": [c.to_dict() for c in approach_checks],
        "remaining": [c.to_dict() for c in after_checks], "scene_stable": stable})
    result["plan"] = {
        "stages": [{"stage": "recovery_approach", "kind": "arm_motion",
                    "from": "current_observed_joints",
                    "to_pose": approach[-1].pose_name, "holds_object": object_rid,
                    "path_samples": len(approach),
                    "path_step_rad": RESUME_PATH_STEP_RAD}]
                  + [{"stage": s.stage, "kind": s.kind, "pose_name": s.pose_name,
                      "holds_object": s.holds_object} for s in after],
        "replays_interrupted_trajectory": False}
    return with_evidence(result["geometry"], len(approach))


def _send(ctx: ResumeContext, kind: str, *args, **kwargs):
    """로봇 명령은 여기로만 나간다. 몇 건 나갔는지 기록한다."""
    ctx.commands.append(kind)
    return getattr(ctx.transport, kind)(*args, **kwargs)


def run_resume(ctx: ResumeContext, *, checkpoint_id: str,
               stop_at: str | None = None) -> dict:
    """체크포인트에서 **새 재계획으로** 이어서 실행한다. 중단 궤적은 재생하지 않는다.

    1. 실행 직전 재검증(저장된 사전검증은 믿지 않는다). 실패 → 명령 0건 BLOCK,
       체크포인트·래치 유지, 기록하지 않는다.
    2. 통과 뒤에만 래치 해제 → 고정 장치 재결속 → 복구 접근 → 남은 단계.
    3. 결과: 완료·확인된 STOP(새 체크포인트)·실패로 상태를 바꾼다.
    """
    cp = ctx.checkpoint
    model = ctx.model
    report = {"schema": "forstick2.simulation_demo_resume/1", "model": model,
              "checkpoint_id": checkpoint_id, "is_simulated": True,
              "real_hardware_ready": False, "real_hardware_verified": False,
              "replays_interrupted_trajectory": False}

    def blocked(reasons, verification=None) -> dict:
        return {**report, "status": "resume_blocked", "reasons": list(reasons),
                "robot_commands_sent": len(ctx.commands),
                "checkpoint_kept": True, "latch_kept": True,
                "revalidation": None if verification is None
                else verification.get("evidence"),
                "observations": None if verification is None
                else verification["observations"]}

    if not cp or cp.get("checkpoint_id") != checkpoint_id:
        return blocked([f"현재 체크포인트가 아니다: {checkpoint_id}"])
    started_at = time.time()
    verification = verify_resume(ctx, started_at=started_at)
    if verification["reasons"]:
        return blocked(verification["reasons"], verification)
    latch_record = verification["latch"] or {}
    released, latch_detail = ctx.latch.release(
        live_goals=0, scene_hash=verification["scene_hash"],
        latched_hash=latch_record.get("scene_hash"))
    if not released:
        return blocked([f"STOP 래치를 해제하지 못했다: {latch_detail}"], verification)
    report["latch_release"] = latch_detail
    report["plan"] = verification["plan"]
    # 실행 직전 재검증 증거. 성공·실패·STOP 보고서 모두에 남는다.
    report["revalidation"] = verification["evidence"]
    scene_hash = verification["scene_hash"]
    object_rid = str(cp["object_id"])
    bindings = ctx.bindings

    # 이 프로세스의 고정 장치에 다시 묶는다(물체는 이미 정적 고정 상태다).
    faults: list[tuple] = []
    try:
        event = ctx.fixture.attach(model, pose_m=verification["object_pose_m"])
        rebound = bool(event.verified)
    except FixtureError as exc:
        rebound = False
        faults.append((exc.reason, str(exc)))
    if not rebound and not faults:
        faults.append((ReasonCode.EXEC_SIM_FIXTURE_FAILED, "고정 장치 재결속 확인 실패"))

    approach = verification["approach"]
    recovery = dataclasses.replace(
        approach[-1], label="복구 접근(현재 → 정지 단계 목표)",
        detail=f"관측 관절에서 새 경로, 표본 {len(approach)}개로 검증")
    exec_stages = [recovery] + list(verification["after"])
    stage_records: list[dict] = []
    follow_samples: list[dict] = []
    detached = False
    stop_requested = False
    stop_info: dict = {}
    for stage in ([] if faults else exec_stages):
        if ctx.client.snapshot().content_hash != scene_hash:
            faults.append((ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
                           f"{stage.label} 앞에서 scene이 바뀌었다"))
            break
        if stage is not recovery:
            checks, bad = check_stages([stage], bindings=bindings, client=ctx.client)
            if not checks[0].passed:
                faults.append((bad[0].reason_code if bad
                               else ReasonCode.GEOMETRY_COLLISION,
                               f"{stage.label} 검사 미통과"))
                break
        joints = stage_joint_state(stage, bindings)
        arm_target = {n: v for n, v in joints.items() if n.startswith("j")}
        follower = None
        if (stage.stage in HOLDING_STAGES and stage.kind == "arm_motion"
                and ctx.make_follower is not None):
            follower = ctx.make_follower()
            follower.start()
        external_stop = ctx.external_stop()
        if stop_at != stage.stage and not external_stop:
            if stage.kind == "gripper":
                outcome = _send(ctx, "send_gripper", float(stage.gripper_joint_rad),
                                GRIPPER_SECONDS, ACTION_TIMEOUT,
                                should_stop=ctx.external_stop)
            else:
                outcome = _send(ctx, "send_arm", arm_target, ARM_SECONDS,
                                ACTION_TIMEOUT, should_stop=ctx.external_stop)
            external_stop = ctx.external_stop()
        if stop_at == stage.stage or external_stop:
            if not external_stop:
                _send(ctx, "send_arm_async", arm_target, ARM_SECONDS, ACTION_TIMEOUT)
                time.sleep(ARM_SECONDS / 3.0)
            stop_requested = True
            stop_at_time = time.time()
            stop_execution_id = f"simstop_{uuid.uuid4().hex[:16]}"
            cancel = _send(ctx, "cancel_all", 5.0)
            observation = settled_observation(ctx.transport, timeout_sec=STOP_SETTLE_TIMEOUT_SEC)
            peak = max((abs(v) for v in observation.velocities.values()), default=0.0)
            confirmed = bool(cancel.accepted and observation.valid and peak < 0.01)
            # 추종기를 켠 채 자재 수렴을 확인 → halt → (뒤에서) 캡처.
            convergence = settle_held_object(
                follower=follower, fixture=ctx.fixture, model=model,
                confirmed=confirmed, capture=True,
                expected_pose_m=(None if ctx.expected_object_pose is None
                                 else ctx.expected_object_pose(
                                     observation.positions)))
            if follower is not None:
                follow_samples.extend(follower.samples)
            ctx.latch.latch({"at": time.time(), "stage": stage.stage,
                             "stop_execution_id": stop_execution_id,
                             "scene_hash": scene_hash,
                             "held_by_fixture": list(ctx.fixture.held()),
                             "arm_peak_rad_s": peak,
                             "goals_canceling": cancel.goals_canceling,
                             "detail": "resume 중 정지 — 새 시뮬레이션은 래치 해제와"
                                       " scene 재검증을 요구한다"})
            EXTERNAL_STOP.consume()   # 처리한 외부 정지 요청만 지운다
            stage_records.append({"stage": stage.stage, "executed": True,
                                  "stopped": True, "stop_confirmed": confirmed})
            stop_info = {"confirmed": confirmed, "observation": observation,
                         "convergence": convergence,
                         "stop_execution_id": stop_execution_id,
                         "requested_at": stop_at_time, "stage": stage.stage}
            break
        if follower is not None:
            row = follower.sample()
            if row is not None:
                follow_samples.append(row)
            follower.halt()
            follow_samples.extend(follower.samples)
        observation = settled_observation(ctx.transport)
        if stage.kind == "gripper":
            error = abs(observation.positions.get(bindings.gripper_joint, float("nan"))
                        - float(stage.gripper_joint_rad))
        else:
            error = max((abs(observation.positions.get(n, float("nan")) - v)
                         for n, v in arm_target.items()), default=float("nan"))
        reached = bool(error == error and error <= JOINT_TOLERANCE_RAD)
        stage_records.append({"stage": stage.stage, "label": stage.label,
                              "executed": True, "goal_accepted": outcome.accepted,
                              "controller_error_code": outcome.error_code,
                              "observed_error_rad": None if error != error
                              else round(error, 6), "reached": reached})
        if not outcome.accepted or outcome.error_code not in (0, None):
            faults.append((ReasonCode.EXEC_GOAL_REJECTED,
                           f"{stage.label} 컨트롤러 결과 {outcome.error_code}"))
            break
        if not reached:
            faults.append((ReasonCode.EXEC_GOAL_NOT_REACHED,
                           f"{stage.label} 목표 미도달 (오차 {error:.5f} rad)"))
            break
        if stage.stage == STAGE_GRIPPER_OPEN_RELEASE:
            try:
                event = ctx.fixture.detach(model, pose_m=ctx.fixture.pose_of(
                    model, timeout_sec=2.0, fresh=True))
                detached = bool(event.verified)
            except FixtureError as exc:
                faults.append((exc.reason, str(exc)))
                break
            ctx.fixture.settle(model)

    final_pose = ctx.fixture.pose_of(model, timeout_sec=3.0, fresh=True)
    in_zone = (in_placement_zone(final_pose, ctx.conveyor_zone)
               if final_pose is not None else (False, "최종 pose를 관측하지 못했다"))
    home_row = next((r for r in stage_records if r["stage"] == STAGE_HOME_END), {})
    moved_ok = (ctx.make_follower is None and not follow_samples) or (
        bool(follow_samples) and all(r["with_tool"] for r in follow_samples))
    completed = bool(not faults and not stop_requested and rebound and detached
                     and moved_ok and in_zone[0] and home_row.get("reached")
                     and len(stage_records) == len(exec_stages))
    reasons = [f"[{code}] {text}" for code, text in faults]
    if not completed and not stop_requested and not reasons:
        reasons.append(f"완료 조건 미충족: 해제={detached} · 배치={in_zone[1]}"
                       f" · 도구 추종={moved_ok} · home={home_row.get('reached')}")
    new_checkpoint = None
    if completed:
        status, outcome_name = RESUMED_COMPLETED, "completed"
    elif stop_requested and stop_info.get("confirmed"):
        status, outcome_name = "resume_stopped", "stopped"
        previous = list(cp.get("completed_stages") or ())
        done = previous + [r["stage"] for r in stage_records if r.get("reached")]
        new_checkpoint = capture_stop_checkpoint(
            mode=str(cp.get("mode")), model=model, object_id=object_rid,
            source_id=str(cp["source_id"]), destination_id=str(cp["destination_id"]),
            origin_slot_id=str(cp.get("origin_slot_id")), stages=ctx.stages,
            stopped_stage=stop_info["stage"],
            stage_records=[{"stage": s, "reached": True} for s in done],
            confirmed=True, stop_execution_id=stop_info["stop_execution_id"],
            stop_requested_at=stop_info["requested_at"],
            observation=stop_info["observation"],
            gripper_joint=bindings.gripper_joint, fixture=ctx.fixture,
            client=ctx.client, scene_hash_at_stop=scene_hash,
            conveyor_zone=ctx.conveyor_zone, origin_home_m=ctx.origin_home_m,
            convergence=stop_info.get("convergence"))
        if new_checkpoint is None:
            reasons.append("확인된 STOP이지만 새 체크포인트를 만들 관측이 부족하다")
    else:
        status = "resume_stopped" if stop_requested else "resume_failed"
        outcome_name = "stopped" if stop_requested else "failed"
        if stop_requested:
            reasons.append("STOP이 확인되지 않았다(unconfirmed)")
    ctx.demo_state.record_resume(
        model, checkpoint_id=checkpoint_id, outcome=outcome_name,
        final_pose_m=final_pose, reasons=reasons, new_checkpoint=new_checkpoint)
    return {**report, "status": status, "reasons": reasons,
            "robot_commands_sent": len(ctx.commands),
            "commands": list(ctx.commands), "fixture_rebound": rebound,
            "detached": detached, "stage_records": stage_records,
            "follow_samples": follow_samples,
            "final_pose_m": None if final_pose is None else list(final_pose),
            "placement": list(in_zone),
            "new_checkpoint_id": None if new_checkpoint is None
            else new_checkpoint["checkpoint_id"],
            "checkpoint_kept": False,
            "simulation_demo": ctx.demo_state.status()}


def conveyor_zone_for(data: dict, resources, target_rid: str, objects: dict,
                      model: str) -> dict:
    """선언된 벨트·가드레일 치수에서 배치 허용 구역을 만든다."""
    conveyor = data["models"][resources.resources[target_rid]["gazebo_model"]]
    belt = next(part for part in conveyor["parts"] if part["name"] == "belt")
    rails = [part for part in conveyor["parts"] if part["name"].startswith("frame_")]
    center = _resolve(data["frames"], conveyor["frame"])
    rail_inner = min(abs(part["center_xyz_m"][1]) - part["size_m"][1] / 2
                     for part in rails) if rails else belt["size_m"][1] / 2
    return placement_zone(
        target_center_m=center,
        surface_half_extent_m=(belt["size_m"][0] / 2,
                               min(belt["size_m"][1] / 2, rail_inner)),
        object_size_m=objects[model].size_m, surface_top_z_m=center[2],
        z_tolerance_m=PLACEMENT_Z_TOLERANCE_M)


def build_resume_context(data: dict, model: str, checkpoint: dict):
    """실제 ROS·Gazebo에 붙은 ResumeContext와 정리할 scene 노드."""
    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=0.5,
        mounting_path=MOUNTING, grasp_path=GRASP)
    bindings = build_bindings(resources)
    grasp_file = json.loads(GRASP.read_text(encoding="utf-8"))
    object_rid = str(checkpoint["object_id"])
    steps = (
        TaskStep(skill="pick", args={"object": object_rid,
                                     "from": checkpoint["source_id"]}),
        TaskStep(skill="move", args={"to": checkpoint["destination_id"]}),
        TaskStep(skill="place", args={"object": object_rid,
                                      "to": checkpoint["destination_id"]}),
        TaskStep(skill="home"),
    )
    stages, stage_findings = ((), ["forward 체크포인트만 재계획한다"]) if (
        checkpoint.get("mode") != "forward") else build_stages(steps, bindings=bindings)
    profile_limits = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        limit = joint.find("limit")
        if limit is not None and joint.get("type") == "revolute":
            profile_limits[joint.get("name")] = (float(limit.get("lower")),
                                                 float(limit.get("upper")))
    client, node, transport = scene_and_transport(resources, profile_limits)
    objects = declarations_from_config(data["models"], data["frames"],
                                       source=str(WORKCELL))
    fixture = GazeboObjectFixture(
        world_name=resources.world_name, gz_partition=resources.gz_partition,
        objects=objects)
    joints_urdf, _links = parse_urdf(URDF)
    mimics = load_mimics()
    tcp_link = str(grasp_file["declared"]["tcp_link"])
    offset = (bindings.attached.get(object_rid) or {}).get("offset_m")

    def tool_object_gap(observation, pose):
        arm = {n: v for n, v in observation.positions.items() if n.startswith("j")}
        if not observation.valid or len(arm) < 6 or pose is None or offset is None:
            return None
        rotation, position = tcp_frame_of(
            joints_urdf, mimics, tcp_link, arm,
            observation.positions.get(bindings.gripper_joint,
                                      bindings.gripper_grasp_rad))
        expected = position + rotation @ np.asarray(offset)
        return float(np.linalg.norm(np.asarray(pose) - expected))

    ctx = ResumeContext(
        model=model, checkpoint=checkpoint, bindings=bindings, stages=tuple(stages),
        client=client, transport=transport, fixture=fixture,
        latch=StopLatchFile(LATCH), demo_state=SimulationDemoState(),
        observe_attachment=lambda: observe_attachment(
            resources.world_name, resources.gz_partition, model),
        observe_goals=lambda: observe_active_goals(node),
        tool_object_gap=tool_object_gap,
        conveyor_zone=conveyor_zone_for(data, resources,
                                        str(checkpoint["destination_id"]),
                                        objects, model),
        origin_home_m=tuple(objects[model].home_pose_m),
        expected_object_pose=lambda positions: expected_held_pose(
            joints_urdf=joints_urdf, mimics=mimics, tcp_link=tcp_link,
            positions=positions, gripper_joint=bindings.gripper_joint,
            gripper_default=bindings.gripper_grasp_rad, offset=offset),
        make_follower=lambda: Follower(
            fixture=fixture, model=model, transport=transport, joints=joints_urdf,
            mimics=mimics, tcp_link=tcp_link, offset=offset,
            gripper_value=bindings.gripper_grasp_rad),
        stage_findings=tuple(stage_findings))
    return ctx, node


def _write_report(out: Path, report: dict) -> None:
    report = {**report, **_raw_log_fields(),
              "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    report.pop("observation", None)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)
                   + "\n", encoding="utf-8")
    print(f"기록: {out}")


def resume_preflight(args, data: dict) -> int:
    """STOP 체크포인트의 resume 사전검증·재계획. **로봇 명령을 보내지 않는다.**"""
    out = Path(args.out) if args.out != str(OUT) else RESUME_PREFLIGHT_OUT
    demo_state = SimulationDemoState()
    model = _model_of(data, args.object) or args.object
    checkpoint = demo_state.checkpoints().get(model)
    report: dict = {"schema": "forstick2.simulation_demo_resume_preflight/1",
                    "model": model, "is_simulated": True,
                    "real_hardware_ready": False, "real_hardware_verified": False,
                    "resume_executed": False, "robot_commands_sent": 0,
                    "checkpoint_id": None if checkpoint is None
                    else checkpoint.get("checkpoint_id")}
    if checkpoint is None:
        print(f"사전검증 BLOCK: {model}의 체크포인트가 없다", file=sys.stderr)
        _write_report(out, {**report, "allowed": False,
                            "reasons": [f"{model}의 체크포인트가 없다"]})
        return 1
    ctx, node = build_resume_context(data, model, checkpoint)
    try:
        verification = verify_resume(ctx, started_at=time.time())
        reasons = verification["reasons"]
        allowed = not reasons
        record = {"allowed": allowed, "checkpoint_id": checkpoint["checkpoint_id"],
                  "validated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                  "scene_hash": verification["scene_hash"], "reasons": reasons}
        if allowed:
            record["resume_plan_id"] = f"simresume_{uuid.uuid4().hex[:16]}"
            record["plan"] = verification["plan"]
        demo_state.record_resume_preflight(model, record)
    finally:
        # 관측만 했다. 래치·체크포인트·고정 장치는 그대로 둔다.
        shutdown_ros(transport=ctx.transport, fixture=ctx.fixture, node=node)
    for text in reasons:
        print(f"사전검증 BLOCK: {text}", file=sys.stderr)
    print(f"사전검증: {'통과' if allowed else 'BLOCK'}"
          f" · 체크포인트 {checkpoint['checkpoint_id']}"
          + (f" · 재계획 {record['resume_plan_id']}" if allowed else ""))
    _write_report(out, {**report, "allowed": allowed, "reasons": reasons,
                        "resume_plan_id": record.get("resume_plan_id"),
                        "plan": verification.get("plan"),
                        "observations": verification["observations"],
                        "geometry": verification["geometry"],
                        "simulation_demo": demo_state.status()})
    return 0 if allowed else 1


def resume_checkpoint(args, data: dict) -> int:
    """`--resume-checkpoint <id>`: 실행 직전 재검증 뒤에만 이어서 실행한다."""
    out = Path(args.out) if args.out != str(OUT) else RESUME_OUT
    demo_state = SimulationDemoState()
    model = _model_of(data, args.object) or args.object
    checkpoint = demo_state.checkpoints().get(model)
    if checkpoint is None or checkpoint.get("checkpoint_id") != args.resume_checkpoint:
        reason = f"{model}의 현재 체크포인트가 {args.resume_checkpoint}가 아니다"
        print(f"resume BLOCK: {reason}", file=sys.stderr)
        _write_report(out, {"schema": "forstick2.simulation_demo_resume/1",
                            "model": model, "status": "resume_blocked",
                            "reasons": [reason], "robot_commands_sent": 0,
                            "is_simulated": True, "real_hardware_ready": False,
                            "real_hardware_verified": False})
        return 1
    ctx, node = build_resume_context(data, model, checkpoint)
    try:
        report = run_resume(ctx, checkpoint_id=args.resume_checkpoint,
                            stop_at=args.stop_at)
    finally:
        shutdown_ros(transport=ctx.transport, fixture=ctx.fixture, node=node)
    for text in report.get("reasons") or ():
        print(f"resume: {text}", file=sys.stderr)
    print(f"\n판정: {report['status']} · 명령 {report['robot_commands_sent']}건"
          " · real_hardware_ready=False")
    _write_report(out, report)
    return 0 if report["status"] == RESUMED_COMPLETED else 1


def clear_latch_after_restore(*, model: str, slot_home_m, latch: "StopLatchFile",
                              demo_state: SimulationDemoState, materials,
                              observe_pose: Callable[[str], Any],
                              observe_static: Callable[[str], bool | None],
                              observe_goals: Callable[[], dict],
                              scene_hash: Callable[[], str | None]) -> dict:
    """`--restore-only` 성공 **뒤에만** 부른다. 셀 전체가 정리됐을 때만 래치를 푼다.

    조건(하나라도 실패·관측 불가면 래치 유지):
    1) 대상 자재가 원래 슬롯 중심 `ORIGIN_TOLERANCE_M` 안(새 관측)
    2) 고정된(정적) 자재가 하나도 없다(Gazebo `dynamic_pose/info` 관측)
    3) 체크포인트가 하나도 없다
    4) reset_required=false — 다른 자재의 미해결 기록도 없다
    5) 컨트롤러 active goal 0
    6) 현재 scene hash를 읽었고, 래치가 기록한 정지 시점 scene과 같다
    지우기 직전에 래치를 다시 읽어 판정한 그 정지인지 확인한다(새 STOP 보호).
    일반 새 계획의 래치 해제·resume 규칙은 이 함수와 무관하다.
    """
    reasons: list[str] = []
    record = latch.latched()
    evidence: dict = {"model": model, "latch": None if record is None
                      else {k: record.get(k) for k in ("stop_execution_id", "stage",
                                                       "scene_hash")}}
    if record is None:
        return {"cleared": False, "reasons": ["래치가 없다"], "evidence": evidence}
    pose = observe_pose(model)
    gap = (None if pose is None else float(np.linalg.norm(
        np.asarray(pose, dtype=float) - np.asarray(slot_home_m, dtype=float))))
    evidence["slot_gap_m"] = None if gap is None else round(gap, 6)
    if gap is None:
        reasons.append("대상 자재 pose를 관측하지 못했다")
    elif gap > ORIGIN_TOLERANCE_M:
        reasons.append(f"대상 자재가 원래 슬롯에서 {gap:.4f} m 떨어져 있다")
    statics = {name: observe_static(name) for name in materials}
    evidence["static_materials"] = statics
    held = sorted(name for name, value in statics.items() if value is True)
    unknown = sorted(name for name, value in statics.items() if value is None)
    if held:
        reasons.append(f"고정된 자재가 남아 있다: {', '.join(held)}")
    if unknown:
        reasons.append(f"고정 여부를 관측하지 못했다: {', '.join(unknown)}")
    status = demo_state.status()
    evidence["checkpoints"] = status.get("checkpoints")
    evidence["reset_required"] = status.get("simulation_demo_reset_required")
    evidence["unresolved_objects"] = sorted(status.get("objects") or {})
    if status.get("available") is not True:
        reasons.append("시연 상태를 읽지 못했다")
    if status.get("checkpoint_available") is not False or status.get("checkpoints"):
        reasons.append("체크포인트가 남아 있다")
    if status.get("simulation_demo_reset_required") is not False:
        reasons.append("reset_required가 false가 아니다 — 미해결 자재: "
                       + (", ".join(evidence["unresolved_objects"]) or "확인 불가"))
    goals = observe_goals()
    evidence["active_goals"] = goals.get("active")
    if goals.get("active") is None:
        reasons.append("active goal 수를 관측하지 못했다")
    elif goals["active"]:
        reasons.append(f"active goal이 {goals['active']}개 남아 있다")
    current = scene_hash()
    evidence["scene_hash"] = current
    if not current:
        reasons.append("scene hash를 다시 읽지 못했다")
    elif record.get("scene_hash") and current != record["scene_hash"]:
        reasons.append("scene이 정지 시점과 다르다 — 일반 해제 경로의 재검증을 따른다")
    if reasons:
        return {"cleared": False, "reasons": reasons, "evidence": evidence}
    again = latch.latched()
    if again is None or again.get("stop_execution_id") != record.get("stop_execution_id"):
        return {"cleared": False, "evidence": evidence,
                "reasons": ["판정 중 래치가 바뀌었다(새 STOP 가능) — 유지"]}
    latch.path.unlink(missing_ok=True)
    return {"cleared": True, "reasons": [], "evidence": evidence}


def clear_latch_after_restore_live(*, data: dict, model: str, fixture, objects,
                                   demo_state: SimulationDemoState) -> dict:
    """실제 Gazebo·MoveIt 관측으로 `clear_latch_after_restore`를 부른다(명령 없음)."""
    import rclpy
    from rclpy.node import Node

    from robots.moveit.ros_client import RosPlanningSceneClient

    if not rclpy.ok():
        rclpy.init()
    node = Node("forstick2_restore_latch_check")
    try:
        client = RosPlanningSceneClient(
            node, group_name="fr3wms_arm", frame_id="base_link", joint_limits={},
            model_hash=f"{data['workcell_id']}:{data['workcell_version']}",
            ttl_sec=5.0, source="scripts/demo_workcell_pick_place.py --restore-only")
        scene_ready = client.wait(timeout_sec=30.0)

        def scene_hash():
            return client.snapshot().content_hash if scene_ready else None

        def static_of(name):
            return observe_attachment(data["world_name"], data["gz_partition"],
                                      name)["static"]

        return clear_latch_after_restore(
            model=model, slot_home_m=objects[model].home_pose_m,
            latch=StopLatchFile(LATCH), demo_state=demo_state,
            materials=sorted(objects),
            observe_pose=lambda name: fixture.pose_of(name, timeout_sec=3.0,
                                                      fresh=True),
            observe_static=static_of,
            observe_goals=lambda: observe_active_goals(node),
            scene_hash=scene_hash)
    finally:
        shutdown_ros(transport=None, fixture=None, node=node)


def _model_of(data: dict, token: str) -> str | None:
    """자원 id 또는 Gazebo 모델 이름 → 모델 이름. 선언에 없으면 None."""
    if token in data["models"]:
        return token
    for row in data["resource_map"]:
        if row["resource_id"] == token:
            return row["gazebo_model"]
    return None


def return_held_to_origin(args, data: dict) -> int:
    """컨베이어에 유지 중인 자재를 원래 슬롯으로 되돌린다(시뮬레이터 전용).

    **사전 조건을 모두 통과해야 움직인다.** 관측으로 슬롯 복귀를 확인했을 때만
    시연 기록을 지운다. 실패·STOP·관측 불가면 기록을 남긴다.
    """
    out = Path(args.out) if args.out != str(OUT) else RETURN_OUT
    demo_state = SimulationDemoState()
    model = _model_of(data, args.object) or args.object
    report: dict = {
        "schema": "forstick2.simulation_demo_return/1",
        "mode": "return_held_to_origin", "model": model,
        "is_simulated": True, "real_hardware_ready": False,
        "real_hardware_verified": False,
    }

    def finish(code: int, **fields) -> int:
        report.update(fields)
        report.update(_raw_log_fields())
        report["written_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        print(f"기록: {out}")
        return code

    def blocked(findings) -> int:
        for code, text in findings:
            print(f"복귀하지 않았다: [{code}] {text}", file=sys.stderr)
        closer = globals().get("_active_fixture")
        if closer is not None:
            closer.close()
        # 움직이지 않았다 — 기록은 **그대로** 둔다.
        return finish(1, status="return_not_started",
                      reason_codes=[str(c) for c, _ in findings],
                      findings=[{"reason_code": str(c), "detail": t}
                                for c, t in findings],
                      simulation_demo=demo_state.status())

    # ── 1. 기록 ────────────────────────────────────────────────────────
    record = demo_state.held_record(model)
    first = return_preflight(record=record, model=model, observed_pose=None,
                             on_conveyor=None, occupant=None)
    if record is None:
        return blocked(first)

    # ── 2. 원래 슬롯(선언의 프레임 부모 관계에서만) ────────────────────
    try:
        slot = origin_slot(data, model)
    except (KeyError, ValueError) as exc:
        return blocked([(ReasonCode.PLAN_UNKNOWN_RESOURCE, str(exc))])
    if args.support not in ("-", slot.support_model, slot.support_id):
        return blocked([(ReasonCode.PLAN_RESOURCE_MISMATCH,
                         f"{model}의 원래 팔레트는 {slot.support_id}로 선언됐다"
                         f" (요청 {args.support})")])
    target_rid = str(record.get("target_id") or "loc_conveyor")
    report.update({"origin": {"support_model": slot.support_model,
                              "support_id": slot.support_id,
                              "frame": slot.frame,
                              "home_pose_m": list(slot.home_pose_m)},
                   "held_record": record})

    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=0.5,
        mounting_path=MOUNTING, grasp_path=GRASP)
    grasp_file = json.loads(GRASP.read_text(encoding="utf-8"))
    base = build_bindings(resources)
    bindings = return_bindings(base, object_id=slot.object_id,
                               target_id=target_rid, grasp_config=grasp_file,
                               origin_id=slot.support_id)
    origin_place = pallet_place_pose(grasp_file, object_id=slot.object_id,
                                     support_id=slot.support_id)
    conveyor_grasp = conveyor_grasp_pose(grasp_file, object_id=slot.object_id,
                                         target_id=target_rid)
    stages, stage_findings = build_return_stages(
        bindings, object_id=slot.object_id, origin_id=slot.support_id,
        target_id=target_rid, conveyor_grasp=conveyor_grasp,
        origin_place=origin_place)

    profile_limits = {}
    for joint in ET.parse(URDF).getroot().findall("joint"):
        limit = joint.find("limit")
        if limit is not None and joint.get("type") == "revolute":
            profile_limits[joint.get("name")] = (float(limit.get("lower")),
                                                 float(limit.get("upper")))
    client, node, transport = scene_and_transport(resources, profile_limits)
    objects = declarations_from_config(data["models"], data["frames"],
                                       source=str(WORKCELL))
    fixture = GazeboObjectFixture(
        world_name=resources.world_name, gz_partition=resources.gz_partition,
        objects=objects)
    globals()["_active_fixture"] = fixture

    latch = StopLatchFile(LATCH)
    snapshot = client.snapshot()
    if latch.latched() is not None:
        released, detail = latch.release(
            live_goals=transport.live_goals(), scene_hash=snapshot.content_hash,
            latched_hash=latch.latched().get("scene_hash"))
        print(f"[래치] {detail}")
        if not released:
            return blocked([(ReasonCode.EXEC_STOPPED,
                             f"정지 래치가 걸려 있다 — {detail}")])
        snapshot = client.snapshot()

    # ── 3. 현재 컨베이어 배치 · 원래 슬롯 비점유 ───────────────────────
    conveyor = data["models"][resources.resources[target_rid]["gazebo_model"]]
    belt = next(part for part in conveyor["parts"] if part["name"] == "belt")
    rails = [part for part in conveyor["parts"] if part["name"].startswith("frame_")]
    center = _resolve(data["frames"], conveyor["frame"])
    rail_inner = min(abs(part["center_xyz_m"][1]) - part["size_m"][1] / 2
                     for part in rails) if rails else belt["size_m"][1] / 2
    zone = placement_zone(
        target_center_m=center,
        surface_half_extent_m=(belt["size_m"][0] / 2,
                               min(belt["size_m"][1] / 2, rail_inner)),
        object_size_m=objects[model].size_m, surface_top_z_m=center[2],
        z_tolerance_m=PLACEMENT_Z_TOLERANCE_M)
    observed = fixture.pose_of(model, timeout_sec=5.0, fresh=True)
    on_conveyor = in_placement_zone(observed, zone) if observed else None
    others, unobserved = {}, []
    for other, item in objects.items():
        if other == model:
            continue
        pose = fixture.pose_of(other, timeout_sec=2.0, fresh=True)
        if pose is None:
            unobserved.append(other)
        others[other] = (pose, item.size_m)
    occupant = slot_occupant(slot, others)

    # ── 4. geometry/scene 재검증(물체를 붙인 상태 포함) ────────────────
    checks, check_rows = (), []
    scene_stable = None
    if stages:
        before = client.snapshot()
        checks, check_findings = check_stages(stages, bindings=bindings,
                                              client=client)
        after = client.snapshot()
        scene_stable = (after.content_hash == before.content_hash
                        == snapshot.content_hash)
        check_rows = [(f.reason_code, f.detail) for f in check_findings]
    findings = return_preflight(
        record=record, model=model, observed_pose=observed,
        on_conveyor=on_conveyor, occupant=occupant,
        unobserved_others=unobserved,
        grasp_object_center=conveyor_grasp_center(grasp_file, conveyor_grasp),
        stage_findings=stage_findings,
        check_findings=check_rows, scene_stable=scene_stable)
    report["preflight"] = {
        "observed_pose_m": None if observed is None else list(observed),
        "on_conveyor": None if on_conveyor is None else list(on_conveyor),
        "occupant": occupant, "unobserved": unobserved,
        "conveyor_grasp_pose": conveyor_grasp,
        "checks": [c.to_dict() for c in checks], "scene_stable": scene_stable,
    }
    if findings:
        return blocked(findings)

    # ── 5. 실행 ────────────────────────────────────────────────────────
    joints_urdf, _links = parse_urdf(URDF)
    mimics = load_mimics()
    tcp_link = str(grasp_file["declared"]["tcp_link"])
    offset = bindings.attached[slot.object_id]["offset_m"]
    faults: list[tuple] = []
    stage_records: list[dict] = []
    follow_samples: list[dict] = []
    follower = None
    stop_requested = False
    pending_checkpoint: dict | None = None
    attached = detached = False
    print(f"[복귀 시작] {model}: 컨베이어 → {slot.support_id}"
          f" · scene {snapshot.content_hash[:12]}")
    for stage in stages:
        current = client.snapshot()
        if current.content_hash != snapshot.content_hash:
            faults.append((ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
                           f"{stage.label} 앞에서 scene이 바뀌었다"))
            break
        stage_checks, stage_bad = check_stages([stage], bindings=bindings,
                                               client=client)
        if not stage_checks[0].passed:
            faults.append((stage_bad[0].reason_code if stage_bad
                           else ReasonCode.GEOMETRY_COLLISION,
                           f"{stage.label} 검사 미통과"))
            break
        joints = stage_joint_state(stage, bindings)
        arm_target = {n: v for n, v in joints.items() if n.startswith("j")}
        stop_here = args.stop_at == stage.stage
        external_stop = EXTERNAL_STOP.requested()
        if stage.stage in HOLDING_STAGES and stage.kind == "arm_motion":
            follower = Follower(
                fixture=fixture, model=model, transport=transport,
                joints=joints_urdf, mimics=mimics, tcp_link=tcp_link,
                offset=offset, gripper_value=bindings.gripper_grasp_rad)
            follower.start()
        if not stop_here and not external_stop:
            if stage.kind == "gripper":
                outcome = transport.send_gripper(
                    float(stage.gripper_joint_rad), GRIPPER_SECONDS,
                    ACTION_TIMEOUT, should_stop=EXTERNAL_STOP.requested)
            else:
                outcome = transport.send_arm(arm_target, ARM_SECONDS,
                                             ACTION_TIMEOUT,
                                             should_stop=EXTERNAL_STOP.requested)
            external_stop = EXTERNAL_STOP.requested()
        if external_stop:
            print(f"[외부 정지 요청] {stage.label} 중 — STOP 절차로 들어간다")
        if stop_here or external_stop:
            if not external_stop:
                transport.send_arm_async(arm_target, ARM_SECONDS, ACTION_TIMEOUT)
                time.sleep(ARM_SECONDS / 3.0)
            stop_requested = True
            stop_requested_at = time.time()
            stop_execution_id = f"simstop_{uuid.uuid4().hex[:16]}"
            cancel = transport.cancel_all(5.0)
            time.sleep(0.5)
            observation = settled_observation(transport, timeout_sec=STOP_SETTLE_TIMEOUT_SEC)
            peak = max((abs(v) for v in observation.velocities.values()),
                       default=0.0)
            confirmed = bool(cancel.accepted and observation.valid and peak < 0.01)
            # 추종기를 켠 채 자재 수렴을 확인 → halt → 캡처.
            convergence = settle_held_object(
                follower=follower, fixture=fixture, model=model,
                confirmed=confirmed, capture=True,
                expected_pose_m=expected_held_pose(
                    joints_urdf=joints_urdf, mimics=mimics, tcp_link=tcp_link,
                    positions=observation.positions,
                    gripper_joint=bindings.gripper_joint,
                    gripper_default=bindings.gripper_grasp_rad, offset=offset))
            if follower is not None:
                follow_samples.extend(follower.samples)
                follower = None
            pending_checkpoint = capture_stop_checkpoint(
                mode="return", model=model, object_id=slot.object_id,
                source_id=target_rid, destination_id=slot.support_id,
                origin_slot_id=slot.support_id, stages=stages,
                stopped_stage=stage.stage, stage_records=stage_records,
                confirmed=confirmed,
                stop_execution_id=stop_execution_id,
                stop_requested_at=stop_requested_at, observation=observation,
                gripper_joint=bindings.gripper_joint, fixture=fixture,
                client=client, scene_hash_at_stop=current.content_hash,
                conveyor_zone=zone, origin_home_m=slot.home_pose_m,
                convergence=convergence)
            latch.latch({"at": time.time(), "stage": stage.stage,
                         "stop_execution_id": stop_execution_id,
                         "scene_hash": current.content_hash,
                         "held_by_fixture": list(fixture.held()),
                         "arm_peak_rad_s": peak,
                         "goals_canceling": cancel.goals_canceling,
                         "detail": "복귀 중 정지 — 새 시뮬레이션은 래치 해제와"
                                   " scene 재검증을 요구한다"})
            EXTERNAL_STOP.consume()   # 처리한 외부 정지 요청만 지운다
            stage_records.append({"stage": stage.stage, "executed": True,
                                  "stopped": True,
                                  "stop_confirmed": bool(cancel.accepted
                                                         and peak < 0.01)})
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
            error = abs(observation.positions.get(bindings.gripper_joint,
                                                  float("nan"))
                        - float(stage.gripper_joint_rad))
        else:
            error = max((abs(observation.positions.get(n, float("nan")) - v)
                         for n, v in arm_target.items()), default=float("nan"))
        reached = bool(error == error and error <= JOINT_TOLERANCE_RAD)
        stage_records.append({"stage": stage.stage, "label": stage.label,
                              "executed": True, "goal_accepted": outcome.accepted,
                              "controller_error_code": outcome.error_code,
                              "observed_error_rad": None if error != error
                              else round(error, 6), "reached": reached})
        if not outcome.accepted or outcome.error_code not in (0, None):
            faults.append((ReasonCode.EXEC_GOAL_REJECTED,
                           f"{stage.label} 컨트롤러 결과 {outcome.error_code}"))
            break
        if not reached:
            faults.append((ReasonCode.EXEC_GOAL_NOT_REACHED,
                           f"{stage.label} 목표 미도달 (오차 {error:.5f} rad)"))
            break
        if stage.stage == STAGE_GRIPPER_CLOSE:
            try:
                event = fixture.attach(model, pose_m=fixture.pose_of(
                    model, timeout_sec=2.0, fresh=True))
                attached = bool(event.verified)
            except FixtureError as exc:
                faults.append((exc.reason, str(exc)))
                break
        elif stage.stage == STAGE_GRIPPER_OPEN_RELEASE:
            try:
                event = fixture.detach(model, pose_m=fixture.pose_of(
                    model, timeout_sec=2.0, fresh=True))
                detached = bool(event.verified)
            except FixtureError as exc:
                faults.append((exc.reason, str(exc)))
                break
            fixture.settle(model)
        print(f"  [{stage.no:2d}/{len(stages)}] {stage.label:16s}"
              f" 오차 {error:.5f} rad · 도달={reached}")
    if follower is not None:
        follower.halt()
        follow_samples.extend(follower.samples)

    # ── 6. 관측으로 판정하고 기록한다 ──────────────────────────────────
    final_pose = fixture.pose_of(model, timeout_sec=3.0, fresh=True)
    home_row = next((r for r in stage_records if r["stage"] == STAGE_HOME_END), {})
    moved_ok = bool(follow_samples) and all(r["with_tool"] for r in follow_samples)
    completed = bool(not faults and not stop_requested and attached and detached
                     and moved_ok and home_row.get("reached")
                     and len(stage_records) == len(stages))
    codes = [str(c) for c, _ in faults]
    if final_pose is None:
        codes.append(str(ReasonCode.EXEC_UNVERIFIABLE))
    attempt = demo_state.record_return(
        model, completed=completed, stop_requested=stop_requested,
        final_pose_m=final_pose, origin_home_m=slot.home_pose_m,
        reason_codes=codes,
        detail=" · ".join(text for _, text in faults))
    if not attempt["returned_to_origin"] and completed:
        codes.append(str(ReasonCode.EXEC_SIM_PLACEMENT_OUT_OF_ZONE))
    record_stop_checkpoint(demo_state, pending_checkpoint)
    # 종료 순서: transport(spin 스레드) → gz 구독·scene 노드 → rclpy.
    shutdown_ros(transport=transport, fixture=fixture, node=node)
    status = ("returned_to_origin" if attempt["returned_to_origin"]
              else "return_stopped" if stop_requested else "return_failed")
    print(f"\n판정: {status} · 간격 {attempt['gap_m']} m"
          f" (허용 {attempt['tolerance_m']} m) · real_hardware_ready=False")
    return finish(0 if attempt["returned_to_origin"] else 1,
                  status=status, reason_codes=codes, attempt=attempt,
                  stage_records=stage_records, follow_samples=follow_samples,
                  attached=attached, detached=detached,
                  simulation_demo=demo_state.status())


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
    payload.update(_raw_log_fields())
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
