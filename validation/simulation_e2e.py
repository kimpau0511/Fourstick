"""시뮬레이터 이송 시연 판정 (md/개발플랜.md 8-11).

이 모듈은 **시뮬레이터 안에서 물체가 옮겨졌는가**만 판정한다. 그것은 실제
로봇으로 pick/place를 할 수 있다는 뜻이 **아니다.** 두 축을 코드로 분리한다.

| 축 | 뜻 | 여기서 나오는 값 |
|---|---|---|
| `simulation_e2e` | Gazebo에서 물체가 팔레트 → 컨베이어로 옮겨졌다 | True일 수 있다 |
| `real_hardware_ready` | 실기 장착·파지·안전 근거가 있어 실제로 실행할 수 있다 | **항상 False** |

## 왜 코드로 분리하는가

시뮬레이터에서 물체가 움직이는 것은 **고정 장치(fixture)** 가 움직였기
때문이다. 마찰 파지도, 물체 감지도 아니다. 그래서:

- `grasp_observation_kind`는 `simulated_observation`으로 고정된다. `measured`를
  넣을 수 없다(불변식이 거부한다).
- 결과 dict에 `task_succeeded`·`verified`·`PASS` 같은 **일반 실행 성공 어휘를
  쓰지 않는다.** 이름이 같으면 집계와 화면에서 섞인다.
- `as_real_hardware_verdict()`는 **예외를 던진다.** 승격 경로를 만들지 않는다.
- pick/place 관문(`validation/pick_place_gate.py`)은 이 결과를 조건 충족
  근거로 받지 않는다. 시뮬레이터 결과로 실기 조건을 열지 않는다.

## 판정 조건

`simulation_transfer_completed`는 다음 **전부**가 관측됐을 때만 참이다.

1. 계획 단계 전체 완료
2. 물체가 **선언된 팔레트에서** 고정됨(attach)
3. 이동 중 물체가 도구와 함께 관측됨
4. 컨베이어 위치에서 해제됨(detach)
5. 최종 물체 pose가 **선언된 배치 허용 구역** 안
6. arm·gripper가 안전 home 또는 retreat 상태로 복귀
7. 실행 중 충돌·snapshot 만료·STOP·컨트롤러 오류가 없음

하나라도 빠지면 완료가 아니고, 이유 코드가 붙는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence

from core.reason_codes import ReasonCode

#: 시뮬레이터 파지 관측 종류. 이 모듈이 낼 수 있는 유일한 값이다.
SIMULATED_OBSERVATION = "simulated_observation"

#: 실기 관측 종류. **이 모듈은 이 값을 만들지 않는다.**
MEASURED_OBSERVATION = "measured"

#: 판정 조건 이름과 라벨. 보고서·화면이 같은 이름을 쓴다.
CRITERIA: tuple[tuple[str, str], ...] = (
    ("stages_completed", "계획 단계 전체 완료"),
    ("attached_at_declared_support", "선언된 팔레트에서 고정"),
    ("moved_with_tool", "이동 중 도구와 함께 관측"),
    ("detached_at_target", "컨베이어 위치에서 해제"),
    ("placement_in_zone", "최종 pose가 배치 허용 구역 안"),
    ("returned_to_safe_pose", "arm·gripper 안전 자세 복귀"),
    ("no_fault_during_run", "충돌·snapshot 만료·STOP·컨트롤러 오류 없음"),
)


class SimulationVerdictError(Exception):
    """시뮬레이터 결과를 실기 결과로 쓰려 할 때 나는 오류."""


class TransferStatus(str, Enum):
    """시뮬레이터 이송 결과. **실행 성공/실패 어휘를 쓰지 않는다.**"""

    #: 위 7개 조건이 모두 관측됐다.
    SIMULATION_TRANSFER_COMPLETED = "simulation_transfer_completed"
    #: 조건 하나 이상이 관측되지 않았다.
    SIMULATION_TRANSFER_INCOMPLETE = "simulation_transfer_incomplete"
    #: 정지가 걸려 중단됐다.
    SIMULATION_TRANSFER_STOPPED = "simulation_transfer_stopped"
    #: 시작 전 조건이 맞지 않아 시작하지 않았다.
    SIMULATION_TRANSFER_NOT_STARTED = "simulation_transfer_not_started"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Criterion:
    """조건 하나의 관측 결과."""

    key: str
    label: str
    met: bool
    detail: str
    reason_code: ReasonCode | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.key not in dict(CRITERIA):
            raise SimulationVerdictError(f"선언되지 않은 조건: {self.key!r}")
        if self.met and self.reason_code is not None:
            raise SimulationVerdictError(
                f"{self.key}: 충족인데 이유 코드가 붙어 있다")
        if not self.met and self.reason_code is None:
            raise SimulationVerdictError(
                f"{self.key}: 미충족인데 이유 코드가 없다")

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "met": self.met,
            "detail": self.detail,
            "reason_code": None if self.reason_code is None
                           else self.reason_code.value,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class SimulationTransferResult:
    """시뮬레이터 이송 시연 한 건의 결과.

    **실기 실행 결과가 아니다.** `real_hardware_ready`는 항상 False이고,
    `grasp_observation_kind`는 `simulated_observation`으로 고정된다.
    """

    scenario: str
    object_id: str
    support_id: str
    target_id: str
    criteria: tuple[Criterion, ...]
    #: 고정 장치가 남긴 사건(attach/follow/detach)과 물체 pose 기록.
    fixture_events: tuple[Mapping[str, Any], ...] = ()
    #: 물체 pose 시간열(단계별 관측).
    object_pose_timeline: tuple[Mapping[str, Any], ...] = ()
    #: 계획 사전 검증 결과(`validation/pick_place_plan.py`).
    plan_validation: Mapping[str, Any] | None = None
    #: 단계별 실행 기록.
    stage_records: tuple[Mapping[str, Any], ...] = ()
    #: 정지가 요청·확인됐는가.
    stop_requested: bool = False
    stop_confirmed: bool | None = None
    #: 시작조차 하지 않았으면 그 이유.
    not_started_reason: ReasonCode | None = None
    detail: str = ""
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        keys = [item.key for item in self.criteria]
        if len(keys) != len(set(keys)):
            raise SimulationVerdictError(f"조건이 중복됐다: {keys}")

    # ── 두 축 ───────────────────────────────────────────────────────────
    @property
    def status(self) -> TransferStatus:
        if self.not_started_reason is not None:
            return TransferStatus.SIMULATION_TRANSFER_NOT_STARTED
        if self.stop_requested:
            return TransferStatus.SIMULATION_TRANSFER_STOPPED
        if self.criteria and all(item.met for item in self.criteria):
            return TransferStatus.SIMULATION_TRANSFER_COMPLETED
        return TransferStatus.SIMULATION_TRANSFER_INCOMPLETE

    @property
    def simulation_e2e(self) -> bool:
        """시뮬레이터에서 이송이 관측됐는가. 이것만이 이 모듈의 주장이다."""
        return self.status is TransferStatus.SIMULATION_TRANSFER_COMPLETED

    @property
    def real_hardware_ready(self) -> bool:
        """**항상 False.**

        시뮬레이터에서 물체가 옮겨진 것은 고정 장치가 움직였다는 뜻이다.
        실기 장착 근거·질량·파지 관측이 없으면 실제로 실행할 수 없고, 그
        판정은 `validation/pick_place_gate.py`가 따로 한다.
        """
        return False

    @property
    def grasp_observation_kind(self) -> str:
        """**항상 `simulated_observation`.** `measured`가 될 수 없다."""
        return SIMULATED_OBSERVATION

    @property
    def unmet(self) -> tuple[Criterion, ...]:
        return tuple(item for item in self.criteria if not item.met)

    @property
    def reason_codes(self) -> tuple[ReasonCode, ...]:
        out: list[ReasonCode] = []
        if self.not_started_reason is not None:
            out.append(self.not_started_reason)
        for item in self.unmet:
            if item.reason_code is not None and item.reason_code not in out:
                out.append(item.reason_code)
        return tuple(out)

    def as_real_hardware_verdict(self):
        """**호출하면 예외다.** 시뮬레이터 결과를 실기 판정으로 바꾸지 않는다."""
        raise SimulationVerdictError(
            "시뮬레이터 이송 결과를 실기 실행 판정으로 승격할 수 없다."
            " 실기 판정은 장착 근거·커플링 질량·실기 파지 관측을 요구하고,"
            " 그 판정은 validation/pick_place_gate.py가 한다")

    def to_dict(self) -> dict:
        return {
            "schema": "forstick2.simulation_transfer/1",
            "scenario": self.scenario,
            "object_id": self.object_id,
            "support_id": self.support_id,
            "target_id": self.target_id,
            # ── 두 축을 항상 함께 적는다 ───────────────────────────────
            "is_simulated": True,
            "simulation_e2e": self.simulation_e2e,
            "real_hardware_verified": False,
            "real_hardware_ready": False,
            "grasp_observation_kind": self.grasp_observation_kind,
            "status": self.status.value,
            # ── 근거 ──────────────────────────────────────────────────
            "criteria": [item.to_dict() for item in self.criteria],
            "unmet": [item.key for item in self.unmet],
            "reason_codes": [code.value for code in self.reason_codes],
            "not_started_reason": (None if self.not_started_reason is None
                                   else self.not_started_reason.value),
            "stop_requested": self.stop_requested,
            "stop_confirmed": self.stop_confirmed,
            "fixture_events": [dict(item) for item in self.fixture_events],
            "object_pose_timeline": [dict(item)
                                     for item in self.object_pose_timeline],
            "stage_records": [dict(item) for item in self.stage_records],
            "plan_validation": (None if self.plan_validation is None
                                else dict(self.plan_validation)),
            "detail": self.detail,
            "limitations": list(self.limitations),
            "note": "시뮬레이터 고정 장치로 물체를 옮긴 결과다. 실제 마찰 파지·"
                    "물체 감지가 아니다. 실제 로봇 pick/place 가능 판정으로"
                    " 쓰지 않는다",
        }


def not_started(
    *, scenario: str, object_id: str, support_id: str, target_id: str,
    reason: ReasonCode, detail: str,
    limitations: Sequence[str] = (),
) -> SimulationTransferResult:
    """시작 전 조건이 맞지 않아 시작하지 않은 결과. 조건 목록은 비어 있다."""
    return SimulationTransferResult(
        scenario=scenario, object_id=object_id, support_id=support_id,
        target_id=target_id, criteria=(), not_started_reason=reason,
        detail=detail, limitations=tuple(limitations),
    )


def placement_zone(
    *, target_center_m: Sequence[float], surface_half_extent_m: Sequence[float],
    object_size_m: Sequence[float], surface_top_z_m: float,
    z_tolerance_m: float,
) -> dict:
    """배치 허용 구역을 **선언된 치수에서** 만든다. 값을 고르지 않는다.

    규칙: 물체 중심이 놓일 면 안쪽으로 물체 반폭만큼 들어와 있어야 하고,
    중심 높이는 면 위 물체 절반 높이에서 허용치 안이어야 한다.
    """
    cx, cy, _ = (float(v) for v in target_center_m)
    hx, hy = (float(v) for v in surface_half_extent_m[:2])
    sx, sy, sz = (float(v) for v in object_size_m)
    return {
        "x_min_m": cx - (hx - sx / 2), "x_max_m": cx + (hx - sx / 2),
        "y_min_m": cy - (hy - sy / 2), "y_max_m": cy + (hy - sy / 2),
        "z_center_m": surface_top_z_m + sz / 2,
        "z_tolerance_m": float(z_tolerance_m),
        "rule": "물체 중심이 놓일 면 안쪽으로 반폭만큼 들어오고, 중심 높이가"
                " 면 위 절반 높이에서 허용치 안에 있어야 한다",
    }


def in_placement_zone(pose_m: Sequence[float], zone: Mapping[str, Any]) -> tuple[bool, str]:
    """물체 중심이 구역 안인가. 밖이면 어느 축이 벗어났는지 말한다."""
    x, y, z = (float(v) for v in pose_m[:3])
    out: list[str] = []
    if not zone["x_min_m"] <= x <= zone["x_max_m"]:
        out.append(f"x={x:.4f} ∉ [{zone['x_min_m']:.4f}, {zone['x_max_m']:.4f}]")
    if not zone["y_min_m"] <= y <= zone["y_max_m"]:
        out.append(f"y={y:.4f} ∉ [{zone['y_min_m']:.4f}, {zone['y_max_m']:.4f}]")
    if abs(z - zone["z_center_m"]) > zone["z_tolerance_m"]:
        out.append(f"z={z:.4f} (기대 {zone['z_center_m']:.4f}"
                   f" ± {zone['z_tolerance_m']:.4f})")
    if out:
        return False, "구역 밖: " + " · ".join(out)
    return True, (f"구역 안 (x={x:.4f} y={y:.4f} z={z:.4f},"
                  f" 기대 z={zone['z_center_m']:.4f})")
