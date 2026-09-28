"""G1 웹 명령 — 해석·확인·세션 분리·준비 확인·STOP·관측 도착 판정 (Gazebo 없이)."""

from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.humanoid_commands import parse  # noqa: E402
from server.humanoid_service import HumanoidService  # noqa: E402

SITE = json.loads((ROOT / "humanoid/g1/config/site.json").read_text(encoding="utf-8"))
POINTS = SITE["safe_points"]


class FakeBridge:
    """제어기 대신: 명령을 기록하고, goto/return이면 목표 지점 관측을 만들어 낸다."""

    def __init__(self, ready=True, pose=(0.0, 0.0, 0.0)):
        self.ready = ready
        self.pose = list(pose)            # x, y, yaw
        self.sent: list[dict] = []
        self.sim = 10.0
        self.goal = None
        self.arrived = None
        self.log: list[tuple] = []
        self.starts: dict[str, dict] = {}

    def send(self, kind, **fields):
        cmd = {"type": kind, "id": f"c{len(self.sent)}", "sent_wall": time.time(), **fields}
        self.sent.append(cmd)
        if kind == "goto":
            p = POINTS[fields["target"]]
            self.goal = (fields["target"], p["xy_m"], p["yaw_rad"])
        elif kind == "return":
            st = self.starts[fields["start_id"]]
            self.goal = ("start", st["xy"], st["yaw"])
        elif kind == "stop":
            self.goal = None
        self.arrived = None
        return cmd

    def reaction(self, cmd_id, timeout=3.0):
        cmd = next(c for c in self.sent if c["id"] == cmd_id)
        row = {"id": cmd_id, "ok": True, "sent_wall": cmd["sent_wall"], "applied_wall": time.time(),
               "applied_sim": self.sim}
        if cmd["type"] == "save_start":
            sid = f"start_{len(self.starts) + 1}"
            self.starts[sid] = {"id": sid, "xy": self.pose[:2], "yaw": self.pose[2], "sim": self.sim}
            row["start"] = self.starts[sid]
        if cmd["type"] == "stop":
            row["cancelled_goal"] = "conveyor_front"
        return row

    def _advance(self):
        # 목표가 있으면 한 번에 도착한 것으로 관측을 만든다(시각은 앞으로만).
        self.sim += 0.5
        if self.goal:
            name, xy, yaw = self.goal
            self.pose = [xy[0] + 0.02, xy[1], yaw]
            if self.arrived is None:
                self.arrived = self.sim
        self.log.append((self.sim, self.pose[0], self.pose[1], self.pose[2], 0.78, 1.0))

    def health(self):
        self._advance()
        nav = {"goal": {"name": self.goal[0]} if self.goal else None,
               "arrived_sim": self.arrived}
        return {"ready": self.ready, "reason": None if self.ready else "제어기 꺼짐",
                "controller": "alive" if self.ready else "absent", "sim_advancing": self.ready,
                "mode": "goto" if self.goal else "stand", "balance": "stepping_in_place",
                "sim_time_s": self.sim, "nav": nav,
                "pose": {"xy": self.pose[:2], "yaw": self.pose[2]}}

    def latest_pose(self):
        return self.log[-1] if self.log else None

    def poses_since(self, t0):
        return [r for r in self.log if r[0] >= t0]


def wait_job(service, job_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = service.job(job_id)
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("작업이 끝나지 않았다")


class ParseTest(unittest.TestCase):
    def test_supported_sentences(self):
        self.assertEqual(parse("컨베이어 앞에 가", POINTS).intent, "goto")
        self.assertEqual(parse("컨베이어 한번 찍고 와", POINTS).intent, "tag")
        self.assertEqual(parse("출발 위치로 돌아와", POINTS).intent, "return")
        self.assertEqual(parse("멈춰", POINTS).decision, "STOP")

    def test_unclear_or_out_of_scope(self):
        self.assertEqual(parse("저기로 가", POINTS).decision, "ASK")
        self.assertEqual(parse("찍고 와", POINTS).decision, "ASK")         # 목적지 없음
        self.assertEqual(parse("앞으로 2미터 가", POINTS).decision, "BLOCK")  # 임의 좌표
        self.assertEqual(parse("자재 집어", POINTS).decision, "BLOCK")       # 팔 작업
        self.assertNotEqual(parse("팔레트로 가", POINTS).reason or "", "")   # 선언 안 된 곳 → 되묻기
        self.assertEqual(parse("팔레트로 가", POINTS).decision, "ASK")


class ServiceTest(unittest.TestCase):
    def test_not_ready_blocks(self):
        svc = HumanoidService(FakeBridge(ready=False), SITE)
        out = svc.command("s1", "컨베이어 앞에 가")
        self.assertEqual(out["decision"], "BLOCK")
        self.assertIn("실행할 수 없는", out["reason"])

    def test_return_needs_this_sessions_start(self):
        bridge = FakeBridge()
        svc = HumanoidService(bridge, SITE)
        self.assertEqual(svc.command("s1", "출발 위치로 돌아와")["decision"], "ASK")
        card = svc.command("s1", "컨베이어 한번 찍고 와")
        self.assertEqual(card["decision"], "CONFIRM")
        self.assertEqual([s["step"] for s in card["confirmation"]["steps"]],
                         ["save_start", "goto", "return"])
        # 다른 세션은 그 토큰으로 승인할 수 없다.
        self.assertEqual(svc.confirm("s2", card["confirmation"]["token"], "confirm")["decision"],
                         "BLOCK")
        run = svc.confirm("s1", card["confirmation"]["token"], "confirm")
        self.assertEqual(run["decision"], "RUN")
        job = wait_job(svc, run["job"]["job_id"])
        self.assertEqual(job["status"], "completed", job.get("reason"))
        self.assertTrue(all(s["status"] == "done" for s in job["steps"]))
        self.assertLess(job["steps"][1]["observed_max_dist_m"], SITE["arrival"]["position_tol_m"])
        # s1은 이제 출발 위치가 있고, s2는 여전히 없다(맥락 분리).
        self.assertEqual(svc.command("s2", "출발 위치로 돌아와")["decision"], "ASK")
        bridge.goal = None                 # 가짜 제어기: 자리 유지 중으로
        bridge.pose = [1.0, 1.0, 0.5]
        self.assertEqual(svc.command("s1", "출발 위치로 돌아와")["decision"], "CONFIRM")
        # 보낸 명령에 좌표가 없다 — 지점 이름과 출발점 id만.
        for cmd in bridge.sent:
            self.assertFalse({"x", "y", "xy", "yaw"} & set(cmd))

    def test_noop_when_already_there(self):
        p = POINTS["conveyor_front"]
        svc = HumanoidService(FakeBridge(pose=(p["xy_m"][0], p["xy_m"][1], p["yaw_rad"])), SITE)
        self.assertEqual(svc.command("s1", "컨베이어 앞에 가")["decision"], "NOOP")

    def test_stop_is_immediate_and_reports_goal_and_balance_separately(self):
        bridge = FakeBridge()
        svc = HumanoidService(bridge, SITE)
        out = svc.command("s1", "멈춰")
        self.assertEqual(out["decision"], "STOP")
        self.assertEqual(bridge.sent[-1]["type"], "stop")
        self.assertEqual(out["stop"]["goal_cancelled"], "conveyor_front")
        self.assertEqual(out["stop"]["balance"], "stepping_in_place")
        self.assertNotIn("confirmation", out)


if __name__ == "__main__":
    unittest.main()
