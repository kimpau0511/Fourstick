"""셀이 정말 멈췄는가 — 중단된 반복 작업의 잠금을 풀기 전 확인 (2026-10-07).

서버가 다시 뜨면 반복은 '중단'으로 남지만, 그 표시만으로 셀 잠금을 풀지 않는다. 사람이 '상태 확인 후 잠금
해제'를 누르면 여기서 **관측으로** 다시 본다. 하나라도 확인되지 않으면 잠금을 유지한다(fail closed).

1. 실행 종료: 시연 실행기(재시작 뒤 이어받은 프로세스 포함)·일반 실행이 없고, 복구 필요 표시가 없다.
2. 로봇 정지: 컨트롤러 액션에 살아 있는 goal이 없고(`ACTIVE_GOAL_STATUSES`), 최신 관절 값이
   `STILL_WINDOW_SEC` 동안 `STILL_TOL_RAD` 안에서 변하지 않았다.
3. 부착 상태: 자재마다 Gazebo 붙임(DetachableJoint) 상태가 확정돼 있다. 알림이 없어 'unknown'이면
   그 자재가 원래 자리나 컨베이어 칸에 놓여 있는 것이 관측될 때만 '놓여 있음'으로 본다.
   그리퍼에 붙어 있으면 막지 않고 알린다 — 잠금이 풀려야 시뮬레이션 보기에서 재개·복구할 수 있다.

로봇에 명령을 보내지 않는다.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Mapping

ACTION_STATUS_TOPICS = (
    "/arm_trajectory_controller/follow_joint_trajectory/_action/status",
    "/gripper_trajectory_controller/follow_joint_trajectory/_action/status",
)
#: action_msgs GoalStatus: ACCEPTED·EXECUTING·CANCELING (scripts/demo_workcell_pick_place.py와 같은 값).
ACTIVE_GOAL_STATUSES = frozenset({1, 2, 3})
STILL_WINDOW_SEC = 1.0
STILL_TOL_RAD = 1e-3
JOINT_FRESH_SEC = 1.0
#: 'unknown' 붙임 상태를 '놓여 있음'으로 볼 관측 허용치(모델 원점 vs 선언 자리 — 높이는 원점·중심 차이를 둔다).
REST_TOL_XY_M = 0.03
REST_TOL_Z_M = 0.06


def observe_active_goals(timeout_sec: float = 3.0) -> int | None:
    """컨트롤러 액션 status에서 살아 있는 goal 수. 받지 못한 토픽이 있으면 None."""
    import rclpy
    from action_msgs.msg import GoalStatusArray
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    if not rclpy.ok():
        rclpy.init()
    node = Node(f"forstick2_cell_quiet_{os.getpid()}_{int(time.time() * 1000) % 100000}")
    try:
        qos = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL,
                         reliability=ReliabilityPolicy.RELIABLE)
        seen: dict[str, list[int]] = {}
        for topic in ACTION_STATUS_TOPICS:
            node.create_subscription(
                GoalStatusArray, topic,
                lambda msg, t=topic: seen.__setitem__(t, [s.status for s in msg.status_list]), qos)
        deadline = time.monotonic() + timeout_sec
        while len(seen) < len(ACTION_STATUS_TOPICS) and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        if len(seen) < len(ACTION_STATUS_TOPICS):
            return None
        return sum(1 for statuses in seen.values() for s in statuses if s in ACTIVE_GOAL_STATUSES)
    finally:
        node.destroy_node()


def observe_attachments(models, partition: str) -> dict[str, dict]:
    """자재별 붙임 상태: 시스템이 떠 있는지(토픽) + 마지막으로 확인된 알림(기록 파일)."""
    from robots.fr3_gazebo.sim_fixture import GazeboObjectFixture, read_joint_record

    os.environ["GZ_PARTITION"] = partition
    from gz.transport import Node as GzNode

    topics = set(GzNode().topic_list())
    record = read_joint_record()
    return {m: {"loaded": GazeboObjectFixture.joint_topics(m)["state"] in topics,
                "state": (record.get(m) or {}).get("state")} for m in models}


def _near(pose, place) -> bool:
    return (abs(pose[0] - place[0]) <= REST_TOL_XY_M and abs(pose[1] - place[1]) <= REST_TOL_XY_M
            and abs(pose[2] - place[2]) <= REST_TOL_Z_M)


def check_cell_quiet(runtime: Any, api: Any, *,
                     goals: Callable[[], int | None] = observe_active_goals,
                     attachments: Callable[..., dict] = observe_attachments,
                     sleep: Callable[[float], None] = time.sleep) -> dict:
    """{"ok", "checks": [{"name","ok","detail"}], "notes": [...]} — 하나라도 ok가 아니면 잠금을 유지한다."""
    checks: list[dict] = []
    notes: list[str] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # 1. 실행 종료
    jobs = runtime.sim_demo_jobs
    running = jobs.running()
    recovery = getattr(jobs, "recovery_required", None)
    with api._flag_lock:
        executing = bool(api._active_executions)
    if running is not None:
        add("process", False, f"실행 프로세스가 아직 동작 중입니다({running.get('action_label') or running.get('action')}"
                              f" · {running.get('job_id')})")
    elif recovery:
        add("process", False, f"실행기 복구가 필요합니다: {recovery.get('reason')}")
    elif executing:
        add("process", False, "일반 실행이 아직 진행 중입니다")
    else:
        add("process", True, "실행 프로세스 없음(재시작 뒤 이어받은 실행도 끝남)")

    # 2. 로봇 정지 — 컨트롤러 goal + 관절 값
    try:
        active = goals()
    except Exception as exc:  # noqa: BLE001 — 확인 못 하면 잠금 유지
        active = None
        notes.append(f"컨트롤러 상태 조회 오류: {type(exc).__name__}")
    if active is None:
        add("controller", False, "컨트롤러 상태를 받지 못했습니다 — 로봇 정지를 확인할 수 없습니다")
    else:
        add("controller", active == 0, "컨트롤러에 실행 중인 궤적 없음" if active == 0
            else f"컨트롤러에 실행 중인 궤적이 {active}개 있습니다")
    view = getattr(runtime, "sim_view", None)
    state = getattr(view, "state", None)
    if state is None:
        add("joints", False, "관절 관측을 쓸 수 없습니다 — 로봇 정지를 확인할 수 없습니다")
        first = None
    else:
        first = state.sample()
        sleep(STILL_WINDOW_SEC)
        second = state.sample()
        fresh = all(s.get("joints") and s.get("joint_age_sec") is not None
                    and s["joint_age_sec"] <= JOINT_FRESH_SEC for s in (first, second))
        if not fresh or second.get("joint_seq") == first.get("joint_seq"):
            add("joints", False, "최신 관절 값을 받지 못했습니다 — 로봇 정지를 확인할 수 없습니다")
        else:
            moved = max(abs(second["joints"][k] - first["joints"].get(k, second["joints"][k]))
                        for k in second["joints"])
            add("joints", moved <= STILL_TOL_RAD,
                f"관절 정지(최대 변화 {moved:.4f} rad / {STILL_WINDOW_SEC:.0f}초)" if moved <= STILL_TOL_RAD
                else f"관절이 움직이고 있습니다(최대 변화 {moved:.4f} rad / {STILL_WINDOW_SEC:.0f}초)")

    # 3. 부착 상태
    materials = dict(jobs.materials)
    workcell = getattr(view, "workcell", None) or {}
    try:
        attached = attachments(list(materials), str(workcell.get("gz_partition") or ""))
    except Exception as exc:  # noqa: BLE001
        attached = None
        notes.append(f"붙임 상태 조회 오류: {type(exc).__name__}")
    if attached is None:
        add("attachment", False, "자재 붙임 상태를 확인할 수 없습니다")
    else:
        from server.sim_view import cell_boxes

        homes = {m["model"]: m["home_xyz_m"] for m in cell_boxes(workcell)["materials"]} if workcell else {}
        places = list(homes.values()) + [list(s.center_m) for s in getattr(jobs, "slots", ())]
        poses = ((second if state is not None else None) or {}).get("materials") or {}
        bad, held = [], []
        for model, row in attached.items():
            name = materials[model].get("korean") or model
            if not row["loaded"] or row["state"] == "detached":
                continue
            if row["state"] == "attached":
                held.append(name)
                continue
            pose = poses.get(model)
            if pose is not None and any(_near(pose, p) for p in places):
                notes.append(f"{name}: 붙임 알림은 없지만 원래 자리·컨베이어 칸에 놓인 것이 관측됨")
                continue
            bad.append(name)
        if bad:
            add("attachment", False, f"{', '.join(bad)}의 붙임 상태를 확인할 수 없습니다(알림 없음·놓인 자리 관측 안 됨)")
        else:
            add("attachment", True, "그리퍼에 붙은 자재 없음" if not held
                else f"{', '.join(held)}이(가) 그리퍼에 붙어 있습니다 — 시뮬레이션 보기에서 재개·복구하세요")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "notes": notes, "at": time.time()}
