#!/usr/bin/env python3
"""자기충돌 행렬에서 **근거 없는 제외를 걷어낸다** (md/개발플랜.md 8-07).

`collisions_updater`는 표본에서 충돌을 보지 못한 쌍을 `reason="Never"`로
비활성화한다. 그런데 독립 검토(`scripts/review_self_collision.py`)는 FR3-WMS
모델에서 그 쌍들이 관절 제한 안에서 **0.2 mm 안까지 접근**하는 자세를 찾았다.
즉 "Never"는 이 모델에서 사실이 아니다 — 100,000 표본이 그 자세를 놓쳤다.

그래서 규칙을 이렇게 둔다.

- `Adjacent`(조인트로 직접 연결, 설계상 접촉)만 비활성 상태로 남긴다.
- `Never`는 **독립 검토가 임계값보다 멀다고 확인한 쌍만** 남긴다.
- 검토 보고서가 없으면 `Never`를 모두 되살린다(모르면 검사한다).
- `Default`·`Always`는 애초에 만들지 않는다(그 옵션을 쓰지 않는다).

제외를 되살리면 그 근처 자세의 계획이 실패할 수 있다. 그것이 옳은 동작이다 —
**계획을 통과시키려고 충돌 검사를 끄지 않는다.**

사용법: prune_self_collision_srdf.py <srdf> [<review.json> ...]

검토 파일을 여러 개 줄 수 있다(팔 검토 + 그리퍼 검토). 어느 검토에도 없는 쌍은
**확인되지 않은 것으로 보고 검사 대상으로 되살린다.**
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: 이보다 가까워지는 쌍은 충돌 가능으로 본다(검토 스크립트와 같은 기준).
PROXIMITY_M = 0.010


def main() -> int:
    srdf_path = Path(sys.argv[1])
    review_paths = [Path(arg) for arg in sys.argv[2:]]
    tree = ET.parse(srdf_path)
    root = tree.getroot()

    distances: dict[frozenset[str], float] = {}
    review_used = []
    for review_path in review_paths:
        if not review_path.is_file():
            continue
        review = json.loads(review_path.read_text(encoding="utf-8"))
        review_used.append(str(review_path))
        for entry in review.get("closest_per_pair", []):
            pair = frozenset(entry["pair"])
            # 여러 검토가 같은 쌍을 다루면 **더 가까운 값**을 쓴다(보수적).
            current = distances.get(pair)
            if current is None or entry["min_distance_m"] < current:
                distances[pair] = entry["min_distance_m"]

    kept, restored = [], []
    for element in list(root.findall("disable_collisions")):
        pair = frozenset((element.get("link1"), element.get("link2")))
        reason = element.get("reason")
        if reason == "Adjacent":
            kept.append((sorted(pair), reason, None))
            continue
        if reason == "mechanism_loop_closure":
            # 기구학적 루프 폐쇄 쌍. 조인트가 없지만 실물은 핀으로 연결돼 있다.
            # 근거를 갖고 기본 SRDF에 명시한 쌍이므로 되살리지 않는다.
            kept.append((sorted(pair), reason, None))
            continue
        distance = distances.get(pair)
        if distance is not None and distance > PROXIMITY_M:
            kept.append((sorted(pair), reason, distance))
            continue
        root.remove(element)
        restored.append((sorted(pair), reason, distance))

    tree.write(srdf_path, encoding="utf-8", xml_declaration=True)
    print(json.dumps({
        "srdf": str(srdf_path),
        "review_reports": review_used,
        "proximity_threshold_m": PROXIMITY_M,
        "kept_disabled": [
            {"pair": pair, "reason": reason, "min_distance_m": distance}
            for pair, reason, distance in kept
        ],
        "restored_to_checked": [
            {"pair": pair, "reason": reason, "min_distance_m": distance}
            for pair, reason, distance in restored
        ],
        "note": ("Adjacent와 mechanism_loop_closure만 무조건 남긴다. Never는 독립 검토가 임계값보다 멀다고"
                 " 확인한 쌍만 남긴다. 확인 근거가 없으면 검사 대상으로 되살린다"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
