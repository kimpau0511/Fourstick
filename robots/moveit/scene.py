"""MoveIt planning scene 관측 경계 (8-07).

`AttachedObject`(적재 상태 선언)는 계약 계층(`core.geometry`)에 있다.
여기서 다시 정의하지 않고 그대로 내보낸다 — 검증 계층이 구현 모듈을
import하지 않아도 되게 하려고.

여기서 정하는 것은 **무엇을 snapshot으로 볼 것인가**다.

- snapshot 본문(메시·점군·물체 형상)은 계약을 넘지 않는다. 식별자와 hash만
  넘긴다(`core.geometry.EnvironmentSnapshot`).
- hash에는 scene 이름, world 물체의 id·형상·치수·자세, 허용 충돌 행렬(ACM)
  항목, 로봇 모델 지문을 넣는다. **하나라도 바뀌면 값이 달라져야 한다** —
  계획 중에 scene이 바뀐 것을 승인 단계에서 잡아내는 근거다.
- 관측 시각은 ROS 시계(시뮬레이션 시간이면 시뮬레이션 시각)가 아니라 **벽시계**
  UTC로 남긴다. 만료 판정을 앱과 같은 시계로 해야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from core.geometry import AttachedObject, EnvironmentSnapshot, snapshot_hash


@dataclass(frozen=True)
class SceneSnapshot:
    """planning scene 한 장의 식별자 + 요약. 본문 데이터는 담지 않는다."""

    snapshot_id: str
    snapshot_version: str
    content_hash: str
    frame_id: str
    captured_at: float
    ttl_sec: float
    source: str
    #: 감사용 요약(물체 수, ACM 비활성 쌍 수 등). 형상 데이터는 넣지 않는다.
    summary: Mapping[str, Any] = field(default_factory=dict)

    def to_environment(self) -> EnvironmentSnapshot:
        return EnvironmentSnapshot(
            snapshot_id=self.snapshot_id,
            snapshot_version=self.snapshot_version,
            content_hash=self.content_hash,
            frame_id=self.frame_id,
            captured_at=self.captured_at,
            ttl_sec=self.ttl_sec,
            source=self.source,
        )

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "content_hash": self.content_hash,
            "frame_id": self.frame_id,
            "captured_at": self.captured_at,
            "ttl_sec": self.ttl_sec,
            "source": self.source,
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class StateValidity:
    """한 자세의 유효성 판정. MoveIt의 응답을 그대로 옮긴다."""

    valid: bool
    #: 충돌한 링크·물체 쌍 이름. 형상이 아니라 이름만 남긴다.
    contacts: tuple[tuple[str, str], ...] = ()
    #: 관절 제한을 벗어난 관절 이름.
    out_of_bounds: tuple[str, ...] = ()
    detail: str = ""


@runtime_checkable
class PlanningSceneClient(Protocol):
    """검사기가 보는 최소 계약. ROS 구현체와 테스트 대역이 이것을 만족한다.

    `check_state`의 `attached`는 **적재 상태**를 넘기는 경로다. 넘기지 않으면
    빈손 상태를 검사한다 — 구현체가 물체를 스스로 붙이지 않는다.
    """

    def snapshot(self) -> SceneSnapshot: ...

    def check_state(
        self, joints: Mapping[str, float], *,
        attached: Sequence[AttachedObject] = (),
    ) -> StateValidity: ...


def scene_snapshot_from_parts(
    *,
    scene_name: str,
    robot_model_hash: str,
    world_objects: Sequence[Mapping[str, Any]],
    acm_disabled_pairs: Sequence[tuple[str, str]],
    frame_id: str,
    captured_at: float,
    ttl_sec: float,
    source: str,
    revision: str | None = None,
) -> SceneSnapshot:
    """관측 조각으로 snapshot 식별자를 만든다. 순수 함수 — ROS를 모른다."""
    parts = [f"scene:{scene_name}", f"model:{robot_model_hash}", f"frame:{frame_id}"]
    for obj in sorted(world_objects, key=lambda o: str(o.get("id", ""))):
        pose = obj.get("pose") or ()
        shapes = obj.get("shapes") or ()
        parts.append(
            "object:{id}|shapes:{shapes}|pose:{pose}".format(
                id=obj.get("id", ""),
                shapes=";".join(
                    f"{s.get('type','')}({','.join(f'{v:.6f}' for v in s.get('dimensions', ()))})"
                    for s in shapes
                ),
                pose=",".join(f"{v:.6f}" for v in pose),
            )
        )
    pairs = sorted("|".join(sorted(pair)) for pair in acm_disabled_pairs)
    parts.append("acm:" + ";".join(pairs))
    digest = snapshot_hash(parts)
    # revision을 주지 않으면 내용 지문에서 만든다. 내용이 바뀌면 버전도 바뀌고,
    # 바뀌지 않으면 같은 값이 나온다(승인 재사용 판정의 전제).
    revision = revision or f"h{digest[:12]}"
    return SceneSnapshot(
        snapshot_id=f"moveit:{scene_name}",
        snapshot_version=revision,
        content_hash=digest,
        frame_id=frame_id,
        captured_at=captured_at,
        ttl_sec=ttl_sec,
        source=source,
        summary={
            "world_object_ids": sorted(str(o.get("id", "")) for o in world_objects),
            "world_object_count": len(world_objects),
            "acm_disabled_pair_count": len(acm_disabled_pairs),
            "robot_model_hash": robot_model_hash,
        },
    )
