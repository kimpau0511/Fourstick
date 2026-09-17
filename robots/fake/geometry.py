"""개발용 기하 검사 구현체 (md/개발플랜.md 6-05).

**실제 로봇의 기구학·좌표·치수를 만들지 않는다.** 이 구현체는 좌표를 전혀 다루지
않고, 개발용 셀이 **기호로 선언한 사실**만 본다.

- 어느 위치가 도달 가능하다고 선언됐는가
- 어느 위치가 작업공간 밖이라고 선언됐는가
- 어떤 (물체, 위치) 조합이 충돌한다고 선언됐는가

선언되지 않은 위치가 계획에 나오면 **모른다고 답한다**(ASK). 선언을 확장해
추측하지 않는다. 그래서 이 구현체의 ALLOW는 "개발용 셀 선언 범위 안에서
검증했다"는 뜻이고, 그 사실은 판정에 남는 `validator_id`·snapshot으로 구분된다.

실제 셀(FR3-WMS / UR5e)의 기하 검증은 공식 URDF·MoveIt 자료를 확보한 뒤
별도 구현체로 붙인다. 이 파일은 그 자리를 대신하지 않는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from core.geometry import (
    EnvironmentSnapshot,
    GeometryDecision,
    GeometryReason,
    GeometryRequest,
    GeometryVerdict,
    snapshot_hash,
)
from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind

#: 계획에서 위치를 담는 인자 이름. 계약(`SKILL_ALLOWED_ARGS`)의 부분집합이다.
LOCATION_ARGS: tuple[str, ...] = ("to", "from")
OBJECT_ARGS: tuple[str, ...] = ("object",)


@dataclass(frozen=True)
class FakeCell:
    """개발용 셀 선언. 좌표가 아니라 기호 사실만 담는다."""

    cell_id: str
    cell_version: str
    #: 이 셀의 좌표계 이름. Profile의 frames에서 온 값을 그대로 받는다.
    frame_id: str
    #: 도달 가능하다고 선언된 위치.
    reachable_locations: frozenset[str]
    #: 작업공간 밖이라고 선언된 위치.
    blocked_locations: frozenset[str] = field(default_factory=frozenset)
    #: 충돌한다고 선언된 (물체, 위치) 조합.
    collisions: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_catalog(
        cls, catalog: ResourceCatalog, *, frame_id: str,
        cell_id: str = "dev_fake_cell", cell_version: str | None = None,
        blocked_locations: frozenset[str] = frozenset(),
        collisions: tuple[tuple[str, str], ...] = (),
    ) -> "FakeCell":
        """카탈로그에 등록된 위치를 도달 가능으로 선언한다.

        개발용이므로 카탈로그 = 셀 선언이다. 실제 셀에서는 이 관계가 성립하지
        않는다(등록된 위치가 도달 불가일 수 있다) — 그래서 실제 검증은 미완료로
        남는다.
        """
        locations = frozenset(catalog.ids_of_kind(ResourceKind.LOCATION))
        return cls(
            cell_id=cell_id,
            cell_version=cell_version or catalog.catalog_version,
            frame_id=frame_id,
            reachable_locations=locations - blocked_locations,
            blocked_locations=blocked_locations,
            collisions=collisions,
        )

    def content_hash(self, extra: tuple[str, ...] = ()) -> str:
        """셀 선언의 지문. 선언이 바뀌면 값이 달라진다."""
        parts = [
            self.cell_id, self.cell_version, self.frame_id,
            "reachable:" + ",".join(sorted(self.reachable_locations)),
            "blocked:" + ",".join(sorted(self.blocked_locations)),
            "collisions:" + ",".join(f"{o}@{l}" for o, l in sorted(self.collisions)),
            *extra,
        ]
        return snapshot_hash(parts)

    def snapshot(
        self, *, captured_at: float, ttl_sec: float, source: str = "dev_fake_adapter",
        extra: tuple[str, ...] = (),
    ) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            snapshot_id=self.cell_id,
            snapshot_version=self.cell_version,
            content_hash=self.content_hash(extra),
            frame_id=self.frame_id,
            captured_at=captured_at,
            ttl_sec=ttl_sec,
            source=source,
        )


class DevFakeGeometryValidator:
    """개발용 기하 검사. 선언된 사실만 본다.

    `GeometryValidator` 계약을 만족한다. 좌표 계산을 하지 않으므로 실제 충돌을
    보장하지 않는다 — `validator_id`가 개발용임을 밝히고, 기록에 그대로 남는다.
    """

    def __init__(self, cell: FakeCell, *, version: str = "1.0"):
        self._cell = cell
        self._version = version

    @property
    def validator_id(self) -> str:
        return "dev-fake-geometry"

    @property
    def validator_version(self) -> str:
        return self._version

    @property
    def cell(self) -> FakeCell:
        return self._cell

    def check(self, request: GeometryRequest) -> GeometryVerdict:
        started = request.checked_at
        t0 = time.monotonic()
        snapshot = request.snapshot
        cell = self._cell
        reasons: list[GeometryReason] = []
        unknown: list[str] = []
        checked_steps = 0

        for index, step in enumerate(request.plan.steps, start=1):
            locations = [
                str(step.args[name]) for name in LOCATION_ARGS if name in step.args
            ]
            objects = [
                str(step.args[name]) for name in OBJECT_ARGS if name in step.args
            ]
            if locations:
                checked_steps += 1
            for location in locations:
                if location in cell.blocked_locations:
                    reasons.append(GeometryReason(
                        ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                        f"스텝{index}: {location}은 셀 {cell.cell_id}"
                        f"({cell.cell_version})에서 작업공간 밖으로 선언됐다",
                    ))
                elif location not in cell.reachable_locations:
                    unknown.append(f"스텝{index}: {location}")
                for obj in objects:
                    if (obj, location) in cell.collisions:
                        reasons.append(GeometryReason(
                            ReasonCode.GEOMETRY_COLLISION,
                            f"스텝{index}: {obj}를 {location}에서 다루면 충돌로"
                            f" 선언돼 있다 (셀 {cell.cell_id})",
                        ))

        finished = started + max(0.0, time.monotonic() - t0)
        if unknown:
            # 선언에 없는 위치를 추측하지 않는다. 환경 정보 부족이다.
            return GeometryVerdict(
                decision=GeometryDecision.ASK,
                validator_id=self.validator_id,
                validator_version=self.validator_version,
                input_complete=False, started_at=started, finished_at=finished,
                reasons=(GeometryReason(
                    ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                    f"셀 선언에 없는 위치: {', '.join(unknown)}",
                ),),
                snapshot_id=None if snapshot is None else snapshot.snapshot_id,
                snapshot_version=None if snapshot is None else snapshot.snapshot_version,
                snapshot_hash=None if snapshot is None else snapshot.content_hash,
                frame_id=request.frame_id,
            )
        if reasons:
            return GeometryVerdict(
                decision=GeometryDecision.BLOCK,
                validator_id=self.validator_id,
                validator_version=self.validator_version,
                input_complete=True, started_at=started, finished_at=finished,
                reasons=tuple(reasons),
                snapshot_id=None if snapshot is None else snapshot.snapshot_id,
                snapshot_version=None if snapshot is None else snapshot.snapshot_version,
                snapshot_hash=None if snapshot is None else snapshot.content_hash,
                frame_id=request.frame_id,
                evidence={"checked_steps": checked_steps, "cell_id": cell.cell_id},
            )
        return GeometryVerdict(
            decision=GeometryDecision.ALLOW,
            validator_id=self.validator_id,
            validator_version=self.validator_version,
            input_complete=True, started_at=started, finished_at=finished,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_version=None if snapshot is None else snapshot.snapshot_version,
            snapshot_hash=None if snapshot is None else snapshot.content_hash,
            frame_id=request.frame_id,
            evidence={
                "checked_steps": checked_steps,
                "cell_id": cell.cell_id,
                "declared_reachable": len(cell.reachable_locations),
                # 좌표 계산 없이 선언만 대조했다는 사실을 판정에 남긴다.
                "method": "declared_symbols_only",
            },
        )
