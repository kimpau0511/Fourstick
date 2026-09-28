"""G1 팔·손 조작 실험 제어기 — 다리 균형(보행 정책)은 그대로 돌리고, 오른팔·손을 천천히 움직인다.

    humanoid/g1/run_gazebo.sh --manip
    humanoid/g1/run_manip.sh manip_controller.py --rtf 1.0 --reset-world

- 다리: `nav_controller.NavController` 그대로(정책·PD 플러그인·자리 유지·STOP). 팔을 움직여도 다리 제어는
  바뀌지 않는다 — 균형 유지 여부는 Gazebo 관측(골반 기울기·이동·넘어짐)으로 따로 잰다.
- 상체: 두 번째 PD 플러그인(`/g1/upper/target`)에 허리·양팔·양손 목표를 50 Hz로 보낸다. 허리·왼팔·왼손은
  0에 고정, 오른팔만 속도 제한(`approach.max_joint_speed_rad_s`)으로 움직인다.
- 접근 자세: **관측한** 골반 pose와 **관측한** 물체 pose로 손바닥 목표를 계산한다(물체 중심에서 몸 방향으로
  `standoff_m`). 시뮬레이션 0.2 s마다 다시 계산해 몸 표류를 따라간다. 역기구학으로 풀리지 않거나(오차 >
  `ik_tol_m`), 경로(관절 공간 보간)에서 팔·손이 받침대·물체·몸통 상자에 너무 가까우면 **차단**한다.
- 움직이는 중 몸 기울기가 한계를 넘거나 오른팔·손이 받침대·물체에 닿으면 팔을 그 자리에 멈춘다.
- STOP: 다리는 균형 유지(제자리 걸음 + 자리 유지), 팔·손은 지금 명령 자세에 멈춘다(토크를 끄지 않는다).
- 이번 범위: 팔·손 제어와 접근 가능성. 잡기·들기·운반은 하지 않는다.

명령(`/g1/command` JSON): arm_raise · arm_home · arm_approach(선택: approach_distance_m) · hand_open · hand_close ·
stop · 그 밖의 보행 명령(velocity 등은 이 실험에서 쓰지 않는다).
"""

from __future__ import annotations

import math
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import nav_controller as nav  # noqa: E402
from controller import LEG_JOINTS  # noqa: E402
from urdf_kin import (Robot, collision_sample_points, gravity_torques, ik_pose,  # noqa: E402
                      quat_matrix, rot_error, tf)
from build_world import stable_gains  # noqa: E402

SITE = nav.SITE
WORLD, MODEL = nav.WORLD, nav.MODEL
G1_DESC = Path(os.environ.get("FORSTICK2_G1_DESC",
                              "/home/asd/external/unitree_rl_gym/resources/robots/g1_description"))
ROBOT = Robot(G1_DESC / SITE["model_urdf"])
#: 하체 PD 묶음(정책이 움직이는 관절). 기본 다리 12, GR00T 균형 정책 세계는 다리 12 + 허리 3(설정 lower).
LOWER = list((SITE.get("lower") or {}).get("joints") or LEG_JOINTS)
UPPER = [j.name for j in ROBOT.movable() if j.name not in LOWER]   # 상체 플러그인과 같은 순서
RIGHT_ARM = ["right_shoulder_pitch_joint", "right_shoulder_roll_joint", "right_shoulder_yaw_joint",
             "right_elbow_joint", "right_wrist_roll_joint", "right_wrist_pitch_joint",
             "right_wrist_yaw_joint"]
RIGHT_HAND = [n for n in UPPER if n.startswith("right_hand_")]
LEFT_ARM = [n.replace("right_", "left_") for n in RIGHT_ARM]
APPROACH = SITE["approach"]
ARM_SPEED = APPROACH["max_joint_speed_rad_s"]
#: 상체 PD 이득(세계를 만든 것과 같은 계산). 중력 보상 목표 이동에 쓴다.
KP_UPPER = {n: stable_gains(n, ROBOT.effective_inertia(n))[0] for n in UPPER}
#: 중력 보상 대상: 허리·양팔(손가락 제외 — 링크가 가볍고 이득이 작아 이동량이 커진다).
GRAV_JOINTS = [n for n in UPPER if "_hand_" not in n]
HAND_SPEED = 1.0
#: 손 닫기 목표(관절 한계 안, Dex3-1 오른손 부호). 열기 = 모두 0.
HAND_CLOSE = {"right_hand_thumb_0_joint": 0.0, "right_hand_thumb_1_joint": 0.4,
              "right_hand_thumb_2_joint": -1.2, "right_hand_middle_0_joint": 1.2,
              "right_hand_middle_1_joint": 1.3, "right_hand_index_0_joint": 1.2,
              "right_hand_index_1_joint": 1.3}
