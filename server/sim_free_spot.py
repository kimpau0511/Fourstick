"""표면 **빈 위치 놓기** — 발화(자재·표면) → 후보 계산·검증 → 확인 카드 → 실행 직전 재검사.

"A자재를 작업대의 빈 곳에 놔"처럼 표면만 말한 놓기를 다룬다. 규칙이나 분류기(LLM)가 정하는 것은
**자재와 표면 식별자**뿐이다. 좌표·관절값은 서버가 만든다:

1. 출발지: 기록 위치 + Gazebo 관측(기록 자리 중심과 `ORIGIN_TOLERANCE_M` 안). 발화가 말한
   출발지와 다르면 되묻는다. 관측이 없거나 낡았으면 막는다.
2. 후보: `validation/free_spot.py`(표면 형상·고정 장애물·관측 점유·지지 여유).
3. 검증: 로봇별 계산기(작업 셀 매니페스트의 어댑터 패키지 `build_free_spot_planner`) —
   IK, 실행기와 같은 12단계 검사(든 물체·손가락), 단계 사이 경로 표본. planning scene이 아직
   반영하지 못한 다른 자재는 탐침으로 넣는다.
4. 확인 카드: 표면·선택 위치·근거(후보 수·탈락 사유·선택 규칙·검증 결과). 작업을 만들지 않는다.
5. 확인을 누르면 다시 본다(출발지·자리 빈 상태·MoveIt 검사). 하나라도 어긋나면 실행하지 않는다.
   실행기는 FK·장면 동기화·사전 검사·경로 검사를 한 번 더 한다.

공통 코드라 로봇·모델 이름을 모른다. 표면 선언은 매니페스트의 `surfaces` 파일에서 온다.
"""

from __future__ import annotations

import importlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

#: 이 기능의 시연 작업 동작 이름(`server/sim_demo_jobs.ACTIONS`).
SPOT_ACTION = "spot"
#: "정확한 위치 없이 빈 곳" 표현. 표면 이름과 함께 있어야 이 기능이 받는다.
EMPTY_WORDS = ("빈곳", "빈자리", "빈공간", "빈데", "비어있는", "남는곳", "남는자리", "남는데",
               "아무데", "아무곳", "적당한곳", "적당한데", "적당히")
#: 놓기 동사. 정규화된(공백 없는) 문장에서 찾는다.
PLACE_VERBS = ("놔", "놓", "올려", "옮겨", "옮기", "둬", "두어", "두세요", "갖다", "가져다")
_STOP_WORDS = ("멈춰", "멈춤", "정지", "스톱", "stop")


def surfaces_of(jobs) -> Mapping[str, Any]:
    return dict(getattr(jobs, "surfaces", None) or {})


