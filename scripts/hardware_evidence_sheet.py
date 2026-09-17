#!/usr/bin/env python3
"""실기 근거 수집 시트 (8-12).

```
python3 scripts/hardware_evidence_sheet.py
```

무엇이 모자라는지 **사람이 읽을 수 있는 문장**으로 출력하고
`reports/hardware/collection_sheet.md`에 같은 내용을 쓴다.

입력 18건과 체크리스트 11단계를 각각 나눠 적고, 항목마다

- 지금 상태와 왜 막혔는지(이유 코드)
- 무엇을 가져와야 하는지
- 근거에 무엇이 보여야 하는지
- 누가 수행하는지(사람이 장비를 만지는 단계인가)
- 기록에 쓸 명령 한 줄

을 함께 적는다. **실제 장비에 명령을 보내지 않는다.**
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from robots.hardware.config import connection_state  # noqa: E402
from validation import hardware_readiness  # noqa: E402
from validation.hardware_readiness import HUMAN_ACTORS, StepStatus  # noqa: E402

MANIFEST = ROOT / "config/hardware/active.json"
OUT = ROOT / "reports/hardware/collection_sheet.md"

ACTOR_LABEL = {
    "field_operator": "현장 작업자(손)",
    "field_operator_with_robot": "현장 작업자(실제 장비 조작)",
    "code": "코드(근거 수신 후 판정)",
}


def load() -> tuple[dict, object, object, dict]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    inputs = hardware_readiness.load_input_set(ROOT / manifest["inputs"])
    checklist = hardware_readiness.load_checklist(ROOT / manifest["checklist"])
    procedures = {}
    if manifest.get("procedures"):
        path = ROOT / manifest["procedures"]
        if path.is_file():
            procedures = json.loads(path.read_text(encoding="utf-8"))
    return manifest, inputs, checklist, procedures


def input_command(key: str) -> str:
    return (f"python3 scripts/record_hardware_evidence.py input {key} \\\n"
            f"    --value <값> --unit <단위> --method <측정방법> \\\n"
            f"    --source \"<출처>\" --captured-at <YYYY-MM-DD> \\\n"
            f"    --operator \"<측정한 사람>\" \\\n"
            f"    --evidence reports/hardware/evidence/<파일> --verified")


def step_command(key: str) -> str:
    return (f"python3 scripts/record_hardware_evidence.py step {key} \\\n"
            f"    --status passed --captured-at <YYYY-MM-DD> \\\n"
            f"    --operator \"<수행한 사람>\" \\\n"
            f"    --evidence reports/hardware/evidence/<파일>")


def build() -> tuple[str, dict]:
    manifest, inputs, checklist, procedures = load()
    adapter = connection_state()
    result = hardware_readiness.evaluate(
        inputs=inputs, checklist=checklist,
        adapter_config=(None if not adapter.get("configured") else {
            "arm": adapter.get("arm_kind"), "gripper": adapter.get("gripper_kind")}),
        grasp_observation=None, hardware_connected=False)
    findings = {item.key: item for item in result.findings}
    # 시뮬레이터 축은 **기록에서만** 읽는다. 판정에 넣지 않는다.
    sim_report = ROOT / "reports/workcell/pick_place_sim_e2e_verify.json"
    sim = (json.loads(sim_report.read_text(encoding="utf-8"))
           if sim_report.is_file() else {})
    transfers = [row for row in sim.get("checks", ())
                 if str(row.get("key", "")).startswith(
                     ("01_e2e", "02_e2e", "03_e2e"))]
    state = hardware_readiness.pinned_state(
        result,
        simulation_e2e=(bool(transfers)
                        and all(row.get("passed") for row in transfers)
                        if transfers else None))

    lines: list[str] = []
    add = lines.append
    add("# 실기 근거 수집 시트")
    add("")
    add(f"생성: {time.strftime('%Y-%m-%dT%H:%M:%S%z')} ·"
        f" 설정 `{manifest['inputs']}`")
    add("")
    add("> **실제 하드웨어는 연결되지 않았다.** 이 시트는 무엇을 가져와야"
        " 하는지의 목록이며, Gazebo 결과는 실기 근거가 아니다.")
    add("")
    add("## 지금 상태 (고정)")
    add("")
    add("```text")
    for line in hardware_readiness.state_lines(state):
        add(line)
    add("```")
    add("")
    add("`hardware_evidence`·`hardware_checklist`는 **근거 수집 수**다."
        " 차단 검사 통과 수와 바꿔 읽지 않는다.")
    add("")
    add("| 항목 | 값 |")
    add("|---|---|")
    add(f"| 실기 어댑터 설정 | {'선언됨' if adapter.get('configured') else '없음'} |")
    add(f"| 미충족 항목 | **{len(result.findings)}건** |")
    add("")

    # ── 입력 ────────────────────────────────────────────────────────────
    add("## 1. 실기 입력 (18건)")
    add("")
    for item in inputs.inputs:
        finding = findings.get(item.key)
        mark = "✅ 확보" if item.ready else "❌ 미확보"
        add(f"### {mark} — {item.label}")
        add("")
        add(f"- 키: `{item.key}` · 단위: `{item.unit or '—'}`"
            f" · 측정 방법: `{item.measurement_method}`")
        if item.has_value:
            add(f"- 지금 값: `{json.dumps(item.value, ensure_ascii=False)}`"
                f"{' (확인 전)' if not item.verified else ''}")
        else:
            add("- 지금 값: **없음**")
        if finding is not None:
            add(f"- 막힌 이유: `{finding.reason_code.value}` — {finding.detail}")
        if item.needed:
            add(f"- **가져와야 하는 것**: {item.needed}")
        if item.evidence_requirements:
            add("- 근거에 보여야 하는 것:")
            for line in item.evidence_requirements:
                add(f"  - {line}")
        if item.derivation:
            add(f"- 산출 근거: {item.derivation}")
        add("- 기록 명령:")
        add("")
        add("  ```bash")
        for line in input_command(item.key).splitlines():
            add(f"  {line}")
        add("  ```")
        add("")

    # ── 체크리스트 ──────────────────────────────────────────────────────
    add("## 2. 장착 전·후 체크리스트 (11단계)")
    add("")
    add("실제 장비를 연결하거나 움직이는 단계는 **현장 작업자가 수행한다.**"
        " 코드가 대신하지 않고, 대신한 것처럼 기록하지도 않는다.")
    add("")
    for step in checklist.steps:
        done = step.done
        mark = "✅ 통과" if done else f"❌ {step.effective_status}"
        add(f"### {mark} — {step.no}. {step.label}")
        add("")
        add(f"- 키: `{step.key}` · 수행: **{ACTOR_LABEL.get(step.actor.value, step.actor.value)}**")
        if step.status is StepStatus.PASSED and not done:
            add("- ⚠ `passed`로 적혀 있으나 근거가 모자라"
                " `evidence_missing`으로 판정됐다"
                " — `evidence_path`·`captured_at`·`operator`를 모두 채운다")
        if step.requires_inputs:
            pending = [key for key in step.requires_inputs
                       if (inputs.get(key) is None or not inputs.get(key).ready)]
            add(f"- 먼저 필요한 입력: {', '.join(f'`{k}`' for k in step.requires_inputs)}"
                + (f" — 아직 미확보: {', '.join(pending)}" if pending else " (모두 확보)"))
        if step.needed:
            add(f"- **해야 하는 것**: {step.needed}")
        if step.evidence_fields:
            add("- 근거 양식(이 항목들이 들어 있어야 한다):")
            for line in step.evidence_fields:
                add(f"  - {line}")
        add("- 기록 명령:")
        add("")
        add("  ```bash")
        for line in step_command(step.key).splitlines():
            add(f"  {line}")
        add("  ```")
        add("")

    # ── 절차 ────────────────────────────────────────────────────────────
    if procedures:
        add("## 3. 측정·연결 절차")
        add("")
        add(f"절차 문서: `{manifest.get('procedures')}`")
        add("")
        for key, label in (("tcp_measurement", "TCP 측정"),
                           ("cell_frame_calibration", "작업 셀 frame 보정"),
                           ("connection_requirements", "실기 연결 요구사항"),
                           ("grasp_observation_capture", "실기 파지 관측 수집")):
            block = procedures.get(key)
            if not block:
                continue
            add(f"### {label}")
            add("")
            actor = block.get("actor")
            if actor:
                add(f"- 수행: **{ACTOR_LABEL.get(actor, actor)}**")
            for name, title in (("preconditions", "선행 조건"),
                                ("steps", "절차"),
                                ("records", "남길 기록"),
                                ("do_not", "하지 않는 것")):
                rows = block.get(name)
                if not rows:
                    continue
                add(f"- {title}:")
                for line in rows:
                    add(f"  - {line}")
            add("")

    # ── 어댑터 ──────────────────────────────────────────────────────────
    add("## 4. 실기 어댑터 설정")
    add("")
    add(f"- 상태: {'선언됨' if adapter.get('configured') else '**없음**'}")
    add(f"- 판정: `{adapter.get('reason_code') or '—'}`")
    add(f"- 상세: {adapter.get('detail')}")
    add("")
    add("주소·인증값·포트·한계값은 **저장소에 적지 않는다.** 실행 환경에서만"
        " 들어오고, 없으면 어댑터를 만들 수 없다.")
    add("")
    add("## 5. 이 시트가 열리지 않는 조건")
    add("")
    add("위 항목이 모두 채워지기 전까지 다음이 유지된다.")
    add("")
    add("- `real_hardware_ready = false`")
    add("- `real_hardware_verified = false`")
    add("- `FR3HardwareAdapter.connect()` 차단 — 연결을 시도조차 하지 않는다")
    add("- 실제 pick/place 실행 차단")
    add("- Gazebo `simulation_e2e` 결과를 `real_hardware_ready`로 바꾸려는 시도는"
        " **예외**")
    add("")
    payload = result.to_dict()
    payload["state"] = state
    payload["state_lines"] = list(hardware_readiness.state_lines(state))
    return "\n".join(lines) + "\n", payload


def main() -> int:
    sheet, payload = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(sheet, encoding="utf-8")

    # 화면에는 미충족 항목만 문장으로 추린다.
    print("지금 상태 (고정)")
    print("-" * 60)
    for line in payload["state_lines"]:
        print(f"  {line}")
    print("-" * 60)
    print("실기 근거 수집 — 미충족 항목")
    print("=" * 60)
    for row in payload["findings"]:
        print(f"· [{row['reason_code']}] {row['label']}")
        need = (row.get("needed") or row.get("detail") or "").strip()
        if need:
            print(f"    → {need[:200]}")
    print("=" * 60)
    print(f"미충족 {len(payload['findings'])}건")
    print(f"시트: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
