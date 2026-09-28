#!/usr/bin/env python3
"""웹 E2E — G1 명령 입력 → 확인 → 왕복 → 완료, 이동 중 STOP, 출발 위치 복귀, 그리고 같은 브라우저에서
FR3 색 지정 이송·원래 팔레트 복귀. 로봇 선택에 따라 요청이 섞이지 않는지 본다.

    <playwright가 있는 python> scripts/verify_web_g1_e2e.py --base http://127.0.0.1:8094

- 실제 화면: 로봇 선택 창 → 명령 입력칸 → 명령 보내기 → 확인 카드 버튼.
- 판정: 서버 작업 기록(도착은 서버가 Gazebo pose로 재확인) + 이 스크립트가 따로 읽은
  `/v1/humanoid/view/state`(Gazebo 관측) + 화면 문구. 시간은 시뮬레이션·벽시계를 따로 적는다.
- 결과: reports/web/g1_web_e2e.json + 화면 캡처 reports/web/g1_web_e2e/.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports/web/g1_web_e2e.json"
SHOTS = ROOT / "reports/web/g1_web_e2e"
CHROME = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


class BaseWatch:
    """G1 골반 관측(서버의 Gazebo 구독) 기록."""

    def __init__(self, base):
        self.base, self.rows, self.stop_ev = base, [], threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stop_ev.is_set():
            try:
                s = get(self.base, "/v1/humanoid/view/state")
                if s.get("base_pose"):
                    x, y, z, qx, qy, qz, qw = s["base_pose"]
                    self.rows.append({"wall": time.time(), "sim": s["base_sim_time"], "x": x, "y": y,
                                      "z": z, "yaw": math.atan2(2 * (qw * qz + qx * qy),
                                                                1 - 2 * (qy * qy + qz * qz)),
                                      "stale": s["stale"]})
            except Exception:  # noqa: BLE001
                pass
            self.stop_ev.wait(0.1)

    def last(self):
        return self.rows[-1] if self.rows else None

    def span(self, w0, w1):
        rows = [r for r in self.rows if w0 <= r["wall"] <= w1]
        if not rows:
            return {}
        return {"samples": len(rows), "sim_s": round(rows[-1]["sim"] - rows[0]["sim"], 3),
                "wall_s": round(rows[-1]["wall"] - rows[0]["wall"], 3),
                "path_m": round(sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"])
                                    for a, b in zip(rows, rows[1:])), 3),
                "min_z": round(min(r["z"] for r in rows), 3),
                "start": [round(rows[0]["x"], 3), round(rows[0]["y"], 3), round(rows[0]["yaw"], 3)],
                "end": [round(rows[-1]["x"], 3), round(rows[-1]["y"], 3), round(rows[-1]["yaw"], 3)],
                "stale_samples": sum(r["stale"] for r in rows)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--chrome", default=str(CHROME))
    parser.add_argument("--stop-after-m", type=float, default=0.8)
    parser.add_argument("--skip-fr3", action="store_true")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    api: list[dict] = []
    report = {"schema": "forstick2.web_g1_e2e/1", "is_simulated": True, "base": args.base,
              "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "steps": []}
    watch = BaseWatch(args.base)

    def step(name, **data):
        report["steps"].append({"name": name, **data})
        print(json.dumps({"step": name, **{k: v for k, v in data.items() if k != "api"}},
                         ensure_ascii=False)[:900], flush=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=args.chrome)
        page = browser.new_page(viewport={"width": 1440, "height": 1100})

        def on_response(resp):
            url = resp.url
            if "/v1/" not in url or "/view/" in url or "/v1/sim-view" in url or "/v1/scene" in url:
                return
            path = url.split(args.base, 1)[-1]
            if resp.request.method == "GET" and path.split("?")[0] in ("/v1/sim-demo", "/v1/humanoid"):
                return
            row = {"t": time.time(), "method": resp.request.method, "path": path, "status": resp.status}
            try:
                body = resp.json()
                row["body"] = {k: body.get(k) for k in ("decision", "reason", "status", "intent")
                               if k in body}
                if isinstance(body.get("job"), dict):
                    row["body"]["job_id"] = body["job"].get("job_id")
            except Exception:  # noqa: BLE001
                pass
            api.append(row)

        page.on("response", on_response)
        page.goto(args.base + "/", wait_until="load")
        page.wait_for_selector("#command-input")
        page.wait_for_timeout(1500)

        def select(robot_id):
            page.evaluate("document.querySelector('[data-action=\"open-robot\"]').click()")
            page.wait_for_selector(f'[data-action="select-robot"][data-robot="{robot_id}"]')
            page.evaluate("id => document.querySelector(`[data-action=\"select-robot\"][data-robot=\"${id}\"]`).click()",
                          robot_id)
            page.wait_for_timeout(800)

        def send(text):
            box = page.locator("#command-input")
            box.click()
            box.fill("")
            box.type(text, delay=15)
            n0 = len(api)
            page.evaluate("document.querySelector('[data-action=\"generate\"]').click()")
            return n0

        def g1_text():
            return page.inner_text("#card-humanoid")

        def wait_for(pred, timeout, every=300):
            deadline = time.time() + timeout
            while time.time() < deadline:
                v = pred()
                if v:
                    return v
                page.wait_for_timeout(every)
            return None

        def wait_job_end(timeout=240):
            def done():
                h = get(args.base, "/v1/humanoid")
                j = h.get("running_job")
                return None if j else True
            wait_for(done, timeout, 500)
            page.wait_for_timeout(2500)

        # ── G1 선택 ───────────────────────────────────────────────────────
        page.evaluate("document.querySelector('[data-action=\"open-robot\"]').click()")
        page.wait_for_selector('[data-action="select-robot"]')
        page.screenshot(path=str(SHOTS / "00_robot_modal.png"))
        select("unitree_g1")
        vis = page.evaluate("""() => ({g1: !document.getElementById('card-humanoid').hidden,
            fr3_3d: !document.getElementById('card-sim3d').hidden,
            title: document.querySelector('#card-command .card-title').textContent})""")
        view_ok = wait_for(lambda: "Gazebo 관측" in page.inner_text("#g1-3d-status"), 20)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "01_g1_selected.png"), full_page=True)
        step("select_g1", visibility=vis, view_live=bool(view_ok),
             view_status=page.inner_text("#g1-3d-status"), g1_card=g1_text()[:600])

        # ── 출발 위치 없음 → 되묻기 ─────────────────────────────────────────
        n0 = send("출발 위치로 돌아와")
        wait_for(lambda: "되묻기" in g1_text(), 15)
        step("return_without_start", ask_shown="되묻기" in g1_text(),
             text=page.inner_text("#g1-result")[:400], api=api[n0:])

        # ── 찍고 와: 확인 → 왕복 → 완료 ─────────────────────────────────────
        n0 = send("컨베이어 한번 찍고 와")
        page.wait_for_selector('[data-g1="confirm"]', timeout=20000)
        page.screenshot(path=str(SHOTS / "10_tag_confirm.png"), full_page=True)
        card = page.inner_text("#g1-result")
        w0 = time.time()
        page.evaluate("document.querySelector('[data-g1=\"confirm\"]').click()")
        wait_for(lambda: "실행 중" in g1_text(), 15)
        page.wait_for_timeout(4000)
        page.screenshot(path=str(SHOTS / "11_tag_running.png"), full_page=True)
        # 3D 화면이 이동 중 관측을 따라가는지(정지 화면 표시 횟수) 표본을 뜬다.
        badges = []
        def sample_badge():
            badges.append(page.inner_text("#g1-3d-status"))
            h = get(args.base, "/v1/humanoid")
            return None if h.get("running_job") else True
        wait_for(sample_badge, 240, 500)
        wait_job_end()
        w1 = time.time()
        page.screenshot(path=str(SHOTS / "12_tag_done.png"), full_page=True)
        last_jobs = [r for r in api[n0:] if r["path"].startswith("/v1/humanoid/confirm")]
        step("tag_roundtrip", confirm_card=card[:700],
             completed_shown="완료 — Gazebo 관측으로 도착·복귀 확인" in g1_text(),
             job_text=page.inner_text("#g1-job")[:900], motion=watch.span(w0, w1), api=api[n0:],
             confirm_calls=len(last_jobs),
             view_badge_samples=len(badges),
             view_badge_stale=sum("정지 화면" in b for b in badges),
             view_badge_example=badges[len(badges) // 2] if badges else None)

        # ── 이동 중 STOP ──────────────────────────────────────────────────
        n0 = send("컨베이어 앞에 가")
        page.wait_for_selector('[data-g1="confirm"]', timeout=20000)
        start = watch.last()
        w0 = time.time()
        page.evaluate("document.querySelector('[data-g1=\"confirm\"]').click()")
        moved = wait_for(lambda: (lambda r: r and math.hypot(r["x"] - start["x"], r["y"] - start["y"])
                                  >= args.stop_after_m)(watch.last()), 90, 100)
        ws = time.time()
        at_trigger = watch.last()
        n1 = send("멈춰")
        t_clicked = time.time()
        wait_for(lambda: "균형 상태" in page.inner_text("#g1-result"), 10, 100)
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "20_stop.png"), full_page=True)
        stop_text = page.inner_text("#g1-result")
        # 정지 뒤 10 s(시뮬레이션) 동안: 목표로 계속 가지 않는지, 표류가 자리 유지로 묶이는지.
        s0 = watch.last()["sim"]
        wait_for(lambda: watch.last()["sim"] >= s0 + 10.0, 60, 200)
        page.screenshot(path=str(SHOTS / "21_stop_hold.png"), full_page=True)
        hold_text = page.inner_text("#g1-result")
        w1 = time.time()
        stop_call = next((r for r in api[n1:] if r["path"] == "/v1/humanoid/command"), None)
        applied_sim = None
        try:
            applied_sim = float(stop_text.split("시뮬레이션 ")[1].split(" s")[0])
        except (IndexError, ValueError):
            pass
        # 정지가 **적용된** 시각(시뮬레이션) 이후만 — 입력 지연 동안 간 거리는 따로 적는다.
        after = [r for r in watch.rows if r["wall"] >= ws and
                 (applied_sim is None or r["sim"] >= applied_sim)]
        p0 = after[0]
        travel = max(math.hypot(r["x"] - p0["x"], r["y"] - p0["y"]) for r in after)
        goal = (2.15, 0.0)
        closer = math.hypot(after[-1]["x"] - goal[0], after[-1]["y"] - goal[1]) < \
            math.hypot(p0["x"] - goal[0], p0["y"] - goal[1]) - 0.3
        step("stop_while_moving", moved_before_stop=bool(moved), stop_text=stop_text[:600],
             hold_text=hold_text[:600], max_travel_after_stop_m=round(travel, 3),
             kept_going_to_goal=closer,
             confirm_to_trigger_wall_s=round(ws - w0, 2),
             trigger_to_click_wall_s=round(t_clicked - ws, 3),
             trigger_to_server_response_wall_s=None if not stop_call else round(stop_call["t"] - ws, 3),
             stop_applied_sim=applied_sim,
             sim_from_trigger_to_applied_s=None if applied_sim is None else round(applied_sim - at_trigger["sim"], 3),
             dist_to_goal_at_trigger_m=round(math.hypot(at_trigger["x"] - goal[0], at_trigger["y"] - goal[1]), 3),
             stop_applied_before_arrival="취소됨 (conveyor_front)" in stop_text,
             job_text=page.inner_text("#g1-job")[:600],
             no_confirm_for_stop=not any(r["path"].startswith("/v1/humanoid/confirm") for r in api[n1:]),
             motion_before=watch.span(w0, ws), motion_after_stop=watch.span(ws, w1),
             g1_health=page.inner_text("#g1-health")[:300], api=api[n1:])

        # ── 출발 위치로 돌아와(이 세션 마지막 작업의 출발 위치) ─────────────
        n0 = send("출발 위치로 돌아와")
        page.wait_for_selector('[data-g1="confirm"]', timeout=20000)
        card = page.inner_text("#g1-result")
        w0 = time.time()
        page.evaluate("document.querySelector('[data-g1=\"confirm\"]').click()")
        wait_for(lambda: "실행 중" in g1_text(), 15)
        wait_job_end()
        w1 = time.time()
        page.screenshot(path=str(SHOTS / "30_return_done.png"), full_page=True)
        step("return_to_start", confirm_card=card[:600],
             completed_shown="완료 — Gazebo 관측으로 도착·복귀 확인" in g1_text(),
             job_text=page.inner_text("#g1-job")[:700], motion=watch.span(w0, w1), api=api[n0:])

        g1_calls = [r["path"] for r in api]
        report["g1_isolation"] = {
            "fr3_calls_while_g1": [p for p in g1_calls if p.startswith(("/v1/sim-demo", "/v1/plan"))]}

        # ── FR3: 색 지정 이송 + 원래 팔레트 복귀(같은 브라우저 세션) ──────────
        if not args.skip_fr3:
            n_fr3 = len(api)
            select("fairino_fr3")
            vis = page.evaluate("""() => ({g1: !document.getElementById('card-humanoid').hidden,
                fr3_3d: !document.getElementById('card-sim3d').hidden,
                title: document.querySelector('#card-command .card-title').textContent})""")
            step("select_fr3", visibility=vis)
            for text, expect in (("초록자재를 컨베이어로 옮겨줘", "simulation_transfer_completed"),
                                 ("초록자재다시팔레트로가져다놔", "returned_to_origin")):
                n0 = send(text)
                page.wait_for_selector('[data-action="sim-confirm"]', timeout=90000)
                page.screenshot(path=str(SHOTS / f"40_fr3_confirm_{expect}.png"), full_page=True)
                page.evaluate("document.querySelector('[data-action=\"sim-confirm\"]').click()")
                job_id = wait_for(lambda: next((r["body"].get("job_id") for r in api[n0:]
                                               if r["path"] == "/v1/sim-demo/confirm"
                                               and r.get("body")), None), 30)
                final = None
                deadline = time.time() + 300
                while job_id and time.time() < deadline:
                    detail = get(args.base, f"/v1/sim-demo/jobs/{job_id}")
                    if detail.get("status") not in ("running", "queued"):
                        final = {**detail, "report_status": (detail.get("report") or {}).get("status")}
                        break
                    time.sleep(2)
                page.wait_for_timeout(3000)
                page.screenshot(path=str(SHOTS / f"41_fr3_done_{expect}.png"), full_page=True)
                mats = get(args.base, "/v1/sim-view/state").get("materials", {})
                step(f"fr3_{expect}", final_job=final and {k: final.get(k) for k in
                                                           ("job_id", "status", "action", "material", "exit_code", "report_status")},
                     page_has_result=expect in page.inner_text("body"),
                     material_c=[round(v, 3) for v in mats.get("material_c", [])[:3]], api=api[n0:])
            report["fr3_isolation"] = {"g1_calls_while_fr3": [
                r["path"] for r in api[n_fr3:] if r["path"].startswith("/v1/humanoid")]}
        browser.close()
    watch.stop_ev.set()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("격리:", report.get("g1_isolation"), report.get("fr3_isolation"))
    print(f"기록: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
