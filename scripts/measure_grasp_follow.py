"""파지·운반·해제 구간 측정 — 손–자재 상대 위치 · 순간 최대 이탈 · 깜빡임 원인 대조.

    source /opt/ros/lyrical/setup.bash
    FORSTICK2_SIM_PICK_PLACE_DEMO=1 PYTHONPATH=$PYTHONPATH:/usr/lib/python3/dist-packages \\
        <playwright가 있는 python> scripts/measure_grasp_follow.py --label before \\
        --targets material_c:slot_3 [--back]

같은 순간의 세 가지를 모은다(로봇 명령은 서버 목표 API로만 — 확인 뒤 실행).
1. Gazebo 관측: `pose/info` **전부**(전송 계층 제한 없음). 자재가 메시지에서 빠진
   구간(=Gazebo 장면에 모델이 없다)과 엔티티 id가 바뀐 순간(지우고 다시 만듦)을 센다.
   손–자재 상대 위치는 손목 링크(wrist3_Link) 좌표계로 표현한 자재 위치다 — 들고
   있는 동안 이 값이 변하면 자재가 손을 따라오지 않는 것이다.
2. Gazebo 화면: 서버가 렌더링하는 장면 카메라 프레임. 자재가 빠진 순간 앞뒤 프레임을
   PNG로 남긴다(눈으로 대조).
3. Three.js 화면: 50 ms마다 화면이 그린 자재 위치·표시 여부·stale.

실제 로봇에 연결하지 않는다. 모든 결과는 is_simulated=true다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WORLD = "forstick2_fr3_2f85_workcell"
PARTITION = "forstick2_fr3_workcell"
CAMERA_TOPIC = f"/world/{WORLD}/model/scene_camera/link/link/sensor/camera/image"
WRIST = "wrist3_Link"
TIPS = ("robotiq_85_left_finger_tip_link", "robotiq_85_right_finger_tip_link")
OUT_DIR = ROOT / "reports/workcell"
CHROME = Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"


def rotate_inv(q, v):
    """쿼터니언 q(x, y, z, w)가 나타내는 회전의 역(=전치)을 v에 적용한다."""
    x, y, z, w = q
    r = ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
         (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
         (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))
    return tuple(sum(r[row][col] * v[row] for row in range(3)) for col in range(3))


class Recorder:
    def __init__(self, material: str):
        os.environ["GZ_PARTITION"] = PARTITION
        from gz.transport import Node
        self.node = Node()
        self.material = material
        self.poses: list[tuple[float, bytes]] = []
        self.frames: deque = deque(maxlen=40)          # (t, bytes) 최근 카메라 프레임
        self.saved_frames: list[tuple[float, bytes]] = []
        self.lock = threading.Lock()

    def start(self):
        from gz.transport import SubscribeOptions

        def on_pose(data, _info):
            with self.lock:
                self.poses.append((time.time(), bytes(data)))

        def on_image(data, _info):
            with self.lock:
                self.frames.append((time.time(), bytes(data)))

        self.node.subscribe_raw(f"/world/{WORLD}/pose/info", on_pose, "gz.msgs.Pose_V",
                                SubscribeOptions())
        self.node.subscribe_raw(CAMERA_TOPIC, on_image, "gz.msgs.Image", SubscribeOptions())

    def keep_frames_around(self, t0: float, window: float = 1.0):
        with self.lock:
            for t, data in self.frames:
                if abs(t - t0) <= window and all(t != s for s, _ in self.saved_frames):
                    self.saved_frames.append((t, data))

    def stop(self):
        for topic in (f"/world/{WORLD}/pose/info", CAMERA_TOPIC):
            try:
                self.node.unsubscribe(topic)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)

    def latest_has_material(self) -> bool | None:
        from gz.msgs.pose_v_pb2 import Pose_V
        with self.lock:
            if not self.poses:
                return None
            data = self.poses[-1][1]
        msg = Pose_V()
        msg.ParseFromString(data)
        return any(p.name == self.material for p in msg.pose)

    def decoded(self):
        from gz.msgs.pose_v_pb2 import Pose_V
        out = []
        with self.lock:
            rows = list(self.poses)
        for t, data in rows:
            msg = Pose_V()
            msg.ParseFromString(data)
            item = {}
            for p in msg.pose:
                if p.name in (self.material, WRIST, *TIPS):
                    item[p.name] = (p.id, (p.position.x, p.position.y, p.position.z),
                                    (p.orientation.x, p.orientation.y, p.orientation.z,
                                     p.orientation.w))
            out.append((t, msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9, item))
        return out


def stats(values):
    if not values:
        return {"n": 0}
    v = sorted(values)
    pick = lambda p: v[min(len(v) - 1, round(p / 100 * (len(v) - 1)))]  # noqa: E731
    return {"n": len(v), "p50": round(pick(50), 2), "p95": round(pick(95), 2),
            "max": round(v[-1], 2)}


def analyse(series, material: str) -> dict:
    """손목 좌표계 자재 위치(들고 있는 구간) · 자재가 빠진 구간 · id 변경."""
    present = [(t, item) for t, _s, item in series]
    missing_runs, run_start, ids, id_changes = [], None, [], 0
    last_id = None
    for t, item in present:
        if material not in item:
            run_start = t if run_start is None else run_start
            continue
        if run_start is not None:
            missing_runs.append((run_start, t))
            run_start = None
        entity = item[material][0]
        if last_id is not None and entity != last_id:
            id_changes += 1
            ids.append((round(t, 3), last_id, entity))
        last_id = entity
    # 운반 구간: 자재가 **처음 자리를 떠났고(1 cm) 마지막 자리에 아직 닿지 않은(1 cm)** 때.
    # 파지 전 접근(자재가 처음 자리에 있음)과 해제 뒤(마지막 자리)는 뺀다.
    rows = [(t, item) for t, item in present
            if material in item and WRIST in item and all(n in item for n in TIPS)]
    if not rows:
        return {"error": "관측 없음"}
    start = rows[0][1][material][1]
    final = rows[-1][1][material][1]
    held = []
    for t, item in rows:
        mp = item[material][1]
        if math.dist(mp, start) <= 0.01 or math.dist(mp, final) <= 0.01:
            continue
        tip = tuple((a + b) / 2 for a, b in zip(item[TIPS[0]][1], item[TIPS[1]][1]))
        wp, wq = item[WRIST][1], item[WRIST][2]
        rel = rotate_inv(wq, tuple(a - b for a, b in zip(mp, wp)))
        held.append((t, rel, math.dist(mp, tip)))
    if not held:
        return {"held_samples": 0, "missing_runs": len(missing_runs)}
    med = tuple(sorted(r[1][i] for r in held)[len(held) // 2] for i in range(3))
    dev = [math.dist(r[1], med) * 1000 for r in held]
    worst = max(held, key=lambda r: math.dist(r[1], med))
    return {
        "held_samples": len(held),
        "held_window_sec": round(held[-1][0] - held[0][0], 2),
        "hand_frame_median_m": [round(v, 4) for v in med],
        "hand_frame_deviation_mm": stats(dev),
        "worst_at": round(worst[0], 3),
        "tip_gap_mm": stats([r[2] * 1000 for r in held]),
        "missing_runs": [(round(a, 3), round(b - a, 3)) for a, b in missing_runs],
        "entity_id_changes": id_changes, "entity_ids": ids,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--targets", required=True, help="material:destination")
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--back", action="store_true", help="끝나고 원래 자리로 되돌린다")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.", file=sys.stderr)
        return 2
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_sim_view import call, run_goal, wait_goal  # noqa: E402

    material, destination = args.targets.split(":")
    rec = Recorder(material)
    report = {"schema": "forstick2.grasp_follow/1", "label": args.label,
              "is_simulated": True, "material": material, "destination": destination,
              "fixture_mode": os.environ.get("FORSTICK2_FIXTURE_MODE", "(서버 설정)")}
    view_rows = []
    pw = browser = page = None
    if not args.no_browser:
        from playwright.sync_api import sync_playwright
        os.environ.setdefault("GALLIUM_DRIVER", "d3d12")
        os.environ.setdefault("DISPLAY", ":0")
        pw = sync_playwright().start()
        browser = pw.chromium.launch(executable_path=str(CHROME), headless=False,
                                     args=["--use-gl=angle", "--use-angle=gl",
                                           "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.goto(args.base + "/", wait_until="load")
        page.wait_for_function("window.__simView && window.__simView.metrics().meshesLoaded",
                               timeout=60000)
        page.evaluate("window.__simView.setView('3d')")
        time.sleep(2)
    rec.start()
    time.sleep(1.0)
    goal_id, _ = run_goal(args.base, {material: destination})
    print(f"[{args.label}] {material} → {destination} · 목표 {goal_id}", flush=True)
    final = None
    last_poll = 0.0
    while True:
        now = time.time()
        present = rec.latest_has_material()
        if present is False:
            rec.keep_frames_around(now)
        if page is not None:
            snap = page.evaluate("(() => { const s = window.__simView.snapshot();"
                                 " return {wall: s.wall, stale: s.stale,"
                                 " mat: s.materials[%r] || null}; })()" % material)
            view_rows.append(snap)
        if now - last_poll > 1.0:
            last_poll = now
            _, goal = call(args.base, "GET", f"/v1/sim-demo/goals/{goal_id}")
            if goal.get("status") in ("completed", "failed", "stopped", "cancelled"):
                final = goal
                break
        time.sleep(0.05)
    time.sleep(1.5)
    rec.stop()
    series = rec.decoded()
    report["goal_status"] = final.get("status")
    report["gazebo"] = analyse(series, material)
    report["gazebo_messages"] = len(series)
    # 원 자료(다시 분석할 수 있게): 시각, 자재·손목·손가락 끝 pose.
    raw_path = OUT_DIR / f"grasp_follow_{args.label}_raw.json"
    raw_path.write_text(json.dumps([[round(t, 4), {k: [v[0], list(v[1]), list(v[2])]
                                                     for k, v in item.items()}]
                                    for t, _s, item in series]), encoding="utf-8")
    report["raw"] = str(raw_path)
    # Three.js: stale 비율·표시 위치의 순간 도약
    jumps = []
    for a, b in zip(view_rows, view_rows[1:]):
        if a["mat"] and b["mat"]:
            jumps.append(math.dist(a["mat"], b["mat"]) * 1000)
    report["view"] = {"snapshots": len(view_rows),
                      "stale_snapshots": sum(1 for r in view_rows if r["stale"]),
                      "stale_times": [round(r["wall"], 3) for r in view_rows if r["stale"]][:20],
                      "missing_mesh": sum(1 for r in view_rows if not r["mat"]),
                      "position_step_mm": stats(jumps)}
    # 자재가 빠진 순간 앞뒤의 Gazebo 카메라 프레임
    frames_dir = OUT_DIR / f"grasp_follow_{args.label}_frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    from gz.msgs.image_pb2 import Image
    from server.scene_view import encode_png
    saved = []
    for t, data in sorted(rec.saved_frames)[:24]:
        img = Image()
        img.ParseFromString(data)
        channels = 3 if img.pixel_format_type in (3, 5) or len(img.data) == img.width * img.height * 3 else 4
        path = frames_dir / f"{t:.3f}.png"
        path.write_bytes(encode_png(img.data, img.width, img.height, channels))
        saved.append(path.name)
    report["camera_frames_around_missing"] = saved
    print(json.dumps({k: report[k] for k in ("goal_status", "gazebo", "view")},
                     ensure_ascii=False, indent=1), flush=True)
    if browser is not None:
        browser.close()
        pw.stop()
    if args.back:
        back_id, _ = run_goal(args.base, {material: "origin"})
        report["back"] = wait_goal(args.base, back_id).get("status") \
            if back_id != "rejected" else "rejected"
    out = OUT_DIR / f"grasp_follow_{args.label}_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"기록: {out}", flush=True)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
