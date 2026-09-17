"""개구(그리퍼 패드 간격) 모델 (md/개발플랜.md 8-08).

**관절각과 패드 간격을 선형 동일값으로 취급하지 않는다.** 관계는 비선형이고
(선형 가정은 최대 2.8 mm 틀린다), 공식 URDF 기구학으로 계산한 표를 주입받아
보간한다. 표를 만드는 쪽은 `scripts/analyze_mounting_interface.py`이고, 이
모듈은 **표의 내용을 만들지 않는다**.

이 모듈이 지키는 것:

- 표 범위를 벗어난 관절값·개구는 **추정하지 않는다**(ASK로 돌아갈 수 있게 None).
- 보간 오차 상한을 함께 돌려준다. 관측 판정 허용치는 그 오차보다 커야 한다.
- 관절 effort·모터 전류를 개구나 파지력으로 바꾸지 않는다. 이 모듈은 위치만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from core.reason_codes import ReasonCode


class ApertureError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class AperturePoint:
    """표의 한 점. 관절값과 그때의 패드 간격·TCP z."""

    joint_rad: float
    aperture_m: float
    tcp_z_m: float | None = None


@dataclass(frozen=True)
class ApertureModel:
    """관절값 ↔ 개구 변환. 표를 보간한다.

    `source`는 표가 어디서 왔는지다(공식 자산·버전·commit). 판정 기록에 남는다.
    """

    points: tuple[AperturePoint, ...]
    source: str
    #: 표의 관절값이 단조 증가여야 한다. 개구는 단조 감소를 기대한다.
    def __post_init__(self) -> None:
        if len(self.points) < 2:
            raise ApertureError(ReasonCode.CONFIG_MISSING, "개구 표에 점이 부족하다")
        if not self.source:
            raise ApertureError(ReasonCode.CONFIG_MISSING, "개구 표의 출처가 없다")
        joints = [point.joint_rad for point in self.points]
        if joints != sorted(joints):
            raise ApertureError(
                ReasonCode.CONFIG_INVALID, "개구 표의 관절값이 정렬돼 있지 않다"
            )
        if len(set(joints)) != len(joints):
            raise ApertureError(ReasonCode.CONFIG_INVALID, "개구 표에 중복 관절값이 있다")

    @classmethod
    def from_rows(cls, rows: Sequence[Mapping], *, source: str) -> "ApertureModel":
        """`analyze_mounting_interface.py`가 만든 행을 읽는다."""
        points = []
        for row in rows:
            if row.get("joint_value_rad") is None or row.get("pad_gap_m") is None:
                raise ApertureError(
                    ReasonCode.CONFIG_INVALID, f"개구 표 행에 값이 없다: {row}"
                )
            points.append(AperturePoint(
                joint_rad=float(row["joint_value_rad"]),
                aperture_m=float(row["pad_gap_m"]),
                tcp_z_m=(None if row.get("tcp_pad_center_z_m") is None
                         else float(row["tcp_pad_center_z_m"])),
            ))
        points.sort(key=lambda point: point.joint_rad)
        return cls(points=tuple(points), source=source)

    @property
    def joint_range_rad(self) -> tuple[float, float]:
        return (self.points[0].joint_rad, self.points[-1].joint_rad)

    @property
    def aperture_range_m(self) -> tuple[float, float]:
        values = [point.aperture_m for point in self.points]
        return (min(values), max(values))

    @property
    def max_interpolation_error_m(self) -> float:
        """보간 오차 상한.

        표의 각 점을 이웃 두 점의 선형 보간과 비교한 최대 차이다. 표가 촘촘할수록
        작아진다. 관측 허용치는 이 값보다 커야 한다.
        """
        worst = 0.0
        for index in range(1, len(self.points) - 1):
            before, middle, after = self.points[index - 1:index + 2]
            span = after.joint_rad - before.joint_rad
            if span <= 0:
                continue
            ratio = (middle.joint_rad - before.joint_rad) / span
            straight = before.aperture_m + ratio * (after.aperture_m - before.aperture_m)
            worst = max(worst, abs(middle.aperture_m - straight))
        return worst

    def aperture_for(self, joint_rad: float) -> float | None:
        """관절값 → 개구. 표 범위를 벗어나면 None(추정하지 않는다)."""
        low, high = self.joint_range_rad
        if joint_rad < low or joint_rad > high:
            return None
        for index in range(len(self.points) - 1):
            left, right = self.points[index], self.points[index + 1]
            if left.joint_rad <= joint_rad <= right.joint_rad:
                span = right.joint_rad - left.joint_rad
                if span == 0:
                    return left.aperture_m
                ratio = (joint_rad - left.joint_rad) / span
                return left.aperture_m + ratio * (right.aperture_m - left.aperture_m)
        return None

    def observe(self, joint_rad: float, *, edge_tolerance_rad: float = 0.0) -> dict:
        """관측값을 개구로 바꾼다. 표 끝의 미세 초과를 **명시적으로** 다룬다.

        실제 관절은 표 끝에서 부동소수 잡음이나 정지 오차만큼 벗어날 수 있다
        (실측: 0.8 + 7e-14). 그것을 "관측 불가"로 만들면 정상 동작이 막힌다.
        그래서 허용치 안의 초과는 **경계값으로 평가하고 그 사실을 기록**한다.
        허용치를 넘으면 값을 만들지 않는다(None).

        이것은 **입력을 범위로 잘라 통과시키는 것과 다르다** — 명령이 아니라
        관측을 해석하는 경로이며, 잘랐다는 사실이 결과에 남는다.
        """
        low, high = self.joint_range_rad
        offset = 0.0
        evaluated = joint_rad
        if joint_rad < low:
            offset = low - joint_rad
            evaluated = low
        elif joint_rad > high:
            offset = joint_rad - high
            evaluated = high
        if offset > edge_tolerance_rad:
            return {
                "aperture_m": None,
                "joint_rad": joint_rad,
                "at_table_edge": False,
                "edge_offset_rad": round(offset, 12),
                "edge_tolerance_rad": edge_tolerance_rad,
                "detail": "관절 관측값이 개구 표 범위를 벗어났다 — 값을 만들지 않는다",
            }
        return {
            "aperture_m": self.aperture_for(evaluated),
            "tcp_z_m": self.tcp_z_for(evaluated),
            "joint_rad": joint_rad,
            "evaluated_joint_rad": evaluated,
            "at_table_edge": offset > 0.0,
            "edge_offset_rad": round(offset, 12),
            "edge_tolerance_rad": edge_tolerance_rad,
        }

    def joint_for(self, aperture_m: float) -> float | None:
        """개구 → 관절값. 표 범위를 벗어나면 None."""
        low, high = self.aperture_range_m
        if aperture_m < low or aperture_m > high:
            return None
        for index in range(len(self.points) - 1):
            left, right = self.points[index], self.points[index + 1]
            lo, hi = sorted((left.aperture_m, right.aperture_m))
            if lo <= aperture_m <= hi:
                span = right.aperture_m - left.aperture_m
                if span == 0:
                    return left.joint_rad
                ratio = (aperture_m - left.aperture_m) / span
                return left.joint_rad + ratio * (right.joint_rad - left.joint_rad)
        return None

    def tcp_z_for(self, joint_rad: float) -> float | None:
        """관절값 → TCP z. 개구가 바뀌면 TCP도 움직인다."""
        low, high = self.joint_range_rad
        if joint_rad < low or joint_rad > high:
            return None
        for index in range(len(self.points) - 1):
            left, right = self.points[index], self.points[index + 1]
            if left.joint_rad <= joint_rad <= right.joint_rad:
                if left.tcp_z_m is None or right.tcp_z_m is None:
                    return None
                span = right.joint_rad - left.joint_rad
                if span == 0:
                    return left.tcp_z_m
                ratio = (joint_rad - left.joint_rad) / span
                return left.tcp_z_m + ratio * (right.tcp_z_m - left.tcp_z_m)
        return None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "points": len(self.points),
            "joint_range_rad": list(self.joint_range_rad),
            "aperture_range_m": list(self.aperture_range_m),
            "max_interpolation_error_m": round(self.max_interpolation_error_m, 8),
        }
