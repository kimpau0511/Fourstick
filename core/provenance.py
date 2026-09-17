"""수치의 근거 (md/개발플랜.md 8-02).

계획.md 27장은 로봇 수치를 코드에 두지 못하게 한다. 8단계는 한 걸음 더
요구한다 — **값마다 근거를 붙인다.**

한 수치는 다음을 함께 갖는다.

- 값 (없으면 None)
- SI 단위
- 출처 문서 또는 URDF
- 출처 버전·commit
- 확인 상태
- 확인 시각(UTC)

`value=None`은 "아직 근거가 없다"는 뜻이다. **기본값·추정값·다른 로봇 값을
넣지 않는다.** 없으면 없는 대로 두고, 검증기가 정보 부족(ASK)으로 처리한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class ProvenanceError(Exception):
    """근거가 계약을 만족하지 못할 때."""


class VerificationStatus(str, Enum):
    """값의 확인 상태.

    - `verified`: 공식 자료(문서·URDF)에서 값을 확인했다.
    - `declared`: 자료에 적혀 있으나 **실기·시뮬레이터로 확인하지 않았다.**
    - `unverified`: 출처는 있으나 값 확인이 끝나지 않았다.
    - `unavailable`: 근거가 없다. 값도 없다.
    """

    VERIFIED = "verified"
    DECLARED = "declared"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"

    def __str__(self) -> str:
        return self.value


#: 값이 있다고 볼 수 있는 상태. `unavailable`은 값이 없어야 한다.
HAS_VALUE_STATUSES = (
    VerificationStatus.VERIFIED,
    VerificationStatus.DECLARED,
    VerificationStatus.UNVERIFIED,
)


@dataclass(frozen=True)
class Provenance:
    """값 하나의 출처."""

    #: 출처 종류: "datasheet" / "urdf" / "ros_package" / "cad" / "measurement" 등.
    source_kind: str
    #: 문서 이름·파일 경로·패키지 이름. 사람이 찾아갈 수 있는 값이어야 한다.
    source: str
    #: 출처의 버전. 태그·패키지 버전·문서 판번호.
    source_version: str = ""
    #: 출처의 commit 또는 checksum. 같은 버전 안에서도 내용을 고정한다.
    source_commit: str = ""
    #: 확인 상태.
    status: VerificationStatus = VerificationStatus.UNAVAILABLE
    #: 확인 시각(UTC epoch 초). 0이면 확인하지 않았다.
    checked_at: float = 0.0
    #: 사람이 읽을 메모(무엇이 왜 미확보인지).
    note: str = ""

    def __post_init__(self) -> None:
        if self.status is not VerificationStatus.UNAVAILABLE:
            if not self.source_kind or not self.source:
                raise ProvenanceError(
                    f"{self.status} 상태인데 출처가 없다 — 출처 없는 값은"
                    " unavailable이어야 한다"
                )
            if self.checked_at <= 0:
                raise ProvenanceError(
                    f"{self.status} 상태인데 확인 시각이 없다"
                )

    def to_dict(self) -> dict:
        return {
            "source_kind": self.source_kind,
            "source": self.source,
            "source_version": self.source_version,
            "source_commit": self.source_commit,
            "status": self.status.value,
            "checked_at": self.checked_at,
            "note": self.note,
        }


#: 근거가 아예 없는 값에 붙이는 표시.
UNAVAILABLE = Provenance(
    source_kind="", source="", status=VerificationStatus.UNAVAILABLE,
    note="공식 근거 미확보",
)


@dataclass(frozen=True)
class Measured:
    """근거가 붙은 수치 하나. 값이 없을 수 있다."""

    value: float | None
    #: SI 단위. 값이 있으면 반드시 있어야 한다(단위를 추정하지 않는다).
    unit: str
    provenance: Provenance = UNAVAILABLE

    def __post_init__(self) -> None:
        if self.value is None:
            if self.provenance.status is not VerificationStatus.UNAVAILABLE:
                raise ProvenanceError(
                    f"값이 없는데 상태가 {self.provenance.status}다"
                    " — 값 없는 항목은 unavailable이어야 한다"
                )
            return
        if not self.unit:
            raise ProvenanceError(
                "값이 있는데 단위가 없다 — 단위를 추정하지 않는다"
            )
        if self.provenance.status is VerificationStatus.UNAVAILABLE:
            raise ProvenanceError(
                "값이 있는데 근거가 unavailable이다 — 근거 없는 값을 두지 않는다"
            )

    @property
    def available(self) -> bool:
        return self.value is not None

    @property
    def status(self) -> VerificationStatus:
        return self.provenance.status

    def require(self, label: str) -> float:
        """값을 꺼낸다. 없으면 막는다 — 기본값으로 대체하지 않는다."""
        if self.value is None:
            raise ProvenanceError(f"{label} 값이 없다(근거 미확보)")
        return self.value

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "unit": self.unit,
            "provenance": self.provenance.to_dict(),
        }


def unavailable(unit: str, note: str = "공식 근거 미확보") -> Measured:
    """값 없는 항목을 만든다. 0이나 임의값을 넣지 않기 위한 통로."""
    return Measured(
        value=None, unit=unit,
        provenance=Provenance(
            source_kind="", source="", status=VerificationStatus.UNAVAILABLE,
            note=note,
        ),
    )


def measured(
    value: float, unit: str, *, source_kind: str, source: str,
    source_version: str = "", source_commit: str = "",
    status: VerificationStatus = VerificationStatus.DECLARED,
    checked_at: float, note: str = "",
) -> Measured:
    return Measured(
        value=value, unit=unit,
        provenance=Provenance(
            source_kind=source_kind, source=source, source_version=source_version,
            source_commit=source_commit, status=status, checked_at=checked_at,
            note=note,
        ),
    )


def missing_items(values: Mapping[str, Measured]) -> tuple[str, ...]:
    """값이 없는 항목 이름. 검증기가 ASK 사유로 쓴다."""
    return tuple(sorted(name for name, m in values.items() if not m.available))


def weakest_status(values: Mapping[str, Measured]) -> VerificationStatus:
    """가장 약한 확인 상태. 하나라도 미확보면 전체가 미확보다."""
    order = [
        VerificationStatus.UNAVAILABLE,
        VerificationStatus.UNVERIFIED,
        VerificationStatus.DECLARED,
        VerificationStatus.VERIFIED,
    ]
    if not values:
        return VerificationStatus.UNAVAILABLE
    return min((m.status for m in values.values()), key=order.index)


def as_dict(values: Mapping[str, Measured]) -> dict[str, Any]:
    return {name: m.to_dict() for name, m in values.items()}
