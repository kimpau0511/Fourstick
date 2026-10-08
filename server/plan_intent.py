"""일반 경로(`/v1/plan`) 이송 요청: 해석(Qwen) → 해소·검증(서버) → 검증된 작업 의도 (2026-10-06).

공통 원인(실측 정리): 서버 슬롯(별칭으로 찾은 **역할 없는 id 목록**)과 계획 모델(그 목록 + 발화로
역할을 짐작해 계획을 바로 씀)과 요청↔계획 일치 검증(같은 역할 없는 목록과 대조)이 서로 다른 해석을
썼다. 그래서 축약 발화에서 역할을 뒤바꾼 계획이 통과하고("a자재 컨베이어로"), 상징 표현으로 만든 맞는
계획이 막혔다("초록자재 원래자리로" → 3번 팔레트가 '요청에 없는 리소스').

2차(같은 날, 복귀 버튼·긴 문장 실측) 공통 원인:
- 모델의 **역할 배정**을 그대로 믿었다. "A자재를 원래 자리로 돌려놔"에서 모델이 '원래 자리'를
  출발지 칸에 적고 목적지를 비워, 복귀 버튼이 매번 "어디로 옮길까요?"로 끝났다. '저기 있는 그',
  '팔레트'(어느 팔레트인지 없음)를 말한 출발지로 받아 되물었다.
- 작업 하나만 받는 형식이라 작업 둘·자재 둘·정정을 한 작업으로 뭉갰고, 그 작업이 통과했다.
- 자재 표현이 있어도 모델이 `other`라 하면 기존 계획 생성으로 빠졌다(없는 자재·'A자재 옮겨줘').
- 같은 세션 맥락이 없어 '그거'·되묻기 뒤의 답을 이을 수 없었다.

여기서 하는 일:
1. Qwen이 작업(들)을 말한 순서대로 읽는다(`planning/intent_interpreter.py`): 자재·출발지·목적지
   (id 또는 `origin`·`free_slot`)와 각각의 **원문 표현**.
2. 서버가 모델과 **따로** 원문을 훑는다: 등록된 자재 이름·색, 등록된 장소 표현과 그 **뒤에 붙은 조사**
   (출발 '에서', 도착 '로' — 등록 파일의 role_markers). 조사가 정한 역할이 모델 배정보다 우선이다.
   자재가 둘 이상·작업이 둘 이상·목적지가 둘 이상이면 실행 의도를 만들지 않고 되묻는다.
3. 해소·검증. 고른 id마다 근거를 남긴다(`core/task_intent.py`):
   - 자재: 원문이 **그 자재만** 가리킨다(카탈로그 별칭·이름·등록 색). 원문에 가리키는 말('그거')만
     있으면 같은 세션 맥락(직전에 옮긴 자재·앞 질문에서 확인한 자재)으로 — 맥락이 없으면 되묻는다.
     등록되지 않은 자재 이름은 차단한다.
   - 출발지: 말했으면(조사 '에서' 등) **현재 위치(기록)와 다르면 차단**. '팔레트'처럼 여러 위치를
     가리키면 현재 위치가 그중 하나일 때만 받는다. 말하지 않았으면 현재 위치(state 근거).
   - 목적지: 위치 id면 원문 근거. `origin`은 등록된 원래 팔레트(registry 근거), `free_slot`은
     컨베이어 빈 칸(state 근거, 없으면 차단). 이미 거기 있으면 실행하지 않고 안내(noop).
     다른 자재가 있는 팔레트면 차단.
   - 하나가 모자라면 확인된 것은 세션에 남기고(draft) 모자란 것만 되묻는다.
4. 계획은 검증된 의도로 **결정적으로** 만든다(이동→집기→이동→놓기→종료 스킬). 요청↔계획 일치 검증도
   같은 의도로 역할까지 대조한다(`validation/request_plan_consistency.py`).

모델이 만든 계획을 근거로 요청 리소스를 더하지 않는다. 표현별 예외 규칙을 두지 않는다 — 표현은
모델이 읽고, 서버는 등록 정보(이름·색·장소 표현·조사)·상태·원문 포함 여부만 본다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceCatalog, ResourceKind, normalize
from planning.intent_interpreter import TaskMention
from core.task_intent import (
    CONTEXT,
    REGISTRY,
    STATE,
    TRANSFER,
    UTTERANCE,
    Evidence,
    ResolvedRef,
    TaskIntent,
)

_CONVEYOR = "loc_conveyor"
#: 앞 질문에서 확인한 값(draft)을 이어 쓰는 시간. 지나면 버린다.
DRAFT_TTL_SEC = 300.0


@dataclass(frozen=True)
class IntentDecision:
    """해석 결과. `kind`: intent(검증된 의도) | other(이송 아님 → 기존 계획 생성) | ask | block
    | noop(이미 목적지에 있음 — 실행할 것이 없다는 안내).

    `draft`: 되묻기에서 세션에 남길 확인된 값({"material": ref, "destination": ref, "question"}).
    None이면 세션의 기존 draft를 건드리지 않는다(해석 실패 등)."""

    kind: str
    intent: TaskIntent | None = None
    reason_code: ReasonCode | None = None
    detail: str = ""
    clarification: str | None = None
    interpretation: Mapping[str, Any] | None = None
    draft: Mapping[str, Any] | None = None
    tasks: tuple[str, ...] = ()


def _catalog_location(place: str | None, catalog: ResourceCatalog) -> str | None:
    if not place:
        return None
    if catalog.has(place) and catalog.kind_of(place) is ResourceKind.LOCATION:
        return place
    if place.startswith("slot_") and catalog.has(_CONVEYOR):
        return _CONVEYOR
    return None                                         # 표면 빈 위치 등 — 일반 경로 밖


def cell_facts(catalog: ResourceCatalog, jobs) -> dict:
    """셀 설정·등록 정보·상태 기록만으로 만든 사실. 모델 입력과 서버 검증이 같이 쓴다.

    상태 기록을 읽지 못하면 `state_error`에 이유를 두고 모든 현재 위치를 모름(None)으로 둔다."""
    from validation.conveyor_slots import occupancy

    workcell = jobs.workcell
    state_error = None
    try:
        world = jobs.transfer_world()
        if world is None:
            state_error = "확정되지 않은 자재 기록이 있습니다"
    except Exception as exc:  # noqa: BLE001 — 기록이 확정되지 않았으면 위치를 모른다고 둔다
        world, state_error = None, f"자재 상태 기록을 읽지 못했습니다({exc})"[:160]
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
    try:
        records = ((jobs.status() or {}).get("state") or {}).get("objects") or {}
        free = [name for name, who in occupancy(jobs.slots, records).items() if who is None] \
            if getattr(jobs, "slots", None) else []
    except Exception as exc:  # noqa: BLE001
        free, state_error = [], state_error or f"작업 상태를 읽지 못했습니다({exc})"[:160]
    return {"materials": materials, "locations": locations, "free_conveyor_slots": free,
            "state_error": state_error}


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
    data = _place_terms_file(manifest_path)
    out = {}
    for symbol, spec in (data.get("symbols") or {}).items():
        out[symbol] = {"terms": sorted({normalize(t) for t in spec.get("terms") or () if normalize(t)}),
                       "requires_location": spec.get("requires_location")}
    return out


def load_language_terms(manifest_path) -> dict:
    """같은 파일의 references(가리키는 말·일반 명사)와 role_markers(역할 조사), 정규화해서.
    파일이 없으면 빈 목록 — 맥락 해소를 하지 않고(되묻는다) 조사로 역할을 고치지 않는다."""
    data = _place_terms_file(manifest_path)
    refs = data.get("references") or {}
    markers = data.get("role_markers") or {}
    norm = lambda rows: [normalize(t) for t in rows or () if normalize(t)]  # noqa: E731
    # 긴 조사부터 본다('에서'가 '에'보다 먼저, '으로'가 '로'보다 먼저).
    by_length = lambda rows: sorted(set(norm(rows)), key=len, reverse=True)  # noqa: E731
    codes = refs.get("code_readings") or {}
    return {"context": norm((refs.get("context") or {}).get("terms")),
            "correction": norm((refs.get("correction") or {}).get("terms")),
            "generic": by_length((refs.get("generic") or {}).get("terms")),
            "filler": by_length((refs.get("filler") or {}).get("terms")),
            "pointing": by_length((refs.get("pointing") or {}).get("terms")),
            "return_verbs": by_length((refs.get("return_verbs") or {}).get("terms")),
            # 코드 한글 읽기(2026-10-08): {코드 글자: [읽기]}와 읽기 뒤에 붙을 수 있는 조사. 원문 경계는 material_positions가 본다.
            "code_readings": {str(k).lower(): [str(r) for r in v or () if str(r).strip()]
                              for k, v in (codes.get("readings") or {}).items()},
            "code_particles": sorted({str(t) for t in codes.get("particles") or () if str(t).strip()},
                                     key=len, reverse=True),
            "source": by_length(markers.get("source")),
            "destination": by_length(markers.get("destination"))}


def _place_terms_file(manifest_path) -> dict:
    import json
    from pathlib import Path

    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        name = manifest.get("place_terms")
        if not name:
            return {}
        return json.loads((Path(manifest_path).parent / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── 원문 훑기(모델과 별개) ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlaceMention:
    """원문에서 찾은 등록 장소 표현 하나. `role`은 바로 뒤 조사로 정한다(없으면 None)."""

    kind: str          # location | symbol
    key: str           # 위치 id 또는 상징(origin·free_slot)
    start: int
    end: int
    surface: str       # 정규화된 원문 조각(표현 + 조사)
    role: str | None


def _find_all(text: str, token: str):
    start = text.find(token)
    while token and start >= 0:
        yield start, start + len(token)
        start = text.find(token, start + 1)


def _marker_role(rest: str, terms: Mapping[str, Any]) -> tuple[str | None, str]:
    for role in ("source", "destination"):        # 출발 조사를 먼저 본다('에서' ⊃ '에')
        for marker in terms.get(role) or ():
            if rest.startswith(marker):
                return role, marker
    return None, ""


def scan_places(utterance: str, facts: Mapping[str, Any], terms: Mapping[str, Any]) -> list[PlaceMention]:
    u = normalize(utterance)
    found = []
    for loc in facts["locations"]:
        for tok in loc["tokens"]:
            found += [("location", loc["id"], s, e) for s, e in _find_all(u, tok)]
    for symbol, spec in (facts.get("symbols") or {}).items():
        for tok in spec["terms"]:
            found += [("symbol", symbol, s, e) for s, e in _find_all(u, tok)]
    # 더 긴 표현 안에 든 짧은 표현은 버린다('컨베이어벨트' 안의 '벨트', '원래팔레트' 안의 위치 없음).
    kept = [f for f in found if not any(o is not f and o[2] <= f[2] and f[3] <= o[3]
                                        and (o[3] - o[2]) > (f[3] - f[2]) for o in found)]
    out, seen = [], set()
    for kind, key, s, e in sorted(kept, key=lambda f: f[2]):
        if (s, e) in seen:
            continue
        seen.add((s, e))
        role, marker = _marker_role(u[e:], terms)
        out.append(PlaceMention(kind, key, s, e, u[s:e + len(marker)], role))
    return out


def material_code(m: Mapping[str, Any]) -> str | None:
    """등록 이름이 '라틴 글자 하나 + 자재'(예: 'A자재')면 그 글자. 아니면 None — 이름에서만 뽑는다."""
    import re

    hit = re.fullmatch(r"\s*([A-Za-z])\s*자재\s*", str(m.get("name") or ""))
    return hit.group(1).lower() if hit else None


def material_positions(utterance: str, facts: Mapping[str, Any], *, readings: bool = False) -> dict[str, list[int]]:
    """자재 id → 원문(정규화)에서 가리킨 위치들. 이름·별칭·색 + 단독 코드 글자(2026-10-08: 'A 컨베이어로').

    단독 코드 글자는 앞뒤에 다른 라틴 글자·숫자가 없는 글자 하나다('A로', 'A 컨베이어' — 'AB'·'A1'은 아니다).
    정규화(공백 제거) 전 원문에서 경계를 보고, 위치는 정규화 문자열 기준으로 바꾼다.

    `readings=True`(이송 요청으로 읽혔을 때만)면 코드의 한글 읽기('에이'·'비'·'씨', 등록 파일 code_readings)도 본다.
    따로 떨어진 낱말일 때만이다: 앞에 한글·라틴·숫자가 붙지 않고, 뒤에는 등록된 조사 하나만 붙고 그다음이 끝·공백·
    문장부호다. '비가 오네'(주어 조사 '가'는 등록하지 않았다)·'에이씨'·'시비'·'비닐'은 코드가 아니다."""
    import re

    u = normalize(utterance)
    terms = facts.get("terms") or {}
    table = terms.get("code_readings") or {}
    particles = "|".join(re.escape(p_) for p_ in terms.get("code_particles") or ())
    out: dict[str, list[int]] = {}
    for m in facts["materials"]:
        hits = [s for tok in m["tokens"] if tok for s, _ in _find_all(u, tok)]
        code = material_code(m)
        if code:
            for hit in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z])(?![A-Za-z0-9])", utterance):
                if hit.group(1).lower() == code:
                    hits.append(len(normalize(utterance[:hit.start()])))
            for reading in (table.get(code) or ()) if readings else ():
                # 읽기 + (이름의 일부 '자재') + (등록 조사 하나) — 그 뒤는 끝·공백·문장부호여야 한다.
                pattern = (rf"(?<![가-힣A-Za-z0-9]){re.escape(reading)}(?:자재)?"
                           + (rf"(?:{particles})?" if particles else ""))
                for hit in re.finditer(pattern + r"(?![가-힣A-Za-z0-9])", utterance):
                    hits.append(len(normalize(utterance[:hit.start()])))
        if hits:
            out[m["id"]] = sorted(set(hits))
    return out


def _material_surface(m: Mapping[str, Any], u: str, facts: Mapping[str, Any] | None = None,
                      utterance: str = "", readings: bool = False) -> str:
    """근거 문구: 원문(정규화)에 있는 등록 토큰, 없으면 단독 코드 글자('A' — 단독 글자) 또는 한글 읽기."""
    tok = next((t for t in m["tokens"] if t and t in u), None)
    if tok:
        return tok
    code = (material_code(m) or "?")
    if readings and facts is not None:
        bare = {**facts, "terms": {**(facts.get("terms") or {}), "code_readings": {}}}
        if not material_positions(utterance, {**bare, "materials": [m]}).get(m["id"]):
            said = next((r for r in ((facts.get("terms") or {}).get("code_readings") or {}).get(code, ())
                         if r in u), code.upper())
            return f"'{said}'({code.upper()}의 한글 읽기 — 단독 낱말)"
    return f"'{code.upper()}'(단독 글자)"


def scan_materials(utterance: str, facts: Mapping[str, Any], *, readings: bool = False) -> list[str]:
    """원문이 이름·별칭·색(·단독 코드 글자·한글 읽기)으로 가리킨 자재들(처음 나온 순서)."""
    first = {mid: pos[0] for mid, pos in material_positions(utterance, facts, readings=readings).items()}
    return sorted(first, key=lambda mid: first[mid])


def corrected_choice(positions: Mapping[str, list[int]], marker_at: list[int]) -> tuple[str, str] | None:
    """정정('X 말고 Y', 'X, 아니 Y', 'X 대신 Y') — 마지막 정정 말 **앞**에만 나온 후보 하나와 **뒤**에만 나온 후보
    하나가 있을 때 (앞, 뒤)를 돌려준다. 그 밖(뒤가 둘 이상·한 후보가 앞뒤 모두·정정 말 뒤가 비었음)은 None → 되묻는다."""
    if not marker_at or len(positions) != 2:
        return None
    last = max(marker_at)
    before = [k for k, ps in positions.items() if all(p < last for p in ps)]
    after = [k for k, ps in positions.items() if all(p > last for p in ps)]
    if len(before) == 1 and len(after) == 1:
        return before[0], after[0]
    return None


def category_words(facts: Mapping[str, Any]) -> dict[str, list[str]]:
    """둘 이상의 위치 이름에 같이 들어 있는 낱말(예: '팔레트') → 그 위치들. 등록 이름에서만 뽑는다."""
    owners: dict[str, set[str]] = {}
    for loc in facts["locations"]:
        for name in (loc["name"], *loc.get("aliases", ())):
            for word in str(name).split():
                w = normalize(word)
                if len(w) >= 2 and not any(ch.isdigit() for ch in w):
                    owners.setdefault(w, set()).add(loc["id"])
    return {w: sorted(ids) for w, ids in owners.items() if len(ids) > 1}


def _strip_markers(text: str, terms: Mapping[str, Any]) -> str:
    core = normalize(text or "")
    changed = True
    while changed and core:
        changed = False
        for marker in (*(terms.get("source") or ()), *(terms.get("destination") or ()),
                       "있는", "있던", "에"):
            if core.endswith(marker) and len(core) > len(marker):
                core, changed = core[: -len(marker)], True
                break
    return core


def merge_pick_place(tasks):
    """'A 집은 다음 컨베이어에 놔'처럼 한 이송을 집기·놓기로 나눠 말한 것을 작업 하나로 합친다(2026-10-08).

    합치는 조건(모두): 작업이 둘 이상, 목적지(id·표현)가 있는 작업은 **마지막 하나뿐**, 그 앞 작업들은 목적지가 없다,
    모델이 고른 자재 id가 하나 이하, 출발지 id가 하나 이하. 자재가 둘이거나 목적지가 둘이면(실제 복수 작업)
    합치지 않는다 — 서버가 복수 작업으로 되묻는다. 자재 표현이 실제로 하나인지는 서버가 원문으로 다시 본다."""
    if len(tasks) < 2:
        return tasks
    has_dest = [bool(t.destination or t.destination_text) for t in tasks]
    if not has_dest[-1] or any(has_dest[:-1]):
        return tasks
    if len({t.material_id for t in tasks if t.material_id}) > 1 \
            or len({t.source_id for t in tasks if t.source_id}) > 1:
        return tasks
    texts = [normalize(t.material_text) for t in tasks if t.material_text]
    pick = next((t for t in tasks if t.material_id), None) or next((t for t in tasks if t.material_text), tasks[0])
    if len(set(texts)) > 1 and not all(x in texts[0] or texts[0] in x for x in texts):
        # 자재 표현이 서로 다르다('A' / '파란 거') — 모델 id가 비어 있어도 합치지 않는다. 단, 뒤 표현이 '그거'처럼
        # 앞을 가리키면(모델이 같은 id를 줬을 때) 위에서 id로 이미 하나다.
        if len({t.material_id for t in tasks}) != 1:
            return tasks
    last = tasks[-1]
    src = next((t for t in tasks if t.source_id or t.source_text), None)
    return (TaskMention(material_id=pick.material_id, material_text=pick.material_text,
                        source_id=src.source_id if src else None, source_text=src.source_text if src else None,
                        destination=last.destination, destination_text=last.destination_text),)


def _leftover(text: str | None, facts: Mapping[str, Any], terms: Mapping[str, Any]) -> str:
    """자재 표현에서 위치 이름·조사·일반 명사·군말·가리키는 말을 뺀 나머지. 비면 '위치로만 가리킨 자재'다."""
    core = normalize(text or "")
    for tok in sorted({t for loc in facts["locations"] for t in loc["tokens"]}, key=len, reverse=True):
        core = core.replace(tok, "")
    for word in sorted({w for group in ("source", "destination", "generic", "filler", "pointing")
                        for w in terms.get(group) or ()}, key=len, reverse=True):
        core = core.replace(word, "")
    for word in ("있는", "있던", "놓인", "놓여", "올려진", "올려져", "위", "의"):
        core = core.replace(word, "")
    return core


def _rest_word(text: str | None, facts: Mapping[str, Any], terms: Mapping[str, Any]) -> str:
    """근거 문구용: 자재 표현 끝의 일반 명사('물체'·'거'), 없으면 빈 문자열."""
    core = normalize(text or "")
    return next((g for g in terms.get("generic") or () if core.endswith(g)), "")


# ── 판정 ──────────────────────────────────────────────────────────────────────


def decide_intent(interp, utterance: str, facts: Mapping[str, Any], *,
                  min_confidence: float, context: Mapping[str, Any] | None = None) -> IntentDecision:
    """해석 결과 → 검증된 의도 · 되묻기 · 차단 · 안내(noop) · 기존 경로(이송 아님).

    **원문 근거가 판정의 기준이다.** 모델이 고른 id·역할은 원문(등록 이름·색·장소 표현·조사)과
    맞을 때만 쓴다. `context`는 같은 세션의 서버 기록: {"moved": 직전에 옮긴 자재, "draft": 앞
    질문에서 확인한 값} — 원문에 가리키는 말이 있거나 앞 질문의 답일 때만 쓴다.
    """
    materials = {m["id"]: m for m in facts["materials"]}
    locations = {loc["id"]: loc for loc in facts["locations"]}
    symbols = facts.get("symbols") or {}
    terms = facts.get("terms") or {}
    context = context or {}
    draft = context.get("draft") or None
    moved = context.get("moved") or None
    tasks = tuple(t for t in (getattr(interp, "tasks", ()) or ())
                  if any((t.material_text, t.material_id, t.source_text, t.destination_text,
                          t.destination)))
    tasks = merge_pick_place(tasks)
    task = tasks[0] if tasks else None
    info = {k: getattr(interp, k, None) for k in (
        "ok", "action", "confidence", "model_reason", "failure", "reason", "raw", "model_id",
        "latency_sec", "prompt_tokens")}
    info["tasks"] = [t.__dict__ for t in getattr(interp, "tasks", ()) or ()]
    if len(tasks) == 1 and len(info["tasks"]) > 1:
        info["merged_tasks"] = "집기·놓기로 나눠 말한 한 이송(같은 자재, 목적지 하나)"
    u = normalize(utterance)
    places = scan_places(utterance, facts, terms)
    # 코드 한글 읽기('에이'·'비'·'씨')는 이송 요청으로 읽혔을 때만 본다(2026-10-08) — '비가 오네' 같은 말을 자재로 읽지 않는다.
    readings = bool(interp.ok) and interp.action == "transfer"
    named = scan_materials(utterance, facts, readings=readings)
    pointing = next((t for t in terms.get("context") or () if t in u), None)
    returning = next((t for t in terms.get("return_verbs") or () if t in u), None)
    source_locs = sorted({p.key for p in places if p.role == "source" and p.kind == "location"})
    if len(tasks) > 1 and len({t_.material_id for t_ in tasks}) == 1 and tasks[0].material_id:
        # 2026-10-08 '1번 팔레트에 있는 주황색 자재를 … 집은 다음에 컨베이어로': 모델이 출발지 묘사를 목적지로 읽어
        # 같은 자재의 작업을 하나 더 만들었다. 원문에 근거(출발 조사가 아닌 장소 표현·복귀 동사)가 없는 목적지의 작업은
        # 모델의 잘못 읽기로 보고 뺀다. 근거 있는 작업이 둘 이상이면(실제 복수 작업) 그대로 둔다.
        def backed(t_):
            dt = normalize(t_.destination_text or "") if _in_utterance(t_.destination_text, utterance) else ""
            in_text = [p for p in places if p.role != "source" and dt and u[p.start:p.end] in dt]
            if t_.destination == "origin":
                return any(p.kind == "symbol" and p.key == "origin" for p in in_text) or bool(returning)
            return any(p.key == t_.destination or (p.kind == "symbol" and p.key == t_.destination) for p in in_text)
        def misread(t_):
            # 목적지 표현이 없거나, 그 표현이 원문의 **출발지 묘사**('1번 팔레트에 있는')다 — 그 밖의 근거 없는 목적지
            # ('다시 가져와' 등)는 사용자가 실제로 말한 다른 작업일 수 있어 빼지 않는다(복수 작업으로 되묻는다).
            dt = normalize(t_.destination_text or "")
            return not dt or any(p.role == "source" and u[p.start:p.end] in dt for p in places)
        kept = [t_ for t_ in tasks if backed(t_)]
        if len(kept) == 1 and all(misread(t_) for t_ in tasks if t_ is not kept[0]):
            info["dropped_tasks"] = [t_.__dict__ for t_ in tasks if t_ is not kept[0]]
            tasks = (kept[0],)
            task = tasks[0]
    if len(tasks) > 1 and len({(t_.material_id, t_.destination) for t_ in tasks}) == 1 and tasks[0].material_id \
            and tasks[0].destination and named == [tasks[0].material_id] \
            and len(material_positions(utterance, facts, readings=readings)[named[0]]) == 1 \
            and len([p for p in places if p.role != "source"]) == 1:
        # 모델이 '집은 다음 ~에 놔'를 같은 작업 둘로 적었다. 원문에 자재·장소 표현이 각각 한 번뿐이면 같은 이송 하나다
        # (같은 자재를 두 번 말했거나 장소가 둘이면 합치지 않는다 — 반복·복수 작업은 되묻는다).
        tasks = (tasks[-1],)
        task = tasks[0]
        info["merged_tasks"] = "같은 이송을 두 작업으로 나눠 읽음(원문의 자재·장소 표현 각 1회)"
    loc_name = lambda rid: locations[rid]["name"] if rid in locations else "위치 모름"  # noqa: E731
    choices = " / ".join(f"{m['name']}({'·'.join(m['colors'][:1]) or '-'}, {loc_name(m['location'])})"
                         for m in facts["materials"])

    def ask(detail: str, *, code=ReasonCode.PLAN_CLARIFICATION_REQUIRED, keep=None) -> IntentDecision:
        return IntentDecision("ask", reason_code=code, detail=detail, clarification=detail,
                              interpretation=info, draft=keep)

    def block(detail: str, code=ReasonCode.PLAN_RESOURCE_MISMATCH) -> IntentDecision:
        return IntentDecision("block", reason_code=code, detail=detail, interpretation=info, draft={})

    if not interp.ok:
        return ask(f"요청을 해석하지 못했습니다({interp.reason}) — 자재와 목적지를 말해 주세요: {choices}")

    # ── 여러 작업·여러 자재: 순서는 읽어 보여 주되, 한 작업으로 합치지 않는다 ─────────────────
    dest_marked = [p for p in places if p.role == "destination"]
    corrected = next((c for c in terms.get("correction") or () if c in u), None)
    marker_at = [s for c in terms.get("correction") or () for s, _ in _find_all(u, c)]
    corrections: list[str] = []
    corrected_to = None          # 목적지 정정의 뒤 표현(정규화) — 모델이 그 표현을 읽었는지 아래에서 본다
    label_of = lambda p: (loc_name(p.key) if p.kind == "location"  # noqa: E731
                          else {"origin": "원래 자리", "free_slot": "빈자리"}[p.key])
    if corrected and len({(p.kind, p.key) for p in dest_marked}) == 2:
        # 2026-10-08: 고친 앞뒤가 분명하면(앞 하나 · 정정 말 · 뒤 하나) 뒤의 것을 쓴다. 확인 카드에 정정을 보인다.
        pos: dict = {}
        for p in dest_marked:
            pos.setdefault((p.kind, p.key), []).append(p.start)
        pick = corrected_choice(pos, marker_at)
        if pick:
            old, new = pick
            old_p = next(p for p in dest_marked if (p.kind, p.key) == old)
            new_p = next(p for p in dest_marked if (p.kind, p.key) == new)
            dest_marked = [p for p in dest_marked if (p.kind, p.key) == new]
            corrected_to = u[new_p.start:new_p.end]
            corrections.append(f"목적지 정정: {label_of(old_p)} → {label_of(new_p)}")
    if corrected and len({(p.kind, p.key) for p in dest_marked}) > 1:
        said = ", ".join(dict.fromkeys(
            loc_name(p.key) if p.kind == "location" else {"origin": "원래 자리", "free_slot": "빈자리"}[p.key]
            for p in dest_marked))
        return ask(f"목적지를 고쳐 말한 것 같습니다({said}) — 어느 쪽인지 추측하지 않습니다."
                   " 최종 목적지 하나로 다시 말해 주세요", code=ReasonCode.PLAN_AMBIGUOUS, keep={})
    if len(named) == 2 and corrected:
        mpos = material_positions(utterance, facts, readings=readings)
        pick = corrected_choice({k: mpos[k] for k in named}, marker_at)
        if pick:
            old, new = pick
            named = [new]
            corrections.append(f"자재 정정: {materials[old]['name']} → {materials[new]['name']}")
    if marker_at:
        last = max(marker_at)
        # 정정 말 뒤에 무엇으로 고쳤는지 원문에서 확인되지 않으면(정정 앞의 것만 남음) 계획하지 않는다.
        # 2026-10-08 합성 음성 실측: 'B자재 아니 C자재를' → STT '비차트 아니 시차제를' — C를 못 읽고 B만 남아
        # B를 옮기는 계획이 만들어졌다.
        mpos = material_positions(utterance, facts, readings=readings)
        if len(named) == 1 and all(p_ < last for p_ in mpos.get(named[0], [last + 1])) and not corrections:
            return ask(f"'{materials[named[0]]['name']}' 뒤에 고쳐 말한 내용이 있는데 어느 자재로 고쳤는지 확인할 수"
                       f" 없습니다 — 옮길 자재 하나만 다시 말해 주세요: {choices}", code=ReasonCode.PLAN_AMBIGUOUS, keep={})
        if dest_marked and all(p_.start < last for p_ in dest_marked) \
                and not any(c.startswith("목적지") for c in corrections):
            return ask("목적지 뒤에 고치거나 부정하는 말이 있습니다 — 어디로 옮길지(또는 옮기지 않을지) 다시 말해 주세요",
                       code=ReasonCode.PLAN_AMBIGUOUS, keep={})
    if corrections and len(tasks) > 1:
        # 모델이 정정을 작업 여럿으로 나눠 적었다 — 정정 뒤의 것과 맞는 작업만 남긴다(자재가 정정됐으면 그 자재).
        keep_mat = named[0] if len(named) == 1 else None
        same = [t_ for t_ in tasks if keep_mat is None or t_.material_id in (None, keep_mat)]
        if len({t_.material_id for t_ in same}) <= 1 and same:
            tasks = (same[-1],)
            task = tasks[0]
    if len(tasks) > 1:
        def label(t):
            what = materials[t.material_id]["name"] if t.material_id in materials else (t.material_text or "?")
            where = (loc_name(t.destination) if t.destination in locations
                     else {"origin": "원래 자리", "free_slot": "빈자리"}.get(t.destination or "", t.destination_text or "?"))
            return f"{what} → {where}"
        order = tuple(label(t) for t in tasks)
        steps = " ".join(f"{i}) {s}" for i, s in enumerate(order, 1))
        return IntentDecision(
            "ask", reason_code=ReasonCode.PLAN_AMBIGUOUS, interpretation=info, tasks=order, draft={},
            detail=f"작업 {len(order)}개로 이해했습니다: {steps}. 지금은 한 번에 이송 하나만 계획합니다"
                   " — 먼저 할 작업 하나만 말해 주세요",
            clarification=f"작업 {len(order)}개로 이해했습니다: {steps}. 지금은 한 번에 이송 하나만"
                          " 계획합니다 — 먼저 할 작업 하나만 말해 주세요")
    if len(named) > 1:
        return ask(f"자재가 여럿입니다({', '.join(materials[m]['name'] for m in named)}) — 한 번에 하나만"
                   " 옮길 수 있습니다. 먼저 옮길 자재 하나만 말해 주세요", code=ReasonCode.PLAN_AMBIGUOUS, keep={})

    # ── 이송 요청인가 ────────────────────────────────────────────────────────────
    t = task or TaskMention()
    # 앞 질문("A자재를 어디로 옮길까요?")의 답: 모델이 이송으로 읽었거나, 자재 없이 목적지만 말했다.
    answers_draft = bool(draft) and (interp.action == "transfer" or (
        bool(draft.get("material")) and bool(dest_marked) and not named))
    material_signal = bool(named or t.material_text or t.material_id or pointing
                           or (source_locs and interp.action == "transfer"))
    if not material_signal and not answers_draft:
        if interp.action == "transfer" and dest_marked:
            # 옮기라는데 자재가 없다 — 목적지는 남기고 무엇을 옮길지만 묻는다.
            refs = [{"kind": p.kind, "key": p.key, "text": p.surface} for p in dest_marked]
            what = ", ".join(_describe(r, locations, materials) for r in refs)
            return ask(f"무엇을 {what} 옮길까요? 자재 이름이나 색으로 말해 주세요: {choices}",
                       keep=_draft_with(None, refs))
        if interp.action == "unknown":
            return ask(f"무엇을 어디로 옮길지 알 수 없습니다 — 자재와 목적지를 말해 주세요: {choices}")
        return IntentDecision("other", interpretation=info)
    context_candidates = []
    if draft and draft.get("material") and draft.get("at", 0) >= (moved or {}).get("at", 0):
        context_candidates.append(("앞 질문에서 확인한 자재", draft["material"]["resource_id"]))
    if moved and moved.get("material") in materials:
        context_candidates.append(("이 대화에서 직전에 옮긴 자재", moved["material"]))
    if pointing and not named and not context_candidates:
        # 가리키는 말만 있고 맥락이 없다 — 모델 확신과 상관없이 무엇이 모자란지 구체적으로 묻는다.
        keep = _draft_with(None, [{"kind": p.kind, "key": p.key, "text": p.surface} for p in dest_marked])
        return ask(f"'{pointing}'이(가) 어느 자재인지 알 수 없습니다 — 이 대화에서 앞서 옮기거나 확인한"
                   f" 자재가 없습니다. 자재 이름이나 색으로 말해 주세요: {choices}", keep=keep)
    uncertain = (interp.action == "unknown" and not answers_draft) \
        or interp.confidence is None or interp.confidence < min_confidence
    known = None
    if len(named) == 1:
        m = materials[named[0]]
        known = ResolvedRef(m["id"], Evidence(UTTERANCE, _material_surface(m, u, facts, utterance, readings)))
    elif pointing and context_candidates:
        why, mid = context_candidates[0]
        known = ResolvedRef(mid, Evidence(CONTEXT, f"{why}({materials[mid]['name']}) ← '{pointing}'"))
    if uncertain and known and not dest_marked and not returning \
            and not _in_utterance(t.destination_text, utterance):
        # 자재는 원문(또는 '그거' + 세션 맥락)이 가리켰고 목적지만 없다 — 자재는 남기고 목적지만 묻는다.
        m, ref = materials[known.resource_id], known
        here, home = m["location"], m["origin"]
        where = (f"지금 원래 자리인 {loc_name(home)}에 있습니다" if here and here == home
                 else f"지금 {loc_name(here)}, 원래 자리는 {loc_name(home)}")
        return ask(f"{m['name']}을(를) 어디로 옮길까요? ({where})", keep=_draft_with(ref, []))
    if interp.action == "unknown" and not answers_draft:
        return ask(f"무엇을 어디로 옮길지 알 수 없습니다 — 자재와 목적지를 말해 주세요: {choices}")
    if interp.confidence is None or interp.confidence < min_confidence:
        return ask(f"해석 확신이 낮습니다({(interp.confidence or 0):.2f} < {min_confidence:.2f})"
                   f" — 어느 자재를 어디로 옮길지 말해 주세요: {choices}")

    # ── 목적지 표현(자재보다 먼저 읽어 두고, 자재가 모자랄 때 draft로 남긴다) ─────────────────
    def place_ref(mention: PlaceMention):
        return {"kind": mention.kind, "key": mention.key, "text": mention.surface}

    marked = []
    for p in dest_marked:
        ref = place_ref(p)
        if ref not in marked:
            marked.append(ref)
    dtext = t.destination_text if _in_utterance(t.destination_text, utterance) else None
    if dtext and not marked:
        # 조사가 없는(또는 모델이 조사를 뺀) 목적지: 모델이 적은 표현 안의 등록 표현을 쓴다.
        inside = [p for p in places if p.role != "source"
                  and normalize(dtext).find(u[p.start:p.end]) >= 0]
        for p in inside:
            ref = place_ref(p)
            if ref not in [m for m in marked]:
                marked.append(ref)
    bare = [p for p in places if p.role is None]
    if not marked and len(bare) == 1 and not any(p.role == "source" for p in places):
        # 2026-10-08 생략 발화('파란색 벨트', '씨자재 컨베이어'): 조사 없는 장소 표현이 하나뿐이면 목적지로 본다.
        # 그 자재가 이미 거기 있으면(출발지일 수도 있다) 아래에서 목적지로 쓰지 않고 되묻는다.
        marked = [dict(place_ref(bare[0]), bare=True)]
    if not marked and returning and "origin" in symbols:
        # 2026-10-08 복귀 동사('다시 돌려놔'): 목적지 장소 표현이 따로 없을 때만 그 자재의 원래 자리(등록 정보)로 읽는다.
        marked = [{"kind": "symbol", "key": "origin", "text": returning, "implied": True}]
    if not marked and draft and draft.get("destination") and not dtext:
        marked = [dict(draft["destination"], from_draft=True)]

    # ── 자재 ───────────────────────────────────────────────────────────────────
    mtext = t.material_text if _in_utterance(t.material_text, utterance) else None
    material_ref = None
    if named:
        mid = named[0]
        by_text = _names(facts["materials"], mtext) if mtext else []
        if (t.material_id not in (None, mid)) or (by_text and by_text != [mid]):
            return ask(f"자재 해석이 엇갈립니다(원문: {materials[mid]['name']}) — 자재를 다시 말해 주세요: {choices}")
        surface = (mtext if mtext and by_text == [mid]
                   else _material_surface(materials[mid], u, facts, utterance, readings))
        material_ref = ResolvedRef(mid, Evidence(UTTERANCE, surface))
    elif pointing or (mtext and any(t_ in normalize(mtext) for t_ in terms.get("context") or ())):
        if not context_candidates:
            return ask(f"'{pointing or mtext}'이(가) 어느 자재인지 알 수 없습니다 — 이 대화에서 앞서 옮기거나"
                       f" 확인한 자재가 없습니다. 자재 이름이나 색으로 말해 주세요: {choices}",
                       keep=_draft_with(None, marked))
        why, mid = context_candidates[0]
        if t.material_id not in (None, mid):
            return ask(f"'{pointing or mtext}'이(가) 가리키는 자재가 엇갈립니다(맥락: {materials[mid]['name']},"
                       f" 해석: {materials[t.material_id]['name']}) — 자재 이름으로 말해 주세요")
        material_ref = ResolvedRef(mid, Evidence(
            CONTEXT, f"{why}({materials[mid]['name']}) ← '{pointing or mtext}'"))
    elif len(source_locs) == 1 and not _leftover(mtext, facts, terms):
        # 2026-10-08 위치 묘사('3번 팔레트 위에 놓여 있는 물체'): 이름·색 없이 출발 위치만 말했다 — 그 위치에
        # **기록된** 자재가 하나뿐일 때만 그 자재다(상태 근거). 없거나 여럿이면 추측하지 않고 묻는다.
        src = source_locs[0]
        surface = next(p.surface for p in places if p.role == "source" and p.key == src)
        if facts.get("state_error"):
            return ask(f"{loc_name(src)}에 어떤 자재가 있는지 확인할 수 없습니다({facts['state_error']})"
                       f" — 자재 이름이나 색으로 말해 주세요: {choices}", keep=_draft_with(None, marked))
        holders = [m for m in facts["materials"] if m["location"] == src]
        if len(holders) != 1:
            said = ("기록된 자재가 없습니다" if not holders
                    else f"자재가 여럿 있습니다({', '.join(m['name'] for m in holders)})")
            return ask(f"{loc_name(src)}에 {said} — 옮길 자재를 이름이나 색으로 말해 주세요: {choices}",
                       keep=_draft_with(None, marked))
        mid = holders[0]["id"]
        if t.material_id not in (None, mid):
            return ask(f"자재 해석이 엇갈립니다({loc_name(src)}에 기록된 자재: {materials[mid]['name']},"
                       f" 해석: {materials[t.material_id]['name']}) — 자재 이름으로 말해 주세요: {choices}")
        material_ref = ResolvedRef(mid, Evidence(
            STATE, f"{loc_name(src)}에 기록된 자재({materials[mid]['name']}) ← '{surface}{_rest_word(mtext, facts, terms)}'"))
    elif mtext:
        core = _strip_markers(mtext, terms)
        # 긴 말부터 뺀다('이거'를 '거'보다 먼저 — 아니면 '이'가 남아 없는 자재로 막혔다).
        for g in sorted({*(terms.get("generic") or ()), *(terms.get("filler") or ()), *(terms.get("pointing") or ())},
                        key=len, reverse=True):
            core = core.replace(g, "")
        if core and any(core == normalize(r) for rs in (terms.get("code_readings") or {}).values() for r in rs):
            # 코드 한글 읽기('비')인데 따로 떨어진 낱말이 아니다('비가 오네') — 없는 자재로 막지도, 그 자재로 읽지도 않는다.
            return ask(f"'{mtext}'이(가) 자재를 말한 것인지 확인할 수 없습니다 — 옮길 자재와 목적지를 말해 주세요: {choices}",
                       keep={})
        if not core:
            return ask(f"'{mtext}'이(가) 어느 자재인지 알 수 없습니다 — 자재 이름이나 색으로 말해 주세요:"
                       f" {choices}", keep=_draft_with(None, marked))
        return block(f"'{mtext}'은(는) 등록된 자재가 아닙니다 — 실행하지 않습니다"
                     f" (등록된 자재: {', '.join(m['name'] for m in facts['materials'])})",
                     code=ReasonCode.PLAN_UNKNOWN_RESOURCE)
    elif answers_draft and draft and draft.get("material"):
        mid = draft["material"]["resource_id"]
        material_ref = ResolvedRef(mid, Evidence(
            CONTEXT, f"앞 질문에서 확인한 자재({materials[mid]['name']}) ← {draft['material']['evidence']['text']}"))
    elif t.material_id is not None:
        return ask(f"자재를 가리킨 표현을 발화에서 확인할 수 없습니다 — 자재 이름이나 색으로 말해 주세요: {choices}")
    else:
        what = ", ".join(_describe(m, locations, materials) for m in marked) or "어디로"
        return ask(f"무엇을 {what} 옮길까요? 자재 이름이나 색으로 말해 주세요: {choices}",
                   keep=_draft_with(None, marked))
    material = materials[material_ref.resource_id]
    name = material["name"]

    # ── 목적지 해소 ──────────────────────────────────────────────────────────────
    resolved = []
    for ref in marked:
        out = _resolve_destination(ref, material, facts, locations, symbols, utterance)
        if isinstance(out, IntentDecision):
            return out
        if out.resource_id not in [r.resource_id for r in resolved]:
            resolved.append(out)
    if len(resolved) > 1:
        return ask(f"목적지가 둘 이상입니다({', '.join(loc_name(r.resource_id) for r in resolved)})"
                   " — 하나만 말해 주세요", code=ReasonCode.PLAN_AMBIGUOUS,
                   keep=_draft_with(material_ref, []))
    if not resolved:
        if dtext:
            dloc, dcount = _unique_location(facts, dtext)
            detail = "여러 위치를 가리킵니다" if dcount > 1 else "등록된 위치가 아닙니다"
            return ask(f"'{dtext}'은(는) {detail} — 목적지를 다시 말해 주세요"
                       f" (위치: {', '.join(loc['name'] for loc in facts['locations'])})",
                       keep=_draft_with(material_ref, []))
        here, home = material["location"], material["origin"]
        where = (f"지금 원래 자리인 {loc_name(home)}에 있습니다" if here and here == home
                 else f"지금 {loc_name(here)}, 원래 자리는 {loc_name(home)}")
        return ask(f"{name}을(를) 어디로 옮길까요? ({where})", keep=_draft_with(material_ref, []))
    dest_ref = resolved[0]
    if marked[0].get("bare") and marked[0]["kind"] == "location" and dest_ref.resource_id == material["location"]:
        # 조사 없는 **위치**는 출발지일 수도 있다. '원위치'처럼 상징 표현은 출발지가 될 수 없어 아래 noop으로 간다.
        return ask(f"{name}이(가) 지금 {loc_name(material['location'])}에 있습니다 — 어디로 옮길까요?"
                   " (목적지에 '로/에'를 붙여 말해 주세요)", keep=_draft_with(material_ref, []))
    model_dest = t.destination
    if model_dest in locations and model_dest != dest_ref.resource_id and not marked[0].get("from_draft") \
            and not (corrected_to and any(corrected_to in normalize(t_.destination_text or "")
                                          for t_ in getattr(interp, "tasks", ()) or ())):
        # 목적지를 원문 정정('컨베이어로, 아니 원래 자리에')으로 골랐고 모델도 고친 뒤 표현을 읽었으면(목적지 표현에 있음)
        # 모델 id가 고치기 전 것이어도 엇갈림이 아니다. 모델이 고친 표현을 아예 못 읽었으면 되묻는다.
        return ask(f"목적지 해석이 엇갈립니다(원문: {loc_name(dest_ref.resource_id)}, 해석: {loc_name(model_dest)})"
                   " — 목적지를 다시 말해 주세요", keep=_draft_with(material_ref, []))

    # ── 출발지 ─────────────────────────────────────────────────────────────────
    current = material["location"]
    stated, stated_text = None, None
    source_marked = [p for p in places if p.role == "source" and p.kind == "location"]
    if len({p.key for p in source_marked}) > 1:
        return ask("출발지가 둘 이상입니다 — 하나만 말하거나 빼고 말해 주세요", code=ReasonCode.PLAN_AMBIGUOUS)
    if source_marked:
        stated, stated_text = source_marked[0].key, source_marked[0].surface
    else:
        stext = t.source_text if _in_utterance(t.source_text, utterance) else None
        bare_dest = [m_ for m_ in marked if m_.get("bare")]
        in_stext = [p.key for p in places if stext and normalize(stext).find(u[p.start:p.end]) >= 0]
        if stext and bare_dest and in_stext and set(in_stext) == {bare_dest[0]["key"]}:
            # 2026-10-08 '초록 컨베이어': 모델이 조사 없는 장소를 출발지로 적었다. 서버는 그 장소를 목적지로 읽었고,
            # 자재가 이미 거기 있으면 위에서 되물었다 — 같은 표현을 출발지로 또 쓰지 않는다.
            stext = None
        if stext and not _overlaps(stext, dtext) and not any(
                normalize(stext).find(u[p.start:p.end]) >= 0 for p in dest_marked):
            named_locs = [p.key for p in places if normalize(stext).find(u[p.start:p.end]) >= 0
                          and p.kind == "location"]
            core = _strip_markers(stext, terms)
            category = sorted({rid for word, ids in category_words(facts).items()
                               if word in core for rid in ids})
            followed = _marker_role(u[u.find(normalize(stext)) + len(normalize(stext)):], terms)[0]
            ends_source = _marker_role(normalize(stext)[len(core):], terms)[0] == "source"
            if len(set(named_locs)) == 1:
                stated, stated_text = named_locs[0], stext
            elif len(set(named_locs)) > 1:
                return ask(f"'{stext}'이(가) 어느 위치인지 알 수 없습니다 — 출발지를 다시 말하거나 빼고 말해 주세요")
            elif category:
                if current is not None and current not in category:
                    return block(f"말한 출발지('{stext}')에 {name}이(가) 없습니다(기록: {loc_name(current)})"
                                 " — 실행하지 않습니다")
                if current is not None:
                    stated, stated_text = current, stext
            elif followed == "source" or ends_source:
                return ask(f"'{stext}'이(가) 어느 위치인지 알 수 없습니다 — 출발지를 다시 말하거나 빼고 말해 주세요")
            # 위치 이름도 출발 조사도 없는 말('저기 있는 그')은 출발지를 말한 것이 아니다.
    if current is None:
        why = facts.get("state_error") or "자재 상태 기록에 위치가 없습니다"
        return ask(f"{name}의 현재 위치를 확인할 수 없습니다({why}) — 자재 상태를 정리한 뒤 다시 요청해 주세요")
    if stated is not None and stated != current:
        return block(f"말한 출발지({loc_name(stated)})와 {name}의 실제 위치"
                     f"({loc_name(current)}, 기록)가 다르다 — 실행하지 않는다")
    source_ref = (ResolvedRef(current, Evidence(UTTERANCE, stated_text or loc_name(current))) if stated is not None
                  else ResolvedRef(current, Evidence(STATE, f"{name}의 현재 위치(기록)")))

    # ── 이미 목적지에 있음 · 목적지 점유 ──────────────────────────────────────────────
    if dest_ref.resource_id == source_ref.resource_id:
        where = loc_name(current)
        said = "원래 자리" if dest_ref.evidence.kind == REGISTRY else where
        # 기록상 이미 목적지다. 관측으로 같은지는 호출자(Api)가 본다(리뷰 11번 — 기록만으로 위치를 확정하지 않는다).
        info["noop_at"] = {"model": material.get("model"), "location": current}
        return IntentDecision(
            "noop", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED, interpretation=info, draft={},
            detail=f"{name}은(는) 이미 {said}({where})에 있습니다 — 옮길 필요가 없어 실행하지 않습니다",
            clarification=f"{name}은(는) 이미 {said}({where})에 있습니다 — 옮길 필요가 없어 실행하지 않습니다")
    if dest_ref.resource_id != _CONVEYOR:
        holder = next((m for m in facts["materials"]
                       if m["id"] != material["id"] and m["location"] == dest_ref.resource_id), None)
        if holder is not None:
            said = "원래 자리" if dest_ref.evidence.kind == REGISTRY else "목적지"
            return block(f"{said}({loc_name(dest_ref.resource_id)})에 {holder['name']}이(가) 있습니다(기록)"
                         " — 먼저 비워야 합니다. 실행하지 않습니다")
    if corrections:
        # 정정을 근거에 남긴다 — 확인 카드에서 사용자가 고친 결과를 보고 승인한다.
        note = " · ".join(corrections)
        if any(c.startswith("자재") for c in corrections):
            material_ref = ResolvedRef(material_ref.resource_id, Evidence(material_ref.evidence.kind,
                                       f"{material_ref.evidence.text} ({note})"))
        if any(c.startswith("목적지") for c in corrections):
            dest_ref = ResolvedRef(dest_ref.resource_id, Evidence(dest_ref.evidence.kind,
                                   f"{dest_ref.evidence.text} ({note})"))
        info["corrections"] = corrections
    intent = TaskIntent(action=TRANSFER, material=material_ref, source=source_ref,
                        destination=dest_ref, interpreter=str(interp.model_id or ""),
                        confidence=interp.confidence, model_reason=interp.model_reason)
    return IntentDecision("intent", intent=intent, interpretation=info, draft={})


def _draft_with(material_ref: ResolvedRef | None, marked) -> dict:
    """되묻기에서 세션에 남길 확인된 값. 목적지는 원문 장소 표현(상징 포함) 그대로 남긴다."""
    out: dict = {}
    if material_ref is not None:
        out["material"] = {"resource_id": material_ref.resource_id,
                           "evidence": {"kind": material_ref.evidence.kind,
                                        "text": material_ref.evidence.text}}
    if len(marked) == 1:
        out["destination"] = {k: marked[0][k] for k in ("kind", "key", "text")}
    return out


def _describe(ref, locations, materials) -> str:
    if ref["kind"] == "location":
        return f"{locations[ref['key']]['name']}(으)로"
    return {"origin": "원래 자리로", "free_slot": "빈자리로"}.get(ref["key"], "")


def _resolve_destination(ref, material, facts, locations, symbols, utterance):
    """원문 장소 표현(위치·상징) → 목적지 근거. 실패는 IntentDecision(되묻기·차단)."""
    from_draft = bool(ref.get("from_draft"))
    kind = CONTEXT if from_draft else UTTERANCE
    note = (lambda text: f"앞 질문의 답 '{text}'") if from_draft else (lambda text: text)
    if ref["kind"] == "location":
        return ResolvedRef(ref["key"], Evidence(kind, note(ref["text"])))
    if ref["key"] == "origin":
        origin = material["origin"]
        if origin is None:
            return IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                  detail=f"{material['name']}의 원래 자리가 등록돼 있지 않습니다 — 위치를 말해 주세요",
                                  clarification=f"{material['name']}의 원래 자리가 등록돼 있지 않습니다 — 위치를 말해 주세요")
        return ResolvedRef(origin, Evidence(
            REGISTRY, f"{material['name']}의 원래 자리(등록: {locations[origin]['name']}) ← '{ref['text']}'"))
    if ref["key"] == "free_slot":
        need = (symbols.get("free_slot") or {}).get("requires_location")
        if not need or need not in locations:
            text = "빈자리가 어느 위치의 칸인지 등록돼 있지 않습니다 — 목적지를 위치 이름으로 말해 주세요"
            return IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                  detail=text, clarification=text)
        if need not in _names(facts["locations"], utterance) and not from_draft:
            text = ("어느 빈자리인지 알 수 없습니다 — 일반 모드의 빈자리는 컨베이어 빈 칸입니다."
                    " '컨베이어 빈자리로'처럼 말해 주세요")
            return IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED,
                                  detail=text, clarification=text)
        free = list(facts.get("free_conveyor_slots") or ())
        if not free:
            return IntentDecision("block", reason_code=ReasonCode.EXEC_SIM_TARGET_OCCUPIED,
                                  detail="컨베이어에 빈 칸이 없습니다(기록) — 실행하지 않습니다", draft={})
        return ResolvedRef(need, Evidence(
            STATE, f"컨베이어 빈 칸 {len(free)}개(기록, 칸은 실행 전 검사가 고른다) ← '{ref['text']}'"))
    text = "목적지를 알 수 없습니다 — 다시 말해 주세요"
    return IntentDecision("ask", reason_code=ReasonCode.PLAN_CLARIFICATION_REQUIRED, detail=text,
                          clarification=text)


def _unique_location(facts, text: str | None) -> tuple[str | None, int]:
    named = _names(facts["locations"], text or "")
    return (named[0] if len(named) == 1 else None), len(named)


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
