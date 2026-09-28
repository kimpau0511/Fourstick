"""3D 작업 셀 화면 검증 — 실제 브라우저 프레임·지연 · Gazebo 관측과 위치 대조.

    source /opt/ros/lyrical/setup.bash
    FORSTICK2_SIM_PICK_PLACE_DEMO=1 <playwright가 있는 python> scripts/verify_sim_view.py \
        [--base http://127.0.0.1:8094] [--gpu d3d12|swiftshader] [--transfer material_a:slot_1]

playwright는 프로젝트 의존성이 아니다(검증 도구만 쓴다). 브라우저는 playwright가
받아 둔 chromium을 쓴다(`--chrome`).

하는 일:
1. 페이지를 열고 3D 화면이 관측을 그릴 때까지 기다린 뒤, 정지 상태 FPS·지연을 잰다.
2. **별도 프로세스(이 스크립트)가 Gazebo `pose/info`를 직접 구독**해 링크·자재 pose를
   기록하는 동안, 서버 목표 API로 대표 이송을 실행하고 브라우저가 그린 위치를 50 ms마다
   읽는다. 브라우저가 그린 상태의 관측 시각(서버 수신 시각)에서의 Gazebo pose와 비교한다
   (형상 오차), 같은 벽시계 시각의 Gazebo pose와도 비교한다(표시 지연이 포함된 오차).
   로봇 링크는 three.js가 URDF와 관절값으로 계산한 위치이므로 Gazebo 링크 pose와 독립이다.
3. 끊김 표시: (a) 화면 연결을 끊는다(검증 훅) (b) Gazebo를 잠시 일시정지한다(유휴일 때만).
   두 경우 모두 화면이 `stale`로 바뀌고 **위치가 움직이지 않는지** 본다.
4. 이송한 자재를 원래 자리로 돌려놓는다.

실제 로봇에 연결하지 않는다. 모든 결과는 is_simulated=true다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports/workcell"
WORLD = "forstick2_fr3_2f85_workcell"
PARTITION = "forstick2_fr3_workcell"
LINKS = ("wrist3_Link", "robotiq_85_left_finger_tip_link", "robotiq_85_right_finger_tip_link")
MATERIALS = ("material_a", "material_b", "material_c")
CHROME = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"


def call(base: str, method: str, path: str, body: dict | None = None, timeout=60):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


class GzRecorder:
    """Gazebo pose/info 원본을 (수신 벽시계, 바이트)로 모은다 — 해석은 나중에."""

    def __init__(self):
        os.environ["GZ_PARTITION"] = PARTITION
        from gz.transport import Node
        self.node = Node()
        self.rows: list[tuple[float, bytes]] = []
        self.lock = threading.Lock()
        self.active = False

    def start(self) -> None:
        def on_raw(data, _info):
            if self.active:
                with self.lock:
                    self.rows.append((time.time(), bytes(data)))
        self.active = True
        from gz.transport import SubscribeOptions
        self.node.subscribe_raw(f"/world/{WORLD}/pose/info", on_raw, "gz.msgs.Pose_V",
                                SubscribeOptions())

    def stop(self) -> None:
        self.active = False
        self.node.unsubscribe(f"/world/{WORLD}/pose/info")
        time.sleep(0.3)

    def decoded(self) -> list[tuple[float, dict]]:
        from gz.msgs.pose_v_pb2 import Pose_V
        wanted = set(LINKS) | set(MATERIALS)
        out = []
        with self.lock:
            rows = list(self.rows)
        for t, data in rows:
            msg = Pose_V()
            msg.ParseFromString(data)
            poses = {p.name: (p.position.x, p.position.y, p.position.z)
                     for p in msg.pose if p.name in wanted}
            out.append((t, poses))
        return out


def at(series: list[tuple[float, dict]], t: float, name: str):
    """시각 t의 pose(앞뒤 표본 선형 보간). 범위 밖이면 None."""
    lo, hi = 0, len(series) - 1
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (ta, pa), (tb, pb) = series[lo], series[hi]
    if name not in pa or name not in pb:
        return None
    f = 0.0 if tb == ta else (t - ta) / (tb - ta)
    return tuple(a + (b - a) * f for a, b in zip(pa[name], pb[name]))


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    v = sorted(values)
    pick = lambda p: v[min(len(v) - 1, round(p / 100 * (len(v) - 1)))]  # noqa: E731
    return {"n": len(v), "p50": round(pick(50), 2), "p95": round(pick(95), 2),
            "max": round(v[-1], 2)}


def wait_goal(base: str, goal_id: str, limit=900) -> dict:
    deadline = time.time() + limit
    while time.time() < deadline:
        _, goal = call(base, "GET", f"/v1/sim-demo/goals/{goal_id}")
        if goal.get("status") in ("completed", "failed", "stopped", "cancelled"):
            return goal
        time.sleep(1.0)
    return {"status": "timeout"}


def run_goal(base: str, targets: dict) -> tuple[str, dict]:
    status, goal = call(base, "POST", "/v1/sim-demo/goals",
                        {"goal": "arrange", "spec": {"targets": targets}})
    if status != 201:
        return "rejected", goal
    call(base, "POST", f"/v1/sim-demo/goals/{goal['goal_id']}/confirm", {"action": "confirm"})
    return goal["goal_id"], goal


def sample_frozen(page, seconds: float) -> dict:
    """seconds 동안 화면이 그린 위치·상태를 모은다. 움직였는지(최대 변화)와 stale 비율."""
    snaps = []
    end = time.time() + seconds
    while time.time() < end:
        snaps.append(page.evaluate("window.__simView.snapshot()"))
        time.sleep(0.1)
    moved = 0.0
    first = snaps[0]
    for snap in snaps[1:]:
        for name, xyz in snap["links"].items():
            if name in first["links"]:
                moved = max(moved, math.dist(xyz, first["links"][name]))
    badge = page.evaluate("document.getElementById('sim3d-status').textContent")
    return {"samples": len(snaps), "stale_ratio": sum(s["stale"] for s in snaps) / len(snaps),
            "max_link_motion_mm": round(moved * 1000, 3), "badge": badge}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--gpu", choices=("d3d12", "swiftshader"), default="d3d12")
    parser.add_argument("--chrome", default=str(CHROME))
    parser.add_argument("--transfer", default="material_a:slot_1")
    args = parser.parse_args()
    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.", file=sys.stderr)
        return 2
    from playwright.sync_api import sync_playwright

    model, slot = args.transfer.split(":")
    report: dict = {"schema": "forstick2.sim_view_verify/1", "is_simulated": True,
                    "real_hardware_verified": False, "gpu": args.gpu,
                    "transfer": {"material": model, "slot": slot}}
    launch = {"headless": args.gpu == "swiftshader",
              "args": (["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
                       if args.gpu == "swiftshader" else
                       ["--use-gl=angle", "--use-angle=gl", "--ignore-gpu-blocklist"])}
    if args.gpu == "d3d12":
        os.environ.setdefault("GALLIUM_DRIVER", "d3d12")
        os.environ.setdefault("DISPLAY", ":0")
    recorder = GzRecorder()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=args.chrome, **launch)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)[:300]))
        page.goto(args.base + "/", wait_until="load")
        page.wait_for_function("window.__simView && window.__simView.metrics().meshesLoaded",
                               timeout=60000)
        page.evaluate("window.__simView.setView('3d')")
        report["renderer"] = page.evaluate(
            "(() => { const gl = document.getElementById('sim3d-canvas').getContext('webgl2');"
            " const d = gl && gl.getExtension('WEBGL_debug_renderer_info');"
            " return d ? gl.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'n/a'; })()")
        time.sleep(3)
        # 1) 정지 상태 지표(로딩 구간 제외)
        page.evaluate("window.__simView.resetMetrics()")
        fps_idle = []
        for _ in range(10):
            time.sleep(1)
            fps_idle.append(page.evaluate("window.__simView.metrics().fps"))
        report["idle"] = {"fps_per_sec": fps_idle,
                          "metrics": page.evaluate("window.__simView.metrics()")}
        print(f"[정지] 렌더러 {report['renderer']} · FPS {fps_idle}"
              f" · {report['idle']['metrics']['transportMs']} · {report['idle']['metrics']['displayMs']}",
              flush=True)

        # 2) 대표 이송 중 위치 대조
        page.evaluate("window.__simView.resetMetrics()")
        recorder.start()
        goal_id, goal = run_goal(args.base, {model: slot})
        print(f"[이송] {model} → {slot} · 목표 {goal_id}", flush=True)
        snaps, fps_run = [], []
        last_fps = time.time()
        final = None
        while True:
            snaps.append(page.evaluate("window.__simView.snapshot()"))
            if time.time() - last_fps >= 1.0:
                fps_run.append(page.evaluate("window.__simView.metrics().fps"))
                last_fps = time.time()
                _, g = call(args.base, "GET", f"/v1/sim-demo/goals/{goal_id}")
                if g.get("status") in ("completed", "failed", "stopped", "cancelled"):
                    final = g
                    break
            time.sleep(0.05)
        time.sleep(1.0)
        recorder.stop()
        report["run"] = {"goal_status": final and final.get("status"), "fps_per_sec": fps_run,
                         "metrics": page.evaluate("window.__simView.metrics()"),
                         "snapshots": len(snaps)}
        series = recorder.decoded()
        report["run"]["gazebo_samples"] = len(series)
        shape, lagged, mats, moving = {n: [] for n in LINKS}, [], [], []
        prev = None
        for snap in snaps:
            if snap["stale"] or snap["jointT"] is None:
                continue
            for name in LINKS:
                gz = at(series, snap["jointT"], name)
                if gz and name in snap["links"]:
                    shape[name].append(math.dist(gz, snap["links"][name]) * 1000)
            gz_now = at(series, snap["wall"], "wrist3_Link")
            if gz_now and "wrist3_Link" in snap["links"]:
                lagged.append(math.dist(gz_now, snap["links"]["wrist3_Link"]) * 1000)
            if prev and "wrist3_Link" in prev["links"]:
                moving.append(math.dist(prev["links"]["wrist3_Link"],
                                        snap["links"]["wrist3_Link"]) * 1000)
            for name in MATERIALS:
                gz = at(series, snap["poseT"], name) if snap["poseT"] else None
                if gz and name in snap["materials"]:
                    mats.append(math.dist(gz, snap["materials"][name]) * 1000)
            prev = snap
        # 든 자재가 그리퍼를 따라오는가: 자재 중심 ↔ 두 손가락 끝 중점 거리.
        # Gazebo 자체(원 관측 전체)와 브라우저 화면을 따로 잰다 — 어긋남이 어디서 생기는지 본다.
        def mid(a, b):
            return tuple((x + y) / 2 for x, y in zip(a, b))
        tips = ("robotiq_85_left_finger_tip_link", "robotiq_85_right_finger_tip_link")
        gz_gap, view_gap, gz_gap_rows = [], [], []
        for t, poses in series:
            if model in poses and all(n in poses for n in tips):
                d = math.dist(poses[model], mid(poses[tips[0]], poses[tips[1]])) * 1000
                if d < 150:                      # 들고 있는 구간만(손가락 근처)
                    gz_gap.append(d)
                    gz_gap_rows.append((round(t, 3), round(d, 1)))
        for snap in snaps:
            links, mats_ = snap["links"], snap["materials"]
            if not snap["stale"] and model in mats_ and all(n in links for n in tips):
                d = math.dist(mats_[model], mid(links[tips[0]], links[tips[1]])) * 1000
                if d < 150:
                    view_gap.append(d)
        report["run"]["held_gap_gazebo_mm"] = stats(gz_gap)
        report["run"]["held_gap_view_mm"] = stats(view_gap)
        report["run"]["held_gap_gazebo_series"] = gz_gap_rows[::10]
        print(f"  든 자재↔손가락 중점 거리: Gazebo {stats(gz_gap)} · 화면 {stats(view_gap)}",
              flush=True)
        report["run"]["link_error_at_observation_mm"] = {n: stats(v) for n, v in shape.items()}
        report["run"]["wrist_error_same_wallclock_mm"] = stats(lagged)
        report["run"]["material_error_at_observation_mm"] = stats(mats)
        report["run"]["wrist_step_between_snapshots_mm"] = stats(moving)
        print(f"[이송] 목표 {report['run']['goal_status']} · FPS {fps_run}", flush=True)
        print(f"  링크 오차(관측 시각 기준) {report['run']['link_error_at_observation_mm']}",
              flush=True)
        print(f"  손목 오차(같은 벽시계) {report['run']['wrist_error_same_wallclock_mm']}",
              flush=True)
        print(f"  자재 오차(관측 시각 기준) {report['run']['material_error_at_observation_mm']}",
              flush=True)
        print(f"  지표 {report['run']['metrics']}", flush=True)

        # 3) 끊김 표시
        page.evaluate("window.__simView.debugDisconnect(4000)")
        time.sleep(0.3)
        report["disconnect"] = sample_frozen(page, 3.0)
        time.sleep(3.0)
        report["disconnect"]["recovered"] = not page.evaluate("window.__simView.snapshot().stale")
        print(f"[연결 끊김] {report['disconnect']}", flush=True)
        _, st = call(args.base, "GET", "/v1/sim-demo")
        idle = not st.get("running_job") and not (st.get("state") or {}).get("checkpoint")
        if idle:
            env = dict(os.environ, GZ_PARTITION=PARTITION)
            ctl = ["gz", "service", "-s", f"/world/{WORLD}/control", "--reqtype",
                   "gz.msgs.WorldControl", "--reptype", "gz.msgs.Boolean", "--timeout", "3000"]
            subprocess.run(ctl + ["--req", "pause: true"], env=env, capture_output=True,
                           timeout=10)
            time.sleep(1.2)
            report["gazebo_pause"] = sample_frozen(page, 2.5)
            subprocess.run(ctl + ["--req", "pause: false"], env=env, capture_output=True,
                           timeout=10)
            time.sleep(2.0)
            report["gazebo_pause"]["recovered"] = not page.evaluate(
                "window.__simView.snapshot().stale")
            print(f"[Gazebo 일시정지] {report['gazebo_pause']}", flush=True)
        report["page_errors"] = errors
        browser.close()

    # 4) 되돌리기
    back_id, _ = run_goal(args.base, {model: "origin"})
    back = wait_goal(args.base, back_id) if back_id != "rejected" else {"status": "rejected"}
    report["restore_goal"] = back.get("status")
    print(f"[복귀] {report['restore_goal']}", flush=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"sim_view_verify_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"기록: {out}", flush=True)
    sys.stdout.flush()
    # gz 구독을 이미 끊었다. 인터프리터 정리 중 콜백을 피하려고 바로 끝낸다.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
