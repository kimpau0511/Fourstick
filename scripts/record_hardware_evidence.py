#!/usr/bin/env python3
"""실기 근거를 **계약 검사를 지나서** 기록한다 (8-12).

```bash
# 입력 하나에 값과 근거를 넣는다
python3 scripts/record_hardware_evidence.py input coupling_measured_mass \
    --value 0.041 --unit kg \
    --method scale \
    --source "AND EK-600i 저울 실측" \
    --captured-at 2026-09-20 \
    --operator "홍길동" \
    --evidence reports/hardware/evidence/coupling_measured_mass__scale_reading__2026-09-20.jpg \
    --verified

# 체크리스트 단계 상태를 바꾼다
python3 scripts/record_hardware_evidence.py step power_off_before_mounting \
    --status passed \
    --captured-at 2026-09-20 \
    --operator "홍길동" \
    --evidence reports/hardware/evidence/checklist__power_off__breaker__2026-09-20.jpg
```

## 왜 손으로 JSON을 고치지 않는가

손으로 고치면 계약 검사를 건너뛴다. 이 도구는 **쓰기 전에** 확인한다.

1. `--evidence` 파일이 **실제로 존재하는가.** 없으면 거부한다
2. `--method`가 실기 근거 방법인가 — `simulation`·`simulated_observation`은
   거부하고, `declared`는 `--verified`와 함께 쓸 수 없다
3. `--verified`면 값·단위·출처·시각·작업자·근거 파일이 **모두** 있는가
4. 쓴 뒤 다시 읽어 계약을 통과하는지 확인한다. 통과하지 못하면 되돌린다

**실제 장비에 명령을 보내지 않는다.** 파일만 쓴다.

## 값은 추정하지 않는다

이 도구는 값을 만들어 넣지 않는다. 사람이 잰 값을 그대로 받는다. 단위 변환도
하지 않는다 — g로 쟀으면 kg로 바꿔서 넣고 그 사실을 `--source`에 적는다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.hardware_input import (  # noqa: E402
    HARDWARE_EVIDENCE_METHODS,
    SIMULATION_METHODS,
    HardwareInputError,
    MeasurementMethod,
    load_input,
)
from validation import hardware_readiness  # noqa: E402
from validation.hardware_readiness import StepStatus  # noqa: E402

MANIFEST = ROOT / "config/hardware/active.json"
LOG = ROOT / "reports/hardware/evidence_log.json"


def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def parse_value(raw: str | None, *, numeric: bool, unit: str):
    """값을 그대로 읽는다. **변환하지 않는다.**

    JSON으로 읽을 수 있으면 JSON으로(숫자·리스트·객체), 아니면 문자열로 둔다.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def check_evidence_file(path: str) -> Path:
    """근거 파일이 실제로 있는지 본다. 없으면 기록하지 않는다."""
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    if not target.is_file():
        raise SystemExit(
            f"근거 파일이 없다: {target}\n"
            "  파일을 reports/hardware/evidence/ 에 넣고 다시 실행한다.\n"
            "  이름 규칙은 그 디렉터리의 README.md에 있다.")
    if target.stat().st_size == 0:
        raise SystemExit(f"근거 파일이 비어 있다: {target}")
    return target


