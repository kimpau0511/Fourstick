"""G1 웹 작업 — 해석 → 확인 카드 → 승인 → 실행 · 관측으로 도착 판정 · 즉시 STOP.

- 대화 맥락은 **세션별·로봇별**로 따로 둔다(FR3 시연 맥락 `sim_demo_contexts`와 섞이지 않는다).
  “출발 위치로 돌아와”는 이 세션에서 G1이 마지막으로 시작한 작업의 출발 위치(제어기가 관측으로
  저장한 start_id)로만 간다. 없으면 되묻는다.
- 실행 전에 제어기·시각 진행·넘어짐·관측을 확인한다(`G1Bridge.health`). 준비가 안 되면 BLOCK.
- 도착·복귀 완료는 제어기의 도착 판정 **그리고** 서버가 따로 구독한 Gazebo pose가 1 s(시뮬레이션)
  동안 허용 오차 안에 있는지로 판단한다. 시간 제한도 시뮬레이션 시각으로 센다.
- 웹에서 세계 초기화를 하지 않는다. STOP은 목표 취소 + 명령 0 — 균형 제어(제자리 걸음)는 계속된다.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from typing import Any

from server.humanoid_commands import parse

ROBOT_ID = "unitree_g1"
CONFIRM_TTL_SEC = 60.0
LEG_TIMEOUT_SIM_S = 60.0
VERIFY_WINDOW_SIM_S = 1.0


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class HumanoidService:
    def __init__(self, bridge, site: dict, *, now=time.time):
        self.bridge = bridge
        self.site = site
        self.now = now
        self.lock = threading.Lock()
        self.contexts: dict[str, dict] = {}       # session_id → {"last_start":…, "history": […]}
        self.pending: dict[str, dict] = {}        # token → confirmation
        self.jobs: dict[str, dict] = {}
        self.running: str | None = None
        self.stop_events: dict[str, threading.Event] = {}

    # ── 맥락 ────────────────────────────────────────────────────────────
    def context(self, session_id: str) -> dict:
        with self.lock:
            return self.contexts.setdefault(session_id, {"robot": ROBOT_ID, "last_start": None,
                                                         "history": []})

    def _point(self, name: str) -> dict:
        p = self.site["safe_points"][name]
        return {"name": name, "label": p.get("label_ko", name), "xy_m": p["xy_m"],
                "yaw_rad": p["yaw_rad"], "why": p.get("why")}

    # ── 명령 ────────────────────────────────────────────────────────────
    def command(self, session_id: str, utterance: str, source: str = "text") -> dict:
        spec = parse(utterance, self.site["safe_points"])
        base = {"robot": ROBOT_ID, "utterance": utterance, "source": source,
                "interpretation": spec.to_dict(), "is_simulated": True}
        if spec.decision == "STOP":
            return {**base, **self.stop(session_id, reason=f"명령 “{utterance}”")}
        if spec.decision in ("ASK", "BLOCK"):
            return {**base, "decision": spec.decision, "reason": spec.reason}
        health = self.bridge.health()
        ctx = self.context(session_id)
        if spec.intent == "return" and not ctx["last_start"]:
            return {**base, "decision": "ASK", "health": health,
                    "reason": "복귀할 출발 위치가 없다 — 이 세션에서 G1이 시작한 이동 작업이 없다. "
                              "“컨베이어 한번 찍고 와”처럼 출발 위치를 저장하는 작업을 먼저 하세요."}
        if not health.get("ready"):
            return {**base, "decision": "BLOCK", "health": health,
                    "reason": f"실행할 수 없는 상태: {health.get('reason')}"}
        with self.lock:
            busy = self.running
        if busy:
            return {**base, "decision": "BLOCK", "health": health,
                    "reason": f"G1 작업이 이미 실행 중이다({busy}) — “멈춰”로 먼저 멈추세요"}
        noop = self._already_there(spec, ctx, health)
        if noop:
            return {**base, "decision": "NOOP", "health": health, "reason": noop}
        steps = self._plan_steps(spec, ctx)
        token = uuid.uuid4().hex
        pose = health.get("pose") or {}
        confirmation = {
            "token": token, "session_id": session_id, "robot": ROBOT_ID,
            "intent": spec.intent, "target": spec.target, "steps": steps,
            "summary": self._summary(spec, ctx),
            "evidence": spec.evidence + [
                f"선언된 지점만 사용(site.json) · 좌표를 만들지 않음",
                f"현재 관측 위치 ({pose.get('xy', ['?', '?'])[0]}, {pose.get('xy', ['?', '?'])[1]}) m, "
                f"방향 {pose.get('yaw')} rad (Gazebo pose, 시뮬레이션 {health.get('sim_time_s')} s)"],
            "arrival_rule": (f"위치 오차 < {self.site['arrival']['position_tol_m']} m, 방향 오차 < "
                             f"{self.site['arrival']['yaw_tol_rad']} rad가 "
                             f"{self.site['arrival']['settle_s']} s(시뮬레이션) 유지 — 제어기 판정 + "
                             f"서버의 Gazebo pose 재확인"),
            "health": health, "expires_at": self.now() + CONFIRM_TTL_SEC,
            "start": ctx["last_start"] if spec.intent == "return" else None,
        }
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items()
                            if v["session_id"] != session_id and v["expires_at"] > self.now()}
            self.pending[token] = confirmation
        return {**base, "decision": "CONFIRM", "confirmation": confirmation}

    def _already_there(self, spec, ctx, health) -> str | None:
        """관측 위치가 이미 목적지(가기) 또는 출발 위치(복귀) 허용 오차 안이면 움직이지 않는다.
        “찍고 와”는 목적지에 있을 때만 해당(출발=목적지라 왕복할 게 없다)."""
        pose = health.get("pose") or {}
        if "xy" not in pose:
            return None
        tol = self.site["arrival"]
        if spec.intent in ("goto", "tag"):
            p = self._point(spec.target)
            goal_xy, goal_yaw, label = p["xy_m"], p["yaw_rad"], p["label"]
        else:
            start = ctx["last_start"]
            goal_xy, goal_yaw, label = start["xy"], start["yaw"], "출발 위치"
        dist = math.hypot(pose["xy"][0] - goal_xy[0], pose["xy"][1] - goal_xy[1])
        yaw_err = abs(_wrap(pose["yaw"] - goal_yaw))
        if dist < tol["position_tol_m"] and yaw_err < tol["yaw_tol_rad"]:
            return (f"이미 {label}에 있다(Gazebo 관측 오차 {dist:.3f} m, {yaw_err:.3f} rad) — "
                    "움직이지 않는다")
        return None

    def _plan_steps(self, spec, ctx) -> list[dict]:
        if spec.intent == "goto":
            p = self._point(spec.target)
            return [{"step": "save_start", "text": "지금 관측 위치·방향을 이 작업의 출발 위치로 저장"},
                    {"step": "goto", "target": p["name"],
                     "text": f"{p['label']} ({p['xy_m'][0]}, {p['xy_m'][1]}) m · 방향 {p['yaw_rad']} rad로 이동"}]
        if spec.intent == "tag":
            p = self._point(spec.target)
            return [{"step": "save_start", "text": "지금 관측 위치·방향을 이 작업의 출발 위치로 저장"},
                    {"step": "goto", "target": p["name"],
                     "text": f"{p['label']} ({p['xy_m'][0]}, {p['xy_m'][1]}) m로 이동 — 관측으로 도착 확인"},
                    {"step": "return", "text": "저장한 출발 위치·방향으로 복귀 — 관측으로 도착 확인"}]
        start = ctx["last_start"]
        return [{"step": "return", "start_id": start["id"], "start": start,
                 "text": f"이 세션 마지막 작업의 출발 위치 ({start['xy'][0]:.2f}, {start['xy'][1]:.2f}) m, "
                         f"방향 {start['yaw']:.2f} rad로 복귀(시뮬레이션 {start['sim']} s에 관측으로 저장)"}]

    def _summary(self, spec, ctx) -> str:
        if spec.intent == "goto":
            return f"G1이 {self._point(spec.target)['label']}으로 걸어갑니다."
        if spec.intent == "tag":
            return (f"G1이 {self._point(spec.target)['label']}에 도착한 뒤, "
                    "지금 위치(출발 위치)로 돌아옵니다.")
        return "G1이 이 세션 마지막 작업의 출발 위치로 돌아갑니다."

    # ── 확인 ────────────────────────────────────────────────────────────
    def confirm(self, session_id: str, token: str, action: str) -> dict:
        with self.lock:
            pending = self.pending.get(token)
            if pending is None or pending["session_id"] != session_id:
                return {"decision": "BLOCK", "reason": "확인할 요청이 없다(만료·다른 세션·이미 처리)"}
            del self.pending[token]
        if action != "confirm":
            return {"decision": "CANCELLED", "reason": "사용자가 취소했다 — G1을 움직이지 않았다"}
        if pending["expires_at"] < self.now():
            return {"decision": "BLOCK", "reason": "확인 시간이 지났다 — 다시 명령하세요"}
        health = self.bridge.health()
        if not health.get("ready"):
            return {"decision": "BLOCK", "health": health,
                    "reason": f"실행 직전 확인 실패: {health.get('reason')}"}
        with self.lock:
            if self.running:
                return {"decision": "BLOCK", "reason": f"G1 작업이 이미 실행 중이다({self.running})"}
            job_id = "g1job_" + uuid.uuid4().hex[:10]
            job = {"job_id": job_id, "session_id": session_id, "robot": ROBOT_ID,
                   "intent": pending["intent"], "target": pending["target"],
                   "status": "running", "stage": "starting", "steps": [
                       {**s, "status": "pending"} for s in pending["steps"]],
                   "started_wall": self.now(), "started_sim": health.get("sim_time_s"),
                   "result": None, "reason": None, "is_simulated": True}
            self.jobs[job_id] = job
            self.running = job_id
            stop = threading.Event()
            self.stop_events[job_id] = stop
        threading.Thread(target=self._run, args=(job, stop), daemon=True,
                         name=f"g1-{job_id}").start()
        return {"decision": "RUN", "job": self._public(job)}

    # ── 정지 ────────────────────────────────────────────────────────────
    def stop(self, session_id: str | None, reason: str = "") -> dict:
        """확인 없이 즉시. 이동 목표를 취소하고 명령 0 — 균형 제어는 유지된다."""
        sent = None
        err = None
        try:
            sent = self.bridge.send("stop")
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
        with self.lock:
            job_id = self.running
            if job_id:
                self.stop_events[job_id].set()
            self.pending = {k: v for k, v in self.pending.items()
                            if v["session_id"] != session_id}
        react = self.bridge.reaction(sent["id"], timeout=2.0) if sent else None
        health = self.bridge.health()
        out = {"decision": "STOP", "reason": reason or "정지 요청", "job_id": job_id,
               "stop": {
                   "sent": sent is not None, "error": err,
                   "applied": bool(react and react.get("ok")),
                   "sent_to_applied_wall_ms": None if not react else round(
                       (react["applied_wall"] - react["sent_wall"]) * 1000, 1),
                   "applied_sim_s": react and react.get("applied_sim"),
                   "goal_cancelled": None if not react else react.get("cancelled_goal"),
                   # 목표 취소와 균형 상태는 따로 적는다 — 정지 자세가 아니라 제자리 걸음이다.
                   "balance": health.get("balance"), "mode": health.get("mode"),
                   "hold": health.get("hold"),
                   "controller": health.get("controller")}}
        return out

    # ── 실행 ────────────────────────────────────────────────────────────
    def _run(self, job: dict, stop: threading.Event) -> None:
        ctx = self.context(job["session_id"])
        try:
            job_start = None
            for step in job["steps"]:
                if stop.is_set():
                    break
                step["status"] = "running"
                step["started_sim"] = self._sim()
                job["stage"] = step["step"]
                if step["step"] == "save_start":
                    cmd = self.bridge.send("save_start")
                    react = self.bridge.reaction(cmd["id"])
                    if not react or not react.get("ok") or not react.get("start"):
                        raise RuntimeError("출발 위치를 저장하지 못했다")
                    job_start = react["start"]
                    step.update(status="done", start=job_start)
                    start = job_start
                    with self.lock:
                        ctx["last_start"] = start
                    continue
                if step["step"] == "goto":
                    p = self._point(step["target"])
                    cmd = self.bridge.send("goto", target=p["name"])
                    goal = (p["xy_m"], p["yaw_rad"], p["name"])
                else:
                    # 이 작업에서 저장한 출발 위치, 또는 확인 카드에 적힌 출발 위치(관측으로 저장한 것)만.
                    start = job_start or step.get("start")
                    if not start:
                        raise RuntimeError("복귀할 출발 위치가 없다")
                    cmd = self.bridge.send("return", start_id=start["id"])
                    goal = (start["xy"], start["yaw"], "start")
                react = self.bridge.reaction(cmd["id"])
                if not react or not react.get("ok"):
                    raise RuntimeError(f"제어기가 명령을 받지 않았다: {react and react.get('reason')}")
                step["command_applied_sim"] = react.get("applied_sim")
                step["command_reaction_wall_ms"] = round(
                    (react["applied_wall"] - react["sent_wall"]) * 1000, 1)
                outcome = self._wait_arrival(goal, react["applied_sim"], stop)
                step.update(outcome)
                if outcome["status"] != "done":
                    break
            if stop.is_set():
                job.update(status="stopped", result="stopped",
                           reason="STOP — 이동 목표를 취소했다. 균형 제어(제자리 걸음)는 계속된다")
                for s in job["steps"]:
                    if s["status"] in ("pending", "running"):
                        s["status"] = "cancelled"
            elif all(s["status"] == "done" for s in job["steps"]):
                job.update(status="completed", result="completed")
            else:
                job.update(status="failed", result="failed",
                           reason=next((s.get("reason") for s in job["steps"] if s.get("reason")),
                                       "도착 확인 실패"))
                self.bridge.send("stop")      # 실패한 작업의 이동 목표를 남기지 않는다(균형 제어는 유지)
        except Exception as exc:  # noqa: BLE001
            job.update(status="failed", result="failed", reason=f"{type(exc).__name__}: {exc}")
            try:
                self.bridge.send("stop")                  # 목표만 취소(균형 제어 유지)
            except Exception:  # noqa: BLE001
                pass
        finally:
            job["ended_wall"] = self.now()
            job["ended_sim"] = self._sim()
            job["stage"] = job["status"]
            with self.lock:
                if self.running == job["job_id"]:
                    self.running = None
                ctx["history"].append({"job_id": job["job_id"], "intent": job["intent"],
                                       "result": job["result"]})
                ctx["history"] = ctx["history"][-20:]

    def _sim(self) -> float | None:
        h = self.bridge.latest_pose()
        return None if h is None else round(h[0], 3)

    def _wait_arrival(self, goal, applied_sim: float, stop: threading.Event) -> dict:
        (gx, gy), gyaw, name = goal
        tol = self.site["arrival"]
        claimed_at = None
        while True:
            if stop.is_set():
                return {"status": "cancelled", "reason": "STOP으로 취소"}
            health = self.bridge.health()
            if health.get("controller") != "alive" or not health.get("sim_advancing"):
                return {"status": "failed", "reason": f"실행 중 제어기·Gazebo 이상: {health.get('reason')}"}
            if health.get("mode") == "fallen":
                return {"status": "failed", "reason": "넘어짐"}
            last = self.bridge.latest_pose()
            if last is None:
                time.sleep(0.05)
                continue
            now_sim = last[0]
            if now_sim < applied_sim - 1e-6:
                return {"status": "failed",
                        "reason": "실행 중 세계 시각이 되돌아갔다(세계 초기화) — 실행 중 초기화는 허용하지 않는다"}
            if now_sim - applied_sim > LEG_TIMEOUT_SIM_S:
                return {"status": "failed",
                        "reason": f"{LEG_TIMEOUT_SIM_S:.0f} s(시뮬레이션) 안에 도착하지 못했다"}
            nav = health.get("nav") or {}
            g = nav.get("goal") or {}
            if claimed_at is None and nav.get("arrived_sim") is not None and g.get("name") == name:
                claimed_at = nav["arrived_sim"]
            if claimed_at is not None and now_sim >= claimed_at + VERIFY_WINDOW_SIM_S:
                rows = [r for r in self.bridge.poses_since(claimed_at) if r[0] <= now_sim]
                dists = [math.hypot(r[1] - gx, r[2] - gy) for r in rows]
                yaws = [abs(_wrap(r[3] - gyaw)) for r in rows]
                ok = bool(rows) and max(dists) < tol["position_tol_m"] and \
                    max(yaws) < tol["yaw_tol_rad"]
                result = {"controller_arrived_sim": claimed_at, "verified_until_sim": round(now_sim, 3),
                          "observed_max_dist_m": round(max(dists), 4) if dists else None,
                          "observed_max_yaw_err_rad": round(max(yaws), 4) if yaws else None,
                          "duration_sim_s": round(claimed_at - applied_sim, 3),
                          "observed_final": {"xy": [round(last[1], 4), round(last[2], 4)],
                                             "yaw": round(last[3], 4)}}
                if ok:
                    return {"status": "done", **result}
                return {"status": "failed", **result,
                        "reason": "제어기는 도착이라 했지만 Gazebo 관측이 허용 오차 밖이다"}
            time.sleep(0.05)

    # ── 조회 ────────────────────────────────────────────────────────────
    def _public(self, job: dict) -> dict:
        return {k: v for k, v in job.items() if k != "session_id"}

    def job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return None if job is None else self._public(job)

    def status(self, session_id: str | None) -> dict:
        ctx = self.context(session_id) if session_id else None
        with self.lock:
            running = self.running
        last_job = None
        if ctx and ctx["history"]:
            last_job = self.job(ctx["history"][-1]["job_id"])
        return {"robot": ROBOT_ID, "is_simulated": True, "health": self.bridge.health(),
                "running_job": self.job(running) if running else None,
                "last_job": last_job,
                "session_start": ctx and ctx["last_start"],
                "control": {"controlled_joints": 12, "fixed": "팔·허리 고정(다리 12관절만 제어)",
                            "policy": "unitree_rl_gym G1 사전학습 정책(LSTM, BSD-3-Clause)"},
                "safe_points": {n: self._point(n) for n in self.site["safe_points"]},
                "scope_excluded": ["장애물 회피", "임의 목적지 이동", "팔·손 작업"]}


def build(site: dict | None = None) -> tuple[Any, HumanoidService]:
    from robots.g1_gazebo.bridge import G1Bridge, load_site
    site = site or load_site()
    bridge = G1Bridge(site)
    bridge.start()
    return bridge, HumanoidService(bridge, site)