def surface_aliases(surfaces: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    out = {}
    for sid, spec in (surfaces.get("surfaces") or {}).items():
        if spec.get("placement") != "free_spot":
            continue
        names = {str(spec.get("korean") or "")} | {str(a) for a in spec.get("aliases") or ()}
        out[sid] = tuple(sorted({re.sub(r"\s+", "", n) for n in names if n},
                                key=len, reverse=True))
    return out


def detect(text: str, workcell: Mapping[str, Any], surfaces: Mapping[str, Any]) -> dict | None:
    """발화가 '표면 빈 위치 놓기'인가. 아니면 None(기존 경로가 받는다).

    돌려주는 dict: surface_id, material(모델 또는 None), materials(언급 전부), stated_source.
    """
    from server.sim_demo_commands import material_aliases, named_pallets, pallet_resources

    compact = re.sub(r"\s+", "", text.lower())
    if any(w in compact for w in _STOP_WORDS):
        return None
    mentioned = [sid for sid, names in surface_aliases(surfaces).items()
                 if any(name.lower() in compact for name in names)]
    if not mentioned or not any(v in compact for v in PLACE_VERBS):
        return None
    materials = sorted(m for m, names in material_aliases(workcell).items()
                       if any(name in compact for name in names))
    stated = None
    # 말한 팔레트 번호 → 선언된 자원 id(셀 설정에서만). 모르는 번호는 넣지 않는다.
    resources = pallet_resources(workcell)
    pallets = {resources[n] for n in named_pallets(text, workcell) if n in resources}
    if pallets:
        stated = next(iter(pallets)) if len(pallets) == 1 else "ambiguous"
    elif re.search(r"컨베이어(?:의)?(?:\d+번(?:자리|위치|칸))?(?:에서|위에서)", compact):
        slot = re.search(r"컨베이어(?:의)?(\d+)번", compact)
        stated = f"slot_{slot.group(1)}" if slot else "loc_conveyor"
    return {"surface_id": mentioned[0] if len(mentioned) == 1 else None,
            "surfaces": mentioned, "material": materials[0] if len(materials) == 1 else None,
            "materials": materials, "stated_source": stated,
            "empty_words": [w for w in EMPTY_WORDS if w in compact]}


class FreeSpotService:
    """런타임에 붙는 빈 위치 계산기. 로봇별 계산기는 매니페스트가 가리키는 패키지에서 찾는다."""

    def __init__(self, runtime):
        self.runtime = runtime
        self._planner = None

    @property
    def jobs(self):
        return self.runtime.sim_demo_jobs

    def planner(self):
        if self._planner is None:
            manifest = json.loads((Path(__file__).resolve().parents[1]
                                   / "config/workcell/active.json").read_text(encoding="utf-8"))
            module = importlib.import_module(str(manifest["adapter_module"]))
            build = getattr(module, getattr(module, "FREE_SPOT_ENTRY_POINT"))
            self._planner = build(capability=self.jobs._capability())
        return self._planner

    # ── 관측 ──────────────────────────────────────────────────────────
    def observe(self) -> tuple[dict | None, str]:
        view = getattr(self.runtime, "sim_view", None)
        if view is None:
            return None, "Gazebo 관측 구독이 없다"
        sample = view.state.sample() if hasattr(view, "state") else view.sample()
        if sample.get("stale"):
            return None, "Gazebo 관측이 낡았다(시뮬레이터 응답 없음)"
        return sample, ""

    def _where(self, material: str):
        from server.sim_pick_place import location_center

        world = self.jobs.transfer_world()
        if world is None:
            return None, None, "확정되지 않은 자재 기록이 있어 출발지를 정할 수 없다"
        location = world.location_of.get(material)
        if location is None:
            return None, None, f"{material}의 위치 기록이 없다"
        if str(location).startswith("spot:"):
            return location, None, ("이미 표면 빈 위치에 놓인 자재다 — 거기서 다시 집는 동작은 아직"
                                    " 지원하지 않는다(복구로 원래 자리에 되돌린 뒤 다시 말해 주세요)")
        return location, location_center(self.jobs, location), ""

    # ── 계획 ──────────────────────────────────────────────────────────
    def plan(self, *, material: str, surface_id: str, stated_source: str | None) -> dict:
        """후보 계산·검증. dict(decision, reason, spot, selection, source, ...)."""
        import math

        from server.sim_demo_confirm import DEFAULT_TTL_SEC
        from validation.free_spot import candidates, surface_from_config
        from validation.simulation_demo_state import ORIGIN_TOLERANCE_M

        jobs = self.jobs
        out: dict = {"material": material, "surface_id": surface_id, "stated_source": stated_source}
        location, center, problem = self._where(material)
        if problem:
            return {**out, "decision": "BLOCK", "reason": problem}
        if stated_source == "ambiguous":
            return {**out, "decision": "ASK", "reason": "출발지를 하나로 말해 주세요"}
        if stated_source and not (stated_source == location or (
                stated_source == "loc_conveyor" and str(location).startswith("slot_"))):
            return {**out, "decision": "ASK", "source": location,
                    "reason": f"{material}는 {stated_source}가 아니라 {location}에 있습니다(기록·관측)"
                              " — 출발지를 확인해 주세요"}
        sample, why = self.observe()
        if sample is None:
            return {**out, "decision": "BLOCK", "reason": why}
        poses = sample.get("materials") or {}
        observed = poses.get(material)
        if observed is None or center is None:
            return {**out, "decision": "BLOCK", "reason": f"{material}의 위치를 관측하지 못했다"}
        gap = math.dist(observed[:3], center)
        if gap > ORIGIN_TOLERANCE_M:
            return {**out, "decision": "ASK", "source": location,
                    "reason": f"{material}가 기록 위치({location})에서 {gap:.3f} m 떨어져 관측됩니다"
                              " — 실제 위치를 확인해 주세요"}
        try:
            surface = surface_from_config(jobs.workcell, surfaces_of(jobs), surface_id)
            planner = self.planner()
        except Exception as exc:  # noqa: BLE001 — 표면·계산기를 못 만들면 막는다
            return {**out, "decision": "BLOCK", "reason": f"빈 위치를 계산할 수 없다: {exc}"[:300]}
        sizes = {m: jobs.workcell["models"][m]["size_m"] for m in jobs.materials}
        others = {m: poses.get(m) for m in jobs.materials if m != material}
        found = candidates(surface, material_size_m=sizes[material], others=others,
                           other_sizes=sizes,
                           sufficient_gap_m=planner.sufficient_gap_m(material),
                           robot_base_xy=planner.robot_base_xy())
        selection = found.to_dict()
        out.update(source=location, selection=selection,
                   observed_source_gap_m=round(gap, 4))
        if not found.candidates:
            return {**out, "decision": "BLOCK",
                    "reason": f"{surface.korean}에 놓을 빈 위치가 없습니다: {found.problem}"}
        client = self.runtime.planning_scene_client()
        bindings = self.runtime.cell_bindings()
        if client is None or bindings is None:
            return {**out, "decision": "BLOCK",
                    "reason": "planning scene·셀 선언을 쓸 수 없어 놓기 자리를 검증하지 못했다"}
        try:
            scene = {k: v[1] for k, v in client.world_object_poses().items()}
        except Exception as exc:  # noqa: BLE001
            return {**out, "decision": "BLOCK", "reason": f"planning scene을 읽지 못했다: {exc}"[:200]}
        probes = planner.probes(others={m: p for m, p in others.items() if p is not None},
                                scene_poses=scene, tolerance_m=ORIGIN_TOLERANCE_M)
        start = {k: v for k, v in (sample.get("joints") or {}).items() if k.startswith("j")}
        # 확인 카드가 만료(기본 60 s) 안에 오도록 검증 시간을 그 절반으로 묶는다. 다 못 보면
        # 본 데까지의 결과만 쓴다 — 통과한 자리가 없으면 실행하지 않는다.
        budget = DEFAULT_TTL_SEC / 2
        started = time.monotonic()
        attempts = []
        chosen = None
        for candidate in found.candidates:
            if time.monotonic() - started > budget:
                attempts.append({"center_xy_m": None, "result": "budget",
                                 "detail": f"검증 시간 {budget:.0f} s를 넘겨 멈췄다"})
                break
            row = candidate.to_dict()
            plan = planner.plan(material=material, source=location, surface_id=surface_id,
                                surface_top_z=surface.top_z,
                                center_xy=(candidate.x, candidate.y))
            if not hasattr(plan, "route"):
                attempts.append({**row, "result": "ik", "detail": plan.detail})
                continue
            ok, evidence = planner.verify(plan, material=material, source=location,
                                          bindings=bindings, client=client, obstacles=probes,
                                          start_joints=start or None)
            attempts.append({**row, "result": "pass" if ok else "moveit", **evidence})
            if ok:
                chosen = (candidate, plan, evidence)
                break
        selection["attempts"] = attempts
        selection["verify_seconds"] = round(time.monotonic() - started, 2)
        selection["probes"] = [p.object_id for p in probes]
        if chosen is None:
            return {**out, "decision": "BLOCK",
                    "reason": f"{surface.korean}의 후보 {len(found.candidates)}곳 중 도달·충돌·경로"
                              f" 검증을 통과한 자리를 찾지 못했습니다(검사 {len(attempts)}곳)"}
        candidate, plan, evidence = chosen
        spot = {**plan.to_dict(), "source": location, "material": material}
        return {**out, "decision": "CONFIRM", "spot": spot,
                "reason": None, "summary": self.summary(material, surface, location, spot),
                "why": self.why(found, attempts, candidate)}

    def summary(self, material: str, surface, location: str, spot: Mapping[str, Any]) -> str:
        from server.sim_demo_places import place_label

        korean = (self.jobs.materials.get(material) or {}).get("korean") or material
        where = place_label(getattr(self.jobs, "places", ()) or (), location)
        x, y = spot["center_m"][0], spot["center_m"][1]
        return (f"{korean}를 {where}에서 집어 {surface.korean} 빈 위치"
                f" (x {x:.3f}, y {y:.3f} m)에 놓겠습니다.")

    @staticmethod
    def why(found, attempts, candidate) -> list[str]:
        rejected = ", ".join(f"{k} {v}" for k, v in found.rejected.items() if v) or "없음"
        failed = sum(1 for a in attempts if a.get("result") != "pass")
        gap = candidate.occupant_gap_m
        return [
            f"후보 {len(found.candidates)}곳(형상 탈락: {rejected})",
            f"선택 규칙: {found.to_dict()['rule']}",
            "다른 자재와 거리 " + ("—(표면에 다른 자재 없음)" if gap == float("inf")
                                  else f"{gap:.3f} m") + f", 고정 장애물·가장자리와 {candidate.static_gap_m:.3f} m",
            f"검증: 앞선 후보 {failed}곳 탈락, 이 자리 IK·12단계·경로 표본 "
            f"{attempts[-1].get('path_samples')}개 통과",
        ]

    # ── 실행 직전 재검사 ──────────────────────────────────────────────
    def recheck(self, spec: Mapping[str, Any]) -> tuple[bool, str, dict]:
        """확인 뒤: 출발지·자리 빈 상태·MoveIt 검사를 **지금 관측**으로 다시 본다."""
        import math

        from validation.free_spot import still_free, surface_from_config
        from validation.simulation_demo_state import ORIGIN_TOLERANCE_M

        jobs = self.jobs
        spot = dict(spec.get("spot") or {})
        material = spec.get("material")
        evidence: dict = {}
        location, center, problem = self._where(material)
        if problem:
            return False, problem, evidence
        if location != spot.get("source"):
            return False, f"{material}의 기록 위치가 {spot.get('source')}에서 {location}로 바뀌었다", evidence
        sample, why = self.observe()
        if sample is None:
            return False, why, evidence
        poses = sample.get("materials") or {}
        observed = poses.get(material)
        if observed is None or math.dist(observed[:3], center) > ORIGIN_TOLERANCE_M:
            return False, f"{material}가 관측상 출발지({location})에 없다", evidence
        surface = surface_from_config(jobs.workcell, surfaces_of(jobs), spot["surface_id"])
        sizes = {m: jobs.workcell["models"][m]["size_m"] for m in jobs.materials}
        others = {m: poses.get(m) for m in jobs.materials if m != material}
        free, detail = still_free(surface, spot["center_m"][:2], material_size_m=sizes[material],
                                  others=others, other_sizes=sizes)
        evidence["still_free"] = detail
        if not free:
            return False, detail, evidence
        planner = self.planner()
        problems = planner.fk_check(spot, surface_top_z=surface.top_z)
        if problems:
            return False, "; ".join(problems), evidence
        plan = planner.plan_from_spot(spot, material=material, source=location)
        if not hasattr(plan, "route"):
            return False, plan.detail, evidence
        client = self.runtime.planning_scene_client()
        bindings = self.runtime.cell_bindings()
        if client is None or bindings is None:
            return False, "planning scene·셀 선언을 쓸 수 없다", evidence
        scene = {k: v[1] for k, v in client.world_object_poses().items()}
        probes = planner.probes(others={m: p for m, p in others.items() if p is not None},
                                scene_poses=scene, tolerance_m=ORIGIN_TOLERANCE_M)
        start = {k: v for k, v in (sample.get("joints") or {}).items() if k.startswith("j")}
        ok, verify = planner.verify(plan, material=material, source=location, bindings=bindings,
                                    client=client, obstacles=probes, start_joints=start or None)
        evidence["verify"] = verify
        if not ok:
            return False, f"실행 직전 검사 미통과: {verify.get('problem')}", evidence
        return True, "출발지·자리·도달·충돌·경로를 다시 확인했다", evidence
