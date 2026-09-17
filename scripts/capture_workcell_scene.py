#!/usr/bin/env python3
"""작업 셀 장면 캡처 (8-09 우선 작업 3).

**GUI 렌더러에 의존하지 않는다.** 서버가 렌더링하는 장면 카메라 센서의 프레임을
받아 PNG로 저장한다. GUI가 부하로 캡처 슬롯을 주지 못할 때도 "화면에 무엇이
있는가"를 파일로 확인할 수 있어야 한다.

표준 라이브러리만 쓴다(PNG 인코딩 포함) — 이미지 의존성을 늘리지 않는다.
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"


def encode_png(raw: bytes, width: int, height: int, channels: int) -> bytes:
    if channels not in (3, 4):
        raise SystemExit(f"지원하지 않는 채널 수: {channels}")
    stride = width * channels
    rows = b"".join(b"\x00" + raw[y * stride:(y + 1) * stride]
                    for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    colour_type = 2 if channels == 3 else 6
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8,
                                         colour_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6))
            + chunk(b"IEND", b""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="저장할 PNG 경로")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    camera = data.get("scene_camera") or {}
    if not camera.get("enabled"):
        print("장면 카메라가 꺼져 있다 — config/workcell의 scene_camera.enabled",
              file=sys.stderr)
        return 2
    world = data["world_name"]
    topic = (f"/world/{world}/model/scene_camera/link/link/sensor/camera/image")

    started = time.monotonic()
    try:
        done = subprocess.run(
            ["gz", "topic", "-e", "-t", topic, "-n", "1", "--json-output"],
            capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"프레임을 받지 못했다(제한 {args.timeout}s): {topic}", file=sys.stderr)
        return 3
    lines = done.stdout.strip().splitlines()
    if not lines:
        print(f"프레임이 비었다: {topic}\n{done.stderr.strip()[:200]}",
              file=sys.stderr)
        return 3
    payload = json.loads(lines[-1])
    width, height = int(payload["width"]), int(payload["height"])
    pixel_format = payload.get("pixelFormatType", "")
    channels = 4 if "RGBA" in pixel_format else 3
    raw = base64.b64decode(payload["data"])
    expected = width * height * channels
    if len(raw) != expected:
        print(f"바이트 수가 맞지 않는다: {len(raw)} != {expected}"
              f" ({pixel_format})", file=sys.stderr)
        return 4

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_png(raw, width, height, channels))
    print(f"{out} 저장 — {width}×{height} {pixel_format}"
          f" ({time.monotonic() - started:.1f}s)")
    print(f"  카메라 자세: {camera.get('pose')}")
    print("  서버가 렌더링한 프레임이다. GUI 창과 무관하다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