#: 천천히 팔 들기 시험 자세(앞으로 약 35°, 팔꿈치 조금 펴기). 서기 지점에서 오른쪽 앞에 받침대가 있어 오른팔은
#: 흔들림 여유를 두면 받침대와 가깝다(오프라인 점검) — 들기 시험은 기본 왼팔(장애물 없음).
ARM_RAISE = {"right": {"right_shoulder_pitch_joint": -0.35, "right_elbow_joint": 0.4},
             "left": {"left_shoulder_pitch_joint": -0.35, "left_elbow_joint": 0.4}}
#: 질량 중심 이동 계산용 다리 기본 자세(보행 정책 기본 관절각).
LEG_DEFAULT = {"left_hip_pitch_joint": -0.1, "left_knee_joint": 0.3, "left_ankle_pitch_joint": -0.2,
               "right_hip_pitch_joint": -0.1, "right_knee_joint": 0.3, "right_ankle_pitch_joint": -0.2}
_COM_HOME = ROBOT.com({**LEG_DEFAULT, **{n: 0.0 for n in ROBOT.joints if n not in LEG_DEFAULT}})


def com_shift(q_upper: dict) -> np.ndarray:
    """상체 자세가 몸 전체 질량 중심을 기본 자세에서 옮기는 양(골반 좌표계, m)."""
    return ROBOT.com({**LEG_DEFAULT, **q_upper}) - _COM_HOME


def com_ok(q_upper: dict) -> tuple[bool, list]:
    d = com_shift(q_upper)
    ok = d[0] <= APPROACH["max_com_shift_forward_m"] and abs(d[1]) <= APPROACH["max_com_shift_lateral_m"]
    return ok, [round(float(v) * 1000, 1) for v in d]
TILT_ABORT_COS = math.cos(math.radians(12.8))     # 팔 움직이는 중 골반 기울기 한계
#: 몸통 상자(골반 좌표계, m) — torso_link 메시(torso_link_rev_1_0.STL)의 경계 상자(허리 0). Gazebo는 자기
#: 충돌을 끄고 있어(보행 검증과 같게) 이 상자로 팔·손이 몸에 닿는지 예측한다.
TORSO_BOX = ((-0.071, 0.080), (-0.108, 0.108), (0.035, 0.356))
#: 오른팔·손 충돌 형상 표본점(링크 좌표계). 링크 원점·질량 중심 근사로는 손가락 끝을 과소평가해 예측 여유
#: 3 cm인데 실제로 가운뎃손가락이 받침대에 닿았다(실측) — 충돌 메시 경계 상자 점으로 바꿨다.
ARM_COLLISION_LINKS = [f"{side}_{l}" for side in ("right", "left") for l in (
    "elbow_link", "wrist_roll_link", "wrist_pitch_link", "wrist_yaw_link", "hand_palm_link",
    "hand_thumb_0_link", "hand_thumb_1_link", "hand_thumb_2_link", "hand_middle_0_link",
    "hand_middle_1_link", "hand_index_0_link", "hand_index_1_link")]
COLLISION_POINTS = collision_sample_points(G1_DESC / SITE["model_urdf"], ARM_COLLISION_LINKS)
_T_HOME = ROBOT.all_tf({})
R_HOME = _T_HOME["right_hand_palm_link"][:3, :3]            # 기본 자세 손바닥 방향(골반 좌표계)
GRASP_OFFSET = R_HOME.T @ ((_T_HOME["right_hand_thumb_2_link"][:3, 3] + _T_HOME["right_hand_index_1_link"][:3, 3]) / 2
                           - _T_HOME["right_hand_palm_link"][:3, 3])
CHECK_LINKS = ["right_elbow_link", "right_wrist_roll_link", "right_wrist_pitch_link",
               "right_wrist_yaw_link", "right_hand_palm_link", "right_hand_thumb_2_link",
               "right_hand_middle_1_link", "right_hand_index_1_link"]
CONTACT_LINKS = ["right_elbow_link", "right_wrist_roll_link", "right_wrist_pitch_link",
                 "right_wrist_yaw_link", "right_hand_thumb_0_link", "right_hand_thumb_1_link",
                 "right_hand_thumb_2_link", "right_hand_middle_0_link", "right_hand_middle_1_link",
                 "right_hand_index_0_link", "right_hand_index_1_link"]


def box_distance(p, center, half) -> float:
    """점과 축정렬 상자 사이 거리(안이면 음수)."""
    d = np.abs(np.asarray(p) - np.asarray(center)) - np.asarray(half)
    outside = np.linalg.norm(np.maximum(d, 0.0))
    return float(outside if outside > 0 else max(d))


