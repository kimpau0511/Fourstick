#!/usr/bin/env python3
"""웹 명령 입력 textarea 회귀 확인 (8-13 부수 수정).

```
/home/asd/forstick/venv/bin/python scripts/verify_web_textarea.py --base http://127.0.0.1:8093
```

Playwright(headless Chromium)로 실제 화면을 열어 글자를 하나씩 넣으며 확인한다.
**계획 생성을 누르지 않는다** — 모델·로봇에 아무것도 보내지 않는다.

| # | 항목 | 기대 |
|---|---|---|
| 01 | 연속 입력 뒤 값 | 넣은 글자가 모두 남는다 |
| 02 | 입력 중 focus | 매 글자 뒤 activeElement가 textarea다 |
| 03 | 입력 중 caret | 커서가 항상 끝(입력 길이)에 있다 |
| 04 | textarea 동일성 | 입력 전 표식이 입력 뒤에도 같은 요소에 남아 있다(갈아 끼우지 않았다) |
| 05 | 카드 재렌더링 | `#card-command`의 childList 변경 0회 |
| 06 | 버튼 상태 | 비었을 때 잠김 → 입력 뒤 풀림 → 지우기 뒤 다시 잠김 |
| 07 | 지우기 | 지우기 버튼 뒤 값이 비고 focus 유지 여부를 기록한다(요구는 아니다) |
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/web/textarea_check.json"
TEXT = "1번 팔레트로 이동한 뒤 안전 위치로 복귀해줘 abc 123"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8093")
    parser.add_argument("--delay-ms", type=int, default=60)
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    checks: list[dict] = []

    def check(key: str, passed: bool, detail: str) -> None:
        checks.append({"key": key, "passed": bool(passed), "detail": detail})
        print(f"[{'OK ' if passed else 'NG '}] {key}: {detail}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.goto(args.base, wait_until="domcontentloaded")
        page.wait_for_selector("#command-input", timeout=15000)
        # 캐시를 무시한 새로고침에 해당한다 — 방금 연 새 컨텍스트라 캐시가 없다.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("#command-input", timeout=15000)
        page.evaluate("""() => {
            const ta = document.getElementById('command-input');
            ta.__marker = 'before-typing';
            window.__cardMutations = 0;
            const card = document.getElementById('card-command');
            new MutationObserver((records) => {
                for (const r of records) if (r.type === 'childList') window.__cardMutations += 1;
            }).observe(card, { childList: true });
            window.__buttonStates = [document.querySelector('[data-action="generate"]').disabled];
        }""")
        initial_disabled = page.evaluate(
            "() => document.querySelector('[data-action=\"generate\"]').disabled")
        page.click("#command-input")
        focus_ok = True
        caret_ok = True
        typed = ""
        for ch in TEXT:
            page.keyboard.type(ch)
            typed += ch
            time.sleep(args.delay_ms / 1000.0)
            state = page.evaluate("""() => {
                const ta = document.getElementById('command-input');
                return { active: document.activeElement && document.activeElement.id,
                         value: ta.value, start: ta.selectionStart, end: ta.selectionEnd,
                         marker: ta.__marker || null,
                         disabled: document.querySelector('[data-action="generate"]').disabled };
            }""")
            if state["active"] != "command-input":
                focus_ok = False
            if state["start"] != len(typed) or state["end"] != len(typed):
                caret_ok = False
        final = page.evaluate("""() => {
            const ta = document.getElementById('command-input');
            return { value: ta.value, marker: ta.__marker || null,
                     mutations: window.__cardMutations,
                     disabled: document.querySelector('[data-action="generate"]').disabled,
                     active: document.activeElement && document.activeElement.id };
        }""")
        check("01_value_complete", final["value"] == TEXT,
              f"기대 {len(TEXT)}자, 실제 {len(final['value'])}자: {final['value']!r}")
        check("02_focus_kept", focus_ok, "매 글자 뒤 activeElement=command-input" if focus_ok
              else "입력 중 focus를 잃었다")
        check("03_caret_at_end", caret_ok, "커서가 항상 입력 끝에 있었다" if caret_ok
              else "커서 위치가 어긋났다")
        check("04_same_element", final["marker"] == "before-typing",
              f"표식 {final['marker']!r} (before-typing이면 같은 요소)")
        check("05_no_card_rerender", final["mutations"] == 0,
              f"#card-command childList 변경 {final['mutations']}회")
        # 1초 tick이 몇 번 지나도 같은지 본다.
        time.sleep(2.5)
        after_tick = page.evaluate("""() => ({
            value: document.getElementById('command-input').value,
            marker: document.getElementById('command-input').__marker || null,
            mutations: window.__cardMutations,
            active: document.activeElement && document.activeElement.id })""")
        check("05b_stable_across_ticks",
              after_tick["value"] == TEXT and after_tick["marker"] == "before-typing"
              and after_tick["mutations"] == 0 and after_tick["active"] == "command-input",
              f"2.5초 뒤 값 {len(after_tick['value'])}자 · 표식 {after_tick['marker']} ·"
              f" 변경 {after_tick['mutations']}회 · focus {after_tick['active']}")
        page.click('[data-action="clear-command"]')
        time.sleep(0.3)
        cleared = page.evaluate("""() => ({
            value: document.getElementById('command-input').value,
            disabled: document.querySelector('[data-action="generate"]').disabled,
            active: document.activeElement && document.activeElement.id })""")
        check("06_button_states",
              initial_disabled is True and final["disabled"] is False and cleared["disabled"] is True,
              f"비었을 때 {initial_disabled} → 입력 뒤 {final['disabled']} → 지우기 뒤 {cleared['disabled']}")
        check("07_clear", cleared["value"] == "",
              f"지우기 뒤 값 {cleared['value']!r} · focus {cleared['active']} (focus는 요구가 아니다)")
        page.screenshot(path=str(ROOT / "reports/web/textarea_check.png"))
        browser.close()

    passed = sum(1 for c in checks if c["passed"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "base": args.base,
        "text": TEXT, "delay_ms": args.delay_ms, "checks": checks,
        "passed": passed, "total": len(checks),
        "note": "headless Chromium. 키 입력은 Playwright keyboard.type — 한글 IME 조합 자체는"
                " 흉내내지 않는다. 카드 재렌더링(요소 교체)이 없으면 조합이 끊기지 않는다.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{passed}/{len(checks)} 통과 — {OUT.relative_to(ROOT)}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
