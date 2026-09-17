#!/usr/bin/env python3
"""장착 입력 파일 검사 (md/개발플랜.md 8-08 우선순위 3).

`config/mounting/grp_cpl_062_inputs.json`의 각 값이 출처를 갖고 있는지 본다.
**값이 있는데 출처가 없으면 통과시키지 않는다.** 근거 없는 수치가 실행 경로로
들어가는 것을 막는 검사다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "config/mounting/grp_cpl_062_inputs.json"
REQUIRED_FIELDS = ("value", "unit", "status", "source", "measurement_method",
                   "verified", "captured_at", "note")
VALID_STATUS = {"verified", "declared", "unverified", "blocked"}


def main() -> int:
    data = json.loads(INPUTS.read_text(encoding="utf-8"))
    problems: list[str] = []
    counts = {name: 0 for name in VALID_STATUS}

    for name, item in data["inputs"].items():
        missing = [field for field in REQUIRED_FIELDS if field not in item]
        if missing:
            problems.append(f"{name}: 필드 누락 {missing}")
            continue
        status = item["status"]
        if status not in VALID_STATUS:
            problems.append(f"{name}: status가 유효하지 않다 ({status})")
            continue
        counts[status] += 1
        has_value = item["value"] is not None
        has_source = bool(item["source"]) and bool(item["measurement_method"])
        if has_value and not has_source:
            problems.append(f"{name}: 값이 있는데 출처가 없다 — 근거 없는 수치다")
        if status == "verified":
            if not has_value:
                problems.append(f"{name}: verified인데 값이 없다")
            if not has_source:
                problems.append(f"{name}: verified인데 출처가 없다")
            if not item["verified"]:
                problems.append(f"{name}: status=verified인데 verified=false다")
            if not item["captured_at"]:
                problems.append(f"{name}: verified인데 captured_at이 없다")
        if status == "blocked":
            if has_value:
                problems.append(f"{name}: blocked인데 값이 들어 있다")
            if not item["note"]:
                problems.append(f"{name}: blocked인데 필요한 입력을 적지 않았다")

    print(f"{INPUTS.relative_to(ROOT)} — 입력 {len(data['inputs'])}항목")
    for status, count in sorted(counts.items()):
        print(f"  {status:11} {count}")
    blocked = [name for name, item in data["inputs"].items()
               if item["status"] == "blocked"]
    if blocked:
        print(f"  아직 받아야 하는 입력 {len(blocked)}건:")
        for name in blocked:
            print(f"    - {name}: {data['inputs'][name]['note']}")
    if problems:
        print("\n검사 실패:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("\n검사 통과 — 값이 있는 항목은 모두 출처를 갖는다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
