#!/usr/bin/env python3
"""실기 전환 준비 차단 체계 확인 (8-12).

```
.venv/bin/python scripts/verify_hardware_readiness.py
```

**실제 로봇에 명령을 보내지 않는다.** 이 스크립트는 차단이 실제로 걸리는지만
확인한다.

| # | 항목 | 기대 |
|---|---|---|
| 01 | 필수 입력 전체 누락 | 차단 · `hardware.input_missing` |
| 02 | declared yaw만 존재 | 차단 · `hardware.declared_only` |
| 03 | simulated_observation만 존재 | 차단 · `hardware.simulated_value_rejected` |
| 04 | 질량만 있고 관성 누락 | 차단 · 관성 항목이 미충족으로 남는다 |
| 05 | 실기 어댑터 설정 누락 | 어댑터 생성·연결 차단 |
| 06 | 시뮬레이션 결과 승격 시도 | 예외 |
| 07 | 모든 근거를 채운 합성 입력 | 통과(계약이 열릴 수 있음을 확인) |
| 08 | 현재 저장소 상태 | 차단 유지 · 부족한 입력 목록 |
| 09 | 작업자(측정한 사람) 요구 | 없으면 verified·passed 불가 |
| 10 | 근거 파일 없는 기록 | 기록 도구가 거부 |
| 11 | 실제 장비를 만지는 단계 | 사람 수행으로 분리 |
| 12 | 측정·연결 절차 선언 | TCP·frame·연결·파지 관측 |
| 13 | 수집 시트 | 미충족 항목을 문장으로 출력 |
| 14 | 고정 상태 다섯 줄 | 선언된 문장과 정확히 일치 |
| 15 | 근거 0/18·0/11 | `verified`가 반드시 false |
| 16 | Gazebo 회귀 | home/move/STOP 7/7 · 시뮬레이션 E2E 기록 유지 |

01~07은 **합성 값**으로 계약을 확인한다(실제 수치를 만들지 않는다).
09는 이미 기록된 검증 결과를 다시 읽어 회귀만 본다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.grasp_observation import (  # noqa: E402
    GraspObservationError,
    measured_from_device,
    simulated,
    unavailable,
)
from core.hardware_input import (  # noqa: E402
    HardwareInput,
    HardwareInputError,
    InputSet,
    MeasurementMethod,
)
from core.reason_codes import ReasonCode  # noqa: E402
from robots.hardware import (  # noqa: E402
    FR3HardwareAdapter,
    HardwareConfigError,
    HardwareConnection,
    Robotiq2F85HardwareObservationAdapter,
    load_hardware_connection,
)
from robots.hardware.config import HardwareLimits  # noqa: E402
from validation import hardware_readiness  # noqa: E402
from validation.hardware_readiness import (  # noqa: E402
    Checklist,
    ChecklistStep,
    HardwareReadinessError,
    StepActor,
    StepStatus,
)

OUT = ROOT / "reports/hardware/readiness.json"
MANIFEST = ROOT / "config/hardware/active.json"

#: 합성 검증에 쓰는 최소 입력 키. 실제 필수 목록은 설정이 정한다.
SYNTH_KEYS = ("mounting_yaw", "coupling_mass", "coupling_inertia")


def synth_input(key: str, *, ready: bool = False,
                method: MeasurementMethod = MeasurementMethod.NONE,
                value=None) -> HardwareInput:
    """합성 입력. 실제 수치를 만들지 않는다 — 계약 확인용 값이다."""
    if ready:
        return HardwareInput(
            key=key, label=f"합성 {key}", value=value if value is not None else 1.0,
            unit="kg", source="합성 근거 문서", measurement_method=method,
            captured_at="2026-09-17", verified=True,
            evidence_path="reports/hardware/evidence/synthetic.txt",
            operator="합성 작업자", needed="합성 검증용")
    return HardwareInput(key=key, label=f"합성 {key}", value=value, unit="kg",
                         measurement_method=method, needed="합성 검증용")


def synth_set(inputs) -> InputSet:
    return InputSet(profile_id="synth", profile_version="test-1.0",
                    inputs=tuple(inputs), source="synthetic")


def synth_checklist(status: StepStatus = StepStatus.NOT_STARTED,
                    *, with_evidence: bool = False) -> Checklist:
    return Checklist(
        checklist_id="synth", checklist_version="test-1.0",
        steps=(ChecklistStep(
            no=1, key="synth_step", label="합성 단계", status=status,
            evidence_path=("reports/hardware/evidence/synthetic.txt"
                           if with_evidence else ""),
            captured_at="2026-09-17" if with_evidence else "",
            operator="합성 작업자" if with_evidence else "",
            needed="합성 검증용"),),
        source="synthetic")


def synth_connection() -> HardwareConnection:
    """합성 접속 설정. **실제 주소가 아니다.**"""
    return HardwareConnection(
        arm_kind="synthetic-arm", arm_endpoint="synthetic://arm",
        gripper_kind="synthetic-gripper", gripper_endpoint="synthetic://gripper",
        limits=HardwareLimits(
            source="합성 한계 문서",
            joint_limits_rad={"j1": (-1.0, 1.0)},
            joint_velocity_rad_s={"j1": 0.5}))


def synth_profile():
    """합성 Capability Profile. **실제 로봇 값이 아니다** — 계약 확인용이다."""
    from core.capability_profile import CapabilityProfile, JointLimit
    from core.frames import ANGLE_UNIT, FrameKind, Vector3

    return CapabilityProfile(
        profile_id="synth_hw", profile_version="test-1.0", dof=1,
        joint_limits=(JointLimit("j1", -1.0, 1.0, 0.5, ANGLE_UNIT, "revolute"),),
        frames={FrameKind.BASE: "base", FrameKind.TOOL: "tool"},
        tcp_offset=Vector3(0.0, 0.0, 0.1), work_radius_m=1.0,
        payload_kg=1.0, supported_skills=("home", "move", "stop"))


def main() -> int:
    checks: list[dict] = []

    def record(key: str, ok: bool, detail: str, evidence: dict | None = None):
        checks.append({"key": key, "passed": bool(ok), "detail": detail,
                       "evidence": evidence or {}})
        print(f"[{'OK ' if ok else 'BAD'}] {key}: {detail}")

    # ── 01 필수 입력 전체 누락 ────────────────────────────────────────
    result = hardware_readiness.evaluate(
        inputs=synth_set(synth_input(key) for key in SYNTH_KEYS),
        checklist=synth_checklist())
    codes = [code.value for code in result.reason_codes]
    record("01_all_inputs_missing",
           not result.real_hardware_ready
           and "hardware.input_missing" in codes,
           f"real_hardware_ready={result.real_hardware_ready} · {codes}",
           {"findings": [item.to_dict() for item in result.findings]})

    # ── 02 declared yaw만 존재 ────────────────────────────────────────
    declared_yaw = HardwareInput(
        key="mounting_yaw", label="합성 장착 yaw", value=0.0, unit="rad",
        source="공식 문서", measurement_method=MeasurementMethod.DECLARED,
        captured_at="2026-09-17", verified=False,
        evidence_path="reports/hardware/evidence/synthetic.txt",
        operator="합성 작업자", needed="측정 또는 사진 근거가 필요하다")
    result = hardware_readiness.evaluate(
        inputs=synth_set([declared_yaw]),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=True),
        adapter_config={"arm": "synthetic", "gripper": "synthetic"},
        grasp_observation=None)
    yaw_finding = next((f for f in result.findings if f.key == "mounting_yaw"),
                       None)
    record("02_declared_yaw_only",
           not result.real_hardware_ready and yaw_finding is not None
           and yaw_finding.reason_code is ReasonCode.HARDWARE_DECLARED_ONLY,
           f"yaw 판정 {None if yaw_finding is None else yaw_finding.reason_code.value}"
           f" · ready={result.real_hardware_ready}",
           {"finding": None if yaw_finding is None else yaw_finding.to_dict()})

    # declared 값을 verified로 두려는 시도 자체가 거부되는지도 본다.
    refused = False
    try:
        HardwareInput(key="x", label="x", value=0.0, unit="rad",
                      source="공식 문서",
                      measurement_method=MeasurementMethod.DECLARED,
                      captured_at="2026-09-17", verified=True,
                      evidence_path="p")
    except HardwareInputError:
        refused = True
    record("02b_declared_cannot_be_verified", refused,
           f"declared를 verified로 두려는 시도 거부={refused}")

    # ── 03 simulated_observation만 존재 ───────────────────────────────
    sim_observation = simulated(
        held=True, object_id="synth_obj", observed_at=time.time(),
        source="simulation", method="simulator_fixture_state").to_dict()
    result = hardware_readiness.evaluate(
        inputs=synth_set(synth_input(key, ready=True,
                                     method=MeasurementMethod.SCALE)
                         for key in SYNTH_KEYS),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=True),
        adapter_config={"arm": "synthetic", "gripper": "synthetic"},
        grasp_observation=sim_observation)
    grasp_finding = next((f for f in result.findings
                          if f.key == "grasp_observation"), None)
    sim_value_refused = False
    try:
        HardwareInput(key="y", label="y", value=1.0, unit="kg",
                      source="시뮬레이터 설정",
                      measurement_method=MeasurementMethod.SIMULATION,
                      captured_at="2026-09-17", verified=True,
                      evidence_path="p")
    except HardwareInputError:
        sim_value_refused = True
    record("03_simulated_observation_only",
           not result.real_hardware_ready and grasp_finding is not None
           and grasp_finding.reason_code
           is ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED
           and sim_value_refused,
           f"파지 관측 판정 {None if grasp_finding is None else grasp_finding.reason_code.value}"
           f" · 시뮬레이터 값 verified 거부={sim_value_refused}",
           {"finding": None if grasp_finding is None else grasp_finding.to_dict(),
            "observation": sim_observation})

    # ── 04 질량만 있고 관성 누락 ──────────────────────────────────────
    result = hardware_readiness.evaluate(
        inputs=synth_set([
            synth_input("coupling_mass", ready=True,
                        method=MeasurementMethod.SCALE),
            synth_input("coupling_inertia"),
        ]),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=True),
        adapter_config={"arm": "synthetic", "gripper": "synthetic"})
    pending = [item.key for item in result.findings]
    record("04_mass_without_inertia",
           not result.real_hardware_ready
           and "coupling_inertia" in pending
           and "coupling_mass" not in pending,
           f"미충족 {pending}",
           {"findings": [item.to_dict() for item in result.findings]})

    # ── 05 실기 어댑터 설정 누락 ──────────────────────────────────────
    env_refused = ""
    try:
        load_hardware_connection(env={})
    except HardwareConfigError as exc:
        env_refused = exc.reason.value
    ready_result = hardware_readiness.evaluate(
        inputs=synth_set(synth_input(key, ready=True,
                                     method=MeasurementMethod.SCALE)
                         for key in SYNTH_KEYS),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=True),
        adapter_config=None)
    adapter_finding = next((f for f in ready_result.findings
                            if f.key == "hardware_adapter"), None)
    create_refused = ""
    try:
        FR3HardwareAdapter("synth", synth_profile(),
                           connection=HardwareConnection(
                               arm_kind="a", arm_endpoint="b",
                               gripper_kind="c", gripper_endpoint="d"),
                           readiness=ready_result)
    except HardwareConfigError as exc:
        create_refused = exc.reason.value
    observer_refused = ""
    try:
        Robotiq2F85HardwareObservationAdapter(
            robot_id="r", gripper_id="g",
            connection=HardwareConnection(
                arm_kind="a", arm_endpoint="b",
                gripper_kind="c", gripper_endpoint=""))
    except HardwareConfigError as exc:
        observer_refused = exc.reason.value
    record("05_adapter_unconfigured_blocks",
           env_refused == "hardware.adapter_unconfigured"
           and adapter_finding is not None
           and create_refused == "hardware.adapter_unconfigured"
           and observer_refused == "hardware.adapter_unconfigured",
           f"환경변수 없음={env_refused} · 관문={None if adapter_finding is None else adapter_finding.reason_code.value}"
           f" · 한계 없는 어댑터 생성={create_refused}"
           f" · endpoint 없는 관측기={observer_refused}")

    # 준비되지 않은 어댑터는 연결·스킬을 모두 막는다.
    blocked_adapter = FR3HardwareAdapter(
        "synth", synth_profile(), connection=synth_connection(),
        readiness=hardware_readiness.evaluate(
            inputs=synth_set(synth_input(key) for key in SYNTH_KEYS),
            checklist=synth_checklist()))
    connect = blocked_adapter.connect(1.0)
    home = blocked_adapter.home(1.0)
    record("05b_unready_adapter_refuses_commands",
           not connect.request_accepted and not home.request_accepted
           and connect.reason is ReasonCode.HARDWARE_INPUT_MISSING
           and not blocked_adapter.supports("home")
           and not blocked_adapter.state().valid,
           f"connect={connect.reason} · home={home.reason}"
           f" · supports(home)={blocked_adapter.supports('home')}"
           f" · state.valid={blocked_adapter.state().valid}",
           {"connect": dict(connect.evidence)})

    # ── 06 시뮬레이션 결과 승격 시도 ──────────────────────────────────
    raised: list[str] = []
    try:
        ready_result.promote_simulation({"simulation_e2e": True})
    except HardwareReadinessError as exc:
        raised.append(f"readiness:{type(exc).__name__}")
    try:
        Robotiq2F85HardwareObservationAdapter.from_simulation()
    except GraspObservationError as exc:
        raised.append(f"observer:{type(exc).__name__}")
    try:
        Robotiq2F85HardwareObservationAdapter.from_aperture(0.05)
    except GraspObservationError as exc:
        raised.append(f"aperture:{type(exc).__name__}")
    from validation.simulation_e2e import SimulationVerdictError, not_started

    try:
        not_started(scenario="s", object_id="o", support_id="p", target_id="t",
                    reason=ReasonCode.EXEC_SIM_OBJECT_ABSENT,
                    detail="x").as_real_hardware_verdict()
    except SimulationVerdictError as exc:
        raised.append(f"transfer:{type(exc).__name__}")
    record("06_promotion_raises", len(raised) == 4,
           f"예외 {len(raised)}/4 · {raised}")

    # ── 07 모든 근거를 채우면 통과할 수 있다 ──────────────────────────
    full_observation = measured_from_device(
        held=True, object_id="synth_obj", observed_at=time.time(),
        source="synthetic-device", method="gripper_object_detection_status",
        robot_id="synth_robot", gripper_id="synth_gripper",
        raw_state={"gOBJ": 2}).to_dict()
    result = hardware_readiness.evaluate(
        inputs=synth_set(synth_input(key, ready=True,
                                     method=MeasurementMethod.SCALE)
                         for key in SYNTH_KEYS),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=True),
        adapter_config={"arm": "synthetic", "gripper": "synthetic"},
        grasp_observation=full_observation,
        hardware_connected=False)
    record("07_complete_synthetic_inputs_pass",
           result.real_hardware_ready and not result.real_hardware_verified,
           f"ready={result.real_hardware_ready}"
           f" · verified={result.real_hardware_verified}"
           " (연결 확인 전이므로 verified는 false다)",
           {"observation_hardware_complete":
            full_observation.get("hardware_complete")})

    # 근거 없는 passed는 통과로 세지 않는다.
    no_evidence = hardware_readiness.evaluate(
        inputs=synth_set(synth_input(key, ready=True,
                                     method=MeasurementMethod.SCALE)
                         for key in SYNTH_KEYS),
        checklist=synth_checklist(StepStatus.PASSED, with_evidence=False),
        adapter_config={"arm": "synthetic", "gripper": "synthetic"},
        grasp_observation=full_observation)
    record("07b_passed_without_evidence_is_not_done",
           not no_evidence.real_hardware_ready
           and ReasonCode.HARDWARE_CHECKLIST_INCOMPLETE
           in no_evidence.reason_codes,
           f"근거 없는 passed → {[c.value for c in no_evidence.reason_codes]}")

    # ── 08 현재 저장소 상태 ───────────────────────────────────────────
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    live_inputs = hardware_readiness.load_input_set(ROOT / manifest["inputs"])
    live_checklist = hardware_readiness.load_checklist(
        ROOT / manifest["checklist"])
    live = hardware_readiness.evaluate(
        inputs=live_inputs, checklist=live_checklist,
        adapter_config=None, grasp_observation=None, hardware_connected=False)
    record("08_repository_state_blocks",
           not live.real_hardware_ready
           and not live.real_hardware_verified
           and len(live.findings) > 0,
           f"real_hardware_ready={live.real_hardware_ready}"
           f" · 미충족 {len(live.findings)}건 · 입력"
           f" {len(live_inputs.ready_keys)}/{len(live_inputs.inputs)} 확보"
           f" · 체크리스트 {live_checklist.to_dict()['blocking_done']}"
           f"/{live_checklist.to_dict()['blocking_total']} 통과",
           {"reason_codes": [c.value for c in live.reason_codes],
            "collection_guide": list(
                hardware_readiness.collection_guide(live))})

    # 관측기가 없으면 unavailable이다.
    missing = unavailable(detail="관측 수단이 없다", source="none").to_dict()
    record("08b_grasp_observation_unavailable",
           missing["availability"] == "unavailable"
           and missing["hardware_complete"] is False,
           f"grasp.object_held={missing['availability']}"
           f" · hardware_complete={missing['hardware_complete']}"
           f" · 빠진 항목 {len(missing['hardware_gaps'])}개")

    # ── 09 작업자(측정한 사람) 요구 ────────────────────────────────────
    operator_refused = False
    try:
        HardwareInput(key="z", label="z", value=1.0, unit="kg", source="s",
                      measurement_method=MeasurementMethod.SCALE,
                      captured_at="2026-09-17", verified=True,
                      evidence_path="p")
    except HardwareInputError:
        operator_refused = True
    no_operator_step = ChecklistStep(
        no=1, key="s", label="합성", status=StepStatus.PASSED,
        evidence_path="p", captured_at="2026-09-17")
    with_operator_step = ChecklistStep(
        no=1, key="s", label="합성", status=StepStatus.PASSED,
        evidence_path="p", captured_at="2026-09-17", operator="합성 작업자")
    record("09_operator_is_required",
           operator_refused
           and no_operator_step.effective_status is StepStatus.EVIDENCE_MISSING
           and with_operator_step.done,
           f"작업자 없는 verified 거부={operator_refused}"
           f" · 작업자 없는 passed → {no_operator_step.effective_status}"
           f" · 작업자 있으면 → {with_operator_step.effective_status}",
           {"rule": "측정값과 단계 통과에는 수행한 사람이 붙어 있어야 한다"})

    # ── 10 근거 파일이 실제로 있어야 기록된다 ──────────────────────────
    tool = ROOT / "scripts/record_hardware_evidence.py"
    def run_tool(*extra: str) -> tuple[int, str]:
        done = subprocess.run(
            [sys.executable, str(tool), *extra],
            capture_output=True, text=True, timeout=120)
        return done.returncode, (done.stdout + done.stderr).strip()

    missing_code, missing_out = run_tool(
        "input", "coupling_measured_mass", "--value", "0.041", "--unit", "kg",
        "--method", "scale", "--source", "합성", "--captured-at", "2026-09-17",
        "--operator", "합성 작업자",
        "--evidence", "reports/hardware/evidence/__does_not_exist__.jpg",
        "--verified")
    sim_code, sim_out = run_tool(
        "input", "coupling_measured_mass", "--value", "0.2",
        "--method", "simulation")
    declared_code, declared_out = run_tool(
        "input", "mounting_yaw_evidence", "--value", "0.0",
        "--method", "declared", "--verified")
    record("10_intake_refuses_without_real_evidence",
           missing_code != 0 and "근거 파일이 없다" in missing_out
           and sim_code != 0 and "시뮬레이터에서 온 값" in sim_out
           and declared_code != 0 and "verified로 둘 수 없다" in declared_out,
           f"없는 파일 거부(exit {missing_code}) ·"
           f" 시뮬레이터 방법 거부(exit {sim_code}) ·"
           f" declared+verified 거부(exit {declared_code})",
           {"missing_file": missing_out[:200],
            "simulation": sim_out[:160],
            "declared": declared_out[:160]})

    # ── 11 실제 장비를 만지는 단계는 사람이 한다 ───────────────────────
    human = [s for s in live_checklist.steps if s.performed_by_human]
    with_robot = [s for s in live_checklist.steps
                  if s.actor is StepActor.FIELD_OPERATOR_WITH_ROBOT]
    code_steps = [s for s in live_checklist.steps if s.actor is StepActor.CODE]
    forms_missing = [s.key for s in live_checklist.steps if not s.evidence_fields]
    record("11_hardware_steps_are_human_performed",
           len(human) == len(live_checklist.steps)
           and len(with_robot) >= 6 and not code_steps and not forms_missing,
           f"사람 수행 {len(human)}/{len(live_checklist.steps)} ·"
           f" 실제 장비 조작 {len(with_robot)}건 ·"
           f" 코드 수행 {len(code_steps)}건 · 근거 양식 누락 {len(forms_missing)}건",
           {"with_robot": [s.key for s in with_robot]})

    # ── 12 측정·연결 절차가 선언돼 있다 ────────────────────────────────
    manifest_payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    procedures_path = ROOT / str(manifest_payload.get("procedures") or "")
    procedures = (json.loads(procedures_path.read_text(encoding="utf-8"))
                  if procedures_path.is_file() else {})
    required_blocks = ("tcp_measurement", "cell_frame_calibration",
                       "connection_requirements", "grasp_observation_capture")
    present = [name for name in required_blocks if procedures.get(name)]
    frames = [row["frame"] for row
              in (procedures.get("cell_frame_calibration") or {}).get(
                  "frames_to_measure", ())]
    expected_frames = {"world", "pedestal_frame", "fr3_base_frame",
                       "pallet_1_frame", "pallet_2_frame", "pallet_3_frame",
                       "conveyor_frame"}
    env_names = {row["name"] for row
                 in (procedures.get("connection_requirements") or {}).get(
                     "environment_variables", ())}
    record("12_measurement_and_connection_procedures_declared",
           len(present) == len(required_blocks)
           and expected_frames <= set(frames)
           and len(env_names) >= 5,
           f"절차 블록 {len(present)}/{len(required_blocks)} ·"
           f" 측정 대상 frame {len(frames)}개 ·"
           f" 필요 환경변수 {len(env_names)}개",
           {"blocks": present, "frames": frames,
            "environment_variables": sorted(env_names)})

    # ── 13 수집 시트가 사람이 읽을 문장으로 나온다 ─────────────────────
    sheet_run = subprocess.run(
        [sys.executable, str(ROOT / "scripts/hardware_evidence_sheet.py")],
        capture_output=True, text=True, timeout=300)
    sheet_path = ROOT / "reports/hardware/collection_sheet.md"
    sheet = sheet_path.read_text(encoding="utf-8") if sheet_path.is_file() else ""
    input_labels = [item.label for item in live_inputs.inputs]
    step_labels = [step.label for step in live_checklist.steps]
    record("13_collection_sheet_lists_every_missing_item",
           sheet_run.returncode == 0
           and all(label in sheet for label in input_labels)
           and all(label in sheet for label in step_labels)
           # 시트 맨 앞의 고정 상태 다섯 줄이 그대로 들어 있다.
           and all(line in sheet for line in (
               "simulation_e2e=true", "real_hardware_ready=false",
               "real_hardware_verified=false", "hardware_evidence=0/18",
               "hardware_checklist=0/11")),
           f"시트 {len(sheet.splitlines())}줄 ·"
           f" 입력 {len(input_labels)}건 + 단계 {len(step_labels)}건 모두 기재"
           f" · 미충족 문장 출력 {len(sheet_run.stdout.splitlines())}줄",
           {"sheet": str(sheet_path.relative_to(ROOT))})

    # ── 14 고정 상태 다섯 줄 ──────────────────────────────────────────
    state = hardware_readiness.pinned_state(live, simulation_e2e=True)
    lines = hardware_readiness.state_lines(state)
    expected = ("simulation_e2e=true", "real_hardware_ready=false",
                "real_hardware_verified=false", "hardware_evidence=0/18",
                "hardware_checklist=0/11")
    unchanged = all(
        hardware_readiness.pinned_state(live, simulation_e2e=flag)[
            "real_hardware_verified"] is False
        for flag in (True, False, None))
    record("14_pinned_state_is_exact",
           lines == expected and unchanged,
           " · ".join(lines) + f" · 시뮬레이터 축이 실기 값을 바꾸지 않는다={unchanged}",
           {"state": dict(state), "expected": list(expected)})

    # ── 15 근거 수 0이면 verified는 반드시 false ──────────────────────
    zero_evidence = (state["hardware_evidence"].split("/")[0] == "0"
                     and state["hardware_checklist"].split("/")[0] == "0")
    record("15_zero_evidence_forces_not_verified",
           zero_evidence
           and state["real_hardware_verified"] is False
           and state["real_hardware_ready"] is False
           and isinstance(state["hardware_evidence"], str),
           f"근거 {state['hardware_evidence']} · 체크리스트"
           f" {state['hardware_checklist']} →"
           f" ready={state['real_hardware_ready']}"
           f" verified={state['real_hardware_verified']}"
           " (수집 수는 문자열이라 참/거짓으로 읽히지 않는다)")

    # ── 16 Gazebo 회귀 ───────────────────────────────────────────────
    outcome = ROOT / "reports/workcell/outcome_contract.json"
    sim = ROOT / "reports/workcell/pick_place_sim_e2e_verify.json"
    outcome_payload = json.loads(outcome.read_text(encoding="utf-8")) \
        if outcome.is_file() else {}
    sim_payload = json.loads(sim.read_text(encoding="utf-8")) \
        if sim.is_file() else {}
    record("16_gazebo_records_unchanged",
           outcome_payload.get("passed_count") == outcome_payload.get("total_count")
           and sim_payload.get("passed_count") == sim_payload.get("total_count")
           and sim_payload.get("real_hardware_ready") is False,
           f"판정 계약 {outcome_payload.get('passed_count')}"
           f"/{outcome_payload.get('total_count')}"
           f" · 시뮬레이션 E2E {sim_payload.get('passed_count')}"
           f"/{sim_payload.get('total_count')}"
           f" · 시뮬레이션 기록의 real_hardware_ready="
           f"{sim_payload.get('real_hardware_ready')}")

    passed = sum(1 for row in checks if row["passed"])
    # 시뮬레이터 축은 기록에서만 읽는다(판정에 쓰지 않는다).
    sim_report = ROOT / "reports/workcell/pick_place_sim_e2e_verify.json"
    sim_payload = (json.loads(sim_report.read_text(encoding="utf-8"))
                   if sim_report.is_file() else {})
    sim_transfers = [
        row for row in sim_payload.get("checks", ())
        if str(row.get("key", "")).startswith(("01_e2e", "02_e2e", "03_e2e"))]
    report_state = hardware_readiness.pinned_state(
        live,
        simulation_e2e=(bool(sim_transfers)
                        and all(row.get("passed") for row in sim_transfers)
                        if sim_transfers else None))
    report = {
        "schema": "forstick2.hardware_readiness_verify/2",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        # ── 지금 상태 (다섯 줄 고정) ────────────────────────────────────
        "state": report_state,
        "state_lines": list(hardware_readiness.state_lines(report_state)),
        "is_simulated": False,
        "real_hardware_connected": False,
        "real_hardware_ready": live.real_hardware_ready,
        "real_hardware_verified": live.real_hardware_verified,
        # ── 차단 검사 결과 (근거 수집 수와 **다른 값**이다) ─────────────
        "gate_checks_passed": passed,
        "gate_checks_total": len(checks),
        "gate_checks_note": "차단 검사 통과 수다. 근거 수집 수"
                            "(state.hardware_evidence)와 바꿔 읽지 않는다",
        "checks": checks,
        "live_readiness": live.to_dict(),
        "note": "실기 전환 준비 차단 체계 확인이다. 이 단계에서 실제 로봇이나"
                " 실제 그리퍼에 명령을 보내지 않았다",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\n실기 준비 차단 확인 {passed}/{len(checks)} 통과"
          f" — {OUT.relative_to(ROOT)}")
    print("지금 상태 (고정):")
    for line in hardware_readiness.state_lines(report_state):
        print(f"  {line}")
    print(f"  (차단 검사 {passed}/{len(checks)}은 **근거 수집 수가 아니다**)")
    print(f"미충족 {len(live.findings)}건")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
