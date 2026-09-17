#!/usr/bin/env python3
"""생성된 SRDF에 루프 폐쇄 제외를 다시 넣는다 (8-08 우선순위 3).

`collisions_updater`는 URDF에서 행렬을 새로 만들면서 기본 SRDF의
`disable_collisions`를 **버린다**(실측). 그래서 기구학적 루프 폐쇄 쌍을
생성 뒤에 다시 넣어야 한다.

넣을 쌍은 **근거 파일**에서만 읽는다(`reports/workcell/loop_closure_pairs.json`).
스크립트에 쌍 목록을 두지 않는다 — 근거 없이 충돌 검사를 끄는 경로를 만들지
않기 위해서다. 근거 파일이 없으면 아무것도 넣지 않고 그 사실을 알린다.

사용법: inject_loop_closure_srdf.py <srdf> [<evidence.json>]
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "reports/workcell/loop_closure_pairs.json"
REASON = "mechanism_loop_closure"


def main() -> int:
    srdf_path = Path(sys.argv[1])
    evidence_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_EVIDENCE
    if not evidence_path.is_file():
        print(f"근거 파일이 없다: {evidence_path} — 아무것도 제외하지 않는다",
              file=sys.stderr)
        return 1
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    pairs = [tuple(pair) for pair in evidence.get("pairs", [])]
    if not pairs:
        print("근거 파일에 쌍이 없다 — 아무것도 제외하지 않는다", file=sys.stderr)
        return 1

    tree = ET.parse(srdf_path)
    root = tree.getroot()
    existing = {frozenset((e.get("link1"), e.get("link2")))
                for e in root.findall("disable_collisions")}

    added, already = [], []
    for link1, link2 in pairs:
        if frozenset((link1, link2)) in existing:
            already.append([link1, link2])
            continue
        element = ET.SubElement(root, "disable_collisions")
        element.set("link1", link1)
        element.set("link2", link2)
        element.set("reason", REASON)
        added.append([link1, link2])

    tree.write(srdf_path, encoding="utf-8", xml_declaration=True)
    print(json.dumps({
        "srdf": str(srdf_path),
        "evidence": str(evidence_path.relative_to(ROOT)),
        "reason": REASON,
        "added": added,
        "already_disabled": already,
        "total_disabled": len(root.findall("disable_collisions")),
        "note": "기구학적 루프 폐쇄 쌍만 넣는다. 근거는 evidence 파일에 있다."
                " 다른 쌍은 검사 대상으로 남는다",
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
