"""목표 배치 → 작업 순서 (Gazebo 시뮬레이션 시연).

사용자가 **도착 상태**(어느 자재를 어디에)와 **작업 환경 제약**(쓰면 안 되는 칸,
지금 자재가 있다고 알려 준 자리, 원하는 순서)을 텍스트로 주면, 이 모듈이 현재
셀 상태에서 그 상태로 가는 **작업 순서**를 스스로 정한다.

- 입력은 선언형이다. 관절값·좌표·궤적을 받을 칸이 없다.
- 순서는 의존성으로 정한다. 칸이 차 있으면 그 칸의 자재를 먼저 빼고, 칸을
  바꾸는 자재는 원래 자리를 거친다(지원하는 동작이 이송·복귀뿐이다).
- 사용자가 말한 순서는 의존성을 어기지 않는 한 따른다. 어기면 **왜 바꿨는지**
  단계마다 적는다.
- 사용자가 알려 준 현재 상태는 관측 기록과 대조만 한다. 다르면 관측을 따르고
  그 차이를 화면에 남긴다 — 말한 것을 사실로 올리지 않는다.
- 실행 단위는 기존 시연 job(`transfer`·`return`)이다. 새 로봇 동작을 만들지 않는다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from server.sim_demo_commands import material_aliases, pallet_resources
from validation.conveyor_slots import (
    OCCUPYING_STATES,
    record_slot,
    slot_label,
)
from validation.simulation_demo_state import HELD_ON_TARGET, ON_PALLET

ORIGIN = "origin"
#: 목표가 "컨베이어의 아무 빈 칸"이다. 계획이 칸을 고른다.
ANY_SLOT = "conveyor"

GOAL_ARRANGE = "arrange"


class ArrangementError(Exception):
    """계획할 수 없다. `decision`은 ASK(되물음) 또는 BLOCK(지원 밖·모순)."""

    def __init__(self, decision: str, message: str):
        self.decision = decision
        super().__init__(message)


@dataclass(frozen=True)
class ArrangementRequest:
    #: 자재 모델 → ORIGIN | ANY_SLOT | slot 이름
    targets: Mapping[str, str]
    blocked_slots: tuple[str, ...] = ()
    #: 사용자가 원하는 순서(자재 모델). 비어 있으면 계획이 정한다.
    priority: tuple[str, ...] = ()
    order_explicit: bool = False
    #: 사용자가 알려 준 현재 위치(자재 모델 → ORIGIN | slot 이름). 관측과 대조만 한다.
    assertions: Mapping[str, str] = field(default_factory=dict)
    #: 해석한 문장 조각(화면 표시용).
    clauses: tuple[Mapping[str, Any], ...] = ()
    #: 환경 정보 — 다시 쓸 수 있게 된 칸(지속 환경에서 뺀다).
    unblocked_slots: tuple[str, ...] = ()
    #: 환경 정보 — 없거나 쓰지 않는 자재(지속). 계획이 건드리지 않는다.
    unavailable_materials: tuple[str, ...] = ()
    #: 환경 정보 — 다시 쓰는 자재(지속 환경에서 뺀다).
    available_materials: tuple[str, ...] = ()
    #: 이번 요청에서만 뺀 자재("A 빼고").
    excluded_materials: tuple[str, ...] = ()
    #: "모든 자재"로 펼친 목표. 쓰지 않는 자재는 조용히 빠진다(메모는 남긴다).
    all_targets: tuple[str, ...] = ()
    #: 문장에서 말한 출발지(자재 → ORIGIN | slot). 관측과 다르면 계획하지 않고 묻는다.
    sources: Mapping[str, str] = field(default_factory=dict)

    @property
    def changes_environment(self) -> bool:
        return bool(self.blocked_slots or self.unblocked_slots
                    or self.unavailable_materials or self.available_materials)

    def to_dict(self) -> dict:
        return {"targets": dict(self.targets),
                "blocked_slots": list(self.blocked_slots),
                "priority": list(self.priority),
                "order_explicit": self.order_explicit,
                "assertions": dict(self.assertions),
                "clauses": [dict(c) for c in self.clauses],
                "unblocked_slots": list(self.unblocked_slots),
                "unavailable_materials": list(self.unavailable_materials),
                "available_materials": list(self.available_materials),
                "excluded_materials": list(self.excluded_materials),
                "all_targets": list(self.all_targets),
                "sources": dict(self.sources)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ArrangementRequest":
        if not isinstance(data, Mapping):
            raise ArrangementError("BLOCK", "배치 요청 형식이 아니다")
        targets = data.get("targets") or {}
        if not isinstance(targets, Mapping):
            raise ArrangementError("BLOCK", "배치 요청 형식이 아니다")

        def names(key: str) -> tuple[str, ...]:
            return tuple(str(v) for v in data.get(key) or ())

        request = cls(
            targets={str(k): str(v) for k, v in targets.items()},
            blocked_slots=names("blocked_slots"),
            priority=names("priority"),
            order_explicit=bool(data.get("order_explicit")),
            assertions={str(k): str(v) for k, v in
                        (data.get("assertions") or {}).items()},
            unblocked_slots=names("unblocked_slots"),
            unavailable_materials=names("unavailable_materials"),
            available_materials=names("available_materials"),
            excluded_materials=names("excluded_materials"),
            all_targets=names("all_targets"),
            sources={str(k): str(v) for k, v in (data.get("sources") or {}).items()},
        )
        if not request.targets and not request.blocked_slots:
            raise ArrangementError("ASK", "어느 자재를 어디에 둘지 알려 주세요")
        return request


def _has_batchim(word: str) -> bool:
    for ch in reversed(str(word or "")):
        if "가" <= ch <= "힣":
            return (ord(ch) - 0xAC00) % 28 != 0
        if ch.isdigit():
            return ch in "013678"
        if ch.isalpha():
            return ch.lower() in "lmn"
    return False


def _copula(word: str) -> str:
    return f"{word}{'이다' if _has_batchim(word) else '다'}"


def _j(word: str, pair: str) -> str:
    """조사를 받침에 맞춘다. pair는 "이/가"처럼 받침 있음/없음 순서다."""
    with_b, without_b = pair.split("/")
    return f"{word}{with_b if _has_batchim(word) else without_b}"


def empty_environment() -> dict:
    return {"blocked_slots": [], "unavailable_materials": []}


def apply_environment(environment: Mapping[str, Any] | None,
                      request: ArrangementRequest) -> dict:
    """지속 환경 + 이 요청의 환경 정보 → 새 지속 환경. 입력을 바꾸지 않는다."""
    env = environment or {}
    blocked = [s for s in env.get("blocked_slots") or ()
               if s not in request.unblocked_slots]
    for name in request.blocked_slots:
        if name not in blocked:
            blocked.append(name)
    unavailable = [m for m in env.get("unavailable_materials") or ()
                   if m not in request.available_materials]
    for model in request.unavailable_materials:
        if model not in unavailable:
            unavailable.append(model)
    return {"blocked_slots": blocked, "unavailable_materials": unavailable}


# ── 계획 ──────────────────────────────────────────────────────────────
def _place_label(where: str) -> str:
    if where == ORIGIN:
        return "원래 자리"
    if where.startswith("pallet:"):
        where = where.split(":", 1)[1]
    if where.startswith("loc_pallet_"):
        return f"{where.rsplit('_', 1)[-1]}번 팔레트"
    if where == ANY_SLOT:
        return "컨베이어 빈 위치"
    return slot_label(where)


def current_locations(objects: Mapping[str, Mapping[str, Any]],
                      materials: Iterable[str]) -> dict[str, str]:
    """자재 모델 → ORIGIN | slot 이름. **확정된 기록만** 받는다."""
    uncertain = sorted(
        model for model, row in objects.items()
        if (row or {}).get("state") in OCCUPYING_STATES
        and (row or {}).get("state") != HELD_ON_TARGET)
    if uncertain:
        raise ArrangementError(
            "BLOCK", "위치가 확정되지 않은 자재가 있어 배치를 계획하지 않는다: "
            + ", ".join(uncertain) + " — 먼저 복구·정합이 필요하다")
    out = {model: ORIGIN for model in materials}
    for model, row in objects.items():
        if model not in out:
            raise ArrangementError("BLOCK", f"셀 선언에 없는 자재 기록이 있다: {model}")
        if (row or {}).get("state") == HELD_ON_TARGET:
            out[model] = record_slot(row) or ""
        elif (row or {}).get("state") == ON_PALLET and (row or {}).get("pallet"):
            out[model] = str(row["pallet"])
    return out


def plan_arrangement(request: ArrangementRequest, *,
                     materials: Mapping[str, Mapping[str, Any]],
                     slot_names: Sequence[str],
                     objects: Mapping[str, Mapping[str, Any]],
                     environment: Mapping[str, Any] | None = None,
                     route_ok=None,
                     origins: Mapping[str, str] | None = None,
                     route_path=None) -> dict:
    """현재 기록 → 목표 배치. 단계 목록과 판단 근거를 돌려준다.

    `environment`는 지속 환경(쓰지 않는 칸·자재)이다. 이 요청의 환경 정보를
    덧붙여 적용한 결과로 계획한다.
    """
    labels = {m: str((spec or {}).get("korean") or m) for m, spec in materials.items()}
    slots = list(slot_names)
    if not slots:
        raise ArrangementError("BLOCK", "검증된 컨베이어 위치가 없어 배치를 계획할 수 없다")
    current = current_locations(objects, materials)
    notes: list[str] = []
    env = apply_environment(environment, request)

    for model in (*request.targets, *env["unavailable_materials"],
                  *request.excluded_materials):
        if model not in materials:
            raise ArrangementError("BLOCK", f"셀 선언에 없는 자재다: {model}")
    blocked = []
    for name in env["blocked_slots"]:
        if name not in slots:
            raise ArrangementError("BLOCK", f"검증된 컨베이어 위치가 아니다: {name}")
        if name not in blocked:
            blocked.append(name)
    frozen = set(env["unavailable_materials"]) | set(request.excluded_materials)
    targets = dict(request.targets)
    for model in sorted(frozen & set(targets)):
        if model in request.all_targets:
            targets.pop(model)
            notes.append(f"{_j(labels[model], '은/는')} 쓰지 않는 자재라 이번 배치에서 뺐다")
        else:
            raise ArrangementError(
                "BLOCK", f"{_j(labels[model], '은/는')} 없다고(쓰지 않는다고) 했다 —"
                " 옮길 수 없다")
    origins = dict(origins or {})
    pallets = set(origins.values())
    # 자기 원래 팔레트를 이름으로 말했으면 "원래 자리"다.
    targets = {m: (ORIGIN if w == origins.get(m) else w) for m, w in targets.items()}
    request = replace(request, targets=targets)
    for model, where in request.targets.items():
        if where in (ORIGIN, ANY_SLOT) or where in pallets:
            continue
        if where not in slots:
            raise ArrangementError(
                "BLOCK", f"{labels[model]}의 목적지 {_j(where, '은/는')} 검증된 컨베이어 위치가 아니다")
        if where in blocked:
            raise ArrangementError(
                "BLOCK", f"{_j(labels[model], '을/를')} {_place_label(where)}에 놓으라고 했지만"
                " 그 위치는 사용하지 말라고 했다 — 모순이다")

    # 명령에서 말한 출발지는 관측과 같아야 한다. 다르면 임의로 계획하지 않는다.
    for model, said in request.sources.items():
        if model in materials and said != current[model]:
            raise ArrangementError(
                "ASK", f"{_j(labels[model], '을/를')} {_place_label(said)}에서 옮기라고 했지만"
                f" 관측상 {_place_label(current[model])}에 있습니다 — 출발지를 확인해 주세요")

    # 사용자가 알려 준 현재 상태 대조. 관측(기록)이 우선한다.
    for model, said in request.assertions.items():
        if model not in materials:
            continue
        seen = current[model]
        if said != seen:
            notes.append(
                f"{_j(labels[model], '이/가')} {_place_label(said)}에 있다고 했지만 관측 기록은"
                f" {_copula(_place_label(seen))} — 관측 기록을 기준으로 계획했다")

    # ── 구체 위치 모델 ─────────────────────────────────────────────
    # 모든 자재는 한 자리를 차지한다: 원래 팔레트(ORIGIN) · 컨베이어 칸 · 다른 팔레트.
    def key(model: str, where: str) -> str:
        return origins.get(model, f"origin:{model}") if where == ORIGIN else where

    start_at = {key(m, current[m]): m for m in materials}
    final: dict[str, str] = {m: request.targets.get(m, current[m]) for m in materials}
    reasons: dict[str, str] = {}
    wanted = {key(m, w): m for m, w in final.items()
              if m in request.targets and w != ANY_SLOT}

    def free_at_end(where_key: str, model: str, chosen: set[str]) -> bool:
        others_end = {key(m, w) for m, w in final.items() if m != model and w != ANY_SLOT}
        holder = start_at.get(where_key)
        stays = holder is not None and holder != model and final.get(holder) == current[holder]
        return where_key not in others_end and where_key not in chosen and not stays

    # 목표 자리를 목표 없는 자재가 차지하고 있으면 먼저 비킨다(자기 원래 자리 →
    # 검증된 빈 칸 순). 비킬 곳이 없으면 묻는다.
    chosen: set[str] = set()
    for model in materials:
        if model in request.targets:
            continue
        here = current[model]
        here_key = key(model, here)
        owner = wanted.get(here_key)
        must_move = (here in blocked) or (owner is not None and owner != model)
        if not must_move:
            continue
        if model in frozen:
            raise ArrangementError(
                "BLOCK", f"{_j(labels[model], '이/가')} {_place_label(here)}에 있지만"
                " 쓰지 않는 자재라 옮길 수 없다 — 그 자리를 쓰는 계획을 세울 수 없다")
        spot = _place_label(here_key if here == ORIGIN and here_key in origins.values() else here)
        why_here = (f"{_j(spot, '을/를')} 쓰지 말라고 해서" if here in blocked
                    else f"{_j(labels[owner], '이/가')} {_j(spot, '을/를')} 써야 해서")
        if here != ORIGIN and free_at_end(key(model, ORIGIN), model, chosen):
            final[model] = ORIGIN
            chosen.add(key(model, ORIGIN))
            reasons[model] = (f"{why_here} 그 자리에 있던 {_j(labels[model], '을/를')}"
                              " 원래 자리로 뺀다")
            continue
        spare = next((s_ for s_ in slots if s_ not in blocked
                      and free_at_end(s_, model, chosen) and s_ not in start_at), None)
        if spare is None:
            raise ArrangementError(
                "ASK", f"{why_here} {_j(labels[model], '을/를')} 먼저 비워야 하는데 옮겨 둘"
                " 검증된 빈 자리가 없습니다 — 컨베이어 칸을 하나 비우거나 다른 목적지를"
                " 말해 주세요")
        final[model] = spare
        chosen.add(spare)
        reasons[model] = (f"{why_here} 그 자리의 {_j(labels[model], '을/를')}"
                          f" {_place_label(spare)}로 먼저 옮긴다(비워 둘 곳: 검증된 빈 칸)")

    # 목표 자리가 겹치면 모순이다.
    taken: dict[str, str] = {}
    for model, where in final.items():
        if where == ANY_SLOT:
            continue
        k = key(model, where)
        if k in taken:
            raise ArrangementError(
                "BLOCK", f"{_j(labels[taken[k]], '과/와')} {_j(labels[model], '을/를')} 같은 위치"
                f"({_place_label(where)})에 둘 수 없다")
        taken[k] = model

    # "컨베이어 아무 칸"은 남은 빈 칸에서 앞 번호부터. 이미 허용된 칸이면 그대로.
    order_hint = _priority_order(request, materials)
    for model in order_hint:
        if final.get(model) != ANY_SLOT:
            continue
        here = current[model]
        if here.startswith("slot_") and here not in blocked and here not in taken:
            final[model] = here
            taken[here] = model
            continue
        free = next((s_ for s_ in slots if s_ not in taken and s_ not in blocked), None)
        if free is None:
            raise ArrangementError(
                "BLOCK", f"{_j(labels[model], '을/를')} 놓을 컨베이어 빈 위치가 없다"
                f" (검증된 {len(slots)}칸, 사용 금지 {len(blocked)}칸)")
        final[model] = free
        taken[free] = model
        reasons.setdefault(model, f"비어 있는 {_j(_place_label(free), '을/를')} 골랐다")

    # ── 경로: 검증된 직접 경로, 아니면 임시 자리 경유 ────────────────
    def supported(model: str, here: str, there: str) -> tuple[bool, str]:
        if route_ok is None:
            ok = (here == ORIGIN and there.startswith("slot_")) or \
                 (there == ORIGIN and here.startswith("slot_"))
            return ok, "경로 판정기가 없다"
        return route_ok(model, here, there)

    def crossings(model: str, here: str, there: str) -> frozenset[str]:
        """측정된 경로가 스치는 자리(구체 id). 판정기가 없거나 모르면 빈 집합 —
        그 경우 실행 직전 경로 검사가 본다."""
        if route_path is None:
            return frozenset()
        return frozenset(route_path(model, here, there) or ())

    changed = [m for m in materials if current[m] != final[m]]
    end_keys = {key(m, final[m]) for m in materials}
    # 계획 내내 움직이지 않는 자재가 있는 자리 — 그 자리를 스치는 경로는 쓸 수 없다.
    statics = {key(m, current[m]): m for m in materials if m not in changed}

    def static_hits(model: str, here: str, there: str) -> list[str]:
        return sorted(loc for loc in crossings(model, here, there)
                      if statics.get(loc) not in (None, model))

    pallet_temps = [rid for rid in dict.fromkeys(origins.values())]
    rank = {m: i for i, m in enumerate(_priority_order(request, materials))}
    force_detour: set[str] = set()
    while True:
        nodes: list[dict] = []
        legs: dict[str, list[int]] = {}
        reserved: set[str] = set()
        for model in changed:
            here, there = current[model], final[model]
            ok, why = supported(model, here, there)
            in_way = static_hits(model, here, there) if ok else []
            if in_way:
                ok = False
                why = (", ".join(f"{_place_label(loc)}의 {labels[statics[loc]]}" for loc in in_way)
                       + "가 경로를 막는다(측정한 경로 표본)")
            if ok and model not in force_detour:
                legs[model] = [len(nodes)]
                nodes.append({"material": model, "from": here, "to": there, "after": set(),
                              "why": reasons.get(model) or _direct_reason(here, there)})
                continue
            temp = None
            # 임시 자리: 원래 자리 → 비어 있는 다른 팔레트 → 검증된 빈 칸.
            options = ([ORIGIN] if here != ORIGIN and there != ORIGIN else []) + [
                rid for rid in pallet_temps if rid != origins.get(model)] + list(slots)
            for cand in options:
                k = key(model, cand)
                if cand in (here, there) or cand in blocked or k in reserved \
                        or k in end_keys or (k in start_at and start_at[k] != model):
                    continue
                if supported(model, here, cand)[0] and supported(model, cand, there)[0] \
                        and not static_hits(model, here, cand) \
                        and not static_hits(model, cand, there):
                    temp = cand
                    break
            if temp is None:
                raise ArrangementError(
                    "BLOCK", f"{_j(labels[model], '을/를')} {_place_label(here)}에서"
                    f" {_place_label(there)}로 옮길 검증된 경로가 없다 — {why}")
            reserved.add(key(model, temp))
            via = ("자리끼리 서로 비켜야 하는 순환이라" if model in force_detour
                   else f"{_place_label(here)}→{_place_label(there)} 직접 경로가 없어({why})")
            first = len(nodes)
            nodes.append({"material": model, "from": here, "to": temp, "after": set(),
                          "why": reasons.get(model) or
                          (f"{via} 원래 자리를 거친다" if temp == ORIGIN
                           else f"{via} {_place_label(temp)}를 거쳐 간다")})
            nodes.append({"material": model, "from": temp, "to": there, "after": {first},
                          "why": reasons.get(model) if model not in force_detour and
                          reasons.get(model) else f"목표가 {_copula(_place_label(there))}"})
            legs[model] = [first, first + 1]
        # 자리를 채우는 단계는 그 자리를 비우는 단계(차지한 자재의 첫 구간) 뒤에 온다.
        vacate = {key(m, current[m]): legs[m][0] for m in changed}
        for node in nodes:
            k = key(node["material"], node["to"])
            holder = start_at.get(k)
            if holder is None or holder == node["material"] or k not in vacate:
                continue
            node["after"].add(vacate[k])
            node["why"] += (f" · {_place_label(node['to'])}에 있던"
                            f" {_j(labels[holder], '이/가')} 먼저 빠져야 한다")
        # 경로가 스치는 자리: 다른 자재가 **들어오기 전**에, 또는 **빠진 뒤**에 옮긴다.
        for i, node in enumerate(nodes):
            cross = crossings(node["material"], node["from"], node["to"])
            if not cross:
                continue
            for j, other in enumerate(nodes):
                if other["material"] == node["material"]:
                    continue
                into = key(other["material"], other["to"])
                out_of = key(other["material"], other["from"])
                if into in cross and i not in other["after"]:
                    other["after"].add(i)
                    node["why"] += (f" · 경로가 {_place_label(into)}를 지나므로"
                                    f" {_j(labels[other['material']], '이/가')} 그 자리에 오기 전에 옮긴다")
                if out_of in cross and j not in node["after"]:
                    node["after"].add(j)
                    node["why"] += (f" · 경로가 {_place_label(out_of)}를 지나므로"
                                    f" {_j(labels[other['material']], '이/가')} 그 자리에서 빠진 뒤 옮긴다")
        stuck = _cycle_members(nodes)
        if not stuck:
            break
        candidates = [nodes[i]["material"] for i in stuck
                      if len(legs[nodes[i]["material"]]) == 1]
        if not candidates:
            raise ArrangementError("BLOCK", "작업 순서에 풀 수 없는 순환 의존이 있다")
        pick = max(candidates, key=lambda m: rank.get(m, 99))
        force_detour.add(pick)
        notes.append(f"{_j(labels[pick], '은/는')} 자리끼리 맞바꾸는 순환을 풀려고 임시 자리를"
                     " 거친다")
    for node in nodes:
        node["action"] = _action_of(node["from"], node["to"])

    ordered = _topological(nodes, request, materials, notes, labels)
    steps = []
    position = {id(node): no for no, node in enumerate(ordered, 1)}
    for no, node in enumerate(ordered, 1):
        model = node["material"]
        steps.append({
            "step": no, "action": node["action"], "material": model,
            "material_label": labels[model],
            "from": node["from"], "from_label": _place_label(node["from"]),
            "to": node["to"], "to_label": _place_label(node["to"]),
            "slot": (node["to"] if str(node["to"]).startswith("slot_")
                     else node["from"] if str(node["from"]).startswith("slot_") else None),
            "reason": node["why"],
            "depends_on": sorted(position[id(nodes[i])] for i in node["after"]),
            "status": "pending", "job_id": None, "result": None,
        })

    expected = {model: final[model] for model in materials}
    return {
        "steps": steps,
        "current": dict(current),
        "expected": expected,
        "blocked_slots": blocked,
        "environment": env,
        "notes": notes,
        "summary": _summary(steps, expected, current, labels, blocked),
    }


def _priority_order(request: ArrangementRequest,
                    materials: Mapping[str, Any]) -> list[str]:
    seen: list[str] = []
    for model in (*request.priority, *request.targets, *materials):
        if model in materials and model not in seen:
            seen.append(model)
    return seen


def _action_of(here: str, there: str) -> str:
    """표시용 동작 이름. 실행은 모두 공통 transfer(출발, 도착)다."""
    if here == ORIGIN and str(there).startswith("slot_"):
        return "transfer"
    if there == ORIGIN:
        return "return"
    if str(here).startswith("slot_") and str(there).startswith("slot_"):
        return "move"
    return "relocate"


def _direct_reason(here: str, there: str) -> str:
    if str(here).startswith("slot_") and str(there).startswith("slot_"):
        return (f"{_place_label(here)}에서 {_place_label(there)}로 바로"
                " 옮긴다(검증된 직접 경로 — 원래 자리를 거치지 않는다)")
    if there == ORIGIN:
        return "목표가 원래 자리다"
    return f"목표가 {_copula(_place_label(there))}"


def _cycle_members(nodes: list[dict]) -> list[int]:
    """Kahn으로 풀리지 않고 남는 단계(순환에 걸린 것). 없으면 빈 목록."""
    pending = set(range(len(nodes)))
    done: set[int] = set()
    progressed = True
    while pending and progressed:
        progressed = False
        for i in sorted(pending):
            if nodes[i]["after"] <= done:
                done.add(i)
                pending.discard(i)
                progressed = True
    return sorted(pending)


def _topological(nodes: list[dict], request: ArrangementRequest,
                 materials: Mapping[str, Any], notes: list[str],
                 labels: Mapping[str, str]) -> list[dict]:
    """의존성을 지키면서 사용자 순서를 최대한 따른다."""
    rank = {model: i for i, model in enumerate(_priority_order(request, materials))}

    def key(i: int) -> tuple:
        node = nodes[i]
        # 순서를 말하지 않았으면 비우는 동작(복귀)을 먼저 한다 — 칸을 먼저 확보한다.
        phase = 0 if (not request.order_explicit and node["action"] == "return") else 1
        return (phase, rank.get(node["material"], 99),
                0 if node["action"] == "return" else 1)

    pending = set(range(len(nodes)))
    done: list[int] = []
    while pending:
        ready = sorted((i for i in pending if nodes[i]["after"] <= set(done)), key=key)
        if not ready:
            raise ArrangementError("BLOCK", "작업 순서에 순환 의존이 있다")
        done.append(ready[0])
        pending.discard(ready[0])

    if request.order_explicit and request.priority:
        wanted = [m for m in request.priority]
        actual: list[str] = []
        for i in done:
            model = nodes[i]["material"]
            if model in wanted and model not in actual and nodes[i]["action"] == "transfer":
                actual.append(model)
        asked = [m for m in wanted if m in actual]
        if asked != actual:
            notes.append(
                "요청한 순서(" + " → ".join(labels[m] for m in asked) + ")를 그대로"
                " 따를 수 없어 바꿨다: " + " → ".join(labels[m] for m in actual)
                + " — 앞 작업이 자리를 비워야 한다")
    return [nodes[i] for i in done]


def _summary(steps, expected, current, labels, blocked) -> str:
    if not steps:
        return "이미 목표 배치와 같습니다 — 움직일 작업이 없습니다."
    moved = sorted({s["material"] for s in steps})
    parts = [f"{labels[m]} → {_place_label(expected[m])}" for m in moved]
    text = f"작업 {len(steps)}단계로 배치합니다: " + ", ".join(parts)
    if blocked:
        text += " (사용 금지: " + ", ".join(_place_label(s) for s in blocked) + ")"
    return text + "."


# ── 텍스트 해석 ───────────────────────────────────────────────────────
_CLAUSE_SPLIT = re.compile(
    r"(?:[,，.;\n]|그리고|그\s*다음(?:에|으로)?|다음(?:에|으로)|마지막으로|그\s*뒤(?:에)?"
    r"|이후(?:에)?|(?<=[가-힣])(?<!빼)(?<!말)(?<!제외하)(?<!빼놓)고\s)")
_SLOT_TOKEN = re.compile(
    r"(?:컨베이어(?:의)?)?(\d+)번(?:자리|위치|칸|슬롯)?(?!팔레트)"
    r"|슬롯(\d+)")
_BLOCK_WORDS = ("사용금지", "쓰지마", "쓰지말", "못써", "못쓰", "고장", "막혀", "막혔",
                "비워둬", "비워놔", "비워두", "비워놓", "사용하지마", "사용하지말", "금지",
                "쓰면안", "사용불가")
_ORIGIN_WORDS = ("원래자리", "원래위치", "원위치", "제자리", "본래자리", "본래위치",
                 "돌려놔", "돌려놓", "복귀")
_STAY_WORDS = ("그대로", "놔둬", "놔두", "두고", "건드리지")
_ASSERT_WORDS = ("있어", "있다", "있음", "있는데", "있고", "놓여", "올라가있")
_TRANSFER_WORDS = ("옮겨", "옮기", "올려", "이송", "가져다", "갖다", "놓아", "놔", "놓",
                   "둬", "두")
#: 칸을 다시 쓸 수 있다 / 자재가 다시 있다.
_AVAILABLE_WORDS = ("고쳤", "고쳐졌", "수리", "다시써", "다시사용", "다시쓸", "써도돼",
                    "써도된", "사용해도", "사용가능", "풀어", "해제", "돌아왔", "다시있",
                    "다시생겼")
#: 자재가 없다 / 쓰지 않는다(지속).
_UNAVAILABLE_WORDS = ("없어", "없다", "없음", "없는상태", "사용안", "안써", "안쓸",
                      "쓰지마", "쓰지말", "사용하지마", "사용하지말", "손대지마", "건드리지마")
#: 이번 요청에서만 뺀다.
_EXCLUDE_WORDS = ("빼고", "말고", "제외하고", "제외한", "빼놓고")
_FIRST_WORDS = ("먼저", "우선", "첫번째", "처음")
_LAST_WORDS = ("나중", "마지막", "끝으로")
_ORDER_WORDS = (*_FIRST_WORDS, *_LAST_WORDS, "다음", "순서", "순으로", "차례")
_ALL_WORDS = ("모두", "전부", "모든", "전체", "나머지")
_SOURCE_SUFFIX = re.compile(r"^(?:에서|의|에있|에놓여|쪽에서)")
#: "컨베이어"가 **도착**으로 쓰였을 때만. "컨베이어의 자재", "컨베이어에서"는 아니다.
_CONVEYOR_DEST = re.compile(r"컨베이어(?:위|쪽)?(?:에|로|으로)(?!서|있|놓여)")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _mentioned(clause: str, aliases: Mapping[str, tuple[str, ...]]) -> list[str]:
    hits: list[tuple[int, str]] = []
    for model, names in aliases.items():
        positions = [clause.find(n) for n in names if n and n in clause]
        if positions:
            hits.append((min(positions), model))
    return [model for _, model in sorted(hits)]


def _slot_mentions(clause: str) -> list[tuple[str, bool]]:
    """(slot 이름, 출발·현재 위치로 쓰였는가)."""
    out = []
    for match in _SLOT_TOKEN.finditer(clause):
        number = match.group(1) or match.group(2)
        tail = clause[match.end():]
        # "1번 팔레트"는 칸이 아니다.
        if tail.startswith("팔레트"):
            continue
        out.append((f"slot_{number}", bool(_SOURCE_SUFFIX.match(tail))))
    return out


def parse_arrangement(text: str, workcell: Mapping[str, Any],
                      slot_names: Sequence[str]) -> ArrangementRequest | None:
    """문장 → 배치 요청. 배치 요청이 아니면 None이다.

    이해하지 못한 조각이 있으면 ArrangementError(ASK)를 던진다 — 모르는 부분을
    빼고 계획하지 않는다.
    """
    aliases = material_aliases(workcell)
    if not aliases:
        return None
    materials = list(aliases)
    origin_pallet = _origin_pallets(workcell)
    pallets = pallet_resources(workcell)
    raw_clauses = [c for c in _CLAUSE_SPLIT.split(str(text or "")) if c and c.strip()]
    targets: dict[str, str] = {}
    blocked: list[str] = []
    unblocked: list[str] = []
    unavailable: list[str] = []
    available: list[str] = []
    excluded: list[str] = []
    all_targets: list[str] = []
    assertions: dict[str, str] = {}
    sources: dict[str, str] = {}
    appearance: list[tuple[int, int, str]] = []
    clauses: list[dict] = []
    unresolved: list[str] = []
    order_explicit = False
    whole = _norm(text)

    for index, raw in enumerate(raw_clauses):
        clause = _norm(raw)
        if not clause:
            continue
        mentioned = _mentioned(clause, aliases)
        expanded_all = False
        cut = min((clause.find(w) for w in _EXCLUDE_WORDS if w in clause), default=-1)
        if cut >= 0 and mentioned:
            # "A자재 빼고 …": 빼는 말 앞에 나온 자재만 이번 요청에서 뺀다.
            before = [m for m in mentioned
                      if min(clause.find(n) for n in aliases[m] if n in clause) < cut]
            for model in before:
                if model not in excluded:
                    excluded.append(model)
            mentioned = [m for m in mentioned if m not in before]
        if (not mentioned or cut >= 0) and any(w in clause for w in _ALL_WORDS) and (
                "자재" in clause or "나머지" in clause or cut >= 0):
            rest = "나머지" in clause
            mentioned = [m for m in materials if m not in excluded
                         and not (rest and m in targets)]
            expanded_all = True
        slots = _slot_mentions(clause)
        dest_slots = [s for s, is_source in slots if not is_source]
        src_slots = [s for s, is_source in slots if is_source]
        if any(w in clause for w in _ORDER_WORDS):
            order_explicit = True
        weight = (-1 if any(w in clause for w in _FIRST_WORDS)
                  else 1 if any(w in clause for w in _LAST_WORDS) else 0)

        # 환경 정보: 칸을 다시 쓸 수 있다 / 자재가 다시 있다
        says_available = any(w in clause for w in _AVAILABLE_WORDS)
        # 칸만 말했거나(칸 복구) 자재만 말했을 때(자재 복귀)만 환경 정보다.
        if says_available and ((slots and not mentioned) or (mentioned and not slots)):
            for name, _ in slots:
                if name not in unblocked:
                    unblocked.append(name)
            for model in ([] if slots else mentioned):
                if model not in available:
                    available.append(model)
            clauses.append({"text": raw.strip(), "kind": "environment_available",
                            "slots": [s for s, _ in slots],
                            "materials": [] if slots else mentioned})
            continue

        # 환경 정보: 자재가 없다 / 쓰지 않는다
        if mentioned and not slots and not expanded_all and any(
                w in clause for w in _UNAVAILABLE_WORDS):
            for model in mentioned:
                if model not in unavailable:
                    unavailable.append(model)
            clauses.append({"text": raw.strip(), "kind": "environment_unavailable",
                            "materials": mentioned})
            continue

        # 환경 제약: 칸 사용 금지
        if any(w in clause for w in _BLOCK_WORDS) and slots:
            for name, _ in slots:
                if name not in blocked:
                    blocked.append(name)
            clauses.append({"text": raw.strip(), "kind": "blocked_slot",
                            "slots": [s for s, _ in slots]})
            continue

        if not mentioned:
            if clause.strip():
                unresolved.append(raw.strip())
            continue

        # 현재 상태 알림: "A는 지금 2번 칸에 있어"
        if (any(w in clause for w in _ASSERT_WORDS)
                and not any(w in clause for w in _TRANSFER_WORDS if w not in ("놓", "두"))
                and not any(w in clause for w in _ORIGIN_WORDS)):
            where = (slots[0][0] if slots else ORIGIN if (
                "팔레트" in clause or any(w in clause for w in _ORIGIN_WORDS)) else None)
            if where is None:
                unresolved.append(raw.strip())
                continue
            for model in mentioned:
                assertions[model] = where
            clauses.append({"text": raw.strip(), "kind": "state_assertion",
                            "materials": mentioned, "where": where})
            continue

        if any(w in clause for w in _STAY_WORDS) and not dest_slots:
            where = "stay"
        elif dest_slots:
            if len(dest_slots) != 1:
                raise ArrangementError(
                    "ASK", f"한 문장에 목적지가 여러 개입니다: {raw.strip()}")
            where = dest_slots[0]
        elif any(w in clause for w in _ORIGIN_WORDS):
            where = ORIGIN
        elif "팔레트" in clause and (not src_slots or re.search(
                r"\d+번팔레트(?:로|에|쪽)(?!서)", clause)):
            numbers = re.findall(r"(\d+)번팔레트", clause)
            named = [pallets.get(n) for n in numbers]
            # "N번 팔레트에서"는 출발지다. 도착으로 쓴 팔레트만 본다
            # ("컨베이어 2번에서 1번 팔레트로"처럼 칸 출발지가 있어도 도착은 본다).
            as_dest = [pallets.get(n) for n in re.findall(r"(\d+)번팔레트(?:로|에|쪽)(?!서)", clause)]
            if as_dest:
                if as_dest[0] is None:
                    raise ArrangementError(
                        "BLOCK", f"{raw.strip()} — 셀에 없는 팔레트입니다")
                # 자기 원래 팔레트면 "원래 자리", 아니면 그 팔레트 자원 id(계획기가
                # 검증된 자세와 점유를 본다).
                where = as_dest[0]
            elif _CONVEYOR_DEST.search(clause):
                where = ANY_SLOT
            elif named:
                unresolved.append(raw.strip())
                continue
            else:
                where = ORIGIN
        elif _CONVEYOR_DEST.search(clause):
            where = ANY_SLOT
        else:
            unresolved.append(raw.strip())
            continue

        stated = _stated_source(clause, src_slots, pallets, origin_pallet, mentioned)
        for model in mentioned:
            if where == "stay":
                continue
            if model in stated:
                sources[model] = stated[model]
            target = ORIGIN if where == origin_pallet.get(model) else where
            if model in targets and targets[model] != target:
                raise ArrangementError(
                    "ASK", f"{model}의 목적지를 두 번 다르게 말했습니다")
            targets[model] = target
            if expanded_all and model not in all_targets:
                all_targets.append(model)
            appearance.append((weight, index, model))
        clauses.append({"text": raw.strip(), "kind": "target",
                        "materials": mentioned,
                        "where": "stay" if where == "stay" else where})

    environment_only = bool(unblocked or unavailable or available)
    if not targets and not blocked and not environment_only:
        return None
    if unresolved and (targets or blocked or environment_only):
        raise ArrangementError(
            "ASK", "이해하지 못한 부분이 있습니다: " + " / ".join(unresolved)
            + " — 자재와 위치(예: A자재는 컨베이어 2번, C자재는 원래 자리)로 말해 주세요")
    if not targets and blocked and not any(w in whole for w in _TRANSFER_WORDS + _ORIGIN_WORDS):
        # 제약만 알려 준 경우 — 그 칸에 있는 자재를 빼는 계획이 된다.
        pass
    priority = tuple(model for _, _, model in sorted(appearance, key=lambda r: (r[0], r[1])))
    return ArrangementRequest(targets=targets, blocked_slots=tuple(blocked),
                              priority=priority, order_explicit=order_explicit,
                              assertions=assertions, clauses=tuple(clauses),
                              unblocked_slots=tuple(unblocked),
                              unavailable_materials=tuple(unavailable),
                              available_materials=tuple(available),
                              excluded_materials=tuple(excluded),
                              all_targets=tuple(all_targets), sources=sources)


def _stated_source(clause: str, src_slots: list[str], pallets: Mapping[str, str],
                   origin_pallet: Mapping[str, str], mentioned: list[str]) -> dict[str, str]:
    """"컨베이어 2번에서", "1번 팔레트에서"처럼 말한 출발지. 자재 하나일 때만 묶는다."""
    if len(mentioned) != 1:
        return {}
    model = mentioned[0]
    if len(src_slots) == 1:
        return {model: src_slots[0]}
    numbers = re.findall(r"(\d+)번팔레트(?:에서|의)", clause)
    if len(numbers) == 1:
        rid = pallets.get(numbers[0])
        if not rid:
            return {}
        return {model: ORIGIN if rid == origin_pallet.get(model) else rid}
    return {}


def _origin_pallets(workcell: Mapping[str, Any]) -> dict[str, str]:
    """자재 모델 → 원래 팔레트 **자원 id**. 프레임 부모 선언에서만 온다."""
    from server.sim_demo_jobs import materials_from_workcell

    pallet_rid = {row.get("gazebo_model"): row.get("resource_id")
                  for row in workcell.get("resource_map", ())}
    out: dict[str, str] = {}
    for model, spec in materials_from_workcell(workcell).items():
        rid = pallet_rid.get(spec.get("support_model"))
        if rid:
            out[model] = rid
    return out


def arrangement_signal(text: str, workcell: Mapping[str, Any]) -> bool:
    """해석에 실패해도 **배치 요청이었다고** 볼 근거가 있는가.

    자재 둘 이상, 칸 사용 금지, 순서 낱말이 있을 때만 참이다. 자재 하나짜리
    명령은 해석 실패를 여기서 알리지 않고 기존 단일 명령 경로에 맡긴다.
    """
    clause = _norm(text)
    if len(_mentioned(clause, material_aliases(workcell))) >= 2:
        return True
    if any(w in clause for w in _BLOCK_WORDS) and _slot_mentions(clause):
        return True
    if any(w in clause for w in _EXCLUDE_WORDS):
        return True
    return any(w in clause for w in _ORDER_WORDS if w not in ("다음",))


def states_slot_source(text: str) -> bool:
    """"컨베이어 2번에서"처럼 컨베이어 칸을 **출발지**로 말했는가."""
    return any(is_source for _, is_source in _slot_mentions(_norm(text)))


def is_arrangement_like(request: ArrangementRequest | None) -> bool:
    """단일 자재 명령 경로로 둬도 되는 요청이면 False다."""
    if request is None:
        return False
    if request.blocked_slots or request.assertions or request.changes_environment:
        return True
    if request.excluded_materials:
        return True
    if len(request.targets) >= 2:
        return True
    return request.order_explicit and bool(request.targets)


# ── 규칙이 풀지 못한 배치 문장: Qwen 해석(열거값만) ─────────────────────
LLM_FIELDS = ("targets", "blocked_slots", "order", "order_explicit", "confidence")
LLM_SYSTEM_PROMPT = (
    "너는 로봇 작업 셀의 배치 요청 해석기다. 사용자의 문장을 **도착 배치**로만 옮긴다. "
    "순서·관절값·좌표·궤적을 만들지 않는다. 자재는 주어진 id만, 목적지는 "
    "origin(자기 원래 팔레트)·conveyor(컨베이어 아무 빈 칸)·주어진 slot id만 쓴다. "
    "'자리를 바꿔'처럼 현재 위치에 기대는 말은 주어진 현재 위치로 풀어 쓴다. "
    "사용하지 말라고 한 칸은 blocked_slots에 넣는다. 사용자가 순서를 말했으면 order에 "
    "그 순서로 자재 id를 넣고 order_explicit=true. 확실하지 않으면 confidence를 낮게 준다."
)


def llm_schema(material_ids: Sequence[str], slot_names: Sequence[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False,
        "required": list(LLM_FIELDS),
        "properties": {
            "targets": {"type": "array", "maxItems": len(material_ids), "items": {
                "type": "object", "additionalProperties": False,
                "required": ["material_id", "destination"],
                "properties": {
                    "material_id": {"enum": list(material_ids)},
                    "destination": {"enum": [ORIGIN, ANY_SLOT, *slot_names]},
                }}},
            "blocked_slots": {"type": "array", "items": {"enum": list(slot_names)}},
            "order": {"type": "array", "items": {"enum": list(material_ids)}},
            "order_explicit": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }


def llm_user_prompt(text: str, materials: Mapping[str, Mapping[str, Any]],
                    slot_names: Sequence[str], current: Mapping[str, str]) -> str:
    lines = ["자재(현재 위치):"]
    for model, spec in materials.items():
        names = [str(spec.get("korean") or model), *(f"{c}자재" for c in
                                                     spec.get("korean_colors") or ())]
        lines.append(f"- {model}: {', '.join(names)} (현재 {current.get(model, ORIGIN)})")
    lines.append("컨베이어 칸: " + ", ".join(f"{s}({slot_label(s)})" for s in slot_names))
    lines.append(f"문장: {text}")
    return "\n".join(lines)


def request_from_llm(payload: Any, *, material_ids: Sequence[str],
                     slot_names: Sequence[str], min_confidence: float,
                     text: str) -> ArrangementRequest:
    """모델 출력을 믿지 않고 다시 본다. 어긋나면 ArrangementError(ASK)."""
    if not isinstance(payload, Mapping) or set(payload) != set(LLM_FIELDS):
        raise ArrangementError("ASK", "해석 결과 형식이 맞지 않습니다")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ArrangementError("ASK", "해석 확신도가 없습니다")
    if confidence < min_confidence:
        raise ArrangementError(
            "ASK", f"문장을 확실히 해석하지 못했습니다(확신도 {confidence:.2f}) —"
            " 자재와 위치로 다시 말해 주세요")
    allowed = {ORIGIN, ANY_SLOT, *slot_names}
    targets: dict[str, str] = {}
    for row in payload.get("targets") or ():
        if not isinstance(row, Mapping):
            raise ArrangementError("ASK", "해석 결과 형식이 맞지 않습니다")
        model, where = row.get("material_id"), row.get("destination")
        if model not in material_ids or where not in allowed:
            raise ArrangementError("ASK", "해석 결과에 셀에 없는 자재·위치가 있습니다")
        if model in targets and targets[model] != where:
            raise ArrangementError("ASK", "한 자재에 목적지가 둘입니다")
        targets[model] = where
    blocked = []
    for name in payload.get("blocked_slots") or ():
        if name not in slot_names:
            raise ArrangementError("ASK", "해석 결과에 셀에 없는 위치가 있습니다")
        if name not in blocked:
            blocked.append(name)
    order = []
    for model in payload.get("order") or ():
        if model not in material_ids:
            raise ArrangementError("ASK", "해석 결과에 셀에 없는 자재가 있습니다")
        if model not in order:
            order.append(model)
    if not targets and not blocked:
        raise ArrangementError("ASK", "어느 자재를 어디에 둘지 알아듣지 못했습니다")
    return ArrangementRequest(
        targets=targets, blocked_slots=tuple(blocked),
        priority=tuple(order) or tuple(targets),
        order_explicit=bool(payload.get("order_explicit")) and bool(order),
        clauses=({"text": str(text), "kind": "llm", "confidence": confidence},))


def interpret_with_llm(client: Any, text: str, *,
                       materials: Mapping[str, Mapping[str, Any]],
                       slot_names: Sequence[str],
                       current: Mapping[str, str],
                       min_confidence: float) -> tuple[ArrangementRequest, dict]:
    """Qwen 호출 → 검증된 배치 요청. 실패는 ArrangementError(ASK)다."""
    ids = list(materials)
    try:
        completion = client.chat(
            system=LLM_SYSTEM_PROMPT,
            user=llm_user_prompt(text, materials, slot_names, current),
            json_schema=llm_schema(ids, slot_names),
            schema_name="sim_demo_arrangement")
    except Exception as exc:  # noqa: BLE001 — 서버가 없으면 되묻는다
        raise ArrangementError("ASK", f"해석기를 쓸 수 없습니다: {exc}"[:200]) from exc
    raw = str(getattr(completion, "content", "") or "")[:4000]
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ArrangementError("ASK", "해석 결과를 JSON으로 읽지 못했습니다") from exc
    request = request_from_llm(payload, material_ids=ids, slot_names=slot_names,
                               min_confidence=min_confidence, text=text)
    return request, {"interpreted_by": "qwen", "raw": raw,
                     "latency_sec": getattr(completion, "latency_sec", None)}
