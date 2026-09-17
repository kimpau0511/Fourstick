"""실기 전환 준비 관문 (md/개발플랜.md 8-12).

이 관문이 답하는 질문은 하나다 — **실제 팔과 실제 그리퍼에 명령을 보낼 근거가
있는가.** 없으면 `real_hardware_ready=false`이고, 무엇을 가져와야 하는지
사람이 바로 읽을 수 있는 문장으로 돌려준다.

## 시뮬레이터 결과로는 통과할 수 없다

- `simulation` / `simulated_observation` / `declared` 방법으로 얻은 값은 실기
  근거가 아니다(`core/hardware_input.py`가 `verified=true`를 거부한다).
- Gazebo 이송 시연(`validation/simulation_e2e.py`)은 이 관문의 입력이 아니다.
  `evaluate()`에 시뮬레이터 결과를 받는 인자가 없고, `promote_simulation()`은
  **예외를 던진다.**
- 파지 관측은 `measured`이면서 실기 필수 항목(로봇·그리퍼 id, 대상 물체 id,
  원본 device 상태)이 모두 있어야 인정된다.

## 무엇을 보는가

1. 필수 입력(설정에 선언된 것 전부)이 `verified`인가
2. 장착 전 체크리스트의 blocking 단계가 모두 `passed`인가 — 근거 파일이
   없으면 `evidence_missing`으로 되돌린다
3. 실기 어댑터 설정이 있는가(주소·엔드포인트는 **여기에 적지 않는다**)
4. 실기 파지 관측이 있는가

하나라도 아니면 차단이고, 각각 다른 이유 코드가 붙는다(`hardware.*`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.hardware_input import (
    HardwareInput,
    InputSet,
    MeasurementMethod,
    load_input,
)
from core.reason_codes import ReasonCode


class HardwareReadinessError(Exception):
    """시뮬레이터 결과를 실기 준비로 쓰려 할 때 나는 오류."""


class StepStatus(str, Enum):
    """체크리스트 단계 상태."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    PASSED = "passed"
    FAILED = "failed"
    EVIDENCE_MISSING = "evidence_missing"

    def __str__(self) -> str:
        return self.value


class StepActor(str, Enum):
    """단계를 **누가 수행하는가.**

    실제 장비를 연결하거나 움직이는 단계는 사람이 현장에서 한다. 코드가
    대신할 수 없고, 대신한 것처럼 기록해서도 안 된다.
    """

    #: 현장 작업자가 손으로 한다(전원 차단·체결·사진·저울).
    FIELD_OPERATOR = "field_operator"
    #: 현장 작업자가 장비를 움직여 한다(저속 home·open/close·STOP·TCP 측정).
    FIELD_OPERATOR_WITH_ROBOT = "field_operator_with_robot"
    #: 근거가 들어온 뒤 코드가 판정·기록한다.
    CODE = "code"

    def __str__(self) -> str:
        return self.value


#: 실제 장비를 만지거나 움직이는 역할. **코드가 수행하지 않는다.**
HUMAN_ACTORS: frozenset[StepActor] = frozenset({
    StepActor.FIELD_OPERATOR,
    StepActor.FIELD_OPERATOR_WITH_ROBOT,
})


@dataclass(frozen=True)
class ChecklistStep:
    """장착 전·후 단계 하나."""

    no: int
    key: str
    label: str
    status: StepStatus
    requires_inputs: tuple[str, ...] = ()
    evidence: str = ""
    evidence_path: str = ""
    captured_at: str = ""
    detail: str = ""
    blocking: bool = True
    needed: str = ""
    #: 누가 수행하는가. 실제 장비를 만지는 단계는 사람이다.
    actor: StepActor = StepActor.FIELD_OPERATOR
    #: 실제로 수행한 사람. `passed`로 두려면 있어야 한다.
    operator: str = ""
    #: 근거 양식 — 이 항목들이 근거에 들어 있어야 한다.
    evidence_fields: tuple[str, ...] = ()

    @property
    def performed_by_human(self) -> bool:
        return self.actor in HUMAN_ACTORS

    @property
    def effective_status(self) -> StepStatus:
        """근거 없는 `passed`는 `evidence_missing`으로 본다.

        "했다"는 말만으로 통과로 세지 않는다 — 근거 파일·시각·**수행한 사람**이
        모두 있어야 한다.
        """
        if self.status is StepStatus.PASSED and not (
                self.evidence_path and self.captured_at and self.operator):
            return StepStatus.EVIDENCE_MISSING
        return self.status

    @property
    def done(self) -> bool:
        return self.effective_status is StepStatus.PASSED

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "key": self.key,
            "label": self.label,
            "status": self.status.value,
            "effective_status": self.effective_status.value,
            "requires_inputs": list(self.requires_inputs),
            "evidence": self.evidence,
            "evidence_path": self.evidence_path,
            "captured_at": self.captured_at,
            "detail": self.detail,
            "blocking": self.blocking,
            "needed": self.needed,
            "actor": self.actor.value,
            "performed_by_human": self.performed_by_human,
            "operator": self.operator,
            "evidence_fields": list(self.evidence_fields),
            "done": self.done,
        }


