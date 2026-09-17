#!/usr/bin/env python3
"""웹 장면 카메라에서 자재가 실제로 옮겨지는지 확인한다 (8-11).

```
FORSTICK2_SIM_PICK_PLACE_DEMO=1 .venv/bin/python \\
    scripts/verify_sim_e2e_scene_view.py pallet_1 mat_a
```

시연을 돌리는 동안 `/v1/scene.raw`로 원본 RGB 프레임을 받아 **자재 색 화소의
무게중심**을 추적한다. 판정은 이렇게 한다.

1. 자재의 선언된 색(`models[*].color_rgba`)에 가까운 화소만 고른다
2. 그 화소들의 무게중심을 화면 좌표로 구한다
3. 팔레트 중심과 컨베이어 중심을 **카메라 파라미터로 투영**해 기대 위치를
   계산한다(설정의 `scene_camera` pose·fov·해상도)
4. 처음 프레임의 무게중심이 팔레트 쪽에, 마지막 프레임의 무게중심이 컨베이어
   쪽에 더 가까우면 "장면에서 이동을 확인했다"로 적는다

**화면을 보고 사람이 판단했다고 쓰지 않는다.** 프레임의 화소와 투영 좌표로
판정하고, 프레임은 파일로 남겨 눈으로도 볼 수 있게 한다.
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
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
OUT = ROOT / "reports/workcell/sim_e2e_scene_view.json"
FRAMES = Path("/tmp/forstick2_workcell/sim_e2e_frames")
DEMO = ROOT / "scripts/demo_workcell_pick_place.sh"
PORT = int(os.environ.get("FORSTICK2_PORT", "8093"))
BASE = f"http://127.0.0.1:{PORT}"

#: 창 변화량이 대조 창보다 몇 배 이상이어야 "바뀌었다"로 볼지.
#: 근거를 임계값 하나에 두지 않기 위해 **대조 창과의 비**로 판정한다.
CHANGE_RATIO = 5.0
#: 프레임 표집 주기(초).
SAMPLE_PERIOD_SEC = 1.0


def fetch_raw() -> tuple[np.ndarray, dict] | None:
    """`/v1/scene.raw`에서 원본 RGB 프레임 한 장. 없으면 None."""
    try:
        with urllib.request.urlopen(f"{BASE}/v1/scene.raw", timeout=5.0) as response:
            head = {k.lower(): v for k, v in response.getheaders()}
            body = response.read()
    except Exception:  # noqa: BLE001 — 프레임이 없으면 없는 것이다
        return None
    width = int(head.get("x-scene-width", 0))
    height = int(head.get("x-scene-height", 0))
    channels = int(head.get("x-scene-channels", 3))
    if not width or not height or len(body) < width * height * channels:
        return None
    frame = np.frombuffer(body[:width * height * channels], dtype=np.uint8)
    return frame.reshape(height, width, channels), {
        "width": width, "height": height, "channels": channels,
        "seq": head.get("x-scene-seq"), "captured_at": head.get("x-scene-captured-at"),
    }


def camera_from_config(data: dict) -> tuple[np.ndarray, np.ndarray, float, int, int]:
    """장면 카메라의 위치·회전·수평 fov·해상도. 설정이 유일한 출처다."""
    camera = data["scene_camera"]
    values = [float(v) for v in str(camera["pose"]).split()]
    position = np.asarray(values[:3])
    roll, pitch, yaw = values[3:6]

    def rotation_matrix(r: float, p: float, y: float) -> np.ndarray:
        cr, sr, cp, sp, cy, sy = (math.cos(r), math.sin(r), math.cos(p),
                                  math.sin(p), math.cos(y), math.sin(y))
        return np.array([
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ])

    return (position, rotation_matrix(roll, pitch, yaw),
            float(camera["horizontal_fov_rad"]), int(camera["width"]),
            int(camera["height"]))


def project(point: np.ndarray, position: np.ndarray, rotation: np.ndarray,
            hfov: float, width: int, height: int) -> tuple[float, float] | None:
    """world 점을 화면 화소 좌표로 투영한다. 카메라 뒤면 None."""
    local = rotation.T @ (np.asarray(point) - position)
    forward, left, up = local[0], local[1], local[2]
    if forward <= 0.01:
        return None
    focal = (width / 2) / math.tan(hfov / 2)
    # gz 카메라는 +x 전방, +y 좌, +z 상이다. 화면 x는 오른쪽, y는 아래.
    return (width / 2 - focal * (left / forward),
            height / 2 - focal * (up / forward))


def resolve(frames: dict, name: str) -> np.ndarray:
    out = np.zeros(3)
    while name is not None:
        out = out + np.asarray(frames[name]["xyz_m"])
        name = frames[name]["parent"]
    return out


def window_of(centre: tuple[float, float], size_px: tuple[float, float],
              width: int, height: int) -> tuple[int, int, int, int]:
    """투영 위치를 감싸는 창. 크기는 물체의 **투영 크기**에서 나온다."""
    half_w = max(3.0, size_px[0] / 2 + 2.0)
    half_h = max(3.0, size_px[1] / 2 + 2.0)
    x0 = int(max(0, round(centre[0] - half_w)))
    x1 = int(min(width, round(centre[0] + half_w)))
    y0 = int(max(0, round(centre[1] - half_h)))
    y1 = int(min(height, round(centre[1] + half_h)))
    return x0, y0, x1, y1


def window_change(before: np.ndarray, after: np.ndarray,
                  window: tuple[int, int, int, int]) -> float:
    """창 안의 평균 절대 화소 변화량(0~255)."""
    x0, y0, x1, y1 = window
    a = before[y0:y1, x0:x1, :3].astype(np.float32)
    b = after[y0:y1, x0:x1, :3].astype(np.float32)
    if a.size == 0:
        return 0.0
    return float(np.abs(b - a).mean())


def projected_size_px(distance_m: float, size_m, focal_px: float) -> tuple[float, float]:
    """물체의 투영 크기(가로·세로 화소)."""
    return (float(size_m[0]) * focal_px / distance_m,
            float(size_m[2]) * focal_px / distance_m)


def save_png(frame: np.ndarray, path: Path) -> None:
    """표준 라이브러리로 PNG를 쓴다(외부 의존성 없이)."""
    from server.scene_view import encode_png

    height, width = frame.shape[:2]
    path.write_bytes(encode_png(frame[:, :, :3].tobytes(), width, height, 3))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("support")
    parser.add_argument("object")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.",
              file=sys.stderr)
        return 3

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    table = {row["resource_id"]: row for row in data["resource_map"]}

    def find(token: str):
        return table.get(token) or next(
            (r for r in data["resource_map"] if r["gazebo_model"] == token), None)

    row = find(args.object)
    support_row = find(args.support)
    if row is None or support_row is None:
        print(f"자원을 찾지 못했다: {args.object} / {args.support}", file=sys.stderr)
        return 1
    model = row["gazebo_model"]
    declared = data["models"][model]
    size_m = declared["size_m"]

    position, rotation, hfov, width, height = camera_from_config(data)
    focal = (width / 2) / math.tan(hfov / 2)

    def window_for(frame_name: str):
        centre_world = resolve(data["frames"], frame_name) + np.array(
            [0.0, 0.0, float(size_m[2]) / 2])
        pixel = project(centre_world, position, rotation, hfov, width, height)
        if pixel is None:
            return None, None, None
        distance = float(np.linalg.norm(centre_world - position))
        size_px = projected_size_px(distance, size_m, focal)
        return pixel, window_of(pixel, size_px, width, height), distance

    support_px, support_window, support_distance = window_for(support_row["frame"])
    conveyor_row = next(r for r in data["resource_map"]
                        if r["resource_id"] == "loc_conveyor")
    target_px, target_window, target_distance = window_for(conveyor_row["frame"])
    # 대조 창: 자재가 오지 않는 자리(작업대 프레임 원점 위)로 둔다. 여기가
    # 크게 바뀌면 판정 근거가 조명·카메라 변화지 자재 이동이 아니다.
    control_px, control_window, _ = window_for("workbench_frame")
    if not all((support_window, target_window, control_window)):
        print("창을 만들 수 없다 — 투영이 화면 밖이다", file=sys.stderr)
        return 1

    FRAMES.mkdir(parents=True, exist_ok=True)
    for old in FRAMES.glob("*.png"):
        old.unlink()

    before = fetch_raw()
    if before is None:
        print("장면 프레임을 받지 못했다 — 장면 카메라가 꺼져 있다",
              file=sys.stderr)
        return 1
    save_png(before[0], FRAMES / "00_before.png")

    samples: list[dict] = []
    stop = threading.Event()

    def sample_loop() -> None:
        index = 0
        while not stop.is_set():
            got = fetch_raw()
            if got is not None:
                frame, meta = got
                path = FRAMES / f"run_{index:03d}.png"
                save_png(frame, path)
                samples.append({
                    "index": index, "at": time.time(), "seq": meta["seq"],
                    "frame": str(path),
                    "support_window_change": round(
                        window_change(before[0], frame, support_window), 3),
                    "target_window_change": round(
                        window_change(before[0], frame, target_window), 3),
                    "control_window_change": round(
                        window_change(before[0], frame, control_window), 3),
                })
                index += 1
            stop.wait(SAMPLE_PERIOD_SEC)

    worker = threading.Thread(target=sample_loop, daemon=True)
    worker.start()
    env = dict(os.environ, FORSTICK2_SIM_PICK_PLACE_DEMO="1")
    run = subprocess.run(
        [str(DEMO), args.support, args.object, "--no-restore",
         "--out", str(FRAMES / "transfer.json")],
        env=env, capture_output=True, text=True, timeout=900)
    # 시연이 끝난 뒤(팔이 안전 home으로 돌아온 뒤) 비교 프레임을 받는다.
    time.sleep(4.0)
    stop.set()
    worker.join(timeout=5.0)
    after = fetch_raw()
    if after is None:
        print("마지막 프레임을 받지 못했다", file=sys.stderr)
        return 1
    save_png(after[0], FRAMES / "99_after.png")

    transfer = {}
    if (FRAMES / "transfer.json").is_file():
        transfer = json.loads((FRAMES / "transfer.json").read_text(encoding="utf-8"))

    support_change = window_change(before[0], after[0], support_window)
    target_change = window_change(before[0], after[0], target_window)
    control_change = window_change(before[0], after[0], control_window)
    floor = max(control_change, 0.5)
    moved = (support_change >= CHANGE_RATIO * floor
             and target_change >= CHANGE_RATIO * floor)
    detail = (f"팔레트 창 변화 {support_change:.2f} · 컨베이어 창 변화"
              f" {target_change:.2f} · 대조 창 변화 {control_change:.2f}"
              f" (요구: 대조의 {CHANGE_RATIO:g}배 이상)")

    subprocess.run([str(DEMO), args.support, args.object, "--restore-only"],
                   env=env, capture_output=True, text=True, timeout=300)

    transfer_ok = transfer.get("status") == "simulation_transfer_completed"
    report = {
        "schema": "forstick2.workcell_sim_e2e_scene_view/2",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "simulation_e2e": bool(transfer.get("simulation_e2e")),
        "real_hardware_verified": False,
        "real_hardware_ready": False,
        "object": model,
        "support": support_row["gazebo_model"],
        "method": "장면 카메라 프레임의 **투영 창 변화량**을 비교한다."
                  " 시연 전후 모두 팔이 안전 home에 있으므로 창의 변화는"
                  " 자재 이동에서 온다. 대조 창은 자재가 오지 않는 자리다",
        "camera": {"pose": data["scene_camera"]["pose"], "width": width,
                   "height": height, "horizontal_fov_rad": hfov,
                   "focal_px": round(focal, 2)},
        "windows": {
            "support": {"centre_px": [round(v, 2) for v in support_px],
                        "box": list(support_window),
                        "distance_m": round(support_distance, 4)},
            "conveyor": {"centre_px": [round(v, 2) for v in target_px],
                         "box": list(target_window),
                         "distance_m": round(target_distance, 4)},
            "control": {"centre_px": [round(v, 2) for v in control_px],
                        "box": list(control_window)},
        },
        "change": {
            "support_window": round(support_change, 3),
            "conveyor_window": round(target_change, 3),
            "control_window": round(control_change, 3),
            "required_ratio": CHANGE_RATIO,
        },
        "frames_captured": len(samples) + 2,
        "samples": samples,
        "transfer_status": transfer.get("status"),
        "scene_shows_transfer": bool(moved),
        "detail": detail,
        "frames_dir": str(FRAMES),
        "demo_exit_code": run.returncode,
        "note": "웹 장면 카메라 프레임으로 판정했다. 시뮬레이터 이송 시연이며"
                " 실제 로봇 pick/place 가능 판정이 아니다",
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"프레임 {len(samples) + 2}장 · {detail}")
    print(f"이송 판정 {transfer.get('status')} · 장면에서 이동 확인={moved}")
    print(f"프레임: {FRAMES} · 기록: {args.out}")
    return 0 if (moved and transfer_ok) else 1


if __name__ == "__main__":
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
