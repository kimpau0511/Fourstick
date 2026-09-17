"""STL 점군 읽기 보조. 여러 검토 스크립트가 함께 쓴다.

메시 내용을 바꾸지 않는다 — 꼭짓점을 균등 간격으로 골라 점군으로 돌려준다.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def read_stl_points(path: Path, samples: int) -> np.ndarray:
    """이진·ASCII STL의 꼭짓점을 읽어 표본 점군을 돌려준다."""
    raw = path.read_bytes()
    if raw[:5] == b"solid" and b"facet" in raw[:2048]:
        points = [
            [float(value) for value in line.split()[1:4]]
            for line in raw.decode("utf-8", "replace").splitlines()
            if line.split()[:1] == ["vertex"]
        ]
        cloud = np.asarray(points, dtype=float)
    else:
        count = struct.unpack("<I", raw[80:84])[0]
        body = np.frombuffer(raw, dtype=np.uint8, count=count * 50, offset=84)
        body = body.reshape(count, 50)[:, 12:48].copy()
        cloud = body.view("<f4").reshape(count * 3, 3).astype(float)
    if len(cloud) > samples:
        step = max(1, len(cloud) // samples)
        cloud = cloud[::step][:samples]
    return cloud
