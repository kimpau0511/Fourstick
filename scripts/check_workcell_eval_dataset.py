#!/usr/bin/env python3
"""작업 셀 명령 평가셋 검사 (8-13).

```
python3 scripts/check_workcell_eval_dataset.py
```

확인하는 것:
1. schema — 시나리오·발화·정답 필드와 값 범위
2. 중복 발화 · 정답 누락
3. 개발셋 ↔ 봉인셋 겹침
4. 자원 id ↔ 활성 작업 셀 설정(`resource_map`)·카탈로그 일치
5. 자원 추출 정답 — 기대 `resources`·`stop_bypass`가 실제 카탈로그 추출과 같은가
6. 봉인셋 누출 — 프롬프트·카탈로그·테스트·스크립트·화면·예제·문서에 봉인 발화가 없는가

봉인 발화를 이 스크립트가 출력하지 않는다(id만 낸다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from planning import workcell_command_eval as ev  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402

FIXTURES = ROOT / "fixtures/workcell_eval"
MANIFEST = ROOT / "config/workcell/active.json"
#: 봉인 발화가 있으면 안 되는 곳.
LEAK_ROOTS = ("planning", "core", "validation", "server", "storage", "stt", "robots",
              "tests", "scripts", "html", "examples", "config", "fixtures", "md", "README.md")
REQUIRED_CATEGORIES = (
    "home", "팔레트 접근 move", "컨베이어 접근 move", "STOP", "정상 자원 인식",
    "자재·팔레트 불일치", "존재하지 않는 자원", "모호한 발화", "복수 자재·복수 위치",
    "pick/place 실행 차단", "resource mismatch", "STOP 키워드 변형",
)


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cell = json.loads((MANIFEST.parent / manifest["workcell_config"]).read_text(encoding="utf-8"))
    catalog = load_resource_catalog(json.loads(
        (MANIFEST.parent / manifest["resource_catalog"]).read_text(encoding="utf-8")))
    stt = json.loads((ROOT / "examples/config/valid_stt_policy.json").read_text(encoding="utf-8"))
    stop_keywords = tuple(stt["stop_keywords"])
    cell_ids = {row["resource_id"] for row in cell["resource_map"]}
    catalog_ids = {e.resource_id for e in catalog.entries}

    problems: list[str] = []
    if cell_ids != catalog_ids:
        problems.append(f"셀 resource_map과 카탈로그 id가 다르다: {sorted(cell_ids ^ catalog_ids)}")

    scenarios = ev.load_scenarios(FIXTURES / "scenarios.json")
    dev = ev.load_set(FIXTURES / "dev_set.jsonl", scenarios, "dev")
    sealed = ev.load_set(FIXTURES / "sealed_set.jsonl", scenarios, "sealed")
    problems += ev.validate_dataset(scenarios, dev, sealed, catalog_ids=catalog_ids)

    # 다음 턴 holdout(sealed2)이 있으면 같은 규칙으로 검사하고, 기존 holdout과
    # 개발셋에 겹치지 않는지, 누출이 없는지 함께 본다. 없으면 건너뛴다.
    extra_holdouts = []
    prior = {"".join(r.utterance.split()) for r in list(dev) + list(sealed)}
    for name in ("sealed2_set.jsonl", "sealed3_set.jsonl"):
        path = FIXTURES / name
        if not path.is_file():
            continue
        rows = ev.load_set(path, scenarios, "sealed")
        problems += ev.validate_dataset(scenarios, dev, rows, catalog_ids=catalog_ids)
        for row in rows:
            if "".join(row.utterance.split()) in prior:
                problems.append(f"{row.id}: {name} 발화가 앞선 세트와 겹친다")
        prior |= {"".join(r.utterance.split()) for r in rows}
        extra_holdouts += list(rows)
    holdout2 = tuple(extra_holdouts)

    for split, rows in (("dev", dev), ("sealed", sealed)):
        present = {r.category for r in rows}
        for name in REQUIRED_CATEGORIES:
            if name not in present:
                problems.append(f"{split}: 필수 분류 '{name}'에 발화가 없다")

    mismatched = 0
    for row in list(dev) + list(sealed) + list(holdout2):
        got = extract_slots(row.utterance, catalog, stop_keywords=stop_keywords)
        distinct: list[str] = []
        for m in got.matches:
            if m.resource_id not in distinct:
                distinct.append(m.resource_id)
        if set(distinct) != set(row.expect.resources):
            mismatched += 1
            problems.append(f"{row.id}: 기대 자원 {sorted(row.expect.resources)} ≠ 추출 {distinct}")
        if got.stop_keyword_hit != row.expect.stop_bypass:
            mismatched += 1
            problems.append(f"{row.id}: 기대 정지 우회 {row.expect.stop_bypass} ≠ 추출 {got.stop_keyword_hit}")

    leaks = ev.leak_scan(
        list(sealed) + list(holdout2), [ROOT / r for r in LEAK_ROOTS],
        exclude=[(FIXTURES / "sealed_set.jsonl").resolve(),
                 (FIXTURES / "sealed2_set.jsonl").resolve(),
                 (FIXTURES / "sealed3_set.jsonl").resolve(), (ROOT / "reports").resolve()],
    )
    for case_id, path in leaks:
        problems.append(f"봉인 누출: {case_id} ← {Path(path).relative_to(ROOT)}")

    print("작업 셀 명령 평가셋 검사")
    print("=" * 60)
    print(f"시나리오 {len(scenarios)}개 · 개발셋 {len(dev)}문장 · 봉인셋 {len(sealed)}문장")
    print(f"자원 id: 셀 {len(cell_ids)}개 · 카탈로그 {len(catalog_ids)}개 · 정지 키워드 {list(stop_keywords)}")
    print(f"추출 정답 대조 불일치 {mismatched}건 · 봉인 누출 {len(leaks)}건")
    if problems:
        print(f"결함 {len(problems)}건")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("결함 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
