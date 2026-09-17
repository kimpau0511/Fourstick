"""MoveIt planning scene을 쓰는 기하 검사 구현체 (8-07).

`core.geometry.GeometryValidator` 계약을 만족한다. 판정 규칙:

1. 계획의 각 스텝에 대응하는 **수치 관절값(resolved_motion)이 없으면 ALLOW를
   만들지 않는다** — 검사할 대상이 없으므로 ASK
   (`geometry.environment_unavailable`)다. 기호 계획만으로 통과시키지 않는다.
2. 자세가 관절 제한을 벗어나면 BLOCK(`geometry.workspace_violation`).
3. 충돌이면 BLOCK(`geometry.collision`). 어떤 쌍이 닿았는지 이름만 남긴다.
4. snapshot이 없으면 ASK(`geometry.environment_unavailable`). 이 판단은
   상위(`validation/geometry_check.py`)에서도 하지만, 구현체 단독으로도
   안전한 쪽이어야 한다.
5. scene의 좌표계와 요청의 좌표계가 다르면 ASK(`geometry.frame_unknown`).
   변환해 주지 않는다.
6. 검사 도중 scene이 바뀌면(검사 전후 hash 불일치) ALLOW를 내지 않는다 —
   ASK(`geometry.environment_unavailable`)다. **검사한 환경과 판정이 붙어
   있어야 한다.**

ROS 접근은 client(`robots.moveit.scene.PlanningSceneClient`)에 있다. 이 파일은
rclpy를 모르므로 ROS 없이 테스트할 수 있다.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from core.geometry import (
    GeometryDecision,
    GeometryReason,
    GeometryRequest,
    GeometryVerdict,
)
from core.reason_codes import ReasonCode
from robots.moveit.scene import PlanningSceneClient

#: resolved_motion에서 관절값을 담는 키. 계약상 이 이름으로 넘어온다.
JOINT_KEY = "joint_positions"


class MoveItGeometryValidator:
    """MoveIt의 상태 유효성 검사로 계획 스텝을 검증한다."""

    def __init__(
        self, client: PlanningSceneClient, *,
        validator_id: str = "moveit-planning-scene",
        validator_version: str = "moveit 2.15.0",
    ):
        self._client = client
        self._id = validator_id
        self._version = validator_version

    @property
    def validator_id(self) -> str:
        return self._id

    @property
    def validator_version(self) -> str:
        return self._version

    def check(self, request: GeometryRequest) -> GeometryVerdict:
        started = request.checked_at
        t0 = time.monotonic()

        def finish() -> float:
            return started + max(0.0, time.monotonic() - t0)

        snapshot = request.snapshot
        if snapshot is None:
            return self._ask(
                ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                "planning scene snapshot이 없다 — 검사하지 않은 상태를 통과로"
                " 쓰지 않는다",
                request, started, finish(),
            )

        before = self._client.snapshot()
        if before.frame_id != request.frame_id:
            return self._ask(
                ReasonCode.GEOMETRY_FRAME_UNKNOWN,
                f"scene 좌표계 {before.frame_id} != 요청 좌표계"
                f" {request.frame_id} — 변환하지 않는다",
                request, started, finish(),
            )
        if before.content_hash != snapshot.content_hash:
            return self._ask(
                ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                "요청이 가리키는 snapshot과 현재 scene의 hash가 다르다"
                f" ({snapshot.content_hash[:12]} != {before.content_hash[:12]})",
                request, started, finish(),
            )

        missing: list[str] = []
        reasons: list[GeometryReason] = []
        checked: list[dict[str, Any]] = []
        for index, step in enumerate(request.plan.steps, start=1):
            motion = request.resolved_motion.get(index)
            joints = None if motion is None else motion.get(JOINT_KEY)
            if not joints:
                missing.append(f"스텝{index}({step.skill})")
                continue
            validity = self._client.check_state(joints)
            checked.append({
                "step": index, "skill": step.skill, "valid": validity.valid,
            })
            if validity.out_of_bounds:
                reasons.append(GeometryReason(
                    ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                    f"스텝{index}: 관절 제한 위반"
                    f" ({', '.join(validity.out_of_bounds)})",
                ))
            if not validity.valid and not validity.out_of_bounds:
                pairs = ", ".join(f"{a}↔{b}" for a, b in validity.contacts)
                reasons.append(GeometryReason(
                    ReasonCode.GEOMETRY_COLLISION,
                    f"스텝{index}: 충돌 {pairs or validity.detail or '쌍 미보고'}",
                ))

        if missing:
            return self._ask(
                ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                f"수치 관절값이 없는 스텝: {', '.join(missing)}"
                " — 기호 계획만으로 기하 검사를 통과시키지 않는다",
                request, started, finish(),
            )

        after = self._client.snapshot()
        if after.content_hash != before.content_hash:
            return self._ask(
                ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                "검사 중에 planning scene이 바뀌었다 — 판정을 환경에 붙일 수 없다",
                request, started, finish(),
            )

        evidence = {
            "checked_steps": len(checked),
            "steps": checked,
            "scene_revision": before.snapshot_version,
            "world_object_count": before.summary.get("world_object_count"),
            "acm_disabled_pair_count": before.summary.get("acm_disabled_pair_count"),
            "robot_model_hash": before.summary.get("robot_model_hash"),
            "method": "moveit_state_validity_per_step",
        }
        if reasons:
            return GeometryVerdict(
                decision=GeometryDecision.BLOCK,
                validator_id=self._id, validator_version=self._version,
                input_complete=True, started_at=started, finished_at=finish(),
                reasons=tuple(reasons),
                snapshot_id=before.snapshot_id,
                snapshot_version=before.snapshot_version,
                snapshot_hash=before.content_hash,
                frame_id=before.frame_id, evidence=evidence,
            )
        return GeometryVerdict(
            decision=GeometryDecision.ALLOW,
            validator_id=self._id, validator_version=self._version,
            input_complete=True, started_at=started, finished_at=finish(),
            snapshot_id=before.snapshot_id,
            snapshot_version=before.snapshot_version,
            snapshot_hash=before.content_hash,
            frame_id=before.frame_id, evidence=evidence,
        )

    def _ask(
        self, reason: ReasonCode, detail: str, request: GeometryRequest,
        started: float, finished: float,
    ) -> GeometryVerdict:
        snapshot = request.snapshot
        return GeometryVerdict(
            decision=GeometryDecision.ASK,
            validator_id=self._id, validator_version=self._version,
            input_complete=False, started_at=started, finished_at=finished,
            reasons=(GeometryReason(reason, detail),),
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_version=None if snapshot is None else snapshot.snapshot_version,
            snapshot_hash=None if snapshot is None else snapshot.content_hash,
            frame_id=request.frame_id,
        )


def joint_motion(joints: Mapping[str, float]) -> dict[str, Any]:
    """`resolved_motion` 항목을 만드는 보조 함수. 키 이름을 한 곳에서 정한다."""
    return {JOINT_KEY: dict(joints)}
