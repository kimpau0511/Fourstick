"""실기 전환에 필요한 입력 한 건의 계약 (md/개발플랜.md 8-12).

시뮬레이터 선언값과 **같은 자리에 두지 않는다.** 여기 담기는 값은 실제 장착·
연결 전에 사람이 도면·저울·측정으로 가져와야 하는 것들이고, 각 값에는 일곱
필드가 **반드시** 있어야 한다.

| 필드 | 뜻 |
|---|---|
| `value` | 값. 없으면 `None`이고 그 사실이 판정에 들어간다 |
| `unit` | SI 단위. 값이 있으면 비어 있을 수 없다 |
| `source` | 사람이 찾아갈 수 있는 출처(도면 번호·문서·측정 기록) |
| `measurement_method` | 어떻게 얻었는가(`drawing`·`scale`·`cad`·`photo`…) |
| `captured_at` | 언제 얻었는가(ISO 8601) |
| `verified` | 확인이 끝났는가 |
| `evidence_path` | 근거 파일 경로(사진·도면·측정 기록) |
| `operator` | **누가 측정했는가.** 측정값에 사람이 붙어 있어야 되짚을 수 있다 |

값의 성격에 따라 두 필드가 더 붙는다.

| 필드 | 언제 |
|---|---|
| `evidence_requirements` | 근거에 **무엇이 보여야 하는가**(예: 핀 구멍과 기준 방향) |
| `derivation` | 계산으로 얻은 값의 **검증 가능한 산출 근거**(쓴 공식과 입력값) |

## 이 계약이 거부하는 것

- 값만 있고 출처·방법·시각·작업자·근거가 없는 입력 → 값이 있어도 `verified`가
  될 수 없다
- `verified=true`인데 근거 파일이나 작업자가 없는 입력 → 계약 위반으로 거부한다
- **시뮬레이터에서 온 값** → `measurement_method`가 시뮬레이터 계열이면
  `verified`가 될 수 없다. 시뮬레이션은 실기 근거가 아니다
- **선언만 있는 값**(`declared`) → 측정이 아니므로 실기 조건을 채우지 못한다
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

#: ISO 8601 날짜 또는 날짜시각. 사람이 언제 쟀는지 알 수 있어야 한다.
CAPTURED_AT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?([+-]\d{2}:?\d{2}|Z)?)?$")


class HardwareInputError(Exception):
    """입력이 계약을 만족하지 못할 때."""


class MeasurementMethod(str, Enum):
    """값을 어떻게 얻었는가. **실기 근거가 되는 것과 아닌 것을 나눈다.**"""

    #: 아직 얻지 않았다.
    NONE = "none"
    #: 제조사 도면·데이터시트에 적힌 값(치수·규격).
    DRAWING = "drawing"
    #: 제조사 CAD/공식 모델에서 산출한 값.
    CAD = "cad"
    #: 저울 실측.
    SCALE = "scale"
    #: 자·캘리퍼·게이지 실측.
    GAUGE = "gauge"
    #: 조립 사진에서 확인(핀 위치·방향).
    PHOTO = "photo"
    #: 로봇·센서 관측(TCP 측정, 프레임 보정).
    ROBOT_MEASUREMENT = "robot_measurement"
    #: 공식 문서에 적힌 값이지만 실기로 확인하지 않았다.
    DECLARED = "declared"
    #: 시뮬레이터 설정·URDF에서 온 값. **실기 근거가 아니다.**
    SIMULATION = "simulation"
    #: 시뮬레이터 관측. **실기 근거가 아니다.**
    SIMULATED_OBSERVATION = "simulated_observation"

    def __str__(self) -> str:
        return self.value


#: 실기 근거로 인정하는 측정 방법. 이 목록에 없으면 `verified`가 될 수 없다.
HARDWARE_EVIDENCE_METHODS: frozenset[MeasurementMethod] = frozenset({
    MeasurementMethod.DRAWING,
    MeasurementMethod.CAD,
    MeasurementMethod.SCALE,
    MeasurementMethod.GAUGE,
    MeasurementMethod.PHOTO,
    MeasurementMethod.ROBOT_MEASUREMENT,
})

#: 시뮬레이터에서 온 방법. 실기 조건을 절대 채우지 못한다.
SIMULATION_METHODS: frozenset[MeasurementMethod] = frozenset({
    MeasurementMethod.SIMULATION,
    MeasurementMethod.SIMULATED_OBSERVATION,
})


@dataclass(frozen=True)
class HardwareInput:
    """실기 입력 한 건. 일곱 필드를 모두 갖는다."""

    key: str
    label: str
    #: 값. 숫자·문자열·리스트·매핑을 허용한다(transform은 6개 값이다).
    value: Any = None
    unit: str = ""
    source: str = ""
    measurement_method: MeasurementMethod = MeasurementMethod.NONE
    captured_at: str = ""
    verified: bool = False
    evidence_path: str = ""
    #: 사람이 무엇을 가져와야 하는지. 누락 판정에 그대로 실려 나간다.
    needed: str = ""
    #: 누가 측정했는가. **측정값에는 사람이 붙어 있어야 한다.**
    operator: str = ""
    #: 근거에 무엇이 보여야 하는가. 사람이 수집할 때 그대로 확인표로 쓴다.
    evidence_requirements: tuple[str, ...] = ()
    #: 계산으로 얻은 값의 산출 근거(쓴 공식과 입력값). 계산값이면 필요하다.
    derivation: str = ""
    #: 값이 필요 없는 항목인가(예: 연결 방식은 문자열 선택). 기본은 필요하다.
    numeric: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if not self.key or not self.label:
            raise HardwareInputError("입력에는 key와 label이 있어야 한다")
        if not isinstance(self.measurement_method, MeasurementMethod):
            raise HardwareInputError(
                f"{self.key}: measurement_method가 계약 값이 아니다:"
                f" {self.measurement_method!r}")
        if self.value is not None and self.numeric and not self.unit:
            raise HardwareInputError(
                f"{self.key}: 값이 있는데 단위가 없다 — 단위를 추정하지 않는다")
        if self.captured_at and not CAPTURED_AT_PATTERN.match(self.captured_at):
            raise HardwareInputError(
                f"{self.key}: captured_at이 ISO 8601이 아니다:"
                f" {self.captured_at!r}")
        if not self.verified:
            return
        # 여기서부터는 `verified=true`가 성립하는지 본다.
        if self.value is None:
            raise HardwareInputError(
                f"{self.key}: 값이 없는데 verified=true일 수 없다")
        if self.measurement_method in SIMULATION_METHODS:
            raise HardwareInputError(
                f"{self.key}: 시뮬레이터에서 온 값"
                f"({self.measurement_method})을 verified로 둘 수 없다 —"
                " 시뮬레이션은 실기 근거가 아니다")
        if self.measurement_method not in HARDWARE_EVIDENCE_METHODS:
            raise HardwareInputError(
                f"{self.key}: {self.measurement_method}는 실기 근거 방법이"
                " 아니다 — verified로 둘 수 없다")
        for name in ("source", "captured_at", "evidence_path", "operator"):
            if not getattr(self, name):
                raise HardwareInputError(
                    f"{self.key}: verified=true인데 {name}가 없다")

    # ── 판정 ────────────────────────────────────────────────────────────
    @property
    def has_value(self) -> bool:
        return self.value is not None

    @property
    def from_simulation(self) -> bool:
        return self.measurement_method in SIMULATION_METHODS

    @property
    def declared_only(self) -> bool:
        """공식 문서에 적혀 있으나 측정이 아니다."""
        return self.measurement_method is MeasurementMethod.DECLARED

    @property
    def has_evidence(self) -> bool:
        """출처·근거 파일·시각·작업자가 모두 있는가."""
        return bool(self.source and self.evidence_path and self.captured_at
                    and self.operator)

    @property
    def ready(self) -> bool:
        """실기 조건을 채우는가. **verified만으로는 부족하지 않다** — 불변식이
        이미 근거·방법·시각을 요구했으므로 여기서는 verified가 곧 준비다."""
        return bool(self.verified)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "measurement_method": self.measurement_method.value,
            "captured_at": self.captured_at,
            "verified": self.verified,
            "evidence_path": self.evidence_path,
            "operator": self.operator,
            "evidence_requirements": list(self.evidence_requirements),
            "derivation": self.derivation,
            "needed": self.needed,
            "note": self.note,
            "has_value": self.has_value,
            "has_evidence": self.has_evidence,
            "from_simulation": self.from_simulation,
            "declared_only": self.declared_only,
            "ready": self.ready,
        }


def load_input(payload: Mapping[str, Any]) -> HardwareInput:
    """설정 한 줄을 입력으로 읽는다. **없는 필드를 채우지 않는다.**"""
    missing = [name for name in ("key", "label") if not payload.get(name)]
    if missing:
        raise HardwareInputError(f"입력에 {', '.join(missing)}가 없다")
    raw_method = str(payload.get("measurement_method") or "none")
    try:
        method = MeasurementMethod(raw_method)
    except ValueError:
        raise HardwareInputError(
            f"{payload['key']}: 모르는 측정 방법이다: {raw_method!r}"
            f" (허용: {[m.value for m in MeasurementMethod]})") from None
    return HardwareInput(
        key=str(payload["key"]),
        label=str(payload["label"]),
        value=payload.get("value"),
        unit=str(payload.get("unit") or ""),
        source=str(payload.get("source") or ""),
        measurement_method=method,
        captured_at=str(payload.get("captured_at") or ""),
        verified=bool(payload.get("verified")),
        evidence_path=str(payload.get("evidence_path") or ""),
        needed=str(payload.get("needed") or ""),
        operator=str(payload.get("operator") or ""),
        evidence_requirements=tuple(
            str(item) for item in (payload.get("evidence_requirements") or ())),
        derivation=str(payload.get("derivation") or ""),
        numeric=bool(payload.get("numeric", True)),
        note=str(payload.get("note") or ""),
    )


@dataclass(frozen=True)
class InputSet:
    """입력 묶음. 키로 찾고, 누락을 모아 본다."""

    profile_id: str
    profile_version: str
    inputs: tuple[HardwareInput, ...]
    source: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        keys = [item.key for item in self.inputs]
        if len(keys) != len(set(keys)):
            raise HardwareInputError(f"입력 키가 중복됐다: {keys}")

    def get(self, key: str) -> HardwareInput | None:
        for item in self.inputs:
            if item.key == key:
                return item
        return None

    @property
    def ready_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.inputs if item.ready)

    @property
    def pending_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.inputs if not item.ready)

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "source": self.source,
            "inputs": [item.to_dict() for item in self.inputs],
            "ready": list(self.ready_keys),
            "pending": list(self.pending_keys),
            "extras": dict(self.extras),
        }
