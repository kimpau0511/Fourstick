"""URDF 사슬 역기구학 — 실행 시점에 **새 위치의 자세**를 푼다.

`scripts/derive_workcell_poses.solve_ik`(오프라인 자세 유도)와 같은 방식이다: URDF 값만으로
순기구학을 계산하고 감쇠 최소제곱으로 푼다. 다른 점은 둘이다.

- 트리 전체가 아니라 **뿌리 → 끝 링크 사슬**만 계산하고, 야코비안을 수치 미분 대신 회전
  관절 기하식(z × (p − o))으로 구한다. 오프라인 유도는 한 자세에 수 분이 걸려 실행 시점
  후보 계산에 쓸 수 없었다(2026-10-02 탐침).
- 허용치는 호출자가 준다. 기본값은 오프라인 유도와 같은 값이다(아래 상수).

지키는 것:

- **허용치에 들지 못하면 실패를 그대로 돌려준다.** 가까운 값을 성공으로 쓰지 않는다.
- 관절 제한(URDF limit) 밖의 값을 내지 않는다(매 반복 잘라 낸다).
- 충돌·도달 판정을 하지 않는다. 여기서 나온 자세는 MoveIt 검사를 거쳐야 쓸 수 있다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from robots.moveit.kinematics import axis_matrix, rpy_matrix

#: IK 허용치. 오프라인 자세 유도(`scripts/derive_workcell_poses.py`)와 같은 값이다 —
#: 컨트롤러 관절 허용치(0.05 rad)보다 훨씬 작게 잡은 위치 허용치와 자세 허용치.
POSITION_TOLERANCE_M = 0.002
ORIENTATION_TOLERANCE_RAD = 0.05
#: 한 시작점의 최대 반복 수와 한 번에 움직일 관절 변화 상한(rad). 오프라인 유도와 같다.
MAX_ITERATIONS = 200
MAX_STEP_RAD = 0.35


@dataclass(frozen=True)
class IkResult:
    joints: Mapping[str, float]
    position_error_m: float
    orientation_error_rad: float
    converged: bool
    seed_index: int

    def to_dict(self) -> dict:
        return {"joint_rad": {k: round(float(v), 6) for k, v in self.joints.items()},
                "position_error_m": round(self.position_error_m, 6),
                "orientation_error_rad": round(self.orientation_error_rad, 6),
                "converged": self.converged, "seed_index": self.seed_index}


class ChainIk:
    """`root` → `tip` 사슬. 사슬 위 회전 관절 중 `arm_joints`만 푼다(나머지는 0)."""

    def __init__(self, joints: Sequence[Mapping], *, root: str, tip: str,
                 arm_joints: Sequence[str]):
        by_child = {j["child"]: j for j in joints}
        chain = []
        link = tip
        while link != root:
            joint = by_child.get(link)
            if joint is None:
                raise ValueError(f"{tip}에서 {root}까지 이어지는 사슬이 없다 ({link}에서 끊김)")
            chain.append(joint)
            link = joint["parent"]
        self.chain = list(reversed(chain))
        self.arm_joints = tuple(arm_joints)
        movable = [j["name"] for j in self.chain if j["type"] in ("revolute", "continuous")]
        if sorted(movable) != sorted(self.arm_joints):
            raise ValueError(f"사슬의 회전 관절 {movable}이 팔 관절 {list(arm_joints)}과 다르다")
        self.limits = {j["name"]: (float(j["limit"]["lower"]), float(j["limit"]["upper"]))
                       for j in self.chain if j["name"] in self.arm_joints}

    def fk(self, q: Mapping[str, float]):
        """끝 링크의 (회전, 위치)와 관절별 (축 world, 원점 world)."""
        rotation, position = np.eye(3), np.zeros(3)
        axes = {}
        for joint in self.chain:
            position = position + rotation @ np.asarray(joint["xyz"], dtype=float)
            rotation = rotation @ rpy_matrix(*joint["rpy"])
            if joint["type"] in ("revolute", "continuous"):
                axis = np.asarray(joint["axis"], dtype=float)
                axes[joint["name"]] = (rotation @ (axis / np.linalg.norm(axis)), position.copy())
                rotation = rotation @ axis_matrix(axis, float(q.get(joint["name"], 0.0)))
        return rotation, position, axes

    @staticmethod
    def _errors(rotation, position, target_rotation, target_position):
        p_err = target_position - position
        relative = target_rotation @ rotation.T
        angle = float(np.arccos(np.clip((np.trace(relative) - 1) / 2, -1, 1)))
        axis = np.array([relative[2, 1] - relative[1, 2], relative[0, 2] - relative[2, 0],
                         relative[1, 0] - relative[0, 1]])
        norm = np.linalg.norm(axis)
        r_err = np.zeros(3) if norm < 1e-9 else axis / norm * angle
        return p_err, r_err, float(np.linalg.norm(p_err)), angle

    def _solve_from(self, seed, target_rotation, target_position, *, position_tol,
                    orientation_tol):
        names = self.arm_joints
        q = np.array([float(np.clip(seed[n], *self.limits[n])) for n in names])
        lower = np.array([self.limits[n][0] for n in names])
        upper = np.array([self.limits[n][1] for n in names])
        damping = 0.05
        best = None
        for _ in range(MAX_ITERATIONS):
            rotation, position, axes = self.fk(dict(zip(names, q)))
            p_err, r_err, dist, angle = self._errors(rotation, position, target_rotation,
                                                     target_position)
            if best is None or dist + 0.05 * angle < best[1] + 0.05 * best[2]:
                best = (q.copy(), dist, angle)
            if dist <= position_tol and angle <= orientation_tol:
                return q, dist, angle
            jacobian = np.zeros((6, len(names)))
            for index, name in enumerate(names):
                z, origin = axes[name]
                jacobian[:3, index] = np.cross(z, position - origin)
                jacobian[3:, index] = z
            error = np.concatenate([p_err, r_err])
            delta = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T
                                                 + damping ** 2 * np.eye(6), error)
            q = np.clip(q + np.clip(delta, -MAX_STEP_RAD, MAX_STEP_RAD), lower, upper)
        return best

    def solve(self, target_xyz: Sequence[float], target_rpy: Sequence[float],
              seeds: Sequence[Mapping[str, float]], *,
              position_tol: float = POSITION_TOLERANCE_M,
              orientation_tol: float = ORIENTATION_TOLERANCE_RAD) -> IkResult:
        """시작점을 차례로 써 보고 처음 허용치에 든 해를 돌려준다(없으면 가장 가까운 것, 실패 표시)."""
        target_rotation = rpy_matrix(*target_rpy)
        target_position = np.asarray(target_xyz, dtype=float)
        best = None
        for index, seed in enumerate(seeds):
            q, dist, angle = self._solve_from(seed, target_rotation, target_position,
                                              position_tol=position_tol,
                                              orientation_tol=orientation_tol)
            ok = dist <= position_tol and angle <= orientation_tol
            result = IkResult(dict(zip(self.arm_joints, [float(v) for v in q])), dist, angle,
                              ok, index)
            if ok:
                return result
            if best is None or dist + 0.05 * angle < (best.position_error_m
                                                      + 0.05 * best.orientation_error_rad):
                best = result
        if best is None:
            raise ValueError("시작점이 없다")
        return best

    def tip_pose(self, q: Mapping[str, float]):
        rotation, position, _ = self.fk(q)
        return rotation, position


def nearest_seed(seeds: Mapping[str, Mapping[str, float]], target_xyz: Sequence[float],
                 tips: Mapping[str, Sequence[float]]) -> list[Mapping[str, float]]:
    """검증된 자세들을 목표와 끝 위치가 가까운 순으로. 시작점일 뿐 결과가 아니다."""
    order = sorted(seeds, key=lambda name: math.dist(tips[name], target_xyz))
    return [seeds[name] for name in order]