@dataclass(frozen=True)
class Checklist:
    checklist_id: str
    checklist_version: str
    steps: tuple[ChecklistStep, ...]
    source: str = ""

    @property
    def blocking_steps(self) -> tuple[ChecklistStep, ...]:
        return tuple(step for step in self.steps if step.blocking)

    @property
    def pending(self) -> tuple[ChecklistStep, ...]:
        return tuple(step for step in self.blocking_steps if not step.done)

    @property
    def complete(self) -> bool:
        return bool(self.blocking_steps) and not self.pending

    def to_dict(self) -> dict:
        counts: dict[str, int] = {}
        for step in self.steps:
            key = step.effective_status.value
            counts[key] = counts.get(key, 0) + 1
        return {
            "checklist_id": self.checklist_id,
            "checklist_version": self.checklist_version,
            "source": self.source,
            "steps": [step.to_dict() for step in self.steps],
            "counts": counts,
            "blocking_total": len(self.blocking_steps),
            "blocking_done": len(self.blocking_steps) - len(self.pending),
            "human_steps": [step.key for step in self.steps
                            if step.performed_by_human],
            "complete": self.complete,
        }


@dataclass(frozen=True)
class Finding:
    """차단 항목 하나. 사람이 무엇을 가져와야 하는지 함께 담는다."""

    key: str
    label: str
    reason_code: ReasonCode
    detail: str
    needed: str = ""
    evidence_path: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "needed": self.needed,
            "evidence_path": self.evidence_path,
        }


@dataclass(frozen=True)
class HardwareReadinessResult:
    """실기 준비 판정.

    **`real_hardware_ready`는 모든 필수 근거가 채워질 때만 true다.** 그리고
    이 객체는 시뮬레이터 결과를 입력으로 받지 않는다.
    """

    profile_id: str
    profile_version: str
    inputs: InputSet
    checklist: Checklist
    findings: tuple[Finding, ...]
    adapter_configured: bool
    adapter_detail: str
    grasp_observation: Mapping[str, Any] | None = None
    hardware_connected: bool = False
    source: str = ""
    limitations: tuple[str, ...] = ()

    @property
    def real_hardware_ready(self) -> bool:
        """필수 입력·체크리스트·어댑터 설정·파지 관측이 모두 갖춰졌는가."""
        return not self.findings

    @property
    def real_hardware_verified(self) -> bool:
        """실기에서 동작을 확인했는가. **연결 확인 없이는 false다.**"""
        return bool(self.real_hardware_ready and self.hardware_connected)

    @property
    def reason_codes(self) -> tuple[ReasonCode, ...]:
        out: list[ReasonCode] = []
        for item in self.findings:
            if item.reason_code not in out:
                out.append(item.reason_code)
        return tuple(out)

    @property
    def missing_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.findings)

    def promote_simulation(self, *_args, **_kwargs):
        """**호출하면 예외다.** 시뮬레이터 결과를 실기 준비로 바꾸지 않는다."""
        raise HardwareReadinessError(
            "Gazebo 이송 시연 결과를 실기 준비(real_hardware_ready)로 바꿀 수"
            " 없다. 시뮬레이터에서 물체가 옮겨진 것은 고정 장치가 움직였다는"
            " 뜻이고, 실기 준비는 장착 근거·질량·관성·실기 파지 관측을"
            " 요구한다")

    def to_dict(self) -> dict:
        return {
            "schema": "forstick2.hardware_readiness_result/1",
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            # ── 두 축을 섞지 않는다 ───────────────────────────────────
            "real_hardware_ready": self.real_hardware_ready,
            "real_hardware_verified": self.real_hardware_verified,
            "real_hardware_connected": self.hardware_connected,
            "is_simulated": False,
            # ── 근거 ──────────────────────────────────────────────────
            "inputs": self.inputs.to_dict(),
            "checklist": self.checklist.to_dict(),
            "findings": [item.to_dict() for item in self.findings],
            "reason_codes": [code.value for code in self.reason_codes],
            "missing_labels": list(self.missing_labels),
            "adapter_configured": self.adapter_configured,
            "adapter_detail": self.adapter_detail,
            "grasp_observation": (None if self.grasp_observation is None
                                  else dict(self.grasp_observation)),
            "source": self.source,
            "limitations": list(self.limitations),
            "note": "실기 준비 판정이다. Gazebo 시뮬레이션 결과와 같은 축이"
                    " 아니며, 시뮬레이터 결과로 이 판정을 채울 수 없다",
        }


