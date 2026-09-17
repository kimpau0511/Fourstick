"""pick/place **계획 검증** (md/개발플랜.md 8-10).

실행을 열지 않는다. 이 모듈이 하는 일은 하나다 — pick/place 계획 초안을
단계로 펼쳐 놓고, 각 단계가 **자원·도달·관절 제한·충돌** 기준을 지나는지
planning scene에 물어 기록하는 것.

## 왜 원자 스킬이 아니라 "단계"인가

계약의 원자 스킬은 5개뿐이다(`core/constants.ATOMIC_SKILLS`). "그리퍼 열기"는
스킬이 아니다 — 카탈로그가 거부한다. 그래서 계획은 그대로 원자 스킬로 두고,
검증만 **단계(stage)** 로 펼친다. 단계는 계약을 늘리지 않는 검증용 표현이며,
어느 원자 스텝에서 나왔는지 `source_step`으로 되짚을 수 있다.

    home → 팔레트 접근 → pre-grasp → 그리퍼 열기 → pick 접근
         → 그리퍼 닫기 → lift → 컨베이어 접근 → place 접근
         → 그리퍼 열기 → retreat → home

## 판정 규칙

1. 자원은 **셀 선언에만** 있는 것을 쓴다. 발화에 없는 자원, 셀에 없는 자원,
   선언된 받침과 다른 팔레트를 가리키는 조합은 각각 다른 이유 코드다.
2. 단계에 대응하는 **검증된 자세가 없으면 통과시키지 않는다.** 자세를 만들지
   않는다(`geometry.workspace_violation`).
3. 관절 제한과 충돌은 planning scene 구현체에 묻는다. 우리가 추정하지 않는다.
4. 파지 이후 단계는 **물체를 붙여서** 묻는다. 들고 있는 물체가 무엇과 닿는지
   보지 않으면 적재 상태를 검사한 것이 아니다.
5. 접촉 허용은 **선언된 것만**이다(패드 ↔ 대상 물체, 물체 ↔ 같은 물체의 world
   인스턴스, 파지 단계의 물체 ↔ 선언된 받침면). 나머지 접촉은 전부 충돌이다.
   통과시키려고 허용 범위를 넓히지 않는다.
6. 검사 전후 scene hash가 다르면 **통과로 두지 않는다**(`geometry.snapshot_expired`).
7. **계획 검증 통과는 실행 가능이 아니다.** `execution_allowed`는 항상 False다 —
   실행 허가는 `validation/pick_place_gate.py`의 관문이 내고, 그 관문에는
   장착 근거·질량·파지 관측 조건이 남아 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from core.geometry import AttachedObject
from core.reason_codes import ReasonCode

#: 단계 역할 이름. 계약(원자 스킬)이 아니라 **검증 단계**의 이름이다.
STAGE_HOME_START = "home_start"
STAGE_PICK_APPROACH = "pick_approach"
STAGE_PRE_GRASP = "pre_grasp"
STAGE_GRIPPER_OPEN_BEFORE = "gripper_open_before_grasp"
STAGE_GRASP_APPROACH = "grasp_approach"
STAGE_GRIPPER_CLOSE = "gripper_close"
STAGE_LIFT = "lift"
STAGE_PLACE_APPROACH = "place_approach"
STAGE_PLACE_DESCEND = "place_descend"
STAGE_GRIPPER_OPEN_RELEASE = "gripper_open_release"
STAGE_RETREAT = "retreat"
STAGE_HOME_END = "home_end"

#: 사용자가 정한 단계 순서. 이 순서로만 펼친다.
STAGE_SEQUENCE: tuple[str, ...] = (
    STAGE_HOME_START,
    STAGE_PICK_APPROACH,
    STAGE_PRE_GRASP,
    STAGE_GRIPPER_OPEN_BEFORE,
    STAGE_GRASP_APPROACH,
    STAGE_GRIPPER_CLOSE,
    STAGE_LIFT,
    STAGE_PLACE_APPROACH,
    STAGE_PLACE_DESCEND,
    STAGE_GRIPPER_OPEN_RELEASE,
    STAGE_RETREAT,
    STAGE_HOME_END,
)

STAGE_LABELS: Mapping[str, str] = {
    STAGE_HOME_START: "안전 home",
    STAGE_PICK_APPROACH: "팔레트 접근",
    STAGE_PRE_GRASP: "pre-grasp",
    STAGE_GRIPPER_OPEN_BEFORE: "그리퍼 열기",
    STAGE_GRASP_APPROACH: "pick 접근",
    STAGE_GRIPPER_CLOSE: "그리퍼 닫기",
    STAGE_LIFT: "lift",
    STAGE_PLACE_APPROACH: "컨베이어 접근",
    STAGE_PLACE_DESCEND: "place 접근",
    STAGE_GRIPPER_OPEN_RELEASE: "그리퍼 열기(해제)",
    STAGE_RETREAT: "retreat",
    STAGE_HOME_END: "안전 home 복귀",
}

#: 그리퍼 명령만 바뀌고 팔은 그 자리에 있는 단계.
GRIPPER_STAGES = frozenset({
    STAGE_GRIPPER_OPEN_BEFORE, STAGE_GRIPPER_CLOSE, STAGE_GRIPPER_OPEN_RELEASE,
})

#: 물체를 들고 있는 단계(파지 이후, 해제 전).
HOLDING_STAGES = frozenset({
    STAGE_GRIPPER_CLOSE, STAGE_LIFT, STAGE_PLACE_APPROACH, STAGE_PLACE_DESCEND,
})

#: 물체가 아직 받침면에 놓여 있는 단계. 받침면 접촉이 선언된 예외다.
SUPPORTED_STAGES = frozenset({STAGE_GRASP_APPROACH, STAGE_GRIPPER_CLOSE})


class PlanValidationError(Exception):
    def __init__(self, reason: ReasonCode, message: str):
        self.reason = reason
        super().__init__(f"[{reason}] {message}")


@dataclass(frozen=True)
class CellBindings:
    """검증에 필요한 **셀 선언**. 공통 코드는 모델 이름을 모른다.

    설정이 채운다(`config/workcell/*`). 여기 없는 자원·자세는 만들지 않는다.
    """

    #: resource_id -> {"korean", "gazebo_model", "frame"}
    resources: Mapping[str, Mapping[str, str]]
    #: 물체 resource_id -> **선언된** 받침 위치 resource_id(프레임 부모 관계).
    object_support: Mapping[str, str]
    #: 위치 resource_id -> 접근 자세 이름.
    approach_pose: Mapping[str, str]
    #: 물체 resource_id -> 측정된 파지 자세 이름.
    grasp_pose: Mapping[str, str]
    #: 위치 resource_id -> place 자세 이름.
    place_pose: Mapping[str, str]
    #: 안전 home 자세 이름.
    home_pose: str
    #: 자세 이름 -> {관절명: rad}. **verified인 자세만** 담는다.
    poses: Mapping[str, Mapping[str, float]]
    #: 그리퍼 명령 관절 이름.
    gripper_joint: str = ""
    #: 열림·파지 명령값(rad). 근거가 없으면 None이고 그 단계는 검사하지 않는다.
    gripper_open_rad: float | None = None
    gripper_grasp_rad: float | None = None
    #: 그리퍼 mimic 관절 -> (multiplier, offset). URDF 선언에서 온다.
    gripper_mimic: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    #: 물체에 닿아도 되는 링크(파지 패드). **선언된 것만.**
    pad_links: tuple[str, ...] = ()
    #: 물체 resource_id -> 붙일 물체 선언({object_id, world_instance_id, link,
    #: size_m, offset_m, touch_links}).
    attached: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: 자세 이름 -> 유도·검증 근거.
    pose_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: 선언의 출처(화면·보고서에 그대로 남긴다).
    sources: Mapping[str, str] = field(default_factory=dict)

    def scene_model(self, resource_id: str) -> str | None:
        row = self.resources.get(resource_id)
        return None if row is None else row.get("gazebo_model")

    def frame(self, resource_id: str) -> str | None:
        row = self.resources.get(resource_id)
        return None if row is None else row.get("frame")

    def korean(self, resource_id: str) -> str | None:
        row = self.resources.get(resource_id)
        return None if row is None else row.get("korean")


@dataclass(frozen=True)
class PickPlaceStage:
    """단계 하나. 어떤 자세·그리퍼 명령·적재 상태인지 명시한다."""

    no: int
    stage: str
    label: str
    kind: str
    pose_name: str | None
    joint_rad: Mapping[str, float]
    gripper_joint_rad: float | None
    holds_object: str | None
    resources: tuple[str, ...]
    scene_models: tuple[str, ...]
    frames: tuple[str, ...]
    source_step: int | None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "no": self.no,
            "stage": self.stage,
            "label": self.label,
            "kind": self.kind,
            "pose_name": self.pose_name,
            "joint_rad": dict(self.joint_rad),
            "gripper_joint_rad": self.gripper_joint_rad,
            "holds_object": self.holds_object,
            "resources": list(self.resources),
            "scene_models": list(self.scene_models),
            "frames": list(self.frames),
            "source_step": self.source_step,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ResourceRow:
    """발화 ↔ 계획 ↔ Gazebo 모델 ↔ 프레임 대조 한 줄."""

    resource_id: str
    role: str
    korean: str | None
    scene_model: str | None
    frame: str | None
    in_utterance: bool
    utterance_surface: str | None
    known: bool

    @property
    def matched(self) -> bool:
        return self.known and self.in_utterance and bool(self.scene_model)

    def to_dict(self) -> dict:
        return {
            "resource_id": self.resource_id,
            "role": self.role,
            "korean": self.korean,
            "scene_model": self.scene_model,
            "frame": self.frame,
            "in_utterance": self.in_utterance,
            "utterance_surface": self.utterance_surface,
            "known": self.known,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class PlanFinding:
    """검증에서 걸린 항목. 이유 코드를 종류별로 나눈다."""

    key: str
    reason_code: ReasonCode
    detail: str
    stage: str | None = None

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "reason_code": self.reason_code.value,
            "detail": self.detail,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class StageCheck:
    """단계 하나의 사전 검증 결과."""

    stage: str
    label: str
    checked: bool
    reach: str
    joint_limits_ok: bool | None
    collision_free: bool | None
    expected_contacts: tuple[tuple[str, str], ...]
    collisions: tuple[tuple[str, str], ...]
    out_of_bounds: tuple[str, ...]
    attached: Mapping[str, Any] | None
    detail: str

    @property
    def passed(self) -> bool:
        return bool(self.checked and self.joint_limits_ok
                    and self.collision_free and self.reach == "verified")

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "label": self.label,
            "checked": self.checked,
            "reach": self.reach,
            "joint_limits_ok": self.joint_limits_ok,
            "collision_free": self.collision_free,
            "expected_contacts": [list(pair) for pair in self.expected_contacts],
            "collisions": [list(pair) for pair in self.collisions],
            "out_of_bounds": list(self.out_of_bounds),
            "attached": None if self.attached is None else dict(self.attached),
            "detail": self.detail,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class PickPlaceValidation:
    """계획 검증 전체 결과.

    **`execution_allowed`는 항상 False다.** 이 결과는 실행 허가가 아니다 —
    계획과 기하가 검사를 지났다는 사실만 말한다.
    """

    stages: tuple[PickPlaceStage, ...]
    resource_rows: tuple[ResourceRow, ...]
    checks: tuple[StageCheck, ...]
    findings: tuple[PlanFinding, ...]
    snapshot: Mapping[str, Any] | None
    snapshot_after_hash: str | None
    scene_stable: bool | None
    grasp_observation: Mapping[str, Any]
    utterance: str = ""
    limitations: tuple[str, ...] = ()

    @property
    def resources_matched(self) -> bool:
        return bool(self.resource_rows) and all(r.matched for r in self.resource_rows)

    @property
    def checked_all_stages(self) -> bool:
        return (len(self.checks) == len(self.stages) > 0
                and all(check.checked for check in self.checks))

    @property
    def plan_verified(self) -> bool:
        """계획·자원·도달·충돌 검사를 모두 지났는가. **실행 가능이 아니다.**"""
        return bool(
            not self.findings
            and self.resources_matched
            and self.checked_all_stages
            and all(check.passed for check in self.checks)
            and self.scene_stable is True
            and self.snapshot is not None
        )

    @property
    def execution_allowed(self) -> bool:
        """**항상 False.** 실행 허가는 이 모듈이 내지 않는다.

        계획 검증이 전부 통과해도 장착 근거·커플링 질량·파지 관측이 없으면
        실행하지 않는다. 그 판정은 `validation/pick_place_gate.py`가 한다.
        """
        return False

    @property
    def reason_codes(self) -> tuple[ReasonCode, ...]:
        out: list[ReasonCode] = []
        for item in self.findings:
            if item.reason_code not in out:
                out.append(item.reason_code)
        return tuple(out)

    def to_dict(self) -> dict:
        return {
            "utterance": self.utterance,
            "plan_verified": self.plan_verified,
            # 계획 검증 통과와 실행 가능을 **같은 값으로 두지 않는다.**
            "execution_allowed": self.execution_allowed,
            "execution_note": "계획 검증 통과는 실행 가능이 아니다."
                              " 실행은 pick/place 관문이 막고 있다",
            "stages": [stage.to_dict() for stage in self.stages],
            "stage_count": len(self.stages),
            "resource_rows": [row.to_dict() for row in self.resource_rows],
            "resources_matched": self.resources_matched,
            "checks": [check.to_dict() for check in self.checks],
            "checks_passed": sum(1 for c in self.checks if c.passed),
            "findings": [item.to_dict() for item in self.findings],
            "reason_codes": [code.value for code in self.reason_codes],
            "snapshot": None if self.snapshot is None else dict(self.snapshot),
            "snapshot_after_hash": self.snapshot_after_hash,
            "scene_stable": self.scene_stable,
            "grasp_observation": dict(self.grasp_observation),
            "limitations": list(self.limitations),
        }


# ── 접촉 분류 ────────────────────────────────────────────────────────────
def classify_contacts(
    contacts: Sequence[Sequence[str]], *,
    object_ids: Sequence[str] = (),
    pad_links: Sequence[str] = (),
    support_ids: Sequence[str] = (),
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """접촉 쌍을 (선언된 예외, 충돌)로 나눈다.

    허용하는 것은 **선언된 세 가지뿐**이다.

    1. 파지 패드 ↔ 대상 물체 — 파지는 닿아야 성립한다.
    2. 대상 물체 ↔ 같은 물체의 world 인스턴스 — scene이 들어올림을 모른다.
    3. (받침 단계만) 대상 물체 ↔ 선언된 받침면 — 아직 놓여 있는 상태다.

    나머지는 모두 충돌이다. 이 목록을 넓혀 검사를 통과시키지 않는다.
    """
    objects = set(object_ids)
    pads = set(pad_links)
    supports = set(support_ids)
    allowed: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    for pair in contacts:
        a, b = str(pair[0]), str(pair[1])
        members = {a, b}
        touches_object = bool(members & objects)
        if touches_object and members <= (objects | pads):
            allowed.append((a, b))
            continue
        if touches_object and supports and members <= (objects | supports):
            allowed.append((a, b))
            continue
        blocked.append((a, b))
    return tuple(allowed), tuple(blocked)


# ── 자원 대조 ────────────────────────────────────────────────────────────
def _slot_surfaces(slots) -> dict[str, str]:
    """발화에서 뽑힌 자원 → 발화에 나온 표면 문자열."""
    out: dict[str, str] = {}
    for match in getattr(slots, "matches", ()) or ():
        rid = getattr(match, "resource_id", None)
        if rid:
            out.setdefault(rid, getattr(match, "surface", "") or "")
    return out


def _steps(steps) -> list[tuple[int, str, dict[str, str]]]:
    out = []
    for index, step in enumerate(steps or (), start=1):
        out.append((index, getattr(step, "skill", ""),
                    dict(getattr(step, "args", {}) or {})))
    return out


def cross_check_resources(
    steps, *, slots, bindings: CellBindings,
) -> tuple[tuple[ResourceRow, ...], tuple[PlanFinding, ...]]:
    """발화 자원 ↔ 계획 자원 ↔ 씬 모델 ↔ 프레임을 대조한다.

    이유 코드를 **종류별로 나눈다**: 셀에 없는 자원, 발화에 없는 자원, 선언된
    받침과 다른 조합은 서로 다른 항목이다.
    """
    surfaces = _slot_surfaces(slots)
    rows: list[ResourceRow] = []
    findings: list[PlanFinding] = []
    seen: set[tuple[str, str]] = set()

    roles = {"object": "object", "from": "source", "to": "target"}
    for index, skill, args in _steps(steps):
        for arg, rid in args.items():
            role = roles.get(arg, arg)
            if (rid, role) in seen:
                continue
            seen.add((rid, role))
            known = rid in bindings.resources
            rows.append(ResourceRow(
                resource_id=rid, role=role,
                korean=bindings.korean(rid),
                scene_model=bindings.scene_model(rid),
                frame=bindings.frame(rid),
                in_utterance=rid in surfaces,
                utterance_surface=surfaces.get(rid),
                known=known,
            ))
            if not known:
                findings.append(PlanFinding(
                    key=f"unknown_resource:{rid}",
                    reason_code=ReasonCode.PLAN_UNKNOWN_RESOURCE,
                    detail=f"셀 선언에 없는 자원이다: {rid}"
                           f" (스텝{index} {skill}.{arg})",
                ))
            elif rid not in surfaces:
                findings.append(PlanFinding(
                    key=f"not_in_utterance:{rid}",
                    reason_code=ReasonCode.PLAN_RESOURCE_MISMATCH,
                    detail=f"발화에 없는 자원을 계획이 썼다: {rid}"
                           f" (스텝{index} {skill}.{arg})",
                ))

    # 자재 ↔ 팔레트 조합. **선언된 받침 관계**만 옳다.
    for index, skill, args in _steps(steps):
        if skill != "pick":
            continue
        obj = args.get("object")
        source = args.get("from")
        if not obj or not source:
            continue
        declared = bindings.object_support.get(obj)
        if declared is None:
            findings.append(PlanFinding(
                key=f"support_unknown:{obj}",
                reason_code=ReasonCode.PLAN_UNKNOWN_RESOURCE,
                detail=f"{obj}이(가) 어디에 놓여 있는지 선언이 없다",
            ))
        elif declared != source:
            findings.append(PlanFinding(
                key=f"support_mismatch:{obj}",
                reason_code=ReasonCode.PLAN_RESOURCE_MISMATCH,
                detail=f"{obj}은(는) {declared}에 놓여 있다고 선언됐는데"
                       f" 계획은 {source}에서 집으려 한다"
                       f" (스텝{index})",
            ))
    return tuple(rows), tuple(findings)


# ── 단계 펼치기 ──────────────────────────────────────────────────────────
def _pick_place_args(steps) -> tuple[dict[str, str | int | None], tuple[PlanFinding, ...]]:
    """원자 계획에서 물체·출발·도착을 뽑는다. 추측하지 않는다."""
    pick = next(((i, a) for i, s, a in _steps(steps) if s == "pick"), None)
    place = next(((i, a) for i, s, a in _steps(steps) if s == "place"), None)
    findings: list[PlanFinding] = []
    if pick is None or place is None:
        missing = ", ".join(
            name for name, value in (("pick", pick), ("place", place))
            if value is None)
        findings.append(PlanFinding(
            key="incomplete_pair",
            reason_code=ReasonCode.PLAN_SLOT_INCOMPLETE,
            detail=f"pick/place 짝이 없다(없는 스텝: {missing})"
                   " — 단계를 펼칠 수 없다",
        ))
        return {}, tuple(findings)
    pick_index, pick_args = pick
    place_index, place_args = place
    out = {
        "object": pick_args.get("object"),
        "source": pick_args.get("from"),
        "target": place_args.get("to"),
        "pick_step": pick_index,
        "place_step": place_index,
    }
    if place_args.get("object") and place_args["object"] != out["object"]:
        findings.append(PlanFinding(
            key="object_changed",
            reason_code=ReasonCode.PLAN_RESOURCE_MISMATCH,
            detail=f"pick은 {out['object']}, place는 {place_args['object']}를"
                   " 가리킨다 — 같은 물체여야 한다",
        ))
    for name in ("object", "source", "target"):
        if not out[name]:
            findings.append(PlanFinding(
                key=f"missing_{name}",
                reason_code=ReasonCode.PLAN_ARG_MISSING,
                detail=f"{name} 인자가 없다",
            ))
    return out, tuple(findings)


def build_stages(
    steps, *, bindings: CellBindings,
) -> tuple[tuple[PickPlaceStage, ...], tuple[PlanFinding, ...]]:
    """원자 계획을 12단계로 펼친다. 자세가 없으면 만들지 않는다."""
    parts, findings = _pick_place_args(steps)
    if not parts:
        return (), findings
    obj = str(parts["object"] or "")
    source = str(parts["source"] or "")
    target = str(parts["target"] or "")
    findings = list(findings)

    approach = bindings.approach_pose.get(source)
    grasp = bindings.grasp_pose.get(obj)
    place_approach = bindings.approach_pose.get(target)
    place = bindings.place_pose.get(target)
    home = bindings.home_pose

    needed = {
        STAGE_PICK_APPROACH: approach,
        STAGE_GRASP_APPROACH: grasp,
        STAGE_PLACE_APPROACH: place_approach,
        STAGE_PLACE_DESCEND: place,
        STAGE_HOME_START: home,
    }
    for stage, pose in needed.items():
        if pose is None or pose not in bindings.poses:
            findings.append(PlanFinding(
                key=f"pose_missing:{stage}",
                reason_code=ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                detail=f"{STAGE_LABELS[stage]} 단계에 쓸 **검증된 자세가 없다**"
                       f" (필요한 자세: {pose or '미지정'}) — 자세를 만들지 않는다",
                stage=stage,
            ))
    if findings:
        return (), tuple(findings)

    open_rad = bindings.gripper_open_rad
    grasp_rad = bindings.gripper_grasp_rad
    if open_rad is None or grasp_rad is None:
        findings.append(PlanFinding(
            key="gripper_command_missing",
            reason_code=ReasonCode.CONFIG_MISSING,
            detail="그리퍼 열림·파지 명령값의 근거가 없다 — 값을 만들지 않는다",
        ))
        return (), tuple(findings)

    pick_step = parts["pick_step"]
    place_step = parts["place_step"]
    # pre-grasp과 lift는 **검증된 접근 자세를 그대로 쓴다.** 새 높이를 만들지
    # 않는다 — 접근 자세는 이미 자재 바로 위 수직 위치이고 간극이 측정돼 있다.
    layout: tuple[tuple[str, str, str | None, float, str | None, int | None], ...] = (
        (STAGE_HOME_START, "arm_motion", home, open_rad, None, None),
        (STAGE_PICK_APPROACH, "arm_motion", approach, open_rad, None, pick_step),
        (STAGE_PRE_GRASP, "arm_motion", approach, open_rad, None, pick_step),
        (STAGE_GRIPPER_OPEN_BEFORE, "gripper", approach, open_rad, None, pick_step),
        (STAGE_GRASP_APPROACH, "arm_motion", grasp, open_rad, None, pick_step),
        (STAGE_GRIPPER_CLOSE, "gripper", grasp, grasp_rad, obj, pick_step),
        (STAGE_LIFT, "arm_motion", approach, grasp_rad, obj, pick_step),
        (STAGE_PLACE_APPROACH, "arm_motion", place_approach, grasp_rad, obj, place_step),
        (STAGE_PLACE_DESCEND, "arm_motion", place, grasp_rad, obj, place_step),
        (STAGE_GRIPPER_OPEN_RELEASE, "gripper", place, open_rad, None, place_step),
        (STAGE_RETREAT, "arm_motion", place_approach, open_rad, None, place_step),
        (STAGE_HOME_END, "arm_motion", home, open_rad, None, None),
    )
    assert tuple(row[0] for row in layout) == STAGE_SEQUENCE

    stages: list[PickPlaceStage] = []
    for no, (stage, kind, pose_name, gripper, holds, step_no) in enumerate(
            layout, start=1):
        if stage in (STAGE_HOME_START, STAGE_HOME_END):
            resources: tuple[str, ...] = ()
        elif stage in (STAGE_PICK_APPROACH, STAGE_PRE_GRASP,
                       STAGE_GRIPPER_OPEN_BEFORE):
            resources = (source,)
        elif stage in (STAGE_GRASP_APPROACH, STAGE_GRIPPER_CLOSE, STAGE_LIFT):
            resources = (obj, source)
        elif holds:
            resources = (obj, target)
        else:
            resources = (target,)
        models = tuple(m for m in (bindings.scene_model(r) for r in resources) if m)
        frames = tuple(f for f in (bindings.frame(r) for r in resources) if f)
        stages.append(PickPlaceStage(
            no=no, stage=stage, label=STAGE_LABELS[stage], kind=kind,
            pose_name=pose_name,
            joint_rad=dict(bindings.poses[pose_name]),
            gripper_joint_rad=gripper,
            holds_object=holds,
            resources=resources, scene_models=models, frames=frames,
            source_step=step_no,
            detail=("팔은 그 자리에 있고 그리퍼 명령만 바뀐다"
                    if kind == "gripper" else ""),
        ))
    return tuple(stages), ()


# ── 단계 검사 ────────────────────────────────────────────────────────────
def _joint_state(stage: PickPlaceStage, bindings: CellBindings) -> dict[str, float]:
    out = dict(stage.joint_rad)
    if bindings.gripper_joint and stage.gripper_joint_rad is not None:
        value = float(stage.gripper_joint_rad)
        out[bindings.gripper_joint] = value
        for name, (multiplier, offset) in bindings.gripper_mimic.items():
            out[name] = value * multiplier + offset
    return out


def _attached_for(stage: PickPlaceStage, bindings: CellBindings):
    """적재 단계에 붙일 물체. 선언이 없으면 붙이지 않는다."""
    if stage.stage not in HOLDING_STAGES or not stage.holds_object:
        return None, None
    spec = bindings.attached.get(stage.holds_object)
    if not spec:
        return None, None
    item = AttachedObject(
        object_id=str(spec["object_id"]),
        link=str(spec["link"]),
        size_m=tuple(float(v) for v in spec["size_m"]),
        offset_m=tuple(float(v) for v in spec.get("offset_m", (0.0, 0.0, 0.0))),
        touch_links=tuple(spec.get("touch_links") or bindings.pad_links),
        source=str(spec.get("source", "")),
    )
    return item, spec


def stage_joint_state(
    stage: PickPlaceStage, bindings: CellBindings,
) -> dict[str, float]:
    """단계의 관절 상태(팔 + 그리퍼 명령 + mimic). 실행기도 이것을 쓴다."""
    return _joint_state(stage, bindings)


def stage_attachment(stage: PickPlaceStage, bindings: CellBindings):
    """적재 단계에 붙일 물체와 그 선언. 아니면 (None, None)."""
    return _attached_for(stage, bindings)


def check_stages(
    stages: Sequence[PickPlaceStage], *, bindings: CellBindings, client,
) -> tuple[tuple[StageCheck, ...], tuple[PlanFinding, ...]]:
    """단계별로 관절 제한·충돌을 planning scene에 묻는다."""
    checks: list[StageCheck] = []
    findings: list[PlanFinding] = []
    for stage in stages:
        joints = _joint_state(stage, bindings)
        attached, spec = _attached_for(stage, bindings)
        if stage.stage in HOLDING_STAGES and stage.holds_object and attached is None:
            checks.append(StageCheck(
                stage=stage.stage, label=stage.label, checked=False,
                reach="verified", joint_limits_ok=None, collision_free=None,
                expected_contacts=(), collisions=(), out_of_bounds=(),
                attached=None,
                detail="적재 상태로 붙일 물체 선언이 없다 — 검사하지 않았다",
            ))
            findings.append(PlanFinding(
                key=f"attach_missing:{stage.stage}",
                reason_code=ReasonCode.GEOMETRY_ENVIRONMENT_UNAVAILABLE,
                detail=f"{stage.label} 단계를 적재 상태로 검사할 수 없다"
                       " — 붙일 물체 선언이 없다",
                stage=stage.stage,
            ))
            continue
        try:
            validity = (client.check_state(joints, attached=[attached])
                        if attached is not None else client.check_state(joints))
        except Exception as exc:  # noqa: BLE001 — 검사 실패를 통과로 쓰지 않는다
            checks.append(StageCheck(
                stage=stage.stage, label=stage.label, checked=False,
                reach="verified", joint_limits_ok=None, collision_free=None,
                expected_contacts=(), collisions=(), out_of_bounds=(),
                attached=None if spec is None else dict(spec),
                detail=f"검사 호출이 실패했다: {type(exc).__name__}: {exc}"[:200],
            ))
            findings.append(PlanFinding(
                key=f"check_error:{stage.stage}",
                reason_code=ReasonCode.GEOMETRY_VALIDATOR_ERROR,
                detail=f"{stage.label} 단계 검사가 오류를 냈다",
                stage=stage.stage,
            ))
            continue

        object_ids: list[str] = []
        support_ids: list[str] = []
        if stage.holds_object and spec is not None:
            object_ids.append(str(spec["object_id"]))
            world_instance = spec.get("world_instance_id")
            if world_instance:
                object_ids.append(str(world_instance))
        elif stage.holds_object:
            model = bindings.scene_model(stage.holds_object)
            if model:
                object_ids.append(model)
        if stage.stage in SUPPORTED_STAGES:
            target_object = stage.holds_object or (
                stage.resources[0] if stage.resources else None)
            if target_object:
                model = bindings.scene_model(target_object)
                if model and model not in object_ids:
                    object_ids.append(model)
                support = bindings.object_support.get(target_object)
                support_model = bindings.scene_model(support) if support else None
                if support_model:
                    # 받침면은 **모델 이름으로 시작하는 씬 물체 전부**다
                    # (팔레트는 tray 같은 부품으로 등록된다).
                    support_ids.append(support_model)

        contacts = [list(pair) for pair in validity.contacts]
        expected, collisions = classify_contacts(
            contacts, object_ids=object_ids, pad_links=bindings.pad_links,
            support_ids=_support_scene_ids(support_ids, contacts),
        )
        out_of_bounds = tuple(validity.out_of_bounds)
        checks.append(StageCheck(
            stage=stage.stage, label=stage.label, checked=True,
            reach="verified",
            joint_limits_ok=not out_of_bounds,
            collision_free=not collisions,
            expected_contacts=expected, collisions=collisions,
            out_of_bounds=out_of_bounds,
            attached=None if spec is None else dict(spec),
            detail="" if not collisions else
                   f"충돌 {len(collisions)}쌍: "
                   + ", ".join(f"{a}↔{b}" for a, b in collisions[:4]),
        ))
        if out_of_bounds:
            findings.append(PlanFinding(
                key=f"joint_limit:{stage.stage}",
                reason_code=ReasonCode.GEOMETRY_WORKSPACE_VIOLATION,
                detail=f"{stage.label} 단계가 관절 제한을 벗어났다:"
                       f" {', '.join(out_of_bounds)}",
                stage=stage.stage,
            ))
        if collisions:
            findings.append(PlanFinding(
                key=f"collision:{stage.stage}",
                reason_code=ReasonCode.GEOMETRY_COLLISION,
                detail=f"{stage.label} 단계에서 선언되지 않은 접촉:"
                       + ", ".join(f"{a}↔{b}" for a, b in collisions[:4]),
                stage=stage.stage,
            ))
    return tuple(checks), tuple(findings)


def _support_scene_ids(
    support_models: Sequence[str], contacts: Sequence[Sequence[str]],
) -> tuple[str, ...]:
    """받침 모델에 속한 씬 물체 id를 접촉 목록에서 찾는다.

    씬은 팔레트를 부품 단위(`<모델>__<부품>`)로 담는다. 모델 이름을 접두사로
    **같은 모델에 속한 것만** 받침으로 인정한다 — 다른 팔레트는 받침이 아니다.
    """
    if not support_models:
        return ()
    names = {str(name) for pair in contacts for name in pair}
    out: list[str] = []
    for model in support_models:
        for name in sorted(names):
            if name == model or name.startswith(f"{model}__"):
                out.append(name)
    return tuple(out)


# ── 전체 검증 ────────────────────────────────────────────────────────────
def validate(
    steps, *, slots, bindings: CellBindings, client,
    grasp_observation: Mapping[str, Any] | None = None,
    utterance: str = "",
    limitations: Sequence[str] = (),
) -> PickPlaceValidation:
    """자원 대조 → 단계 펼치기 → 단계 검사를 한 번에 돌린다.

    검사 전후 scene hash를 비교한다. 다르면 `plan_verified`가 False다 —
    **검사한 환경과 판정이 붙어 있어야 한다.**
    """
    rows, resource_findings = cross_check_resources(
        steps, slots=slots, bindings=bindings)
    stages, stage_findings = build_stages(steps, bindings=bindings)
    findings = list(resource_findings) + list(stage_findings)

    snapshot_payload: dict[str, Any] | None = None
    after_hash: str | None = None
    scene_stable: bool | None = None
    checks: tuple[StageCheck, ...] = ()

    if client is None:
        findings.append(PlanFinding(
            key="scene_unavailable",
            reason_code=ReasonCode.GEOMETRY_VALIDATOR_UNAVAILABLE,
            detail="planning scene 클라이언트가 없다 —"
                   " 검사하지 않은 상태를 통과로 쓰지 않는다",
        ))
    elif stages:
        before = client.snapshot()
        snapshot_payload = {
            "snapshot_id": before.snapshot_id,
            "snapshot_version": before.snapshot_version,
            "content_hash": before.content_hash,
            "frame_id": before.frame_id,
            "captured_at": before.captured_at,
            "world_object_count": before.summary.get("world_object_count"),
        }
        checks, check_findings = check_stages(
            stages, bindings=bindings, client=client)
        findings.extend(check_findings)
        after = client.snapshot()
        after_hash = after.content_hash
        scene_stable = after.content_hash == before.content_hash
        if not scene_stable:
            findings.append(PlanFinding(
                key="scene_changed",
                reason_code=ReasonCode.GEOMETRY_SNAPSHOT_EXPIRED,
                detail="검사 중 planning scene이 바뀌었다"
                       f" ({before.content_hash[:12]} →"
                       f" {after.content_hash[:12]})"
                       " — 판정을 환경에 붙일 수 없다",
            ))

    observation = dict(grasp_observation or {})
    return PickPlaceValidation(
        stages=stages, resource_rows=rows, checks=checks,
        findings=tuple(findings), snapshot=snapshot_payload,
        snapshot_after_hash=after_hash, scene_stable=scene_stable,
        grasp_observation=observation, utterance=utterance,
        limitations=tuple(limitations),
    )
