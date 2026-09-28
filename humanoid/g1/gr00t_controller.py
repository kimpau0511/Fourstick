"""G1 조작 제어기 — 하체(다리 12 + 허리 3)를 GR00T-WholeBodyControl **Balance** 정책으로.

    humanoid/g1/run_gazebo.sh --wbc
    humanoid/g1/run_wbc.sh gr00t_controller.py --rtf 1.0 --reset-world

- 팔·손·접근·차단·STOP 규칙은 `manip_controller.ManipController` 그대로(같은 기준 — 완화하지 않는다).
- 정책: NVlabs/GR00T-WholeBodyControl `decoupled_wbc` Balance.onnx(가중치 NVIDIA Open Model License, 코드 Apache-2.0).
  서 있기 전용(속도 명령 0). 관측 86×6(명령 7 · 골반 각속도 ×0.5 · 중력 · 29관절 위치·속도 ×0.05 · 이전 행동 15),
  행동 15 → 목표 = 행동×0.25 + 기본 자세. 원 스크립트 `run_mujoco_gear_wbc.py`와 같게 만든다.
- 이 시험은 걷지 않는다: goto·velocity·return 거절, 자리 보정(제자리 걸음 표류 보정) 끔 — 정책이 서 있는다.
- 물리 차이(설정 `physics_diff`): Gazebo에는 관절 armature가 없어 발목 링크 관성을 정책 모델 값으로 대신, PD 500 Hz.
"""

from __future__ import annotations

import collections
import os
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import manip_controller as mc  # noqa: E402
import nav_controller as nav  # noqa: E402

POL = mc.SITE["policy"]
LOWER = mc.LOWER
#: 정책 관측의 29관절 순서(g1_gear_wbc.xml): 다리 12 · 허리 3 · 왼팔 7 · 오른팔 7.
OBS_JOINTS = LOWER + mc.LEFT_ARM + mc.RIGHT_ARM
DEFAULT15 = np.array(mc.SITE["lower"]["default"], dtype=np.float32)


class Gr00tController(mc.ManipController):
    def __init__(self, link, rtf):
        super().__init__(link, rtf)
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = 1          # 작은 망 — 스레드 나눔은 지연 흔들림만 키운다
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(POL["onnx"], so)
        self.in_name = self.sess.get_inputs()[0].name
        self.act15 = np.zeros(15, np.float32)
        self.hist = collections.deque([np.zeros(86, np.float32)] * POL["obs_history_len"],
                                      maxlen=POL["obs_history_len"])
        self.hold.ON_M = 1e9                 # 자리 보정 끔 — 속도 명령을 주면 Walk 정책 영역이다
        self.hold.YAW_ON = 1e9

    def reset_memory(self, why: str, sim_t: float):
        super().reset_memory(why, sim_t)
        if hasattr(self, "hist"):
            self.act15 = np.zeros(15, np.float32)
            self.hist = collections.deque([np.zeros(86, np.float32)] * POL["obs_history_len"],
                                          maxlen=POL["obs_history_len"])

    def compute_leg_target(self, q, dq, quat, omega) -> np.ndarray:
        link = self.link
        with link.cond:
            qa = np.array([link.all_q.get(n, 0.0) for n in OBS_JOINTS], np.float32)
            dqa = np.array([link.all_dq.get(n, 0.0) for n in OBS_JOINTS], np.float32)
        pad = np.zeros(len(OBS_JOINTS), np.float32)
        pad[:15] = DEFAULT15
        obs = np.zeros(86, np.float32)
        obs[3] = POL["height_cmd"]
        obs[4:7] = POL["rpy_cmd"]
        obs[7:10] = np.asarray(omega) * POL["ang_vel_scale"]
        obs[10:13] = nav.gravity_in_body(quat)
        obs[13:42] = qa - pad
        obs[42:71] = dqa * POL["dof_vel_scale"]
        obs[71:86] = self.act15
        self.hist.append(obs)
        out = self.sess.run(None, {self.in_name: np.concatenate(list(self.hist))[None, :]})[0][0]
        self.act15 = out.astype(np.float32)
        return self.act15 * POL["action_scale"] + DEFAULT15

    def fallen_target(self) -> np.ndarray:
        return DEFAULT15.copy()

    def apply_command(self, c: dict, sim_t: float, pose) -> bool:
        if c.get("type") in ("goto", "velocity", "return"):
            self.reactions.append({"id": c.get("id"), "type": c.get("type"), "sent_wall": c.get("sent_wall"),
                                   "accepted_wall": c["accepted_wall"], "applied_wall": time.time(),
                                   "applied_sim": round(sim_t, 3), "ok": False,
                                   "reason": "이 시험은 걷지 않는다(Balance 정책만) — 서기 지점에서 시작한다"})
            return False
        return super().apply_command(c, sim_t, pose)

    def status(self, sim_t, pose, wall0, sim0) -> dict:
        st = super().status(sim_t, pose, wall0, sim0)
        st["policy"] = {"kind": POL["kind"], "sha256": POL["sha256"][:12]}
        # 균형 방식 표시: 보행 정책의 "제자리 걸음"이 아니라 Balance 정책의 서 있기다.
        if st.get("balance") in ("stepping_in_place", "correcting_drift"):
            st["balance"] = "standing_balance"
        return st


def main() -> int:
    import argparse
    import warnings
    warnings.filterwarnings("ignore")
    p = argparse.ArgumentParser()
    p.add_argument("--rtf", type=float, default=1.0)
    p.add_argument("--max-wall-s", type=float, default=0.0)
    p.add_argument("--reset-world", action="store_true")
    args = p.parse_args()
    nav.LOG_DIR.mkdir(parents=True, exist_ok=True)
    import torch  # noqa: F401 — gz 노드보다 먼저(nav_controller.main 주석)
    link = mc.ManipLink()
    ctl = Gr00tController(link, args.rtf)
    code = 0
    try:
        ctl.run(args.max_wall_s, args.reset_world)
    except Exception as exc:  # noqa: BLE001
        print(f"제어기 중단: {type(exc).__name__}: {exc}", flush=True)
        code = 1
    final = nav.LOG_DIR / "nav_controller_final.json"
    if final.exists():
        (nav.LOG_DIR / "gr00t_controller_final.json").write_text(final.read_text(encoding="utf-8"),
                                                                 encoding="utf-8")
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