class ManipLink(nav.Link):
    def __init__(self):
        # 부모가 구독을 시작하면 관절 콜백이 바로 온다 — 먼저 만들어 둔다.
        self.all_q: dict[str, float] = {}
        self.all_dq: dict[str, float] = {}
        self.object_pose = None
        self.contacts: list[dict] = []
        super().__init__()
        from gz.msgs.contacts_pb2 import Contacts
        from gz.msgs.double_v_pb2 import Double_V
        self.upper_pub = self.node.advertise("/g1/upper/target", Double_V)
        from gz.msgs.int32_pb2 import Int32
        self.upper_ack = -1
        self.node.subscribe(Int32, "/g1/upper/ack", self._on_upper_ack)
        for link in CONTACT_LINKS:
            topic = (f"/world/{WORLD}/model/{MODEL}/link/{link}/sensor/{link}_contact/contact")
            self.node.subscribe(Contacts, topic, lambda m, l=link: self._on_contact(l, m))
        deadline = time.time() + 5
        while time.time() < deadline and not self.upper_pub.has_connections():
            time.sleep(0.05)

    def _on_joints(self, msg):
        with self.cond:
            for j in msg.joint:
                self.all_q[j.name] = j.axis1.position
                self.all_dq[j.name] = j.axis1.velocity
        super()._on_joints(msg)

    def _on_pose(self, msg):
        for p in msg.pose:
            if p.name == SITE["object"]["model"]:
                with self.cond:
                    self.object_pose = (p.position.x, p.position.y, p.position.z,
                                        p.orientation.x, p.orientation.y, p.orientation.z,
                                        p.orientation.w)
                break
        super()._on_pose(msg)

    def _on_upper_ack(self, msg):
        with self.cond:
            self.upper_ack = msg.data
            self.cond.notify_all()

    def _on_contact(self, link, msg):
        for c in msg.contact:
            names = (c.collision1.name, c.collision2.name)
            other = next((n for n in names if not n.startswith(MODEL)), None)
            if other is None:
                other = "self:" + "|".join(names)
            with self.cond:
                self.contacts.append({"sim": msg.header.stamp.sec + msg.header.stamp.nsec * 1e-9,
                                      "link": link, "other": other.split("::")[0]})
                if len(self.contacts) > 2000:
                    del self.contacts[:1000]

    def send_upper(self, seq: int, values, timeout=2.0) -> None:
        msg = self._Double_V()
        msg.data.extend([float(seq)] + [float(v) for v in values])
        deadline = time.time() + timeout
        with self.cond:
            while True:
                self.upper_pub.publish(msg)
                if self.cond.wait_for(lambda: self.upper_ack == seq,
                                      min(0.2, max(0.0, deadline - time.time()))):
                    return
                if time.time() >= deadline:
                    raise TimeoutError(f"상체 PD 목표 확인 응답이 없다(seq {seq})")