# ── 설정 읽기 ────────────────────────────────────────────────────────────
def load_input_set(path: Path | str) -> InputSet:
    """실기 준비 입력 설정을 읽는다. **없는 필드를 채우지 않는다.**"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("inputs") or []
    if not rows:
        raise HardwareReadinessError(f"입력이 비어 있다: {path}")
    return InputSet(
        profile_id=str(payload.get("profile_id") or ""),
        profile_version=str(payload.get("profile_version") or ""),
        inputs=tuple(load_input(row) for row in rows),
        source=str(path),
        extras={
            "description": payload.get("description"),
            "how_to_fill": payload.get("how_to_fill"),
            "evidence_dir": payload.get("evidence_dir"),
            "note": payload.get("note"),
        },
    )


def load_checklist(path: Path | str) -> Checklist:
    """장착 전 체크리스트를 읽는다."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = []
    for row in payload.get("steps") or ():
        raw = str(row.get("status") or "not_started")
        try:
            status = StepStatus(raw)
        except ValueError:
            raise HardwareReadinessError(
                f"{row.get('key')}: 모르는 단계 상태다: {raw!r}"
                f" (허용: {[s.value for s in StepStatus]})") from None
        raw_actor = str(row.get("actor") or "field_operator")
        try:
            actor = StepActor(raw_actor)
        except ValueError:
            raise HardwareReadinessError(
                f"{row.get('key')}: 모르는 수행 주체다: {raw_actor!r}"
                f" (허용: {[a.value for a in StepActor]})") from None
        steps.append(ChecklistStep(
            no=int(row["no"]), key=str(row["key"]), label=str(row["label"]),
            status=status,
            requires_inputs=tuple(row.get("requires_inputs") or ()),
            evidence=str(row.get("evidence") or ""),
            evidence_path=str(row.get("evidence_path") or ""),
            captured_at=str(row.get("captured_at") or ""),
            detail=str(row.get("detail") or ""),
            blocking=bool(row.get("blocking", True)),
            needed=str(row.get("needed") or ""),
            actor=actor,
            operator=str(row.get("operator") or ""),
            evidence_fields=tuple(
                str(item) for item in (row.get("evidence_fields") or ())),
        ))
    if not steps:
        raise HardwareReadinessError(f"체크리스트가 비어 있다: {path}")
    return Checklist(
        checklist_id=str(payload.get("checklist_id") or ""),
        checklist_version=str(payload.get("checklist_version") or ""),
        steps=tuple(steps), source=str(path))


