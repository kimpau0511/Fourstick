"""붙임 관절 관측(읽기 전용) — 지금 Gazebo에서 그리퍼에 붙어 있는 자재.

`DetachableJoint` 시스템은 붙일 때 관절 엔티티를 만들고 뗄 때 지운다. 그래서 월드 상태
(`/world/<w>/state`)에 그 관절 컴포넌트가 있으면 붙어 있고, 없으면 떨어져 있다. 컴포넌트 값은
"부모링크id 자식링크id 종류"이고, 링크 id → 모델 이름은 장면 정보(`/world/<w>/scene/info`)로 찾는다.

붙임 상태 알림(`.../state` 토픽)은 **바뀔 때만** 온다 — 이미 떨어진 관절에 detach를 보내면 알림이 없어
기록이 'unknown'으로 남는다(2026-10-09 격리 셀 실측: 정지→복구 뒤 material_b 'unknown', 관절 없음).
이 관측으로 그 'unknown'을 확인한다. 아무것도 바꾸지 않는다.

조회는 **별도 프로세스**(`observe_attached_isolated`)에서 한다. pose 구독 콜백이 도는 프로세스(서버의 sim_view, 실행기의
붙임 고정기)에서 `Node.request`를 부르면 응답을 받지 못하고 시간 초과로 끝났다(2026-10-09 격리 셀 실측: 구독 없는
프로세스 0.03 s, 구독 있는 프로세스 3.0 s 시간 초과 6/6 — 다른 Node로 불러도 같다).
"""

from __future__ import annotations

import time
from typing import Iterable, Mapping

#: gz-sim `components::DetachableJoint`의 컴포넌트 형식 id(이름 해시 — 버전이 바뀌어도 같다).
DETACHABLE_JOINT_COMPONENT = 4618113539621515562
REQUEST_TIMEOUT_MS = 3000
#: 조회 시도 횟수. 부하가 높을 때 한 번씩 응답이 늦었다(2026-10-09 격리 셀: 확인 직전 1회 실패, 단독 측정 0.03~0.24 s).
REQUEST_ATTEMPTS = 3


def attached_models(joint_values: Iterable[bytes | str], link_owner: Mapping[int, str]) -> set[str]:
    """관절 컴포넌트 값들 → 자식 링크가 속한 모델 이름. 모르는 링크는 '?<id>'로 남긴다(숨기지 않는다)."""
    out: set[str] = set()
    for raw in joint_values:
        text = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        parts = text.split()
        if len(parts) < 2:
            continue
        try:
            child = int(parts[1])
        except ValueError:
            continue
        out.add(link_owner.get(child, f"?{child}"))
    return out


def observe_attached(node, world: str, *, timeout_ms: int = REQUEST_TIMEOUT_MS) -> tuple[set[str] | None, str]:
    """(붙어 있는 모델 이름 집합, 설명). 조회에 실패하면 (None, 이유) — 모르는 것을 '떨어짐'으로 만들지 않는다."""
    try:
        from gz.msgs.empty_pb2 import Empty
        from gz.msgs.scene_pb2 import Scene
        from gz.msgs.serialized_map_pb2 import SerializedStepMap

        started, ok, state = time.monotonic(), False, None
        for _attempt in range(REQUEST_ATTEMPTS):
            ok, state = node.request(f"/world/{world}/state", Empty(), Empty, SerializedStepMap, timeout_ms)
            if ok:
                break
        if not ok:
            return None, (f"월드 상태 조회 실패({REQUEST_ATTEMPTS}회, "
                          f"{time.monotonic() - started:.1f}s)")
        values = [entity.components[DETACHABLE_JOINT_COMPONENT].component
                  for entity in state.state.entities.values()
                  if DETACHABLE_JOINT_COMPONENT in entity.components]
        if not values:
            return set(), "붙임 관절 없음(월드 상태)"
        ok, scene = node.request(f"/world/{world}/scene/info", Empty(), Empty, Scene, timeout_ms)
        if not ok:
            return None, "장면 정보 조회 실패(관절은 있다)"
        owner = {link.id: model.name for model in scene.model for link in model.link}
        models = attached_models(values, owner)
        return models, f"붙임 관절 {len(values)}개(월드 상태): {sorted(models)}"
    except Exception as exc:  # noqa: BLE001
        return None, f"붙임 관절 관측 실패: {exc}"[:200]


#: 별도 프로세스 조회의 전체 제한 시간(초) — 파이썬 시작·gz 모듈 적재·서비스 발견을 포함한다.
ISOLATED_TIMEOUT_SEC = 15.0


def observe_attached_isolated(world: str, partition: str | None, *, timeout_sec: float = ISOLATED_TIMEOUT_SEC,
                              runner=None) -> tuple[set[str] | None, str]:
    """`observe_attached`를 구독 없는 새 프로세스에서 돌린다. 실패·시간 초과면 (None, 이유)."""
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = dict(os.environ)
    if partition:
        env["GZ_PARTITION"] = partition
    argv = [sys.executable, "-m", "robots.fr3_gazebo.joint_observation", world]
    run = runner or subprocess.run
    started = time.monotonic()
    try:
        done = run(argv, cwd=str(Path(__file__).resolve().parents[2]), env=env, capture_output=True,
                   text=True, timeout=timeout_sec)
    except Exception as exc:  # noqa: BLE001 — 시간 초과 포함
        return None, f"붙임 관절 관측 프로세스 실패({time.monotonic() - started:.1f}s): {exc}"[:200]
    try:
        out = json.loads((done.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, f"붙임 관절 관측 결과를 읽지 못했다(종료 코드 {done.returncode}): {(done.stderr or '')[-160:]}"
    attached = out.get("attached")
    return (None if attached is None else set(attached)), str(out.get("detail") or "")


def main(argv=None) -> int:
    """`python -m robots.fr3_gazebo.joint_observation <world>` → 한 줄 JSON {"attached": [...]|null, "detail"}."""
    import json
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    from gz.transport import Node

    attached, detail = observe_attached(Node(), args[0])
    print(json.dumps({"attached": None if attached is None else sorted(attached), "detail": detail},
                     ensure_ascii=False))
    return 0 if attached is not None else 3


if __name__ == "__main__":
    raise SystemExit(main())