class ManipController(nav.NavController):
    def __init__(self, link: ManipLink, rtf: float):
        super().__init__(link, rtf)
        self.up_cmd = np.zeros(len(UPPER))
        self.up_goal = np.zeros(len(UPPER))
        self.idx = {n: i for i, n in enumerate(UPPER)}
        self.arm_mode = "hold"            # hold | moving | tracking | blocked
        self.arm_reason = None
        self.arm_goal_name = "home"
        self.palm_target_w = None
        self.ik_err = None
        self.last_ik_sim = -1.0
        self.up_seq = 0
        self.arm_events: list[dict] = []
        self.approach_d = None
        self.grav_offset_max = 0.0

    # ── 관측·기구학 ─────────────────────────────────────────────────────
    def pelvis_tf(self, pose):
        x, y, z, qx, qy, qz, qw = pose
        return tf(quat_matrix(qx, qy, qz, qw), [x, y, z])

    def q_dict(self, upper_values) -> dict:
        return {n: float(upper_values[i]) for i, n in enumerate(UPPER)}

    def points_world(self, q: dict, T_wp) -> dict:
        T = ROBOT.all_tf(q, T_wp)
        pts = {}
        for n, local in COLLISION_POINTS.items():
            world = (T[n][:3, :3] @ local.T).T + T[n][:3, 3]
            for i, p in enumerate(world):
                pts[f"{n}:{i}"] = p
        return pts

    def grasp_point(self, q: dict, T_wp):
        T = ROBOT.link_tf(q, "right_hand_palm_link", T_wp)
        return T[:3, :3] @ GRASP_OFFSET + T[:3, 3], T[:3, :3]

    def clearance(self, q: dict, T_wp) -> dict:
        """팔·손 점들과 받침대·물체·몸통 상자 사이 최소 거리(m)."""
        pts = self.points_world(q, T_wp)
        c = SITE["conveyor"]
        ped = min(box_distance(p, c["center_m"], np.array(c["size_m"]) / 2) for p in pts.values())
        obj_pose = self.link.object_pose
        obj = None
        if obj_pose is not None:
            half = np.array(SITE["object"]["size_m"]) / 2
            obj = min(box_distance(p, obj_pose[:3], half) for p in pts.values())
        T_pw = np.linalg.inv(T_wp)
        torso_c = [np.mean(b) for b in TORSO_BOX]
        torso_h = [(b[1] - b[0]) / 2 for b in TORSO_BOX]
        hand_pts = [p for n, p in pts.items() if "_elbow_link" not in n]
        torso = min(box_distance((T_pw @ np.append(p, 1))[:3], torso_c, torso_h) for p in hand_pts)
        return {"pedestal_m": round(ped, 4), "object_m": None if obj is None else round(obj, 4),
                "torso_m": round(torso, 4)}

    def retreat_safe(self, q_from: dict, q_to: dict, T_wp, steps=12) -> tuple[bool, dict]:
        """물러나기(기본 자세로 돌아가기): 경로가 기준을 지키거나, 시작 자세보다 가까워지지 않으면 허용.
        이미 여유가 모자란 자세에서도 멀어지는 쪽으로는 돌아갈 수 있게 한다."""
        ok, worst = self.path_safe(q_from, q_to, T_wp, steps)
        if ok:
            return ok, worst
        start = self.clearance(q_from, T_wp)
        no_worse = all(worst[k] >= min(start[k], 9.0) - 0.005 for k in worst
                       if start.get(k) is not None)
        return no_worse, {**worst, "start": start, "rule": "시작보다 가까워지지 않음"}

    def path_safe(self, q_from: dict, q_to: dict, T_wp, steps=12) -> tuple[bool, dict]:
        worst = {"pedestal_m": 9.0, "object_m": 9.0, "torso_m": 9.0}
        for k in range(steps + 1):
            f = k / steps
            q = {n: q_from[n] + f * (q_to[n] - q_from[n]) for n in q_from}
            c = self.clearance(q, T_wp)
            for key in worst:
                if c[key] is not None:
                    worst[key] = min(worst[key], c[key])
        return self.clear_ok(worst), {k: round(v, 4) for k, v in worst.items()}

    def sway_margin(self, now_sim: float | None = None) -> float:
        """멈춘 팔의 잡기 점이 창 평균에서 흔들린 최대값(측정). 측정 전이면 기본값."""
        rows = getattr(self, "sway_samples", [])
        if len(rows) < 20:
            return max(APPROACH["sway_default_m"], max((r[1] for r in rows), default=0.0))
        return max(r[1] for r in rows)

    def clear_ok(self, c: dict) -> bool:
        sway = self.sway_margin(getattr(self, "last_sim", None))
        return (c["pedestal_m"] >= APPROACH["min_hand_to_pedestal_m"] + sway
                and c["torso_m"] >= APPROACH["min_hand_to_torso_m"]
                and (c["object_m"] is None or c["object_m"] >= APPROACH["min_hand_to_object_m"] + sway))

    def solve_approach(self, pose, full: bool = True, distance: float | None = None
                       ) -> tuple[dict | None, dict]:
        """잡기 점(엄지·검지 끝 가운데)을 물체 중심에서 몸 방향 뒤로 d, 손바닥은 기본 자세 방향을 몸
        방향으로 돌린 것. full=True: d를 훑어 경로 전체가 여유 기준을 지키는 가장 작은 d(또는 주어진 d).
        full=False(추적 중 재계산): 정한 d로, 지금 해에서 짧게 풀고 목표 자세 여유만 본다."""
        obj = self.link.object_pose
        if obj is None:
            return None, {"reason": "물체 pose 관측이 없다"}
        T_wp = self.pelvis_tf(pose)
        R_wp = T_wp[:3, :3]
        yaw = nav.yaw_of(*pose[3:])
        heading = np.array([math.cos(yaw), math.sin(yaw), 0.0])
        c, s_ = math.cos(yaw), math.sin(yaw)
        R_target_w = np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1]]) @ R_HOME
        R_target_p = R_wp.T @ R_target_w
        q_now = self.q_dict(self.up_cmd)
        if distance is not None:
            ds = [distance]
        else:
            lo, hi, st = APPROACH["approach_distance_scan_m"]
            ds = list(np.arange(lo, hi + 1e-9, st))
        last = {}
        seed = self.q_dict(self.up_goal) if not full else dict(q_now)
        for d in ds:
            gp_w = np.array(obj[:3]) - d * heading
            gp_p = (np.linalg.inv(T_wp) @ np.append(gp_w, 1))[:3]
            q, ep, er = ik_pose(ROBOT, "right_hand_palm_link", RIGHT_ARM, gp_p, R_target_p, seed,
                                tip_offset=GRASP_OFFSET, iters=200 if full else 100)
            if full and (ep > APPROACH["ik_tol_m"] or er > APPROACH["ik_rot_tol_rad"]) and ep < 0.05:
                # 연속 시작점이 국소 해에 빠지면 다른 시작점으로 다시(서기 자세 기준 해 · 대표 자세).
                for alt in self.fallback_seeds(q_now):
                    q2, ep2, er2 = ik_pose(ROBOT, "right_hand_palm_link", RIGHT_ARM, gp_p, R_target_p,
                                           alt, tip_offset=GRASP_OFFSET, iters=120)
                    if ep2 + 0.1 * er2 < ep + 0.1 * er:
                        q, ep, er = q2, ep2, er2
                    if ep <= APPROACH["ik_tol_m"] and er <= APPROACH["ik_rot_tol_rad"]:
                        break
            seed = dict(q)                      # 다음 거리는 이 해에서 시작(연속)
            info = {"approach_distance_m": round(float(d), 3), "target_w": [round(v, 4) for v in gp_w],
                    "ik_err_m": round(ep, 4), "ik_rot_err_rad": round(er, 4)}
            cok, shift = com_ok(q)
            info["com_shift_mm"] = shift
            if ep > APPROACH["ik_tol_m"] or er > APPROACH["ik_rot_tol_rad"]:
                info["reason"] = (f"잡기 자세에 닿지 않는다(위치 오차 {ep * 100:.1f} cm, 방향 오차 {er:.2f} rad) — "
                                  "지금 몸 위치에서 도달 불가")
                last = info
                continue
            if not cok:
                info["reason"] = (f"팔을 이만큼 뻗으면 질량 중심이 {shift} mm 옮겨진다(허용 앞 "
                                  f"{APPROACH['max_com_shift_forward_m'] * 1000:.0f} mm·옆 "
                                  f"{APPROACH['max_com_shift_lateral_m'] * 1000:.0f} mm) — 보행 정책이 몸을 밀어낸다")
                last = info
                continue
            if full:
                ok, worst = self.path_safe(q_now, q, T_wp)
            else:
                worst = self.clearance(q, T_wp)
                ok = self.clear_ok(worst)
            info["path_clearance"] = worst
            if not ok:
                info["reason"] = f"경로에서 팔·손이 너무 가깝다: {worst}"
                last = info
                continue
            return q, info
        return None, last

    @staticmethod
    def fallback_seeds(q_now: dict) -> list[dict]:
        out = []
        for vals in ((0.3, -0.3, -0.3, 0.3, 0.3, -0.3, 0.4), (0.0, -0.3, -0.3, 0.6, 0.3, -0.3, 0.4),
                     (0.5, -0.2, -0.3, 0.1, 0.4, -0.4, 0.5), (-0.3, -0.2, 0.0, 0.8, 0.0, 0.0, 0.0)):
            q = dict(q_now)
            q.update(dict(zip(RIGHT_ARM, vals)))
            out.append(q)
        return out

    def stance_error(self, pose) -> tuple[float, float]:
        st = SITE["safe_points"]["manip_stance"]
        dist = math.hypot(pose[0] - st["xy_m"][0], pose[1] - st["xy_m"][1])
        yerr = abs(nav.wrap(nav.yaw_of(*pose[3:]) - st["yaw_rad"]))
        return dist, yerr

    # ── 명령 ────────────────────────────────────────────────────────────
    def _arm_event(self, sim_t, event, **extra):
        self.arm_events.append({"sim": round(sim_t, 3), "event": event, **extra})

    def retreat(self, sim_t, pose, why: str):
        """팔을 기본 자세로 물린다(시작보다 가까워지지 않는 경로일 때). 안 되면 그 자리에 멈춘다."""
        goal = self.q_dict(self.up_cmd)
        for n in RIGHT_ARM:
            goal[n] = 0.0
        ok, worst = self.retreat_safe(self.q_dict(self.up_cmd), goal, self.pelvis_tf(pose))
        if ok:
            self.up_goal = np.array([goal[n] for n in UPPER])
            self.arm_mode, self.arm_goal_name = "moving", "retreat"
            self.arm_reason = f"{why} — 팔을 기본 자세로 물림"
            self._arm_event(sim_t, "arm_retreat", reason=self.arm_reason)
        else:
            self.freeze_arm(sim_t, f"{why} — 물러날 경로도 가까워져 그 자리에 멈춤 {worst}", "blocked")

    def freeze_arm(self, sim_t, reason, mode="hold"):
        self.up_goal = self.up_cmd.copy()
        self.arm_mode = mode
        self.arm_reason = reason
        self._arm_event(sim_t, "arm_frozen", reason=reason, mode=mode)

    def apply_command(self, c: dict, sim_t: float, pose) -> bool:
        kind = c.get("type")
        arm_out = max(abs(self.up_cmd[self.idx[n]]) for n in RIGHT_ARM + LEFT_ARM) > 0.05 or \
            max(abs(self.up_goal[self.idx[n]]) for n in RIGHT_ARM + LEFT_ARM) > 0.05
        if kind in ("goto", "velocity", "return") and arm_out:
            # 팔을 뻗은 채 걷지 않는다(걸으며 운반은 이번 범위 밖). 먼저 arm_home.
            rec = {"id": c.get("id"), "type": kind, "sent_wall": c.get("sent_wall"),
                   "accepted_wall": c["accepted_wall"], "applied_wall": time.time(),
                   "applied_sim": round(sim_t, 3), "ok": False,
                   "reason": "팔이 기본 자세가 아니다 — 팔을 뻗은 채 걷지 않는다(먼저 arm_home)"}
            self.reactions.append(rec)
            return False
        if kind not in ("arm_raise", "arm_home", "arm_approach", "hand_open", "hand_close"):
            stop = super().apply_command(c, sim_t, pose)
            if kind == "stop":
                self.freeze_arm(sim_t, "STOP — 팔·손을 지금 자세에 멈춤(균형 제어 유지)")
            return stop
        rec = {"id": c.get("id"), "type": kind, "sent_wall": c.get("sent_wall"),
               "accepted_wall": c["accepted_wall"], "applied_wall": time.time(),
               "applied_sim": round(sim_t, 3), "ok": True}
        if self.mode == "fallen":
            rec.update(ok=False, reason="넘어진 상태")
        elif kind in ("arm_raise", "arm_home"):
            goal = self.q_dict(self.up_cmd)
            for n in RIGHT_ARM + LEFT_ARM:
                goal[n] = 0.0
            if kind == "arm_raise":
                goal.update(ARM_RAISE[c.get("arm", "left")])
            check = self.retreat_safe if kind == "arm_home" else self.path_safe
            ok, worst = check(self.q_dict(self.up_cmd), goal, self.pelvis_tf(pose))
            rec["path_clearance"] = worst
            cok, shift = com_ok(goal)
            rec["com_shift_mm"] = shift
            if kind == "arm_raise" and not cok:
                ok = False
                worst = {**worst, "com_shift_mm": shift}
            if not ok:
                rec.update(ok=False, reason=f"경로가 안전하지 않다: {worst}")
                self.freeze_arm(sim_t, rec["reason"], "blocked")
            else:
                self.up_goal = np.array([goal[n] for n in UPPER])
                self.arm_mode, self.arm_reason = "moving", None
                self.arm_goal_name = f"raise_{c.get('arm', 'left')}" if kind == "arm_raise" else "home"
                self.palm_target_w = None
        elif kind == "arm_approach" and self.stance_error(pose)[0] > APPROACH["stance_tol_m"] or \
                kind == "arm_approach" and self.stance_error(pose)[1] > APPROACH["stance_yaw_tol_rad"]:
            dist, yerr = self.stance_error(pose)
            rec.update(ok=False, reason=(f"몸이 조작 자세 서기 지점에서 벗어나 있다(위치 {dist:.3f} m > "
                                          f"{APPROACH['stance_tol_m']} m 또는 방향 {yerr:.3f} rad) — 먼저 서기 지점으로"))
            self.freeze_arm(sim_t, rec["reason"], "blocked")
        elif kind == "arm_approach":
            fixed = c.get("approach_distance_m")
            q, info = self.solve_approach(pose, distance=None if fixed is None else float(fixed))
            rec.update(info)
            if q is None:
                rec.update(ok=False)
                self.freeze_arm(sim_t, info["reason"], "blocked")
            else:
                self.up_goal = np.array([q[n] for n in UPPER])
                self.arm_mode, self.arm_reason, self.arm_goal_name = "tracking", None, "approach"
                self.palm_target_w = info["target_w"]
                self.ik_err = info["ik_err_m"]
                self.approach_d = info["approach_distance_m"]
                self.last_ik_sim = sim_t
        else:                                             # hand_open / hand_close
            goal = self.up_goal.copy()
            for n in RIGHT_HAND:
                goal[self.idx[n]] = HAND_CLOSE.get(n, 0.0) if kind == "hand_close" else 0.0
            if kind == "hand_close":
                T_wp = self.pelvis_tf(pose)
                q_closed = self.q_dict(goal)
                cl = self.clearance(q_closed, T_wp)
                rec["closed_clearance"] = cl
            self.up_goal = goal
            if self.arm_mode in ("hold", "blocked"):
                self.arm_mode = "moving"
        self.reactions.append(rec)
        self.events.append({"sim": round(sim_t, 3), "event": "command", "id": rec["id"],
                            "type": kind, "ok": rec["ok"]})
        return False

    # ── 매 주기 ─────────────────────────────────────────────────────────
    def before_step(self, sim_t: float, pose) -> None:
        link = self.link
        x, y, z, qx, qy, qz, qw = pose
        upright = 1 - 2 * (qx * qx + qy * qy)
        moving = self.arm_mode in ("moving", "tracking") and np.any(np.abs(self.up_goal - self.up_cmd) > 1e-4)
        if moving and upright < TILT_ABORT_COS and self.arm_goal_name != "retreat":
            self.retreat(sim_t, pose, f"골반 기울기 초과(cos {upright:.3f})")
        with link.cond:
            recent = [c for c in link.contacts if c["sim"] >= sim_t - 0.05
                      and not c["other"].startswith("self:")]
        if recent and self.arm_mode in ("moving", "tracking", "hold") and self.arm_goal_name != "retreat" \
                and any(abs(self.up_cmd[self.idx[n]]) > 0.05 for n in RIGHT_ARM):
            # 멈추면 계속 걷는(제자리 걸음) 몸이 손을 더 밀어 넣는다(실측: 물체가 밀려 떨어짐) — 물러난다.
            self.retreat(sim_t, pose, f"접촉: {recent[-1]['link']} ↔ {recent[-1]['other']}")
        if self.arm_mode == "tracking" and self.stance_error(pose)[0] > APPROACH["stance_leave_m"]:
            # 멈추면 뻗은 손이 걸어가는 몸과 함께 물체 쪽으로 간다(실측) — 물린다.
            self.retreat(sim_t, pose, f"몸이 서기 지점에서 {self.stance_error(pose)[0]:.3f} m 벗어남")
        if self.arm_mode == "tracking" and sim_t - self.last_ik_sim >= 0.2:
            q, info = self.solve_approach(pose, full=False, distance=self.approach_d)
            self.last_ik_sim = sim_t
            if q is None:
                # 재계산이 수렴하지 않으면 이전(검증된) 목표를 유지한다. 멈춤은 서기 이탈·접촉·기울기가 맡는다.
                self.retrack_failures = getattr(self, "retrack_failures", 0) + 1
            else:
                self.up_goal = np.array([q[n] for n in UPPER])
                self.palm_target_w = info["target_w"]
                self.ik_err = info["ik_err_m"]
        # 속도 제한으로 한 주기만큼 목표 쪽으로.
        limit = np.array([HAND_SPEED if n.startswith("right_hand_") else ARM_SPEED for n in UPPER])
        self.up_cmd = self.up_cmd + np.clip(self.up_goal - self.up_cmd, -limit * nav.TICK_SIM,
                                            limit * nav.TICK_SIM)
        # 중력 보상: PD가 목표에서 처지지 않게 목표를 τ_중력/kp만큼 옮겨 보낸다(관측 골반 방향의 중력).
        R_wp = quat_matrix(qx, qy, qz, qw)
        tau = gravity_torques(ROBOT, self.q_dict(self.up_cmd), GRAV_JOINTS,
                              R_wp.T @ np.array([0.0, 0.0, -9.81]))
        sent = self.up_cmd.copy()
        for n, t in tau.items():
            sent[self.idx[n]] = self.up_cmd[self.idx[n]] - t / KP_UPPER[n]
        self.grav_offset_max = float(np.max(np.abs(sent - self.up_cmd)))
        self.up_seq = (self.up_seq + 1) % 1_000_000_000
        link.send_upper(self.up_seq, sent)

    def status(self, sim_t, pose, wall0, sim0) -> dict:
        st = super().status(sim_t, pose, wall0, sim0)
        link = self.link
        with link.cond:
            obs = {n: link.all_q.get(n) for n in UPPER}
            obj = link.object_pose
            contacts = [c for c in link.contacts if c["sim"] >= sim_t - 0.5]
        have = all(v is not None for v in obs.values())
        palm_obs = palm_cmd = None
        if have and pose is not None:
            T_wp = self.pelvis_tf(pose)
            palm_obs, R_obs = self.grasp_point(obs, T_wp)
            palm_cmd, _ = self.grasp_point(self.q_dict(self.up_cmd), T_wp)
            yaw = nav.yaw_of(*pose[3:])
            c, s_ = math.cos(yaw), math.sin(yaw)
            rot_err_obs = float(np.linalg.norm(rot_error(
                np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1]]) @ R_HOME, R_obs)))
        self.last_sim = sim_t
        # 흔들림: 팔이 멈춰 있는 동안(어느 자세든) 잡기 점 월드 위치를 모아, 창 평균에서 가장 먼 거리.
        # 몸이 제자리 걸음으로 흔들려 손도 흔들린다 — 접근 여유에 더한다.
        if palm_obs is not None and float(np.max(np.abs(self.up_goal - self.up_cmd))) < 1e-3:
            self.sway_points = [r for r in getattr(self, "sway_points", [])
                                if r[0] >= sim_t - APPROACH["sway_window_sim_s"]] + [(sim_t, palm_obs.copy())]
            pts = np.array([r[1] for r in self.sway_points])
            ts = np.array([r[0] for r in self.sway_points])
            if len(pts) >= 50:
                # 느린 표류는 0.2 s마다 목표를 다시 계산해 따라간다 — 여유에는 걸음 주기(0.8 s)의 빠른 흔들림만
                # 넣는다: 각 점의 앞뒤 0.8 s(두 걸음 주기) 평균에서 떨어진 거리의 최대값(누적합으로 계산).
                csum = np.vstack([np.zeros(3), np.cumsum(pts, axis=0)])
                lo = np.searchsorted(ts, ts - 0.8, side="left")
                hi = np.searchsorted(ts, ts + 0.8, side="right")
                ok = (ts - 0.8 >= ts[0]) & (ts + 0.8 <= ts[-1])
                if ok.any():
                    means = (csum[hi] - csum[lo]) / (hi - lo)[:, None]
                    devs = np.linalg.norm(pts - means, axis=1)[ok]
                    self.sway_samples = [(sim_t, float(devs.max()))] * 20
                    self.sway_drift = float(np.max(np.linalg.norm(pts - pts.mean(0), axis=1)))
        elif float(np.max(np.abs(self.up_goal - self.up_cmd))) >= 1e-3:
            self.sway_points = []                               # 자세가 바뀌면 새로 모은다
        arm_err = None if not have else max(abs(obs[n] - self.up_cmd[self.idx[n]]) for n in RIGHT_ARM + LEFT_ARM)
        hand_err = None if not have else max(abs(obs[n] - self.up_cmd[self.idx[n]]) for n in RIGHT_HAND)
        st["arm"] = {
            "mode": self.arm_mode, "goal": self.arm_goal_name, "reason": self.arm_reason,
            "remaining_rad": round(float(np.max(np.abs(self.up_goal - self.up_cmd))), 4),
            "cmd": {n.replace("right_", "").replace("_joint", ""): round(float(self.up_cmd[self.idx[n]]), 4)
                    for n in RIGHT_ARM},
            "obs": None if not have else {n.replace("right_", "").replace("_joint", ""): round(obs[n], 4)
                                          for n in RIGHT_ARM},
            "left_obs": None if not have else {n.replace("left_", "").replace("_joint", ""): round(obs[n], 4)
                                               for n in LEFT_ARM},
            "track_err_max_rad": None if arm_err is None else round(arm_err, 4),
            "palm_obs_w": None if palm_obs is None else [round(v, 4) for v in palm_obs],
            "palm_cmd_w": None if palm_cmd is None else [round(v, 4) for v in palm_cmd],
            "palm_target_w": self.palm_target_w, "ik_err_m": self.ik_err,
            "approach_distance_m": self.approach_d,
            "gravity_offset_max_rad": round(self.grav_offset_max, 4),
            "retrack_failures": getattr(self, "retrack_failures", 0),
            "sway_margin_m": round(self.sway_margin(sim_t), 4),
            "sway_samples": len(getattr(self, "sway_samples", [])),
            "sway_incl_drift_m": round(getattr(self, "sway_drift", 0.0), 4),
            "grasp_rot_err_obs_rad": None if palm_obs is None else round(rot_err_obs, 4),
            "note": "palm_* = 잡기 점(엄지·검지 끝 가운데), 관측 관절 + 관측 골반 pose로 순방향 기구학",
            "palm_to_object_m": None if (palm_obs is None or obj is None) else round(
                float(np.linalg.norm(palm_obs - np.array(obj[:3]))), 4),
            "clearance_obs": None if not have else self.clearance(obs, self.pelvis_tf(pose)),
            "events": self.arm_events[-6:],
            "stance_error": [round(v, 4) for v in self.stance_error(pose)] if pose is not None else None,
        }
        st["hand"] = {"cmd": {n.replace("right_hand_", "").replace("_joint", ""): round(float(self.up_cmd[self.idx[n]]), 3)
                              for n in RIGHT_HAND},
                      "obs": None if not have else {n.replace("right_hand_", "").replace("_joint", ""): round(obs[n], 3)
                                                    for n in RIGHT_HAND},
                      "track_err_max_rad": None if hand_err is None else round(hand_err, 4)}
        st["object_pose"] = None if obj is None else [round(v, 4) for v in obj[:3]]
        st["contacts_last_0_5s"] = contacts[-10:]
        st["model"] = MODEL
        return st


def main() -> int:
    import argparse
    import json
    import warnings
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser()
    parser.add_argument("--rtf", type=float, default=1.0)
    parser.add_argument("--max-wall-s", type=float, default=0.0)
    parser.add_argument("--reset-world", action="store_true")
    args = parser.parse_args()
    nav.LOG_DIR.mkdir(parents=True, exist_ok=True)
    import torch  # noqa: F401 — gz 노드보다 먼저(nav_controller.main 주석 참고)
    link = ManipLink()
    ctl = ManipController(link, args.rtf)
    code = 0
    try:
        ctl.run(args.max_wall_s, args.reset_world)
    except Exception as exc:  # noqa: BLE001
        print(f"제어기 중단: {type(exc).__name__}: {exc}", flush=True)
        code = 1
    final = nav.LOG_DIR / "nav_controller_final.json"
    if final.exists():
        (nav.LOG_DIR / "manip_controller_final.json").write_text(final.read_text(encoding="utf-8"),
                                                                 encoding="utf-8")
    _ = json
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    raise SystemExit(main())
