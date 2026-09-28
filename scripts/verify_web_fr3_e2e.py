#!/usr/bin/env python3
"""FR3 웹 E2E — 실제 브라우저 입력 → 확인 카드 → 승인 → Gazebo 동작 → 완료 표시.

    <playwright가 있는 python> scripts/verify_web_fr3_e2e.py --base http://127.0.0.1:8094 \\
        --plan-text "1번 팔레트 위로 이동한 뒤 홈으로 복귀해줘" \\
        --sim-text "초록자재를 컨베이어로 옮겨줘"

- 화면에서 로봇 선택 창을 열어 FR3를 고르고, 명령 입력칸에 글자를 넣어 버튼을 누른다.
- 브라우저가 주고받은 `/v1/*` 응답을 모두 기록한다(어느 경로로 갔는지 확인).
- 움직임은 `/v1/sim-view/state`(Gazebo 관절 구독, 나이·stale 포함)를 실행 중 계속 읽어
  관절 변화량으로 확인한다. 화면 표시만으로 동작을 판정하지 않는다.
- 결과: reports/web/fr3_web_e2e.json + 단계별 화면 캡처.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/web/fr3_web_e2e.json"
SHOTS = ROOT / "reports/web/fr3_web_e2e"
CHROME = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"
ARM = ("j1", "j2", "j3", "j4", "j5", "j6")


class JointWatch:
    """실행 중 Gazebo 관절 관측을 계속 읽는다(웹 서버의 sim-view 구독)."""

    def __init__(self, base: str):
        self.base = base
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(f"{self.base}/v1/sim-view/state", timeout=2) as r:
                    state = json.loads(r.read())
                self.samples.append({"t": time.time(), "stale": state.get("stale"),
                                     "joints": {k: state["joints"].get(k) for k in ARM},
                                     "gripper": state["joints"].get("robotiq_85_left_knuckle_joint"),
                                     "materials": state.get("materials")})
            except Exception as exc:  # noqa: BLE001
                self.samples.append({"t": time.time(), "error": str(exc)})
            self._stop.wait(0.2)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(3)

    def summary(self, t0: float, t1: float) -> dict:
        rows = [s for s in self.samples if t0 <= s["t"] <= t1 and "joints" in s
                and not s.get("stale")]
        if not rows:
            return {"samples": 0}
        first = rows[0]["joints"]
        max_delta = max(max(abs((r["joints"][k] or 0) - (first[k] or 0)) for k in ARM)
                        for r in rows)
        last = rows[-1]["joints"]
        return {"samples": len(rows), "max_arm_delta_rad": round(max_delta, 4),
                "start": {k: round(first[k], 4) for k in ARM},
                "end": {k: round(last[k], 4) for k in ARM},
                "stale_samples": sum(1 for s in self.samples
                                     if t0 <= s["t"] <= t1 and s.get("stale"))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--plan-text", default="1번 팔레트 위로 이동한 뒤 홈으로 복귀해줘")
    parser.add_argument("--sim-text", default="")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--chrome", default=str(CHROME))
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    report: dict = {"schema": "forstick2.web_fr3_e2e/1", "is_simulated": True,
                    "base": args.base, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "scenarios": []}
    api: list[dict] = []
    watch = JointWatch(args.base).start()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=args.chrome)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def on_response(resp):
            url = resp.url
            if "/v1/" not in url or "/v1/sim-view" in url or "/v1/scene" in url:
                return
            row = {"t": time.time(), "method": resp.request.method,
                   "path": url.split(args.base, 1)[-1], "status": resp.status}
            try:
                body = resp.json()
                row["body"] = {k: body.get(k) for k in
                               ("decision", "reason", "reason_code", "detail", "ok", "status",
                                "execution_id", "plan_id", "intent", "error") if k in body}
                if isinstance(body.get("job"), dict):
                    row["body"]["job"] = {k: body["job"].get(k)
                                          for k in ("job_id", "status", "action", "material")}
            except Exception:  # noqa: BLE001
                pass
            api.append(row)

        page.on("response", on_response)
        page.goto(args.base + "/", wait_until="load")
        page.wait_for_selector("#command-input")
        page.wait_for_timeout(1500)

        # 로봇 선택: 선택 창을 열어 FR3를 누른다.
        # 머리글은 상태가 바뀔 때마다 다시 그려진다 — 페이지 안에서 누른다.
        page.evaluate("document.querySelector('[data-action=\"open-robot\"]').click()")
        page.wait_for_selector('[data-action="select-robot"]')
        page.screenshot(path=str(SHOTS / "00_robot_modal.png"))
        options = page.eval_on_selector_all(
            '[data-action="select-robot"]',
            "els => els.map(e => ({id: e.dataset.robot, text: e.innerText.split('\\n')[0],"
            " disabled: e.disabled}))")
        fr3 = next(o for o in options if "fr3" in o["id"].lower())
        page.evaluate("id => document.querySelector("
                      "`[data-action=\"select-robot\"][data-robot=\"${id}\"]`).click()", fr3["id"])
        page.wait_for_timeout(500)
        report["robot_options"] = options
        report["selected_robot"] = fr3

        def type_and_send(text: str, tag: str) -> float:
            box = page.locator("#command-input")
            box.click()
            box.fill("")
            box.type(text, delay=20)
            t = time.time()
            page.click('[data-action="generate"]')
            return t

        def wait_text(patterns, timeout, tag):
            deadline = time.time() + timeout
            while time.time() < deadline:
                body = page.inner_text("body")
                for p in patterns:
                    if p in body:
                        return p
                page.wait_for_timeout(500)
            page.screenshot(path=str(SHOTS / f"{tag}_timeout.png"))
            return None

        # ── 1) 일반 계획 경로(PASS_THROUGH → /v1/plan) ─────────────────────
        if args.plan_text:
            sc = {"name": "plan_path", "text": args.plan_text}
            n0 = len(api)
            t_send = type_and_send(args.plan_text, "plan")
            deadline = time.time() + 120
            while time.time() < deadline and not any(
                    r["path"].startswith("/v1/plan") for r in api[n0:]):
                page.wait_for_timeout(300)
            # 계획 뒤 검증·판정 결과가 화면에 올 때까지: 실행 버튼이 풀리거나 차단이 뜬다.
            hit = None
            while time.time() < deadline:
                if page.locator('[data-action="execute"]:not([disabled])').count():
                    hit = "execute_enabled"
                    break
                if "등록된 로봇이 없다" in page.inner_text("body"):
                    hit = "robot_missing"
                    break
                page.wait_for_timeout(500)
            page.wait_for_timeout(1000)
            page.screenshot(path=str(SHOTS / "10_plan_card.png"), full_page=True)
            sc["plan_card_text_hit"] = hit
            body = page.inner_text("body")
            sc["robot_missing_shown"] = "등록된 로봇이 없다" in body
            execute = page.locator('[data-action="execute"]:not([disabled])')
            sc["execute_enabled"] = execute.count() > 0
            if sc["execute_enabled"]:
                execute.first.click()
                page.wait_for_selector('[data-action="execute-confirm"]')
                page.screenshot(path=str(SHOTS / "11_execute_confirm.png"))
                t_exec = time.time()
                page.click('[data-action="execute-confirm"]')
                done = wait_text(["작업 성공", "작업 실패", "실행 실패", "취소됨", "정지됨"],
                                 args.timeout, "12_exec")
                t_end = time.time()
                page.wait_for_timeout(1500)
                page.screenshot(path=str(SHOTS / "12_exec_done.png"), full_page=True)
                sc["completion_text_hit"] = done
                sc["gazebo_motion"] = watch.summary(t_exec, t_end + 1.5)
            sc["api"] = api[n0:]
            sc["elapsed_s"] = round(time.time() - t_send, 1)
            report["scenarios"].append(sc)

        # ── 2) 시뮬레이션 명령 경로(/v1/sim-demo/command → 확인 카드) ───────
        if args.sim_text:
            sc = {"name": "sim_demo_path", "text": args.sim_text}
            n0 = len(api)
            t_send = type_and_send(args.sim_text, "sim")
            page.wait_for_selector('[data-action="sim-confirm"]', timeout=90000)
            page.screenshot(path=str(SHOTS / "20_sim_confirm_card.png"), full_page=True)
            t_exec = time.time()
            page.click('[data-action="sim-confirm"]')
            # 작업 상세를 읽어 끝날 때까지 기다린다(화면은 1.5 s마다 폴링한다).
            job_id, final = None, None
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                for row in api[n0:]:
                    job = (row.get("body") or {}).get("job") or {}
                    job_id = job_id or job.get("job_id")
                if job_id:
                    with urllib.request.urlopen(
                            f"{args.base}/v1/sim-demo/jobs/{job_id}", timeout=5) as r:
                        detail = json.loads(r.read())
                    if detail.get("status") not in ("running", "queued"):
                        final = {k: detail.get(k) for k in ("status", "exit_code", "action",
                                                            "material", "slot")}
                        final["report_status"] = (detail.get("report") or {}).get("status")
                        break
                page.wait_for_timeout(1000)
            t_end = time.time()
            page.wait_for_timeout(3000)
            page.screenshot(path=str(SHOTS / "21_sim_done.png"), full_page=True)
            sc["job_id"] = job_id
            sc["job_final"] = final
            # 마지막 작업 배지(안전 판단 카드)와 시연 카드의 결과 줄.
            body = page.inner_text("body")
            # 화면 배지 문구는 html/static/js/sim-demo.js RESULT_LABELS에서 온다.
            labels = {"simulation_transfer_completed": "이송 완료",
                      "returned_to_origin": "원래 슬롯 복귀"}
            status = (final or {}).get("report_status") or ""
            sc["page_shows_complete"] = bool(status) and status in body and \
                labels.get(status, "완료") in body
            sc["gazebo_motion"] = watch.summary(t_exec, t_end)
            sc["api"] = api[n0:]
            report["scenarios"].append(sc)

        browser.close()
    watch.stop()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for sc in report["scenarios"]:
        print(json.dumps({k: v for k, v in sc.items() if k != "api"}, ensure_ascii=False))
        for row in sc["api"]:
            print("   ", row["method"], row["path"], row["status"],
                  json.dumps(row.get("body"), ensure_ascii=False)[:220])
    print(f"기록: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
