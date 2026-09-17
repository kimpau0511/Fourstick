#!/usr/bin/env python3
"""pick/place 계획 검증 확인 (8-10).

**로봇을 움직이지 않는다.** 계획 초안을 만들고, 단계를 펼쳐 planning scene에
묻고, 실행이 계속 막혀 있는지 확인한다.

확인하는 것:

| 항목 | 기대 |
|---|---|
| 01~03 팔레트 1/A · 2/B · 3/C 초안 | 12단계 전부 통과 · 실행 불가 유지 |
| 04 잘못된 조합(1번 팔레트 + C자재) | `plan.resource_mismatch` |
| 05 측정 전 pick 자세로 파지 | `geometry.collision` |
| 06 검사 중 scene 변경 | `geometry.snapshot_expired` |
| 07 모든 계획의 최종 실행 | BLOCK(`capability.profile_incomplete`) |
| 08 `grasp.object_held` | `unavailable` · 관문 조건 미충족 |

06번은 **실제 scene 변경 전후의 snapshot**을 쓴다. 검사 앞뒤로 임시 물체를
넣었다 빼서 hash를 실제로 바꾸고, 그 두 snapshot을 순서대로 돌려주는
클라이언트로 검사한다 — hash를 손으로 만들지 않는다. 끝나면 scene을 되돌린다.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.grasp_observation import GraspAvailability  # noqa: E402
from core.task_plan import TaskStep  # noqa: E402
from robots.fr3_gazebo.adapter import load_workcell_resources  # noqa: E402
from validation import pick_place_gate  # noqa: E402
from validation.pick_place_plan import (  # noqa: E402
    STAGE_SEQUENCE,
    CellBindings,
    validate,
)

BASE = f"http://127.0.0.1:{int(__import__('os').environ.get('FORSTICK2_PORT', 8093))}"
OUT = ROOT / "reports/workcell/pick_place_plan.json"
WORKCELL = ROOT / "config/workcell/fr3_2f85_workcell.json"
POSES = ROOT / "config/workcell/fr3_2f85_workcell_poses.json"
GRASP = ROOT / "config/workcell/fr3_2f85_workcell_grasp.json"
MOUNTING = ROOT / "config/profiles/fr3wms_to_robotiq_2f85_mounting.json"

UTTERANCES = (
    ("01_pallet_1_mat_a", "1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘",
     "mat_a", "loc_pallet_1"),
    ("02_pallet_2_mat_b", "2번 팔레트에서 B자재를 집어서 컨베이어에 올려줘",
     "mat_b", "loc_pallet_2"),
    ("03_pallet_3_mat_c", "3번 팔레트에서 C자재를 집어서 컨베이어에 올려줘",
     "mat_c", "loc_pallet_3"),
)


def post(path: str, payload: dict, timeout: float = 300.0) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read())


def get(path: str, timeout: float = 30.0) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as response:
        return json.loads(response.read())


class Slots:
    """발화 자원 대조용 최소 슬롯. 서버 응답의 matches를 그대로 옮긴다."""

    class Match:
        def __init__(self, resource_id: str, surface: str):
            self.resource_id = resource_id
            self.surface = surface

    def __init__(self, rows):
        self.matches = [self.Match(r["resource_id"], r.get("surface", ""))
                        for r in rows]


class ReplaySnapshotClient:
    """snapshot을 **미리 받아 둔 순서대로** 돌려준다. check_state는 위임한다.

    hash를 만들지 않는다 — 실제 scene에서 받은 snapshot 두 개를 쓴다.
    """

    def __init__(self, inner, snapshots):
        self._inner = inner
        self._queue = list(snapshots)

    def snapshot(self):
        return self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]

    def check_state(self, joints, *, attached=()):
        return (self._inner.check_state(joints, attached=attached) if attached
                else self._inner.check_state(joints))


def build_bindings(*, grasp_override: dict | None = None) -> CellBindings:
    resources = load_workcell_resources(
        WORKCELL, POSES, state_max_age_sec=0.5,
        mounting_path=MOUNTING, grasp_path=GRASP)
    declaration = dict(resources.grasp_declaration)
    grasp_pose = dict(resources.grasp_pose)
    if grasp_override:
        grasp_pose.update(grasp_override)
    return CellBindings(
        resources={rid: dict(row) for rid, row in resources.resources.items()},
        object_support=dict(resources.object_support),
        approach_pose=dict(resources.move_pose),
        grasp_pose=grasp_pose,
        place_pose=dict(resources.place_pose),
        home_pose=resources.safe_home_pose,
        poses={name: dict(j) for name, j in resources.poses.items()},
        gripper_joint=str(declaration.get("gripper_joint") or ""),
        gripper_open_rad=declaration.get("gripper_open_rad"),
        gripper_grasp_rad=declaration.get("gripper_grasp_rad"),
        gripper_mimic=dict(declaration.get("gripper_mimic") or {}),
        pad_links=tuple(declaration.get("pad_links") or ()),
        attached={rid: dict(spec) for rid, spec
                  in (declaration.get("attached") or {}).items()},
        pose_evidence={name: dict(v) for name, v in resources.pose_evidence.items()},
        sources={"grasp": str(GRASP)},
    )


def scene_client():
    """살아 있는 planning scene 클라이언트."""
    import rclpy
    from rclpy.node import Node

    from robots.moveit.ros_client import RosPlanningSceneClient

    data = json.loads(WORKCELL.read_text(encoding="utf-8"))
    import xml.etree.ElementTree as ET

    urdf = Path("/tmp/forstick2_gazebo/workcell/fr3wms_with_2f85.moveit.urdf")
    limits = {}
    for joint in ET.parse(urdf).getroot().findall("joint"):
        limit = joint.find("limit")
        if limit is not None and joint.get("type") == "revolute":
            limits[joint.get("name")] = (float(limit.get("lower")),
                                         float(limit.get("upper")))
    if not rclpy.ok():
        rclpy.init()
    node = Node("forstick2_pick_place_verify")
    client = RosPlanningSceneClient(
        node, group_name="fr3wms_arm", frame_id="base_link",
        joint_limits=limits,
        model_hash=f"{data['workcell_id']}:{data['workcell_version']}",
        ttl_sec=5.0, source="scripts/verify_pick_place_plan.py")
    if not client.wait(timeout_sec=30.0):
        node.destroy_node()
        raise SystemExit("MoveIt planning scene 서비스가 없다")
    return client, node


def apply_probe_object(node, *, add: bool) -> bool:
    """scene을 **실제로** 바꾼다(임시 물체 추가/제거). 끝나면 되돌린다."""
    import rclpy
    from geometry_msgs.msg import Pose
    from moveit_msgs.msg import CollisionObject, PlanningScene
    from moveit_msgs.srv import ApplyPlanningScene
    from shape_msgs.msg import SolidPrimitive

    apply_client = node.create_client(ApplyPlanningScene, "/apply_planning_scene")
    if not apply_client.wait_for_service(timeout_sec=15.0):
        return False
    obj = CollisionObject()
    obj.header.frame_id = "base_link"
    obj.id = "forstick2_scene_change_probe"
    obj.operation = CollisionObject.ADD if add else CollisionObject.REMOVE
    if add:
        primitive = SolidPrimitive()
        primitive.type = SolidPrimitive.BOX
        primitive.dimensions = [0.02, 0.02, 0.02]
        pose = Pose()
        # 로봇·셀에서 멀리 둔다 — 충돌 판정을 바꾸지 않고 hash만 바꾼다.
        pose.position.x, pose.position.y, pose.position.z = (-1.5, -1.5, 0.05)
        pose.orientation.w = 1.0
        obj.primitives = [primitive]
        obj.primitive_poses = [pose]
    scene = PlanningScene()
    scene.is_diff = True
    scene.world.collision_objects.append(obj)
    request = ApplyPlanningScene.Request()
    request.scene = scene
    future = apply_client.call_async(request)
    rclpy.spin_until_future_complete(node, future, timeout_sec=30.0)
    return bool(future.done() and future.result() and future.result().success)


def server_gate() -> dict:
    """서버가 내려보낸 관문 판정(같은 구현·같은 입력)."""
    robots = get("/v1/robots")
    rows = robots.get("declared") or []
    target = rows[0] if rows else {}
    return dict(target.get("pick_place_gate") or {})


def main() -> int:
    session = post("/v1/sessions", {})
    session_id = session.get("session_id", "")
    client, node = scene_client()
    bindings = build_bindings()
    observation = None
    checks: list[dict] = []

    def record(key: str, ok: bool, detail: str, evidence: dict | None = None) -> None:
        checks.append({"key": key, "passed": bool(ok), "detail": detail,
                       "evidence": evidence or {}})
        print(f"[{'OK ' if ok else 'BAD'}] {key}: {detail}")

    # ── 01~03 세 조합의 계획 초안과 사전 검증 ──────────────────────────
    drafts: dict[str, dict] = {}
    for key, utterance, obj, source in UTTERANCES:
        reply = post("/v1/plan", {"session_id": session_id, "utterance": utterance})
        drafts[key] = reply
        steps = reply.get("draft_steps") or []
        validation = reply.get("plan_validation") or {}
        skills = [s["skill"] for s in steps]
        args = {s["skill"]: s.get("args", {}) for s in steps}
        ok = (
            reply.get("ok") is False
            and reply.get("reason_code") == "capability.profile_incomplete"
            and "pick" in skills and "place" in skills
            and args.get("pick", {}).get("object") == obj
            and args.get("pick", {}).get("from") == source
            and validation.get("available") is True
            and validation.get("plan_verified") is True
            and validation.get("execution_allowed") is False
            and validation.get("stage_count") == len(STAGE_SEQUENCE)
            and validation.get("checks_passed") == len(STAGE_SEQUENCE)
            and validation.get("resources_matched") is True
            and validation.get("scene_stable") is True
            and not validation.get("findings")
        )
        record(key,
               ok,
               f"초안 {'→'.join(skills)} · 단계"
               f" {validation.get('checks_passed')}/{validation.get('stage_count')}"
               f" 통과 · 계획검증={validation.get('plan_verified')}"
               f" 실행가능={validation.get('execution_allowed')}"
               f" scene={(validation.get('snapshot') or {}).get('snapshot_id')}",
               {
                   "reason_code": reply.get("reason_code"),
                   "draft_steps": steps,
                   "stage_count": validation.get("stage_count"),
                   "checks_passed": validation.get("checks_passed"),
                   "plan_verified": validation.get("plan_verified"),
                   "execution_allowed": validation.get("execution_allowed"),
                   "snapshot": validation.get("snapshot"),
                   "resource_rows": validation.get("resource_rows"),
                   "stages": [
                       {k: v for k, v in stage.items() if k != "joint_rad"}
                       for stage in (validation.get("stages") or [])
                   ],
                   "checks": validation.get("checks"),
                   "grasp_observation": validation.get("grasp_observation"),
               })
        if observation is None:
            observation = validation.get("grasp_observation")

    # ── 04 잘못된 자재-팔레트 조합 ────────────────────────────────────
    wrong_steps = (
        TaskStep(skill="pick", args={"object": "mat_c", "from": "loc_pallet_1"}),
        TaskStep(skill="place", args={"object": "mat_c", "to": "loc_conveyor"}),
        TaskStep(skill="home"),
    )
    wrong = validate(
        wrong_steps,
        slots=Slots([{"resource_id": "loc_pallet_1", "surface": "1번팔레트"},
                     {"resource_id": "mat_c", "surface": "c자재"},
                     {"resource_id": "loc_conveyor", "surface": "컨베이어"}]),
        bindings=bindings, client=client,
        utterance="1번 팔레트에서 C자재를 집어서 컨베이어에 올려줘")
    codes = wrong.to_dict()["reason_codes"]
    record("04_wrong_combination",
           "plan.resource_mismatch" in codes and not wrong.plan_verified,
           f"이유 코드 {codes} · 계획검증={wrong.plan_verified}",
           {"findings": wrong.to_dict()["findings"], "reason_codes": codes})

    # 같은 발화를 서버에도 보내 본다(모델이 어떤 초안을 내는지 기록만 한다).
    live_wrong = post("/v1/plan", {
        "session_id": session_id,
        "utterance": "1번 팔레트에서 C자재를 집어서 컨베이어에 올려줘"})
    live_validation = live_wrong.get("plan_validation") or {}
    record("04b_wrong_combination_live",
           live_validation.get("execution_allowed") is not True,
           f"서버 초안 {[s['skill'] for s in live_wrong.get('draft_steps') or []]}"
           f" · 계획검증={live_validation.get('plan_verified')}"
           f" 사유={live_validation.get('reason_codes')}",
           {"draft_steps": live_wrong.get("draft_steps"),
            "reason_codes": live_validation.get("reason_codes"),
            "findings": live_validation.get("findings"),
            "note": "모델 출력은 결정적이지 않다. 판정 규칙은 04번이 확인한다"})

    # ── 05 측정 전 pick 자세를 파지에 쓰면 충돌로 막힌다 ──────────────
    # `pallet_1_pick`은 TCP를 자재 중심에 두는 자세다. 측정해 보니 그리퍼
    # 몸통이 자재를 파고든다 — 그래서 파지 높이를 따로 측정했다(8-10).
    collision_bindings = build_bindings(grasp_override={"mat_a": "pallet_1_pick"})
    collided = validate(
        (TaskStep(skill="pick", args={"object": "mat_a", "from": "loc_pallet_1"}),
         TaskStep(skill="place", args={"object": "mat_a", "to": "loc_conveyor"})),
        slots=Slots([{"resource_id": "loc_pallet_1", "surface": "1번팔레트"},
                     {"resource_id": "mat_a", "surface": "a자재"},
                     {"resource_id": "loc_conveyor", "surface": "컨베이어"}]),
        bindings=collision_bindings, client=client)
    codes = collided.to_dict()["reason_codes"]
    record("05_collision_blocked",
           "geometry.collision" in codes and not collided.plan_verified,
           f"측정 전 자세(pallet_1_pick)로 파지 → {codes}",
           {"findings": collided.to_dict()["findings"],
            "collisions": [c["collisions"] for c in collided.to_dict()["checks"]
                           if c["collisions"]]})

    # ── 06 검사 중 scene이 바뀌면 통과시키지 않는다 ───────────────────
    first = client.snapshot()
    added = apply_probe_object(node, add=True)
    time.sleep(1.0)
    second = client.snapshot()
    changed = added and second.content_hash != first.content_hash
    replay = ReplaySnapshotClient(client, [first, second])
    moved = validate(
        (TaskStep(skill="pick", args={"object": "mat_a", "from": "loc_pallet_1"}),
         TaskStep(skill="place", args={"object": "mat_a", "to": "loc_conveyor"})),
        slots=Slots([{"resource_id": "loc_pallet_1", "surface": "1번팔레트"},
                     {"resource_id": "mat_a", "surface": "a자재"},
                     {"resource_id": "loc_conveyor", "surface": "컨베이어"}]),
        bindings=bindings, client=replay)
    removed = apply_probe_object(node, add=False)
    time.sleep(1.0)
    restored = client.snapshot()
    codes = moved.to_dict()["reason_codes"]
    record("06_scene_change_blocks",
           changed and "geometry.snapshot_expired" in codes
           and not moved.plan_verified and removed
           and restored.content_hash == first.content_hash,
           f"scene hash {first.content_hash[:10]} → {second.content_hash[:10]}"
           f" → 되돌림 {restored.content_hash[:10]} · 판정 {codes}"
           f" 계획검증={moved.plan_verified}",
           {"hash_before": first.content_hash, "hash_changed": second.content_hash,
            "hash_restored": restored.content_hash,
            "probe_added": added, "probe_removed": removed,
            "reason_codes": codes,
            "method": "실제 scene 변경 전후 snapshot을 순서대로 돌려주는 클라이언트"})

    # ── 07 모든 pick/place 계획의 최종 실행은 BLOCK ───────────────────
    gate = server_gate()
    blocking = list(gate.get("blocking") or ())
    all_blocked = all(
        drafts[key].get("ok") is False
        and drafts[key].get("reason_code") == "capability.profile_incomplete"
        and (drafts[key].get("plan_validation") or {}).get("execution_allowed") is False
        for key, *_ in UTTERANCES)
    plan_gate = pick_place_gate.evaluate(
        mounting=_mounting_view(),
        verification=json.loads(
            (ROOT / "reports/workcell/gripper_control.json").read_text("utf-8")),
        plan_validation=(drafts["01_pallet_1_mat_a"].get("plan_validation") or {}),
        grasp_observation=observation or {},
        environment="simulation")
    record("07_execution_blocked",
           all_blocked and gate.get("enabled") is False
           and plan_gate.enabled is False
           and "plan_collision_revalidation" in [c.key for c in plan_gate.blocking],
           f"세 계획 모두 실행 차단 · 관문 enabled={gate.get('enabled')}"
           f" · 계획 검증 반영 후 미충족 {[c.key for c in plan_gate.blocking]}",
           {"server_gate_blocking": blocking,
            "gate_with_plan_validation": plan_gate.to_dict(),
            "reason_code": "capability.profile_incomplete"})

    # ── 08 파지 관측 ──────────────────────────────────────────────────
    obs = observation or {}
    record("08_grasp_observation_unavailable",
           obs.get("availability") == GraspAvailability.UNAVAILABLE.value
           and obs.get("held") is None
           and obs.get("counts_for_gate") is False,
           f"grasp.object_held = {obs.get('availability')}"
           f" held={obs.get('held')} 관문충족={obs.get('counts_for_gate')}",
           {"grasp_observation": obs})

    node.destroy_node()
    import rclpy

    if rclpy.ok():
        rclpy.shutdown()

    passed = sum(1 for row in checks if row["passed"])
    report = {
        "schema": "forstick2.workcell_pick_place_plan/1",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "is_simulated": True,
        "real_hardware_verified": False,
        "base_url": BASE,
        "stage_sequence": list(STAGE_SEQUENCE),
        "passed_count": passed,
        "total_count": len(checks),
        "checks": checks,
        "note": "계획 검증 통과는 실행 가능이 아니다. pick/place 실행은"
                " capability.profile_incomplete로 계속 차단된다",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"\n계획 검증 {passed}/{len(checks)} 통과 — {OUT.relative_to(ROOT)}")
    return 0 if passed == len(checks) else 1


def _mounting_view():
    """관문에 넘길 장착 Profile. 서버와 같은 로더를 쓴다."""
    from config.loader import load_mounting_profile

    payload = json.loads(MOUNTING.read_text(encoding="utf-8"))
    return load_mounting_profile(payload)


if __name__ == "__main__":
    raise SystemExit(main())