def append_log(entry: dict) -> None:
    """무엇을 언제 누가 넣었는지 append-only로 남긴다."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if LOG.is_file():
        try:
            rows = json.loads(LOG.read_text(encoding="utf-8")).get("entries", [])
        except (OSError, json.JSONDecodeError):
            rows = []
    rows.append(entry)
    LOG.write_text(json.dumps({
        "schema": "forstick2.hardware_evidence_log/1",
        "note": "실기 근거 기록 이력. append-only — 지우지 않는다",
        "entries": rows,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def record_input(args) -> int:
    config_path = ROOT / manifest()["inputs"]
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    row = next((r for r in payload["inputs"] if r["key"] == args.key), None)
    if row is None:
        keys = ", ".join(sorted(r["key"] for r in payload["inputs"]))
        raise SystemExit(f"그런 입력이 없다: {args.key}\n  있는 입력: {keys}")

    # ── 쓰기 전 검사 ────────────────────────────────────────────────────
    if args.method is not None:
        try:
            method = MeasurementMethod(args.method)
        except ValueError:
            raise SystemExit(
                f"모르는 측정 방법: {args.method!r}\n"
                f"  허용: {', '.join(m.value for m in MeasurementMethod)}"
            ) from None
        if method in SIMULATION_METHODS:
            raise SystemExit(
                f"거부: {method}는 시뮬레이터에서 온 값이다 —"
                " 실기 근거로 기록하지 않는다")
        if args.verified and method not in HARDWARE_EVIDENCE_METHODS:
            raise SystemExit(
                f"거부: {method}로는 verified로 둘 수 없다"
                f" (실기 근거 방법: "
                f"{', '.join(sorted(m.value for m in HARDWARE_EVIDENCE_METHODS))})")
    evidence = check_evidence_file(args.evidence) if args.evidence else None

    before = dict(row)
    if args.value is not None:
        row["value"] = parse_value(args.value, numeric=row.get("numeric", True),
                                    unit=args.unit or row.get("unit", ""))
    if args.unit is not None:
        row["unit"] = args.unit
    if args.method is not None:
        row["measurement_method"] = args.method
    if args.source is not None:
        row["source"] = args.source
    if args.captured_at is not None:
        row["captured_at"] = args.captured_at
    if args.operator is not None:
        row["operator"] = args.operator
    if evidence is not None:
        row["evidence_path"] = str(evidence.relative_to(ROOT))
    if args.derivation is not None:
        row["derivation"] = args.derivation
    if args.note is not None:
        row["note"] = args.note
    if args.verified:
        row["verified"] = True
    if args.unverify:
        row["verified"] = False

    # ── 계약 검사 ──────────────────────────────────────────────────────
    try:
        item = load_input(row)
    except HardwareInputError as exc:
        raise SystemExit(f"계약 위반으로 기록하지 않았다: {exc}") from None

    write_json(config_path, payload)
    # 쓴 뒤 다시 읽어 전체 설정이 여전히 통과하는지 본다.
    try:
        hardware_readiness.load_input_set(config_path)
    except Exception as exc:  # noqa: BLE001 — 되돌린다
        payload_rollback = json.loads(config_path.read_text(encoding="utf-8"))
        for r in payload_rollback["inputs"]:
            if r["key"] == args.key:
                r.clear()
                r.update(before)
        write_json(config_path, payload_rollback)
        raise SystemExit(f"설정 전체 읽기에 실패해 되돌렸다: {exc}") from None

    append_log({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": "input", "key": args.key,
        "operator": row.get("operator"),
        "measurement_method": row.get("measurement_method"),
        "evidence_path": row.get("evidence_path"),
        "verified": row.get("verified"),
        "tool": "scripts/record_hardware_evidence.py",
    })
    print(f"[기록] {args.key} · 방법 {item.measurement_method}"
          f" · 작업자 {item.operator or '(없음)'}"
          f" · 근거 {item.evidence_path or '(없음)'}")
    print(f"       verified={item.verified} · 준비됨={item.ready}")
    if not item.ready:
        gaps = [name for name in
                ("value", "source", "captured_at", "evidence_path", "operator")
                if not getattr(item, name if name != "value" else "has_value")]
        print(f"       아직 준비되지 않았다 — 빈 항목: {', '.join(gaps) or 'verified'}")
    if item.evidence_requirements:
        print("       근거가 이 항목들을 모두 보여야 한다:")
        for line in item.evidence_requirements:
            print(f"         - {line}")
    return 0


def record_step(args) -> int:
    config_path = ROOT / manifest()["checklist"]
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    row = next((r for r in payload["steps"] if r["key"] == args.key), None)
    if row is None:
        keys = ", ".join(r["key"] for r in payload["steps"])
        raise SystemExit(f"그런 단계가 없다: {args.key}\n  있는 단계: {keys}")

    if args.status is not None:
        try:
            StepStatus(args.status)
        except ValueError:
            raise SystemExit(
                f"모르는 상태: {args.status!r}\n"
                f"  허용: {', '.join(s.value for s in StepStatus)}") from None
    evidence = check_evidence_file(args.evidence) if args.evidence else None

    before = dict(row)
    if args.status is not None:
        row["status"] = args.status
    if args.captured_at is not None:
        row["captured_at"] = args.captured_at
    if args.operator is not None:
        row["operator"] = args.operator
    if evidence is not None:
        row["evidence_path"] = str(evidence.relative_to(ROOT))
    if args.detail is not None:
        row["detail"] = args.detail

    # `passed`는 근거 세 항목이 모두 있을 때만 **적을 수 있다.**
    # 관문이 뒤에서 `evidence_missing`으로 되돌리기도 하지만, 근거 없는 통과를
    # 설정에 남기지 않는 것이 먼저다 — 파일만 보고 통과로 오인하지 않게.
    if row.get("status") == StepStatus.PASSED.value:
        gaps = [name for name in ("evidence_path", "captured_at", "operator")
                if not row.get(name)]
        if gaps:
            raise SystemExit(
                f"거부: passed로 두려면 {', '.join(gaps)}가 있어야 한다.\n"
                "  '했다'는 말만으로 통과로 적지 않는다."
                " 근거 파일·시각·수행한 사람을 모두 채운다.")

    write_json(config_path, payload)
    try:
        checklist = hardware_readiness.load_checklist(config_path)
    except Exception as exc:  # noqa: BLE001 — 되돌린다
        rollback = json.loads(config_path.read_text(encoding="utf-8"))
        for r in rollback["steps"]:
            if r["key"] == args.key:
                r.clear()
                r.update(before)
        write_json(config_path, rollback)
        raise SystemExit(f"체크리스트 읽기에 실패해 되돌렸다: {exc}") from None

    step = next(s for s in checklist.steps if s.key == args.key)
    append_log({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": "checklist_step", "key": args.key,
        "status": step.status.value,
        "effective_status": step.effective_status.value,
        "operator": step.operator,
        "evidence_path": step.evidence_path,
        "tool": "scripts/record_hardware_evidence.py",
    })
    print(f"[기록] {step.no}. {step.label}")
    print(f"       적은 상태 {step.status} → 판정 상태 **{step.effective_status}**")
    if step.status is StepStatus.PASSED and not step.done:
        print("       근거가 모자라 통과로 세지 않았다"
              " — evidence_path·captured_at·operator를 모두 채운다")
    if step.evidence_fields:
        print("       근거에 이 항목들이 들어 있어야 한다:")
        for line in step.evidence_fields:
            print(f"         - {line}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="실기 근거를 계약 검사를 지나서 기록한다 (실제 장비에"
                    " 명령을 보내지 않는다)")
    sub = parser.add_subparsers(dest="kind", required=True)

    one = sub.add_parser("input", help="실기 입력에 값·근거를 넣는다")
    one.add_argument("key")
    one.add_argument("--value", help="측정한 값. 숫자·리스트·객체는 JSON으로")
    one.add_argument("--unit")
    one.add_argument("--method", help="측정 방법(drawing·cad·scale·gauge·"
                                       "photo·robot_measurement)")
    one.add_argument("--source", help="사람이 찾아갈 수 있는 출처")
    one.add_argument("--captured-at", help="ISO 8601 (예: 2026-09-20)")
    one.add_argument("--operator", help="측정한 사람")
    one.add_argument("--evidence", help="근거 파일 경로(실제로 있어야 한다)")
    one.add_argument("--derivation", help="계산값이면 쓴 공식과 입력값")
    one.add_argument("--note")
    one.add_argument("--verified", action="store_true",
                     help="확인 완료로 둔다(계약 조건을 모두 만족해야 한다)")
    one.add_argument("--unverify", action="store_true",
                     help="확인 상태를 내린다")

    step = sub.add_parser("step", help="체크리스트 단계 상태를 바꾼다")
    step.add_argument("key")
    step.add_argument("--status", help="not_started·in_progress·passed·"
                                        "failed·evidence_missing")
    step.add_argument("--captured-at")
    step.add_argument("--operator", help="수행한 사람")
    step.add_argument("--evidence", help="근거 파일 경로(실제로 있어야 한다)")
    step.add_argument("--detail")

    args = parser.parse_args()
    if args.kind == "input":
        return record_input(args)
    return record_step(args)


if __name__ == "__main__":
    raise SystemExit(main())