# ── 판정 ─────────────────────────────────────────────────────────────────
def _input_finding(item: HardwareInput) -> Finding | None:
    """입력 하나가 왜 준비되지 않았는지. 이유를 종류별로 나눈다."""
    if item.ready:
        return None
    if item.from_simulation:
        return Finding(
            key=item.key, label=item.label,
            reason_code=ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED,
            detail=f"시뮬레이터에서 온 값이다({item.measurement_method})"
                   " — 실기 근거로 쓸 수 없다",
            needed=item.needed)
    if not item.has_value:
        return Finding(
            key=item.key, label=item.label,
            reason_code=ReasonCode.HARDWARE_INPUT_MISSING,
            detail="값이 없다", needed=item.needed)
    if item.declared_only:
        return Finding(
            key=item.key, label=item.label,
            reason_code=ReasonCode.HARDWARE_DECLARED_ONLY,
            detail="문서에 적힌 선언값만 있고 측정이 없다",
            needed=item.needed, evidence_path=item.evidence_path)
    if not item.has_evidence:
        gaps = [name for name in
                ("source", "captured_at", "evidence_path", "operator")
                if not getattr(item, name)]
        return Finding(
            key=item.key, label=item.label,
            reason_code=ReasonCode.HARDWARE_EVIDENCE_MISSING,
            detail=f"값은 있으나 근거가 없다(빈 항목: {', '.join(gaps)})",
            needed=item.needed, evidence_path=item.evidence_path)
    return Finding(
        key=item.key, label=item.label,
        reason_code=ReasonCode.HARDWARE_UNVERIFIED,
        detail="값과 근거는 있으나 확인되지 않았다(verified=false)",
        needed=item.needed, evidence_path=item.evidence_path)


def _grasp_finding(observation: Mapping[str, Any] | None) -> Finding | None:
    """실기 파지 관측이 있는가. **개구 일치는 근거가 아니다.**"""
    label = "실기 파지 관측(grasp.object_held)"
    if not observation:
        return Finding(
            key="grasp_observation", label=label,
            reason_code=ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE,
            detail="파지 관측 기록이 없다",
            needed="그리퍼의 물체 감지 상태 또는 동등한 센서 값을 읽어"
                   " 관측 기록을 남길 경로가 필요하다. 개구가 목표와 맞는"
                   " 것은 파지 근거가 아니다")
    availability = str(observation.get("availability") or "unavailable")
    if availability != "measured":
        return Finding(
            key="grasp_observation", label=label,
            reason_code=(ReasonCode.HARDWARE_SIMULATED_VALUE_REJECTED
                         if availability == "simulated_observation"
                         else ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE),
            detail=f"관측 종류가 {availability}다 — 실기 관측이 아니다",
            needed="실기 센서 관측(measured)이 필요하다."
                   " 시뮬레이터 관측은 실기 근거로 승격되지 않는다")
    if not observation.get("hardware_complete"):
        gaps = observation.get("hardware_gaps") or ["필수 항목"]
        return Finding(
            key="grasp_observation", label=label,
            reason_code=ReasonCode.HARDWARE_OBSERVATION_UNAVAILABLE,
            detail=f"실기 관측 필수 항목이 빠졌다: {', '.join(gaps)}",
            needed="관측 시각·로봇 id·그리퍼 id·대상 물체 id·관측 방식·"
                   "원본 device 상태를 모두 남긴다")
    return None


def evaluate(
    *,
    inputs: InputSet,
    checklist: Checklist,
    adapter_config: Mapping[str, Any] | None = None,
    grasp_observation: Mapping[str, Any] | None = None,
    hardware_connected: bool = False,
    required_keys: Sequence[str] | None = None,
) -> HardwareReadinessResult:
    """실기 준비를 판정한다. **시뮬레이터 결과를 받는 인자가 없다.**

    `required_keys`를 주지 않으면 설정에 선언된 입력 **전부**가 필수다 —
    필수 목록을 코드에서 줄이지 않는다.
    """
    findings: list[Finding] = []
    keys = tuple(required_keys) if required_keys is not None else tuple(
        item.key for item in inputs.inputs)
    for key in keys:
        item = inputs.get(key)
        if item is None:
            findings.append(Finding(
                key=key, label=key,
                reason_code=ReasonCode.HARDWARE_INPUT_MISSING,
                detail="설정에 이 입력 자체가 없다",
                needed=f"{key} 입력을 설정에 추가하고 값을 채운다"))
            continue
        finding = _input_finding(item)
        if finding is not None:
            findings.append(finding)

    # 체크리스트: blocking 단계가 모두 passed여야 한다.
    for step in checklist.pending:
        findings.append(Finding(
            key=f"checklist:{step.key}", label=f"{step.no}. {step.label}",
            reason_code=ReasonCode.HARDWARE_CHECKLIST_INCOMPLETE,
            detail=f"상태가 {step.effective_status}다",
            needed=step.needed, evidence_path=step.evidence_path))

    # 실기 어댑터 설정.
    configured, adapter_detail = _adapter_state(adapter_config)
    if not configured:
        findings.append(Finding(
            key="hardware_adapter", label="실기 어댑터 설정",
            reason_code=ReasonCode.HARDWARE_ADAPTER_UNCONFIGURED,
            detail=adapter_detail,
            needed="팔 컨트롤러·그리퍼 통신 설정을 실행 환경에 넣는다."
                   " 주소·인증값은 프로파일 파일에 적지 않는다"))

    grasp = _grasp_finding(grasp_observation)
    if grasp is not None:
        findings.append(grasp)

    return HardwareReadinessResult(
        profile_id=inputs.profile_id, profile_version=inputs.profile_version,
        inputs=inputs, checklist=checklist, findings=tuple(findings),
        adapter_configured=configured, adapter_detail=adapter_detail,
        grasp_observation=(None if grasp_observation is None
                           else dict(grasp_observation)),
        hardware_connected=bool(hardware_connected),
        source=inputs.source,
        limitations=(
            "이 판정은 실기 준비 근거만 본다. Gazebo 이송 시연 결과는 입력이"
            " 아니다",
            "모든 조건이 채워져도 `real_hardware_verified`는 실기 연결이"
            " 확인된 뒤에만 true가 된다",
        ),
    )


