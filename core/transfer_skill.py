"""공통 스킬 계약 `transfer(material_id, source_resource, destination_resource)`.

로봇 종류와 무관한 **계약**만 둔다. 어느 자세로 어떻게 움직이는지는 로봇별
Capability(`TransferCapability`)가 채우고, 실행은 로봇별 실행기가 한다. 구현체는
작업 셀 매니페스트(`config/workcell/active.json`의 `adapter_module`)가 가리키는
어댑터 패키지의 `build_transfer_capability`다 — 공통 코드는 로봇 이름을 모른다.

공통 실행 단계:

    approach → grasp → lift → move → place → retreat → observe

계약이 지키는 것(로봇과 무관):

1. 자재·출발지·도착지는 셀이 선언한 것만. 출발지 ≠ 도착지.
2. 사용자가 말한 출발지는 **관측된 현재 위치**와 같아야 한다. 다르면 ASK.
3. 도착지가 다른 자재로 점유됐거나 사용 금지면 BLOCK(선행 작업은 계획기가 만든다).
4. 자세는 Capability가 **검증된 것만** 돌려준다. 역할이 정해져 있다:
   - 파지 자세는 역할 `grasp`, 위치 = 출발지
   - 놓기 자세는 역할 `place`, 위치 = 도착지
   - 놓기 자세가 없으면 다른 자세로 대신하지 않고 BLOCK
   - 놓기 자세가 출발지 파지 자세(이름·관절값)와 같으면 BLOCK — 출발지에서
     집던 자세로 "놓으러" 가는 재사용을 구조로 막는다
5. LLM은 이 계약의 어떤 값도 만들지 않는다 — 입력은 선언된 id뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

SKILL_TRANSFER = "transfer"
#: 든 물체와 무관하게 실행 직전 든 물체 검사로 확인하는 놓기 자세 출처.
RUNTIME_CHECKED_PLACE_SOURCES = frozenset({"conveyor_slots.place_held"})
#: 로봇과 무관한 공통 단계. 로봇별 실행기는 이 순서를 지킨다.
PHASES: tuple[str, ...] = ("approach", "grasp", "lift", "move", "place", "retreat",
                           "observe")


class LocationKind(str, Enum):
    PALLET = "pallet"
    CONVEYOR_SLOT = "conveyor_slot"

    def __str__(self) -> str:
        return self.value


class PoseRole(str, Enum):
    APPROACH = "approach"
    GRASP = "grasp"
    PLACE = "place"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Location:
    """셀이 선언한 위치. `id`는 팔레트 자원 id(`loc_pallet_1`) 또는 칸(`slot_2`)."""

    id: str
    kind: LocationKind
    label: str = ""


@dataclass(frozen=True)
class TransferRequest:
    robot_id: str
    material_id: str
    source: str
    destination: str

    def to_dict(self) -> dict:
        return {"skill": SKILL_TRANSFER, "robot_id": self.robot_id,
                "material_id": self.material_id, "source_resource": self.source,
                "destination_resource": self.destination}


@dataclass(frozen=True)
class PoseRef:
    """검증된 자세 하나. 값은 로봇 설정에서만 온다."""

    name: str
    role: PoseRole
    location: str
    joint_rad: Mapping[str, float]
    #: 파지 자세: 이 파지로 든 물체 선언의 출처 키. 놓기 자세: 검증할 때 든 물체의 출처 키.
    held_object_source: str = ""
    verified: bool = True
    #: 파지 자세가 붙이는 물체 선언(든 물체 포함 검사에 쓴다).
    held_object: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class RoutePoses:
    source_approach: PoseRef
    grasp: PoseRef
    destination_approach: PoseRef
    place: PoseRef


@dataclass(frozen=True)
class Finding:
    decision: str            # "ASK" | "BLOCK"
    code: str
    detail: str

    def to_dict(self) -> dict:
        return {"decision": self.decision, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class TransferPlan:
    request: TransferRequest
    route: str                 # 예: "pallet->conveyor_slot"
    poses: RoutePoses
    phases: tuple[str, ...] = PHASES
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.request.to_dict(), "route": self.route,
                "phases": list(self.phases),
                "poses": {k: {"name": p.name, "role": str(p.role), "location": p.location}
                          for k, p in (("source_approach", self.poses.source_approach),
                                       ("grasp", self.poses.grasp),
                                       ("destination_approach",
                                        self.poses.destination_approach),
                                       ("place", self.poses.place))},
                "evidence": dict(self.evidence)}


class TransferCapability(Protocol):
    """로봇별 구현이 채우는 경계. 휴머노이드도 같은 계약을 구현하면 된다."""

    robot_id: str

    def locations(self) -> Mapping[str, Location]: ...

    def materials(self) -> Sequence[str]: ...

    def routes(self) -> frozenset[tuple[LocationKind, LocationKind]]: ...

    def resolve(self, material_id: str, source: Location,
                destination: Location) -> RoutePoses | Finding: ...


@dataclass(frozen=True)
class WorldView:
    """계약 판정에 쓰는 **관측 기반** 현재 상태."""

    #: 자재 → 현재 위치 id(관측·확정 기록). 모르면 없다.
    location_of: Mapping[str, str]
    #: 사용 금지 위치 id
    blocked: frozenset[str] = frozenset()
    #: 쓰지 않는 자재
    unavailable: frozenset[str] = frozenset()


def _same_joints(a: Mapping[str, float], b: Mapping[str, float], tol: float = 1e-6) -> bool:
    return bool(a) and set(a) == set(b) and all(abs(float(a[k]) - float(b[k])) <= tol
                                                for k in a)


def check_pose_contract(poses: RoutePoses, source: Location,
                        destination: Location) -> list[Finding]:
    """자세 역할·위치 계약. 로봇과 무관하게 같은 규칙이다."""
    bad: list[Finding] = []
    expected = ((poses.source_approach, PoseRole.APPROACH, source.id),
                (poses.grasp, PoseRole.GRASP, source.id),
                (poses.destination_approach, PoseRole.APPROACH, destination.id),
                (poses.place, PoseRole.PLACE, destination.id))
    for pose, role, where in expected:
        if pose.role is not role or pose.location != where:
            bad.append(Finding("BLOCK", "geometry.pose_role_mismatch",
                               f"{pose.name}은 {pose.role}@{pose.location}인데"
                               f" {role}@{where} 자리에 쓰였다"))
        if not pose.verified or not pose.joint_rad:
            bad.append(Finding("BLOCK", "geometry.pose_unverified",
                               f"{pose.name}은 검증된 자세가 아니다"))
    # 놓기 자세는 **그 파지로 든 물체**로 검증된 것이어야 한다. 칸 놓기 자세는 이상적
    # 물체로 측정되어 실행 직전 든 물체 검사로 확인하므로 예외다.
    place_source = poses.place.held_object_source
    if (place_source and place_source not in RUNTIME_CHECKED_PLACE_SOURCES
            and place_source != poses.grasp.held_object_source):
        bad.append(Finding("BLOCK", "geometry.held_source_mismatch",
                           f"놓기 자세 {poses.place.name}는 {place_source}로 든 물체로 검증됐다 —"
                           f" 지금 파지({poses.grasp.held_object_source})에는 쓰지 않는다"))
    if poses.place.name == poses.grasp.name or _same_joints(
            poses.place.joint_rad, poses.grasp.joint_rad):
        bad.append(Finding("BLOCK", "geometry.grasp_reused_as_place",
                           f"놓기 자세 {poses.place.name}가 출발지 파지 자세"
                           f" {poses.grasp.name}와 같다 — 출발지에서 집던 자세로"
                           " 놓으러 가지 않는다"))
    return bad


def validate_transfer(request: TransferRequest, capability: TransferCapability,
                      world: WorldView) -> tuple[TransferPlan | None, list[Finding]]:
    """계약 검사. 통과하면 (계획, []), 아니면 (None, 이유들)."""
    findings: list[Finding] = []
    if request.robot_id != capability.robot_id:
        return None, [Finding("BLOCK", "robot.unknown",
                              f"{request.robot_id}는 이 셀의 로봇이 아니다")]
    locations = capability.locations()
    if request.material_id not in capability.materials():
        return None, [Finding("BLOCK", "plan.unknown_resource",
                              f"셀에 없는 자재다: {request.material_id}")]
    source = locations.get(request.source)
    destination = locations.get(request.destination)
    for name, loc in (("출발지", source), ("도착지", destination)):
        if loc is None:
            findings.append(Finding("BLOCK", "plan.unknown_resource",
                                    f"셀에 없는 {name}다: "
                                    f"{request.source if name == '출발지' else request.destination}"))
    if findings:
        return None, findings
    if source.id == destination.id:
        return None, [Finding("BLOCK", "plan.same_location",
                              "출발지와 도착지가 같다")]
    if request.material_id in world.unavailable:
        return None, [Finding("BLOCK", "plan.material_unavailable",
                              f"{request.material_id}는 쓰지 않는 자재다")]
    seen = world.location_of.get(request.material_id)
    if seen is None:
        return None, [Finding("ASK", "state.unobserved",
                              f"{request.material_id}의 현재 위치를 확인할 수 없다")]
    if seen != source.id:
        return None, [Finding("ASK", "state.source_mismatch",
                              f"{request.material_id}는 {source.id}가 아니라 {seen}에"
                              " 있다(관측) — 출발지를 확인해 주세요")]
    if destination.id in world.blocked:
        return None, [Finding("BLOCK", "plan.destination_blocked",
                              f"{destination.id}는 사용 금지 위치다")]
    holder = next((m for m, where in world.location_of.items()
                   if where == destination.id and m != request.material_id), None)
    if holder is not None:
        return None, [Finding("BLOCK", "plan.destination_occupied",
                              f"{destination.id}에 {holder}가 있다 — 먼저 비워야 한다")]
    if (source.kind, destination.kind) not in capability.routes():
        return None, [Finding("BLOCK", "capability.route_unsupported",
                              f"{source.kind}→{destination.kind} 이송은 이 로봇이"
                              " 지원하지 않는다")]
    resolved = capability.resolve(request.material_id, source, destination)
    if isinstance(resolved, Finding):
        return None, [resolved]
    findings = check_pose_contract(resolved, source, destination)
    if findings:
        return None, findings
    # 측정된 경로가 지나는 자리에 다른 자재가 있으면 막는다(계획 단계의 판단 —
    # 실행 직전 scene 동기화 + 경로 검사가 다시 본다).
    blockers = path_obstructions(capability, request.material_id, source.id, destination.id)
    if blockers:
        if ALWAYS_BLOCKED in blockers:
            return None, [Finding("BLOCK", "geometry.path_obstructed",
                                  f"{source.id}→{destination.id} 경로가 빈 셀에서도 충돌한다(측정)")]
        hits = sorted((where, m) for m, where in world.location_of.items()
                      if m != request.material_id and where in blockers)
        if hits:
            return None, [Finding(
                "BLOCK", "geometry.path_obstructed",
                f"{source.id}→{destination.id} 경로가 "
                + ", ".join(f"{where}의 {m}" for where, m in hits)
                + "와 겹친다(측정한 경로 표본) — 그 자재가 비킨 뒤 옮기거나 다른 경로가 필요하다")]
    return TransferPlan(request=request, route=f"{source.kind}->{destination.kind}",
                        poses=resolved), []


#: 경로가 빈 셀에서도 충돌한다는 표시(측정 결과). 어떤 배치에서도 막는다.
ALWAYS_BLOCKED = "*"


def path_obstructions(capability: TransferCapability, material_id: str, source: str,
                      destination: str) -> frozenset[str] | None:
    """측정된 경로가 지나며 스치는 자리 — 그 자리에 자재가 있으면 충돌한다.

    Capability가 측정값을 주지 않으면 None(모름)이다. 모름은 통과가 아니라 **실행
    직전 경로 검사에 맡긴다**는 뜻이다.
    """
    fn = getattr(capability, "path_obstructions", None)
    return None if fn is None else fn(material_id, source, destination)


def route_supported(capability: TransferCapability, material_id: str, source: str,
                    destination: str) -> tuple[bool, str]:
    """점유·순서와 무관하게 **경로와 자세**만 본다(계획기가 직접/우회를 고를 때)."""
    locations = capability.locations()
    src, dst = locations.get(source), locations.get(destination)
    if src is None or dst is None or src.id == dst.id:
        return False, "선언된 서로 다른 위치가 아니다"
    if (src.kind, dst.kind) not in capability.routes():
        return False, f"{src.kind}→{dst.kind} 경로를 지원하지 않는다"
    poses = capability.resolve(material_id, src, dst)
    if isinstance(poses, Finding):
        return False, poses.detail
    bad = check_pose_contract(poses, src, dst)
    if bad:
        return False, bad[0].detail
    return True, "검증된 자세로 갈 수 있다"
