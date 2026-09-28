"""planning scene ↔ 실제 자재 위치 동기화 (순수 함수 — ROS를 모른다).

MoveIt planning scene은 셀 설정에서 한 번 올라오므로 자재 상자가 늘 **선언된 원래
자리**에 있다. 자재를 칸이나 다른 팔레트로 옮기면 scene은 그 사실을 모른다 —
충돌 검사가 실제로 있는 자재를 보지 못하고, 이미 떠난 자리의 사본과 부딪힌다.

여기서 하는 일(실행 직전, 로봇 명령 전):

1. **기록 ↔ 관측 대조.** 자재마다 상태 기록이 말하는 자리(원래 자리 · 컨베이어 칸 ·
   다른 팔레트)의 선언 중심과 Gazebo 관측 pose가 `SYNC_RECORD_TOLERANCE_M` 안인가.
   기록이 자리를 확정하지 않는 상태(STOP·실패로 남은 자재 등)거나, 관측이 없거나,
   기록과 관측이 다르면 **동기화하지 않고 막는다**(추측해서 scene에 넣지 않는다).
2. **scene 이동 목록.** 관측 pose(위치 + 방향)와 scene 상자 pose가 다른 자재만 옮긴다.
3. **되읽기 확인.** 적용한 뒤 scene을 다시 읽어 모든 자재 상자가 관측 pose와
   `SCENE_POSE_TOLERANCE_M` 안인지 본다. 확인하지 못하면 막는다.

접촉 허용 규칙(`classify_contacts`)은 바꾸지 않는다. scene이 실제를 따라가므로
"떠난 자리의 사본"을 예외로 둘 필요가 없어진다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 기록한 자리와 관측의 허용 차(m). 기록↔관측 정합(`ORIGIN_TOLERANCE_M`)과 같다.
SYNC_RECORD_TOLERANCE_M = 0.02
#: scene 상자를 관측 pose로 옮겼다고 볼 허용 차(m). 적용 뒤 되읽기에 쓴다.
SCENE_POSE_TOLERANCE_M = 0.001
#: 방향 허용 차(rad, 두 quaternion 사이 각).
SCENE_ANGLE_TOLERANCE_RAD = 0.002

Pose7 = tuple  # (x, y, z, qx, qy, qz, qw)


def angle_between(q1: Sequence[float], q2: Sequence[float]) -> float:
    """두 단위 quaternion(x, y, z, w) 사이 회전각(rad)."""
    n1 = math.sqrt(sum(float(v) ** 2 for v in q1)) or 1.0
    n2 = math.sqrt(sum(float(v) ** 2 for v in q2)) or 1.0
    dot = abs(sum(float(a) * float(b) for a, b in zip(q1, q2)) / (n1 * n2))
    return 2.0 * math.acos(min(1.0, dot))


def pose_gap(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """(위치 차 m, 방향 차 rad). 방향이 없으면 방향 차는 0이다."""
    dist = math.dist([float(v) for v in a[:3]], [float(v) for v in b[:3]])
    if len(a) >= 7 and len(b) >= 7:
        return dist, angle_between(a[3:7], b[3:7])
    return dist, 0.0


@dataclass
class SyncPlan:
    """동기화 계획. `findings`가 있으면 scene을 건드리지 말고 막는다."""

    desired: dict[str, Pose7] = field(default_factory=dict)
    moves: dict[str, Pose7] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict:
        return {"ok": self.ok, "findings": list(self.findings),
                "moves": {m: [round(float(v), 6) for v in p] for m, p in self.moves.items()},
                "materials": list(self.rows)}


def plan_scene_sync(
    *, materials: Sequence[str],
    records: Mapping[str, Mapping[str, Any] | None],
    observed: Mapping[str, Sequence[float] | None],
    scene_poses: Mapping[str, Sequence[float]],
    expected_at: Callable[[str, Mapping[str, Any] | None],
                          tuple[Sequence[float] | None, str]],
    exclude: Sequence[str] = (),
) -> SyncPlan:
    """자재마다 기록 ↔ 관측을 대조하고 scene에서 옮길 상자를 고른다.

    `expected_at(model, record)` → (그 기록이 말하는 자리의 선언 중심 또는 None, 자리 이름).
    None이면 기록이 자리를 확정하지 않는다는 뜻이다(막는다).
    `exclude`: 지금 도구가 든 자재 등 이 계획에서 다루지 않는 자재(scene 사본은 그대로).
    """
    plan = SyncPlan()
    skipped = set(exclude)
    for model in materials:
        if model in skipped:
            plan.rows.append({"model": model, "skipped": True})
            continue
        record = records.get(model)
        center, where = expected_at(model, record)
        pose = observed.get(model)
        row: dict[str, Any] = {"model": model, "record_place": where,
                               "observed_pose": None if pose is None
                               else [round(float(v), 6) for v in pose]}
        plan.rows.append(row)
        if center is None:
            plan.findings.append(
                f"{model}: 기록 상태({(record or {}).get('state')})가 자리를 확정하지 않는다"
                " — 장면에 반영할 수 없다")
            continue
        if pose is None:
            plan.findings.append(f"{model}: Gazebo pose를 관측하지 못했다")
            continue
        gap = math.dist([float(v) for v in pose[:3]], [float(v) for v in center[:3]])
        row["record_gap_m"] = round(gap, 6)
        if gap > SYNC_RECORD_TOLERANCE_M:
            plan.findings.append(
                f"{model}: 기록한 자리({where})와 관측이 {gap:.4f} m 다르다"
                f" (허용 {SYNC_RECORD_TOLERANCE_M} m) — 기록↔관측 정합이 먼저다")
            continue
        desired = tuple(float(v) for v in pose[:3]) + (
            tuple(float(v) for v in pose[3:7]) if len(pose) >= 7 else (0.0, 0.0, 0.0, 1.0))
        plan.desired[model] = desired
        current = scene_poses.get(model)
        if current is None:
            plan.findings.append(f"{model}: planning scene에 자재 상자가 없다")
            continue
        dist, angle = pose_gap(current, desired)
        row["scene_gap_m"] = round(dist, 6)
        row["scene_angle_rad"] = round(angle, 6)
        if dist > SCENE_POSE_TOLERANCE_M or angle > SCENE_ANGLE_TOLERANCE_RAD:
            plan.moves[model] = desired
    return plan


def verify_scene_sync(desired: Mapping[str, Sequence[float]],
                      scene_poses: Mapping[str, Sequence[float]]) -> list[str]:
    """적용 뒤 되읽은 scene이 관측 pose를 담고 있는가. 사유 목록(비면 통과)."""
    problems: list[str] = []
    for model, want in desired.items():
        got = scene_poses.get(model)
        if got is None:
            problems.append(f"{model}: 되읽은 scene에 없다")
            continue
        dist, angle = pose_gap(got, want)
        if dist > SCENE_POSE_TOLERANCE_M or angle > SCENE_ANGLE_TOLERANCE_RAD:
            problems.append(f"{model}: scene이 관측과 {dist:.4f} m · {angle:.4f} rad 다르다")
    return problems