def _adapter_state(config: Mapping[str, Any] | None) -> tuple[bool, str]:
    """실기 어댑터 설정이 있는가. 값 자체는 판정에 담지 않는다."""
    if not config:
        return False, "실기 어댑터 설정이 없다"
    missing = [name for name in ("arm", "gripper") if not config.get(name)]
    if missing:
        return False, f"설정에 없는 부분: {', '.join(missing)}"
    return True, "팔·그리퍼 설정이 모두 선언됐다"


#: 고정 상태 줄. **한 곳에서만 만든다** — 화면·API·보고서·시트가 같은 문장을
#: 쓰게 해서 숫자가 다른 뜻으로 읽히는 일을 막는다.
STATE_KEYS: tuple[str, ...] = (
    "simulation_e2e",
    "real_hardware_ready",
    "real_hardware_verified",
    "hardware_evidence",
    "hardware_checklist",
)


def pinned_state(
    result: HardwareReadinessResult, *, simulation_e2e: bool | None = None,
) -> dict:
    """지금 상태를 **다섯 줄로 고정**한다.

    `simulation_e2e`는 **표시용 인자다.** 실기 판정에 쓰이지 않는다 —
    `real_hardware_ready`·`real_hardware_verified`는 `result`에서만 온다.
    두 축을 나란히 적기 위해 받을 뿐이고, 시뮬레이터가 참이어도 실기 값은
    바뀌지 않는다(테스트로 고정).

    왜 한 곳에서 만드는가. 검사 통과 수(예: 차단 검사 18/18)와 근거 수집 수
    (0/18)가 같은 화면에 있으면 서로 다른 뜻의 18이 섞인다. 이 함수가 내는
    문장만 쓰면 그 혼동이 생기지 않는다.
    """
    inputs = result.inputs
    checklist = result.checklist
    blocking_done = len(checklist.blocking_steps) - len(checklist.pending)
    return {
        "simulation_e2e": (None if simulation_e2e is None
                           else bool(simulation_e2e)),
        "real_hardware_ready": result.real_hardware_ready,
        "real_hardware_verified": result.real_hardware_verified,
        "hardware_evidence": f"{len(inputs.ready_keys)}/{len(inputs.inputs)}",
        "hardware_checklist": f"{blocking_done}/{len(checklist.blocking_steps)}",
        "note": "hardware_evidence·hardware_checklist는 **근거 수집 수**다."
                " 차단 검사 통과 수와 다른 값이며 서로 바꿔 읽지 않는다",
    }


def state_lines(state: Mapping[str, Any]) -> tuple[str, ...]:
    """고정 상태를 사람이 읽는 다섯 줄로. 형식을 여기서만 정한다."""
    def render(value: Any) -> str:
        if value is None:
            return "unknown"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return tuple(f"{key}={render(state.get(key))}" for key in STATE_KEYS)


def collection_guide(result: HardwareReadinessResult) -> tuple[dict, ...]:
    """사람이 바로 수집할 수 있게 "무엇을 어떻게" 목록으로 돌려준다."""
    return tuple({
        "label": item.label,
        "reason_code": item.reason_code.value,
        "needed": item.needed or item.detail,
    } for item in result.findings)
