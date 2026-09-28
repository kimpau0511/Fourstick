"""URDF 운동학(numpy) — 순방향 기구학, 질량 중심, 팔 역기구학(감쇠 최소제곱).

Gazebo 판정에 쓰지 않는다. 목표 자세 계산과 모델 비교(질량·질량 중심·관절)에만 쓴다.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


def rpy_matrix(r, p, y):
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array([[cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
                     [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
                     [-sp, cp * sr, cp * cr]])


def axis_angle(axis, angle):
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    k = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    return np.eye(3) + math.sin(angle) * k + (1 - math.cos(angle)) * k @ k


def tf(R, p):
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = p
    return T


def quat_matrix(qx, qy, qz, qw):
    return np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)]])


@dataclass
class Joint:
    name: str
    kind: str
    parent: str
    child: str
    origin: np.ndarray
    axis: np.ndarray
    lower: float = -math.inf
    upper: float = math.inf
    effort: float = 0.0


@dataclass
class Link:
    name: str
    mass: float = 0.0
    com: np.ndarray = field(default_factory=lambda: np.zeros(3))
    inertia: np.ndarray = field(default_factory=lambda: np.zeros((3, 3)))


class Robot:
    def __init__(self, urdf_path: str | Path):
        text = re.sub(r"<!--.*?-->", "", Path(urdf_path).read_text(encoding="utf-8"), flags=re.S)
        root = ET.fromstring(text)
        self.links: dict[str, Link] = {}
        for el in root.findall("link"):
            link = Link(el.get("name"))
            inertial = el.find("inertial")
            if inertial is not None:
                link.mass = float(inertial.find("mass").get("value"))
                o = inertial.find("origin")
                if o is not None:
                    link.com = np.array([float(v) for v in o.get("xyz", "0 0 0").split()])
                d = inertial.find("inertia")
                if d is not None:
                    g = {k: float(d.get(k, 0)) for k in ("ixx", "ixy", "ixz", "iyy", "iyz", "izz")}
                    link.inertia = np.array([[g["ixx"], g["ixy"], g["ixz"]],
                                             [g["ixy"], g["iyy"], g["iyz"]],
                                             [g["ixz"], g["iyz"], g["izz"]]])
            self.links[link.name] = link
        self.joints: dict[str, Joint] = {}
        self.child_of: dict[str, Joint] = {}
        for el in root.findall("joint"):
            o = el.find("origin")
            xyz = [float(v) for v in (o.get("xyz", "0 0 0") if o is not None else "0 0 0").split()]
            rpy = [float(v) for v in (o.get("rpy", "0 0 0") if o is not None else "0 0 0").split()]
            ax = el.find("axis")
            lim = el.find("limit")
            j = Joint(el.get("name"), el.get("type"), el.find("parent").get("link"),
                      el.find("child").get("link"), tf(rpy_matrix(*rpy), xyz),
                      np.array([float(v) for v in (ax.get("xyz") if ax is not None else "1 0 0").split()]))
            if lim is not None and j.kind != "continuous":
                j.lower = float(lim.get("lower", -math.inf))
                j.upper = float(lim.get("upper", math.inf))
                j.effort = float(lim.get("effort", 0))
            self.joints[j.name] = j
            self.child_of[j.child] = j
        children = set(self.child_of)
        self.root = next(n for n in self.links if n not in children)

    def movable(self):
        return [j for j in self.joints.values() if j.kind in ("revolute", "continuous", "prismatic")]

    def chain(self, tip: str) -> list[Joint]:
        out = []
        link = tip
        while link in self.child_of:
            j = self.child_of[link]
            out.append(j)
            link = j.parent
        return out[::-1]

    def link_tf(self, q: dict[str, float], link: str, base=np.eye(4)) -> np.ndarray:
        T = base.copy()
        for j in self.chain(link):
            T = T @ j.origin
            if j.kind in ("revolute", "continuous"):
                T = T @ tf(axis_angle(j.axis, q.get(j.name, 0.0)), np.zeros(3))
        return T

    def all_tf(self, q: dict[str, float], base=np.eye(4)) -> dict[str, np.ndarray]:
        out = {self.root: base}
        pending = [self.root]
        by_parent: dict[str, list[Joint]] = {}
        for j in self.joints.values():
            by_parent.setdefault(j.parent, []).append(j)
        while pending:
            parent = pending.pop()
            for j in by_parent.get(parent, []):
                T = out[parent] @ j.origin
                if j.kind in ("revolute", "continuous"):
                    T = T @ tf(axis_angle(j.axis, q.get(j.name, 0.0)), np.zeros(3))
                out[j.child] = T
                pending.append(j.child)
        return out

    def effective_inertia(self, joint: str, q: dict[str, float] | None = None) -> float:
        """관절 축에 대한 그 아래 전체(자식 부분 트리)의 관성(kg·m²). PD 수치 안정 한계 계산용."""
        T = self.all_tf(q or {})
        j = self.joints[joint]
        Tj = T[j.parent] @ j.origin
        axis = Tj[:3, :3] @ (j.axis / np.linalg.norm(j.axis))
        p0 = Tj[:3, 3]
        kids: dict[str, list[str]] = {}
        for jj in self.joints.values():
            kids.setdefault(jj.parent, []).append(jj.child)
        stack, total = [j.child], 0.0
        while stack:
            name = stack.pop()
            stack.extend(kids.get(name, []))
            link = self.links[name]
            if link.mass <= 0:
                continue
            R = T[name][:3, :3]
            c = R @ link.com + T[name][:3, 3]
            d = (c - p0) - axis * ((c - p0) @ axis)
            total += axis @ (R @ link.inertia @ R.T) @ axis + link.mass * (d @ d)
        return float(total)

    def mass(self) -> float:
        return sum(l.mass for l in self.links.values())

    def com(self, q: dict[str, float], base=np.eye(4), links=None) -> np.ndarray:
        T = self.all_tf(q, base)
        num, m = np.zeros(3), 0.0
        for name, link in self.links.items():
            if links is not None and name not in links:
                continue
            if name in T and link.mass > 0:
                num += link.mass * (T[name][:3, :3] @ link.com + T[name][:3, 3])
                m += link.mass
        return num / m if m else num

    def ik_position(self, tip: str, joints: list[str], target: np.ndarray, q0: dict[str, float],
                    base=np.eye(4), tip_offset=np.zeros(3), iters=200, damping=0.05,
                    step_limit=0.15, limit_margin=0.05) -> tuple[dict[str, float], float]:
        """tip 링크의 한 점(tip_offset)을 target(base 좌표계와 같은 좌표계)으로. 관절 한계 안."""
        q = dict(q0)
        for _ in range(iters):
            T = self.link_tf(q, tip, base)
            p = T[:3, :3] @ tip_offset + T[:3, 3]
            err = target - p
            if np.linalg.norm(err) < 1e-4:
                break
            J = np.zeros((3, len(joints)))
            eps = 1e-5
            for i, name in enumerate(joints):
                qq = dict(q)
                qq[name] += eps
                Tq = self.link_tf(qq, tip, base)
                J[:, i] = ((Tq[:3, :3] @ tip_offset + Tq[:3, 3]) - p) / eps
            dq = J.T @ np.linalg.solve(J @ J.T + damping ** 2 * np.eye(3), err)
            dq = np.clip(dq, -step_limit, step_limit)
            for i, name in enumerate(joints):
                j = self.joints[name]
                q[name] = float(np.clip(q[name] + dq[i], j.lower + limit_margin, j.upper - limit_margin))
        T = self.link_tf(q, tip, base)
        p = T[:3, :3] @ tip_offset + T[:3, 3]
        return q, float(np.linalg.norm(target - p))


def rot_error(R_target: np.ndarray, R: np.ndarray) -> np.ndarray:
    """R을 R_target으로 돌리는 회전 벡터(월드 좌표, rad)."""
    Re = R_target @ R.T
    angle = math.acos(max(-1.0, min(1.0, (np.trace(Re) - 1) / 2)))
    if angle < 1e-9:
        return np.zeros(3)
    axis = np.array([Re[2, 1] - Re[1, 2], Re[0, 2] - Re[2, 0], Re[1, 0] - Re[0, 1]])
    return axis / (2 * math.sin(angle)) * angle if abs(math.sin(angle)) > 1e-9 else np.zeros(3)


def ik_pose(robot: "Robot", tip: str, joints: list[str], target_p: np.ndarray, target_R: np.ndarray,
            q0: dict[str, float], base=np.eye(4), tip_offset=np.zeros(3), w_rot=0.3, iters=200,
            damping=0.05, step_limit=0.15, limit_margin=0.05) -> tuple[dict[str, float], float, float]:
    """tip 링크의 점(tip_offset) 위치 + tip 링크 방향을 목표로(감쇠 최소제곱). (q, 위치 오차 m, 방향 오차 rad)."""
    q = dict(q0)

    def pose(qq):
        T = robot.link_tf(qq, tip, base)
        return T[:3, :3] @ tip_offset + T[:3, 3], T[:3, :3]

    for _ in range(iters):
        p, R = pose(q)
        e = np.concatenate([target_p - p, w_rot * rot_error(target_R, R)])
        if np.linalg.norm(e[:3]) < 5e-4 and np.linalg.norm(e[3:]) < 2e-3:
            break
        J = np.zeros((6, len(joints)))
        eps = 1e-5
        for i, name in enumerate(joints):
            qq = dict(q)
            qq[name] += eps
            p2, R2 = pose(qq)
            J[:3, i] = (p2 - p) / eps
            J[3:, i] = w_rot * rot_error(R2, R) / eps
        dq = J.T @ np.linalg.solve(J @ J.T + damping ** 2 * np.eye(6), e)
        dq = np.clip(dq, -step_limit, step_limit)
        for i, name in enumerate(joints):
            j = robot.joints[name]
            q[name] = float(np.clip(q[name] + dq[i], j.lower + limit_margin, j.upper - limit_margin))
    p, R = pose(q)
    return q, float(np.linalg.norm(target_p - p)), float(np.linalg.norm(rot_error(target_R, R)))


def gravity_torques(robot: "Robot", q: dict[str, float], joints: list[str],
                    g_base=np.array([0.0, 0.0, -9.81])) -> dict[str, float]:
    """각 관절 축에 대한 중력 토크(그 아래 링크들의 무게, base 좌표계의 중력 벡터 g_base).
    정적 유지에 필요한 관절 토크는 이 값의 부호를 바꾼 것이다."""
    T = robot.all_tf(q)
    kids: dict[str, list[str]] = {}
    for jj in robot.joints.values():
        kids.setdefault(jj.parent, []).append(jj.child)
    out = {}
    for name in joints:
        j = robot.joints[name]
        Tj = T[j.parent] @ j.origin
        axis = Tj[:3, :3] @ (j.axis / np.linalg.norm(j.axis))
        p0 = Tj[:3, 3]
        stack, tau = [j.child], 0.0
        while stack:
            link = stack.pop()
            stack.extend(kids.get(link, []))
            L = robot.links[link]
            if L.mass <= 0:
                continue
            c = T[link][:3, :3] @ L.com + T[link][:3, 3]
            tau += float(np.cross(c - p0, L.mass * g_base) @ axis)
        out[name] = tau
    return out


def _stl_points(path: Path) -> np.ndarray:
    import struct
    b = Path(path).read_bytes()
    if b[:5] == b"solid" and b"facet" in b[:400]:
        v = re.findall(rb"vertex\s+(\S+)\s+(\S+)\s+(\S+)", b)
        return np.array(v, float)
    n = struct.unpack("<I", b[80:84])[0]
    a = np.frombuffer(b[84:84 + n * 50], dtype=np.dtype([("n", "<3f4"), ("v", "<9f4"), ("a", "<u2")]))
    return a["v"].reshape(-1, 3).astype(float)


def collision_sample_points(urdf_path: str | Path, links: list[str]) -> dict[str, np.ndarray]:
    """링크 충돌 형상(메시·상자)의 경계 상자에서 점 26개(꼭짓점 8 · 모서리 가운데 12 · 면 가운데 6)를
    링크 좌표계로. 충돌 예측용 — 경계 상자라 실제 형상보다 조금 크다(보수적)."""
    urdf_path = Path(urdf_path)
    text = re.sub(r"<!--.*?-->", "", urdf_path.read_text(encoding="utf-8"), flags=re.S)
    root = ET.fromstring(text)
    out = {}
    for el in root.findall("link"):
        name = el.get("name")
        if name not in links:
            continue
        pts = []
        for col in el.findall("collision"):
            o = col.find("origin")
            xyz = [float(v) for v in (o.get("xyz", "0 0 0") if o is not None else "0 0 0").split()]
            rpy = [float(v) for v in (o.get("rpy", "0 0 0") if o is not None else "0 0 0").split()]
            g = col.find("geometry")
            mesh, box = g.find("mesh"), g.find("box")
            if mesh is not None:
                raw = _stl_points(urdf_path.parent / mesh.get("filename"))
                lo, hi = raw.min(0), raw.max(0)
            elif box is not None:
                size = np.array([float(v) for v in box.get("size").split()])
                lo, hi = -size / 2, size / 2
            else:
                continue
            c, h = (lo + hi) / 2, (hi - lo) / 2
            grid = [np.array([a, b_, d]) for a in (-1, 0, 1) for b_ in (-1, 0, 1) for d in (-1, 0, 1)
                    if (a, b_, d) != (0, 0, 0)]
            local = np.array([c + g_ * h for g_ in grid])
            pts.append((rpy_matrix(*rpy) @ local.T).T + np.array(xyz))
        if pts:
            out[name] = np.vstack(pts)
    return out
