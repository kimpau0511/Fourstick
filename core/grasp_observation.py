"""파지 상태 관측 계약 `grasp.object_held` (md/개발플랜.md 8-10).

지금 이 저장소에는 **물체를 쥐었는지 관측할 수단이 없다.** 그래서 값이 없다.
값이 없는 것을 나중에 채울 수 있도록 **계약만** 여기에 둔다.

## 왜 개구로 대신할 수 없는가

2F-85 개구는 관측으로 확인된다(명령 관절값 + 공식 FK 개구 표, 오차 µm대).
하지만 개구가 목표와 맞는다는 것은 **손가락이 그 간격에 있다**는 뜻일 뿐이다.
물체가 그 사이에 있는지, 미끄러지지 않는지, 이미 떨어졌는지는 말해 주지 않는다.
빈손으로 30 mm를 만들어도 개구는 30 mm다.

그래서 이 계약은 개구에서 파지 상태를 **유도하는 경로를 만들지 않는다.**
`held`가 참이 되려면 관측이 있어야 하고, 관측에는 **어느 물체를 쥐었는지**가
따라온다(`object_id`). 개구 측정에는 물체 식별자가 없으므로 개구만으로는
이 형식을 만들 수 없다 — 규약이 아니라 구조로 막는다.

## 세 가지 상태

| availability | 뜻 | held |
|---|---|---|
| `unavailable` | 관측 수단이 없다 | 반드시 None |
| `simulated_observation` | 시뮬레이터가 알려준 값 | bool |
| `measured` | 실기 센서 관측 | bool |

`simulated_observation`은 **실기 검증으로 승격되지 않는다.** 관문 조건을
충족시키는 것은 `measured`뿐이다(`counts_for_gate`).

## 실기 관측이 더 요구하는 것 (8-12)

실기 전환 준비 관문(`validation/hardware_readiness.py`)은 `measured`만으로도
부족하다고 본다. 실기 관측에는 다음이 **모두** 있어야 한다.

| 항목 | 왜 |
|---|---|
| `observed_at` | 언제 읽은 값인가 |
| `robot_id` · `gripper_id` | 어느 장비에서 읽었는가 |
| `object_id` | 무엇을 쥐었다고 말하는가 |
| `method` | 어떤 신호로 판정했는가 |
| `raw_state` | 장치가 준 **원본 값**(레지스터·비트·토픽 값) |
| `held` | 판정 결과 |

`raw_state`가 없으면 나중에 그 판정이 옳았는지 되짚을 수 없다. 그래서
`hardware_complete`가 False이고 실기 조건을 채우지 못한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

#: 관측 항목 이름. 기록·화면·보고서가 같은 이름을 쓴다.
OBSERVATION_KEY = "grasp.object_held"


class GraspAvailability(str, Enum):
    """관측을 할 수 있는가, 했다면 무엇으로 했는가."""

    #: 수단이 없다. `held`는 None이어야 한다.
    UNAVAILABLE = "unavailable"
    #: 시뮬레이터 관측. **실기 검증이 아니다.**
    SIMULATED = "simulated_observation"
    #: 실기 센서 관측.
    MEASURED = "measured"

    def __str__(self) -> str:
        return self.value


class GraspObservationError(Exception):
    pass


@dataclass(frozen=True)
class GraspObservation:
    """`grasp.object_held` 관측 한 건.

    불변식으로 지킨다.

    1. `unavailable`이면 `held`는 None이고 관측 시각도 없다 — 없는 관측에
       시각을 붙이지 않는다.
    2. 관측했다면 `held`는 bool이고, **어느 물체인지**와 관측 시각·방법이
       있어야 한다. 물체 식별자 없는 관측은 파지 관측이 아니다.
    3. `method`에 개구(aperture)만 적힌 관측은 거부한다 — 개구는 파지 근거가
       아니다.
    """

    availability: GraspAvailability
    held: bool | None = None
    object_id: str | None = None
    observed_at: float | None = None
    source: str = ""
    method: str = ""
    detail: str = ""
    evidence: Mapping[str, Any] | None = None
    #: 어느 장비에서 읽었는가. 실기 관측은 둘 다 있어야 한다(8-12).
    robot_id: str = ""
    gripper_id: str = ""
    #: 장치가 준 **원본 상태**(레지스터·비트·토픽 값). 실기 관측 필수.
    raw_state: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.availability, GraspAvailability):
            raise GraspObservationError(
                f"availability가 계약 값이 아니다: {self.availability!r}")
        if self.availability is GraspAvailability.UNAVAILABLE:
            if self.held is not None:
                raise GraspObservationError(
                    "관측 수단이 없는데 파지 여부를 적을 수 없다")
            if self.observed_at is not None:
                raise GraspObservationError(
                    "관측하지 않았는데 관측 시각을 붙일 수 없다")
            if self.object_id is not None:
                raise GraspObservationError(
                    "관측하지 않았는데 물체를 지목할 수 없다")
            return
        if not isinstance(self.held, bool):
            raise GraspObservationError(
                f"관측했다면 파지 여부는 bool이어야 한다: {self.held!r}")
        if not self.object_id:
            raise GraspObservationError(
                "파지 관측에는 어느 물체를 쥐었는지가 따라와야 한다"
                " — 물체 식별자 없는 값은 파지 관측이 아니다")
        if not self.observed_at or self.observed_at <= 0:
            raise GraspObservationError("관측 시각이 없다")
        if not self.source or not self.method:
            raise GraspObservationError("관측 출처와 방법이 있어야 한다")
        lowered = self.method.lower()
        if "aperture" in lowered and "object" not in lowered:
            raise GraspObservationError(
                "개구 관측을 파지 관측으로 쓸 수 없다 — 개구가 목표와 맞는"
                " 것은 물체를 쥐었다는 뜻이 아니다")

    @property
    def available(self) -> bool:
        return self.availability is not GraspAvailability.UNAVAILABLE

    @property
    def simulated(self) -> bool:
        return self.availability is GraspAvailability.SIMULATED

    @property
    def hardware_gaps(self) -> tuple[str, ...]:
        """실기 관측으로 인정받기에 빠진 항목. 없으면 빈 tuple이다."""
        gaps: list[str] = []
        if self.availability is not GraspAvailability.MEASURED:
            gaps.append("availability=measured")
        if not self.robot_id:
            gaps.append("robot_id")
        if not self.gripper_id:
            gaps.append("gripper_id")
        if not self.object_id:
            gaps.append("object_id")
        if not self.method:
            gaps.append("method")
        if not self.raw_state:
            gaps.append("raw_state")
        if not self.observed_at:
            gaps.append("observed_at")
        return tuple(gaps)

    @property
    def hardware_complete(self) -> bool:
        """실기 준비 관문이 요구하는 항목을 모두 갖췄는가(8-12)."""
        return not self.hardware_gaps

    @property
    def counts_for_gate(self) -> bool:
        """pick/place 관문의 파지 관측 조건을 충족시키는가.

        **실기 관측만** 충족시킨다. 시뮬레이터 관측은 무엇이 참인지 알려
        주지만 실기 검증으로 승격되지 않는다.
        """
        return self.availability is GraspAvailability.MEASURED

    def to_dict(self) -> dict:
        return {
            "key": OBSERVATION_KEY,
            "availability": self.availability.value,
            "held": self.held,
            "object_id": self.object_id,
            "observed_at": self.observed_at,
            "source": self.source,
            "method": self.method,
            "detail": self.detail,
            "simulated": self.simulated,
            "counts_for_gate": self.counts_for_gate,
            "robot_id": self.robot_id,
            "gripper_id": self.gripper_id,
            "raw_state": None if self.raw_state is None else dict(self.raw_state),
            "hardware_complete": self.hardware_complete,
            "hardware_gaps": list(self.hardware_gaps),
            "evidence": None if self.evidence is None else dict(self.evidence),
        }


def unavailable(*, detail: str, source: str = "") -> GraspObservation:
    """관측 수단이 없을 때의 기록. `held`는 None으로 남는다."""
    return GraspObservation(
        availability=GraspAvailability.UNAVAILABLE,
        source=source,
        method="none",
        detail=detail,
    )


def simulated(
    *, held: bool, object_id: str, observed_at: float, source: str,
    method: str, detail: str = "", evidence: Mapping[str, Any] | None = None,
) -> GraspObservation:
    """시뮬레이터 관측. **`simulated_observation`으로 구분해 남긴다.**

    Gazebo 접촉·부착 상태를 읽는 구현이 붙으면 이 경로로 들어온다. 값이
    있어도 관문 조건은 충족되지 않는다 — 실기 관측과 같은 성공으로 세지 않는다.
    """
    return GraspObservation(
        availability=GraspAvailability.SIMULATED,
        held=held, object_id=object_id, observed_at=observed_at,
        source=source, method=method, detail=detail, evidence=evidence,
    )


def measured(
    *, held: bool, object_id: str, observed_at: float, source: str,
    method: str, detail: str = "", evidence: Mapping[str, Any] | None = None,
) -> GraspObservation:
    """실기 센서 관측. 관문 조건을 충족시키는 유일한 경로다."""
    return GraspObservation(
        availability=GraspAvailability.MEASURED,
        held=held, object_id=object_id, observed_at=observed_at,
        source=source, method=method, detail=detail, evidence=evidence,
    )


def measured_from_device(
    *, held: bool, object_id: str, observed_at: float, source: str,
    method: str, robot_id: str, gripper_id: str,
    raw_state: Mapping[str, Any], detail: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> GraspObservation:
    """실기 장치 관측. **실기 준비 관문이 요구하는 항목을 모두 받는다.**

    `raw_state`가 비어 있으면 거부한다 — 원본 값이 없으면 나중에 그 판정이
    옳았는지 되짚을 수 없다.
    """
    if not raw_state:
        raise GraspObservationError(
            "실기 파지 관측에는 장치가 준 원본 상태(raw_state)가 있어야 한다"
            " — 없으면 판정을 되짚을 수 없다")
    if not robot_id or not gripper_id:
        raise GraspObservationError(
            "실기 파지 관측에는 어느 로봇·그리퍼에서 읽었는지가 있어야 한다")
    return GraspObservation(
        availability=GraspAvailability.MEASURED,
        held=held, object_id=object_id, observed_at=observed_at,
        source=source, method=method, detail=detail, evidence=evidence,
        robot_id=robot_id, gripper_id=gripper_id, raw_state=dict(raw_state),
    )


@runtime_checkable
class GraspObserver(Protocol):
    """파지 상태 관측자. 어댑터가 제공할 수 있다(지금은 없다).

    `observe`는 **관측하지 못했으면 `unavailable`을 돌려준다.** 예외를 던져
    호출자가 "모름"을 성공으로 오해하게 두지 않는다.
    """

    def observe(self, object_id: str | None) -> GraspObservation: ...
