"""일반 경로(`/v1/plan`) 이송 요청: 해석(Qwen) → 해소·검증(서버) → 검증된 작업 의도 (2026-10-06).

공통 원인(실측 정리): 서버 슬롯(별칭으로 찾은 **역할 없는 id 목록**)과 계획 모델(그 목록 + 발화로
역할을 짐작해 계획을 바로 씀)과 요청↔계획 일치 검증(같은 역할 없는 목록과 대조)이 서로 다른 해석을
썼다. 그래서 축약 발화에서 역할을 뒤바꾼 계획이 통과하고("a자재 컨베이어로"), 상징 표현으로 만든 맞는
계획이 막혔다("초록자재 원래자리로" → 3번 팔레트가 '요청에 없는 리소스').

여기서 하는 일:
1. Qwen이 의도 하나를 고른다(`planning/intent_interpreter.py`): 자재·말한 출발지·목적지(id 또는
   `origin`·`free_slot`)와 각각의 **원문 표현**.
2. 서버가 해소·검증한다. 고른 id마다 근거를 남긴다(`core/task_intent.py`):
   - 자재: 원문 표현이 발화에 있고, 그 표현이 **그 자재만** 가리킨다(카탈로그 별칭·이름·등록 색).
   - 출발지: 말했으면 원문 근거를 확인하고 **현재 위치(기록)와 다르면 차단**. 말하지 않았으면 현재
     위치(state 근거). 현재 위치를 모르면 되묻는다.
   - 목적지: 위치 id면 원문 근거 확인. `origin`은 등록된 원래 팔레트(registry 근거), `free_slot`은
     컨베이어 빈 칸(state 근거, 없으면 차단). 출발지와 같으면 되묻는다.
3. 계획은 검증된 의도로 **결정적으로** 만든다(이동→집기→이동→놓기→종료 스킬). 요청↔계획 일치 검증도
   같은 의도로 역할까지 대조한다(`validation/request_plan_consistency.py`).

모델이 만든 계획을 근거로 요청 리소스를 더하지 않는다. 표현별 예외 규칙을 두지 않는다 — 표현은
모델이 읽고, 서버는 등록 정보·상태·원문 포함 여부만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind, normalize
from core.task_intent import (
    REGISTRY,
    STATE,
    TRANSFER,
    UTTERANCE,
    Evidence,
    ResolvedRef,
    TaskIntent,
)

_CONVEYOR = "loc_conveyor"


@dataclass(frozen=True)
class IntentDecision:
    """해석 결과. `kind`: intent(검증된 의도) | other(이송 아님 → 기존 계획 생성) | ask | block."""

    kind: str
    intent: TaskIntent | None = None
    reason_code: ReasonCode | None = None
    detail: str = ""
    clarification: str | None = None
    interpretation: Mapping[str, Any] | None = None


def _catalog_location(place: str | None, catalog: ResourceCatalog) -> str | None:
    if not place:
        return None
    if catalog.has(place) and catalog.kind_of(place) is ResourceKind.LOCATION:
        return place
    if place.startswith("slot_") and catalog.has(_CONVEYOR):
        return _CONVEYOR
    return None                                         # 표면 빈 위치 등 — 일반 경로 밖


def cell_facts(catalog: ResourceCatalog, jobs) -> dict:
    """셀 설정·등록 정보·상태 기록만으로 만든 사실. 모델 입력과 서버 검증이 같이 쓴다."""
    from validation.conveyor_slots import occupancy

    workcell = jobs.workcell
    try:
        world = jobs.transfer_world()
    except Exception:  # noqa: BLE001 — 기록이 확정되지 않았으면 위치를 모른다고 둔다
        world = None
    location_of = dict(getattr(world, "location_of", None) or {})
    capability = jobs._capability()
    materials = []
    for row in workcell.get("resource_map", ()):
        rid, model = row.get("resource_id"), row.get("gazebo_model")
        if not rid or not catalog.has(rid) or catalog.kind_of(rid) is not ResourceKind.OBJECT:
            continue
        entry = catalog.get(rid)
        colors = list(row.get("korean_colors") or ())
        materials.append({
            "id": rid, "model": model, "name": entry.display_name, "colors": colors[:4],
            "tokens": sorted({normalize(t) for t in (entry.display_name, *entry.aliases, *colors)
                              if normalize(t)}),
            "location": _catalog_location(location_of.get(model), catalog) if world else None,
            "origin": _catalog_location(capability.origin_of(model), catalog),
        })
    locations = []
    for rid in catalog.ids_of_kind(ResourceKind.LOCATION):
        entry = catalog.get(rid)
        locations.append({
            "id": rid, "name": entry.display_name,
            "aliases": [a for a in entry.aliases if a != entry.display_name][:3],
            "tokens": sorted({normalize(t) for t in (entry.display_name, *entry.aliases)
                              if normalize(t)}),
        })
    records = ((jobs.status() or {}).get("state") or {}).get("objects") or {}
    free = [name for name, who in occupancy(jobs.slots, records).items() if who is None] \
        if getattr(jobs, "slots", None) else []
    return {"materials": materials, "locations": locations, "free_conveyor_slots": free}


def _in_utterance(text: str | None, utterance: str) -> bool:
    return bool(text) and normalize(text) in normalize(utterance)


def _overlaps(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    x, y = normalize(a), normalize(b)
    return x in y or y in x


def _names(rows, text: str) -> list[str]:
    """원문 표현이 가리키는 행(자재·위치)들. 표현 안에 그 행의 토큰이 있으면 가리킨다."""
    span = normalize(text or "")
    return [row["id"] for row in rows if any(tok in span for tok in row["tokens"])]


def load_place_terms(manifest_path) -> dict[str, dict]:
    """활성 작업 셀 매니페스트의 `place_terms` 파일 → {상징: {terms(정규화), requires_location}}.
    파일이 없으면 빈 dict — 상징 목적지(원래 자리·빈자리)를 받지 않는다(되묻는다)."""
    import json
    from pathlib import Path

    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        name = manifest.get("place_terms")
        if not name:
            return {}
        data = json.loads((Path(manifest_path).parent / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for symbol, spec in (data.get("symbols") or {}).items():
        out[symbol] = {"terms": sorted({normalize(t) for t in spec.get("terms") or () if normalize(t)}),
                       "requires_location": spec.get("requires_location")}
    return out


def _unique_location(facts, text: str | None) -> tuple[str | None, int]:
    named = _names(facts["locations"], text or "")
    return (named[0] if len(named) == 1 else None), len(named)


def decide_intent(interp, utterance: str, facts: Mapping[str, Any], *,
                  min_confidence: float) -> IntentDecision:
    """해석 결과 → 검증된 의도 · 되묻기 · 차단 · 기존 경로(이송 아님).

    **원문 근거가 판정의 기준이다.** 모델이 고른 id는 원문 표현이 가리키는 것과 같을 때만 쓴다.
    근거 없는 출발지 주장은 '말하지 않음'으로 보고 상태 기록을 쓴다. 근거 있는 출발지가 기록과
    다르면 차단한다. 목적지는 원문이 가리키는 위치, 또는 등록 표현으로 확인된 상징 목적지뿐이다.
    """
    materials = {m["id"]: m for m in facts["materials"]}
    locations = {loc["id"]: loc for loc in facts["locations"]}
    symbols = facts.get("symbols") or {}
    info = {k: getattr(interp, k, None) for k in (
        "ok", "action", "material_id", "material_text", "source_id", "source_text",
        "destination", "destination_text", "confidence", "model_reason", "failure",
        "reason", "raw", "model_id", "latency_sec")}
    choices = " / ".join(f"{m['name']}({'·'.join(m['colors'][:1]) or '-'},"
                         f" {locations[m['location']]['name'] if m['location'] in locations else '위치 모름'})"
                         for m in facts["materials"])

    def ask(detail: str) -> IntentDecision:
        return IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                              detail=detail, clarification=detail, interpretation=info)

    def block(detail: str) -> IntentDecision:
        return IntentDecision("block", reason_code=ReasonCode.PLAN_RESOURCE_MISMATCH,
                              detail=detail, interpretation=info)

    if not interp.ok:
        return ask(f"요청을 해석하지 못했습니다({interp.reason}) — 자재와 목적지를 말해 주세요: {choices}")
    if interp.action == "unknown":
        return ask(f"무엇을 어디로 옮길지 알 수 없습니다 — 자재와 목적지를 말해 주세요: {choices}")
    # 자재 표현이 없으면 자재 이송이 아니다 — 위치 이동·홈 등은 기존 계획 생성이 맡는다.
    if interp.action == "other" or (interp.material_id is None and not interp.material_text):
        return IntentDecision("other", interpretation=info)
    if interp.confidence is None or interp.confidence < min_confidence:
        return ask(f"해석 확신이 낮습니다({(interp.confidence or 0):.2f} < {min_confidence:.2f})"
                   f" — 어느 자재를 어디로 옮길지 말해 주세요: {choices}")

    # ── 자재: 원문 표현이 발화에 있고 **그 자재만** 가리킨다 ──────────────────────────
    mid = interp.material_id
    if not _in_utterance(interp.material_text, utterance):
        return ask(f"자재를 가리킨 표현을 발화에서 확인할 수 없습니다 — 자재 이름이나 색으로 말해 주세요: {choices}")
    named = _names(facts["materials"], interp.material_text)
    if len(named) > 1:
        return ask(f"'{interp.material_text}'에 맞는 자재가 여럿입니다 — 이름으로 말해 주세요: {choices}")
    if not named or mid not in materials or named != [mid]:
        return ask(f"'{interp.material_text}'은(는) 등록된 자재가 아닙니다 — 말해 주세요: {choices}")
    material = materials[mid]
    material_ref = ResolvedRef(mid, Evidence(UTTERANCE, interp.material_text))

    # ── 목적지: 원문 근거(위치) 또는 등록 표현(상징) ─────────────────────────────────
    dtext = interp.destination_text
    if not _in_utterance(dtext, utterance):
        return ask(f"{material['name']}을(를) 어디로 옮길까요?")
    dloc, dcount = _unique_location(facts, dtext)
    symbol = next((sym for sym, spec in symbols.items()
                   if any(term in normalize(dtext) for term in spec["terms"])), None)
    if symbol == "origin" and dloc is None:
        origin = material["origin"]
        if origin is None:
            return ask(f"{material['name']}의 원래 자리가 등록돼 있지 않습니다 — 위치를 말해 주세요")
        dest_ref = ResolvedRef(origin, Evidence(
            REGISTRY, f"{material['name']}의 원래 자리(등록: {locations[origin]['name']}) ← '{dtext}'"))
    elif symbol == "free_slot":
        need = symbols["free_slot"].get("requires_location")
        if not need or need not in locations:
            return ask("빈자리가 어느 위치의 칸인지 등록돼 있지 않습니다 — 목적지를 위치 이름으로 말해 주세요")
        if need not in _names(facts["locations"], utterance):
            return ask("어느 빈자리인지 알 수 없습니다 — 일반 모드의 빈자리는 컨베이어 빈 칸입니다."
                       " '컨베이어 빈자리로'처럼 말해 주세요")
        free = list(facts.get("free_conveyor_slots") or ())
        if not free:
            return block("컨베이어에 빈 칸이 없다(기록) — 실행하지 않는다")
        dest_ref = ResolvedRef(need, Evidence(
            STATE, f"컨베이어 빈 칸 {len(free)}개(기록, 칸은 실행 전 검사가 고른다) ← '{dtext}'"))
    elif dloc is not None:
        if interp.destination not in (None, dloc, "origin", "free_slot"):
            return ask(f"'{dtext}'이(가) 가리키는 위치({locations[dloc]['name']})와 해석이 다릅니다 — 목적지를 다시 말해 주세요")
        dest_ref = ResolvedRef(dloc, Evidence(UTTERANCE, dtext))
    else:
        detail = ("여러 위치를 가리킵니다" if dcount > 1 else "등록된 위치가 아닙니다")
        return ask(f"'{dtext}'은(는) {detail} — 목적지를 다시 말해 주세요"
                   f" (위치: {', '.join(loc['name'] for loc in facts['locations'])})")

    # ── 출발지: 원문 근거가 있으면 '말한 출발지'(기록과 다르면 차단), 없으면 현재 위치(기록) ──
    current = material["location"]
    stext = interp.source_text
    stated = None
    if stext and _in_utterance(stext, utterance) and not _overlaps(stext, dtext):
        stated, scount = _unique_location(facts, stext)
        if stated is None:
            return ask(f"'{stext}'이(가) 어느 위치인지 알 수 없습니다 — 출발지를 다시 말하거나 빼고 말해 주세요")
    if current is None:
        return ask(f"{material['name']}의 현재 위치를 확인할 수 없습니다 — 자재 상태를 정리한 뒤 다시 요청해 주세요")
    if stated is not None and stated != current:
        return block(f"말한 출발지({locations[stated]['name']})와 {material['name']}의 실제 위치"
                     f"({locations[current]['name']}, 기록)가 다르다 — 실행하지 않는다")
    source_ref = (ResolvedRef(current, Evidence(UTTERANCE, stext)) if stated is not None
                  else ResolvedRef(current, Evidence(STATE, f"{material['name']}의 현재 위치(기록)")))

    if dest_ref.resource_id == source_ref.resource_id:
        return ask(f"{material['name']}은(는) 이미 {locations[source_ref.resource_id]['name']}에 있습니다"
                   " — 어디로 옮길지 말해 주세요")
    intent = TaskIntent(action=TRANSFER, material=material_ref, source=source_ref,
                        destination=dest_ref, interpreter=str(interp.model_id or ""),
                        confidence=interp.confidence, model_reason=interp.model_reason)
    return IntentDecision("intent", intent=intent, interpretation=info)


class IntentPlanProvider:
    """검증된 의도로 계획 초안을 **결정적으로** 만든다(모델 호출 없음). 계획 파이프라인의 나머지
    검사(카탈로그·스킬·Profile·잡기 순서)는 그대로 거친다."""

    def __init__(self, intent: TaskIntent, *, final_skill: str | None, latency_sec: float | None):
        self.intent = intent
        self.final_skill = final_skill
        self.latency_sec = latency_sec or 0.0

    @property
    def provider_id(self) -> str:
        return "intent-compose"

    @property
    def model_name(self) -> str:
        return self.intent.interpreter or "intent"

    @property
    def is_mock(self) -> bool:
        return False

    @property
    def prompt_template_version(self) -> str:
        from planning.intent_interpreter import INTERPRETER_VERSION

        return INTERPRETER_VERSION

    def generate(self, context):
        from planning.plan_provider import DraftStep, PlanDraft, PlanningMode
        from planning.prompt import OUTPUT_SCHEMA_VERSION

        i = self.intent
        obj, src, dst = (i.material.resource_id, i.source.resource_id,
                         i.destination.resource_id)
        steps = [DraftStep("move", {"to": src}),
                 DraftStep("pick", {"object": obj, "from": src}),
                 DraftStep("move", {"to": dst}),
                 DraftStep("place", {"object": obj, "to": dst})]
        if self.final_skill:
            steps.append(DraftStep(self.final_skill, {}))
        return PlanDraft(steps=tuple(steps), mode=PlanningMode.JSON_SCHEMA,
                         provider_id=self.provider_id, model_name=self.model_name,
                         prompt_template_version=self.prompt_template_version,
                         output_schema_version=OUTPUT_SCHEMA_VERSION,
                         latency_sec=self.latency_sec, terminal_hold=None, is_mock=False)
