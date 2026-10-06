"""표면 위 **빈 위치 후보** — 형상·점유로 거르고, 선택 근거를 남긴다.

"A자재를 작업대의 빈 곳에 놔"처럼 표면만 말한 놓기에서, 어디에 놓을지 **서버가** 정한다.
이 모듈은 로봇을 모른다(관절값·IK 없음). 여기서 통과한 후보도 아직 놓을 수 있는 자리가
아니다 — 로봇별 계산기가 IK·MoveIt(든 물체·손가락 포함)·경로로 다시 거른다.

후보를 거르는 규칙(값은 모두 셀 설정에서 온다):

1. **지지**: 자재 바닥면 전체가 표면 안에 있고, 가장자리와 `margin_m` 이상 떨어진다.
2. **고정 장애물**: 표면 위에 놓인 정적 부품(예: 팔레트 판)과 자재 바닥면이 `margin_m`
   이상 떨어진다. 표면 위에 놓였다는 판정은 부품 바닥 높이 = 표면 높이(1 mm 안)다.
3. **점유**: 관측된 다른 자재의 바닥면과 `margin_m` 이상 떨어진다. 관측이 없는 자재가
   있으면 점유를 확인할 수 없으므로 후보를 내지 않는다.

손가락 여유처럼 높이·방향이 걸린 판정은 여기서 하지 않는다(MoveIt이 한다). 여기서 거르면
실제로 놓을 수 있는 자리를 근거 없이 빼거나, 못 놓는 자리를 넣게 된다.

선택 순서(근거로 기록한다): ① 다른 자재와의 거리 큰 것 ② 고정 장애물·가장자리와의 거리
큰 것 ③ 로봇 base와 가까운 것 ④ 좌표 순. 같은 점유 배치면 언제나 같은 자리를 고른다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 표면 위에 놓였다고 보는 높이 차(m). 설정 좌표는 mm 단위로 적혀 있다.
RESTING_TOLERANCE_M = 0.001
#: 관측 자재가 표면 위에 있다고 보는 높이 차(m) — 자재 바닥과 표면 사이.
#: 기록↔관측 같은 자리 허용치(`ORIGIN_TOLERANCE_M`)와 같은 값.
OCCUPANT_HEIGHT_TOLERANCE_M = 0.02


@dataclass(frozen=True)
class Rect:
    """world xy 축정렬 사각형."""

    x0: float
    x1: float
    y0: float
    y1: float
    label: str = ""

    def gap(self, other: "Rect") -> float:
        """두 사각형 사이 거리(겹치면 음수 — 겹친 깊이의 최솟값)."""
        dx = max(other.x0 - self.x1, self.x0 - other.x1)
        dy = max(other.y0 - self.y1, self.y0 - other.y1)
        if dx < 0 and dy < 0:
            return max(dx, dy)
        return math.hypot(max(dx, 0.0), max(dy, 0.0))

    def to_dict(self) -> dict:
        return {"x": [round(self.x0, 4), round(self.x1, 4)],
                "y": [round(self.y0, 4), round(self.y1, 4)], "label": self.label}


@dataclass(frozen=True)
class Surface:
    surface_id: str
    korean: str
    top_z: float
    area: Rect
    obstacles: tuple[Rect, ...]
    step_m: float
    margin_m: float
    sources: Mapping[str, str] = field(default_factory=dict)
    #: 후보 중심을 찾는 영역(근거가 있는 놓기 가능 띠). 없으면 표면 전체다.
    region: Rect | None = None

    def to_dict(self) -> dict:
        return {"surface_id": self.surface_id, "korean": self.korean,
                "top_z_m": round(self.top_z, 4), "area": self.area.to_dict(),
                "region": None if self.region is None else self.region.to_dict(),
                "obstacles": [o.to_dict() for o in self.obstacles],
                "step_m": self.step_m, "margin_m": self.margin_m,
                "sources": dict(self.sources)}


def _frame_world(frames: Mapping[str, Any], name: str | None) -> tuple[float, float, float]:
    out = [0.0, 0.0, 0.0]
    while name:
        row = frames[name]
        out = [a + float(b) for a, b in zip(out, row["xyz_m"])]
        name = row.get("parent")
    return out[0], out[1], out[2]


def _part_box(models: Mapping[str, Any], frames: Mapping[str, Any], model: str, part: Mapping):
    origin = _frame_world(frames, models[model]["frame"])
    center = [o + float(c) for o, c in zip(origin, part["center_xyz_m"])]
    size = [float(s) for s in part["size_m"]]
    return center, size


def surface_from_config(workcell: Mapping[str, Any], surfaces: Mapping[str, Any],
                        surface_id: str) -> Surface:
    """셀 설정 + 표면 선언 → Surface. 선언이 없거나 형상이 맞지 않으면 ValueError."""
    spec = (surfaces.get("surfaces") or {}).get(surface_id)
    if not spec:
        raise ValueError(f"선언되지 않은 표면이다: {surface_id}")
    if spec.get("placement") != "free_spot":
        raise ValueError(f"{surface_id}는 빈 위치 놓기를 허용한 표면이 아니다")
    models, frames = workcell["models"], workcell["frames"]
    model = spec["model"]
    part = next((p for p in models[model].get("parts", ()) if p["name"] == spec["part"]), None)
    if part is None or not models[model].get("static"):
        raise ValueError(f"{model}.{spec['part']}가 정적 부품이 아니다")
    center, size = _part_box(models, frames, model, part)
    top = center[2] + size[2] / 2
    area = Rect(center[0] - size[0] / 2, center[0] + size[0] / 2,
                center[1] - size[1] / 2, center[1] + size[1] / 2, f"{model}.{part['name']}")
    obstacles = []
    for other, row in models.items():
        if other == model or not row.get("static") or not row.get("frame"):
            continue
        for p in row.get("parts") or ():
            c, s = _part_box(models, frames, other, p)
            bottom = c[2] - s[2] / 2
            rect = Rect(c[0] - s[0] / 2, c[0] + s[0] / 2, c[1] - s[1] / 2, c[1] + s[1] / 2,
                        f"{other}.{p['name']}")
            if abs(bottom - top) <= RESTING_TOLERANCE_M and rect.gap(area) < 0:
                obstacles.append(rect)
    margin = float(workcell["collision_margins_m"]["environment_clearance_min_m"])
    step = float(spec.get("candidate_step_m") or workcell["grasp"]["sweep_step_m"])
    region = None
    declared = spec.get("placement_region")
    if declared:
        if not declared.get("source"):
            raise ValueError(f"{surface_id}.placement_region에 근거(source)가 없다")
        region = Rect(max(area.x0, float(declared["x_m"][0])), min(area.x1, float(declared["x_m"][1])),
                      max(area.y0, float(declared["y_m"][0])), min(area.y1, float(declared["y_m"][1])),
                      "placement_region")
    return Surface(surface_id=surface_id, korean=str(spec.get("korean") or surface_id),
                   top_z=top, area=area, obstacles=tuple(obstacles), step_m=step,
                   margin_m=margin, region=region,
                   sources={"surface": f"models.{model}.parts.{spec['part']}",
                            "margin_m": "collision_margins_m.environment_clearance_min_m",
                            "step_m": "surfaces.candidate_step_m 또는 grasp.sweep_step_m",
                            "obstacles": "표면 위(바닥 높이 = 상판, 1 mm 안)에 놓인 정적 부품",
                            "region": str((declared or {}).get("source") or "표면 전체")})


@dataclass(frozen=True)
class Candidate:
    x: float
    y: float
    occupant_gap_m: float          # 다른 자재와의 최소 거리(없으면 inf)
    static_gap_m: float            # 고정 장애물·가장자리와의 최소 거리
    base_distance_m: float

    def key(self, sufficient_gap_m: float):
        """선택 순서. 다른 자재와의 거리는 '충분'(손가락 여유)까지만 우열을 가린다."""
        def r(v):
            return round(v, 6)   # 1 µm로 반올림 — 부동소수 잡음이 순서를 바꾸지 않게
        spacing = min(self.occupant_gap_m, sufficient_gap_m)
        return (-r(spacing), -r(self.static_gap_m), r(self.base_distance_m), self.x, self.y)

    def to_dict(self) -> dict:
        return {"center_xy_m": [round(self.x, 4), round(self.y, 4)],
                "occupant_gap_m": (None if math.isinf(self.occupant_gap_m)
                                   else round(self.occupant_gap_m, 4)),
                "static_gap_m": round(self.static_gap_m, 4),
                "base_distance_m": round(self.base_distance_m, 4)}


@dataclass(frozen=True)
class CandidateSet:
    surface: Surface
    candidates: tuple[Candidate, ...]          # 선택 순서
    rejected: Mapping[str, int]
    occupants: Mapping[str, Rect]
    problem: str | None = None
    sufficient_gap_m: float | None = None

    def to_dict(self, limit: int = 5) -> dict:
        return {"surface": self.surface.to_dict(), "count": len(self.candidates),
                "sufficient_gap_m": self.sufficient_gap_m,
                "rejected": dict(self.rejected),
                "occupants": {m: r.to_dict() for m, r in self.occupants.items()},
                "top": [c.to_dict() for c in self.candidates[:limit]],
                "problem": self.problem, "rule": SELECTION_RULE}


SELECTION_RULE = ("다른 자재와 손가락 여유(자재 반폭 + 열린 개구 반폭 + 여유)만큼 떨어진 곳 →"
                  " 고정 장애물·가장자리와 먼 곳 → 로봇 base와 가까운 곳 → 좌표 순")


def footprint(x: float, y: float, size_xy: Sequence[float]) -> Rect:
    hx, hy = float(size_xy[0]) / 2, float(size_xy[1]) / 2
    return Rect(x - hx, x + hx, y - hy, y + hy)


def candidates(surface: Surface, *, material_size_m: Sequence[float],
               others: Mapping[str, Sequence[float] | None],
               other_sizes: Mapping[str, Sequence[float]],
               sufficient_gap_m: float,
               robot_base_xy: Sequence[float] = (0.0, 0.0)) -> CandidateSet:
    """후보를 만들고 선택 순서로 정렬한다.

    `others`: 옮기는 자재를 뺀 자재 → 관측 위치(x, y, z). None이면 관측 못 함.
    `sufficient_gap_m`: 다른 자재와 이만큼 떨어지면 더 멀어도 낫다고 보지 않는다(로봇별
    값 — 열린 손가락이 옆 자재에 닿지 않을 거리). 정렬에만 쓰고 거르지는 않는다.
    """
    unobserved = sorted(m for m, pose in others.items() if pose is None)
    if unobserved:
        return CandidateSet(surface, (), {}, {},
                            problem=f"관측하지 못한 자재가 있어 빈 곳을 확인할 수 없다: {unobserved}")
    occupants: dict[str, Rect] = {}
    for model, pose in others.items():
        size = other_sizes[model]
        bottom = float(pose[2]) - float(size[2]) / 2
        rect = footprint(float(pose[0]), float(pose[1]), size)
        if abs(bottom - surface.top_z) <= OCCUPANT_HEIGHT_TOLERANCE_M and rect.gap(
                surface.area) < 0:
            occupants[model] = rect
    hx, hy = float(material_size_m[0]) / 2, float(material_size_m[1]) / 2
    m, step, area = surface.margin_m, surface.step_m, surface.area
    # 중심은 근거 영역 안에서만 찾고, 지지는 표면 실제 가장자리로 본다.
    region = surface.region or area
    xs = _grid(max(area.x0 + hx + m, region.x0), min(area.x1 - hx - m, region.x1), step)
    ys = _grid(max(area.y0 + hy + m, region.y0), min(area.y1 - hy - m, region.y1), step)
    rejected = {"support": 0, "static_obstacle": 0, "occupied": 0}
    out: list[Candidate] = []
    if not xs or not ys:
        rejected["support"] = 1
    for x in xs:
        for y in ys:
            rect = footprint(x, y, material_size_m)
            static_gaps = [rect.gap(o) for o in surface.obstacles]
            edge_gap = min(rect.x0 - area.x0, area.x1 - rect.x1, rect.y0 - area.y0,
                           area.y1 - rect.y1)
            if any(g < m for g in static_gaps):
                rejected["static_obstacle"] += 1
                continue
            occupant_gaps = [rect.gap(o) for o in occupants.values()]
            if any(g < m for g in occupant_gaps):
                rejected["occupied"] += 1
                continue
            out.append(Candidate(
                x=x, y=y, occupant_gap_m=min(occupant_gaps, default=math.inf),
                static_gap_m=min([*static_gaps, edge_gap]),
                base_distance_m=math.hypot(x - robot_base_xy[0], y - robot_base_xy[1])))
    out.sort(key=lambda c: c.key(sufficient_gap_m))
    problem = None if out else "지지·고정 장애물·점유 조건을 모두 만족하는 자리가 없다"
    return CandidateSet(surface, tuple(out), rejected, occupants, problem,
                        sufficient_gap_m=sufficient_gap_m)


def _grid(lo: float, hi: float, step: float) -> list[float]:
    if hi < lo:
        return []
    count = int(math.floor((hi - lo) / step + 1e-9)) + 1
    return [round(lo + i * step, 6) for i in range(count)]


def still_free(surface: Surface, center_xy: Sequence[float], *,
               material_size_m: Sequence[float],
               others: Mapping[str, Sequence[float] | None],
               other_sizes: Mapping[str, Sequence[float]]) -> tuple[bool, str]:
    """실행 직전: 고른 자리가 **지금 관측으로도** 지지·비어 있음 조건을 만족하는가."""
    fresh = candidates(surface, material_size_m=material_size_m, others=others,
                       other_sizes=other_sizes, sufficient_gap_m=0.0)
    if fresh.problem and not fresh.candidates:
        return False, fresh.problem
    x, y = float(center_xy[0]), float(center_xy[1])
    hit = any(abs(c.x - x) < 1e-6 and abs(c.y - y) < 1e-6 for c in fresh.candidates)
    if hit:
        return True, "관측상 그 자리가 여전히 비어 있고 지지된다"
    rect = footprint(x, y, material_size_m)
    blockers = [m for m, r in fresh.occupants.items() if rect.gap(r) < surface.margin_m]
    if blockers:
        return False, f"그 자리에 {', '.join(blockers)}가 관측된다 — 다시 계획해야 한다"
    return False, "그 자리가 지금 후보 조건(지지·고정 장애물)을 만족하지 않는다"
