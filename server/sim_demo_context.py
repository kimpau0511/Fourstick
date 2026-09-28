"""생략·지시·맥락 표현 해석 — 대화 맥락 · 관측 상태 · 기억된 환경만 근거로 쓴다.

"파란 거 2번으로", "그다음 그거 제자리로", "빈 칸에 옮겨줘" 같은 말을 **파싱 전에**
셀이 선언한 이름("B자재", "컨베이어 2번 칸", "원래 자리")으로 바꾼다. 바꾼 문장은
기존 해석·계획·확인 카드·실행·관측 검증 경로를 그대로 탄다.

원칙:

- **하나로 정해질 때만** 바꾼다. 근거(`evidence`)를 남기고, 바꾼 명령은 항상 확인
  카드를 거친다(즉시 실행하지 않는다).
- 둘 이상이면 고르지 않고 **짧게 되묻는다**. 물은 내용은 기억해 두었다가 다음 말
  ("3번", "B자재", "칸")로 채운다(`pending`, 2분).
- 지원하지 않는 동작(쌓기·뒤집기·밀기·회전·든 채 대기 …)은 가능한 동작으로 바꾸지
  않고 막는다.
- 음성 확신도가 낮거나 칸 번호를 숫자 낱말("이번", "삼 번")로 들었으면 되묻는다.
- LLM을 쓰지 않는다. 좌표·관절값은 어디에도 없다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 음성 전사 확신도(exp 평균 logprob)가 이보다 낮으면 되묻는다.
STT_MIN_CONFIDENCE = 0.55
FOCUS_TTL_SEC = 600.0
PENDING_TTL_SEC = 120.0

UNSUPPORTED = (
    (re.compile(r"쌓"), "쌓기"), (re.compile(r"뒤집"), "뒤집기"),
    (re.compile(r"던지|던져"), "던지기"), (re.compile(r"밀어|밀기|밀고"), "밀기"),
    (re.compile(r"굴려"), "굴리기"), (re.compile(r"\d+\s*도\s*(?:돌|회전)|회전"), "회전"),
    (re.compile(r"기울"), "기울이기"), (re.compile(r"세워|눕혀"), "세우기·눕히기"),
    (re.compile(r"흔들"), "흔들기"), (re.compile(r"끼워|붙여|조립"), "끼우기·붙이기"),
    (re.compile(r"(?:들고|잡고|쥐고)(?:서)?\s*(?:기다|대기|있어|멈춰)"), "든 채 대기"),
    (re.compile(r"사이에"), "칸 사이에 놓기"),
)
POSITION_WORDS = ("맨앞", "맨뒤", "첫칸", "첫번째칸", "끝칸", "마지막칸", "가운데칸",
                  "중간칸", "왼쪽칸", "오른쪽칸")
NUMBER_WORDS = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5}
_NUMBER_WORD = re.compile(r"(?<![가-힣])(일|이|삼|사|오)\s*번(?:\s*(?:칸|위치|자리|슬롯))?"
                          r"(?=\s*(?:에|으로|로|칸|$|\s))")
PRONOUNS = ("방금옮긴거", "아까옮긴거", "방금그거", "아까그거", "그거", "그것", "저거",
            "저것", "이거", "이것")
ALSO_PRONOUNS = ("그것도", "저것도", "이것도", "그거도", "저거도", "이거도")
MOVE_VERBS = ("옮겨", "옮기", "놔", "놓", "올려", "갖다", "가져다", "돌려", "보내", "넣어")
RETURN_WORDS = ("제자리", "원래자리", "원래위치", "원위치", "원래대로", "돌려놔", "복귀")
ALL_WORDS = ("전부", "모두", "모든", "다")
CANCEL_WORDS = ("취소", "그만", "됐어", "안해", "하지마")
#: "원상복귀" — 모든 자재를 각자의 원래 팔레트로. 직전 작업 하나만 되돌리라는 뜻일
#: 수 있는 문맥(아래 표지나 이 세션의 최근 작업)이면 짧게 되묻는다.
RESTORE_WORDS = ("원상복귀", "원상복구", "원상회복", "원상태로")
UNDO_MARKERS = ("방금", "아까", "마지막", "직전", "그거", "그것", "이거", "하나만")
_LETTER = re.compile(r"(?<![A-Za-z0-9가-힣])([A-Ca-c])(?!\s*자재)(?=\s*(?:는|를|은|을|가|이|도|만|랑|"
                     r"하고|와|과|의|,|$|\s))")


@dataclass
class DialogueContext:
    """한 작업 셀의 짧은 대화 맥락. 서버 메모리에만 있고 오래되면 버린다."""

    clock: Callable[[], float] = time.time
    focus: tuple[str, ...] = ()
    focus_at: float = 0.0
    pending: dict | None = None

    def current_focus(self) -> tuple[str, ...]:
        if self.focus and self.clock() - self.focus_at <= FOCUS_TTL_SEC:
            return self.focus
        return ()

    @property
    def focus_expired(self) -> bool:
        return bool(self.focus) and self.clock() - self.focus_at > FOCUS_TTL_SEC

    def set_focus(self, materials: Sequence[str]) -> None:
        materials = tuple(dict.fromkeys(m for m in materials if m))
        if materials:
            self.focus, self.focus_at = materials, self.clock()

    def current_pending(self) -> dict | None:
        if self.pending and self.clock() - self.pending["at"] <= PENDING_TTL_SEC:
            return self.pending
        self.pending = None
        return None

    def ask(self, template: str, hole: str, options: Mapping[str, str]) -> None:
        self.pending = {"template": template, "hole": hole, "options": dict(options),
                        "at": self.clock()}

    def clear_pending(self) -> None:
        self.pending = None


class DialogueContexts:
    """세션별 대화 맥락. **인증이 아니다** — 브라우저 세션 id로 맥락을 나눌 뿐이다.

    세션 id는 서버가 발급한 것(`/v1/sessions`)만 받는다(호출자가 확인). 같은 세션으로
    다시 연결하면 맥락이 이어지고, 새 세션은 빈 맥락으로 시작한다. 오래 쓰지 않은
    세션부터 버린다(최대 `limit`).
    """

    def __init__(self, *, clock: Callable[[], float] = time.time, limit: int = 64):
        self.clock = clock
        self.limit = limit
        self._by_session: dict[str, DialogueContext] = {}
        self._used: dict[str, float] = {}

    def get(self, session_id: str | None) -> DialogueContext | None:
        if not session_id:
            return None
        context = self._by_session.get(session_id)
        if context is None:
            if len(self._by_session) >= self.limit:
                oldest = min(self._used, key=self._used.get)
                self.forget(oldest)
            context = self._by_session[session_id] = DialogueContext(clock=self.clock)
        self._used[session_id] = self.clock()
        return context

    def forget(self, session_id: str) -> None:
        self._by_session.pop(session_id, None)
        self._used.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._by_session)


@dataclass
class Resolution:
    text: str
    evidence: list[str] = field(default_factory=list)
    decision: str | None = None       # None | "ASK" | "BLOCK"
    reason: str = ""

    @property
    def applied(self) -> bool:
        return bool(self.evidence)

    def to_dict(self) -> dict:
        return {"resolved_utterance": self.text if self.applied else None,
                "evidence": list(self.evidence)}


# ── 셀 사실(선언·기록·환경) ────────────────────────────────────────
@dataclass(frozen=True)
class CellFacts:
    names: Mapping[str, str]            # model → "A자재"
    aliases: Mapping[str, tuple[str, ...]]   # model → 정규화된 호칭들
    origin_pallet_no: Mapping[str, str]  # model → "1"(원래 팔레트 번호)
    slots: tuple[str, ...]               # 검증된 칸(순서)
    location: Mapping[str, str]          # model → "origin" | slot
    blocked: frozenset[str] = frozenset()
    unavailable: frozenset[str] = frozenset()

    def on_conveyor(self) -> list[str]:
        return [m for m in self.names
                if str(self.location.get(m, "origin")).startswith("slot_")]

    def at_origin(self) -> list[str]:
        return [m for m in self.names if self.location.get(m, "origin") == "origin"
                and m not in self.unavailable]

    def holder(self, slot: str) -> str | None:
        return next((m for m, where in self.location.items() if where == slot), None)

    def free_slots(self) -> list[str]:
        return [s for s in self.slots if self.holder(s) is None and s not in self.blocked]


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _slot_word(slot: str) -> str:
    return f"컨베이어 {slot.split('_')[-1]}번 칸"


def _mentioned(text: str, facts: CellFacts) -> list[str]:
    compact = _norm(text)
    hits = []
    for model, names in facts.aliases.items():
        at = [compact.find(n) for n in names if n and n in compact]
        if at:
            hits.append((min(at), model))
    return [m for _, m in sorted(hits)]


def _join(names: Sequence[str]) -> str:
    return ", ".join(names)


# ── 해석 ───────────────────────────────────────────────────────────
def resolve(text: str, *, facts: CellFacts, context: DialogueContext | None,
            source: str = "text", stt_confidence: float | None = None) -> Resolution:
    res = Resolution(text=str(text or "").strip())
    compact = _norm(res.text)
    ctx = context

    # 0. 기다리던 질문에 대한 답 / 취소
    pending = ctx.current_pending() if ctx else None
    if pending is not None:
        if any(w in compact for w in CANCEL_WORDS) and len(compact) <= 12:
            ctx.clear_pending()
            return Resolution(res.text, decision="ASK",
                              reason="이전 질문을 취소했습니다 — 작업을 만들지 않았습니다")
        filled = _answer_pending(res.text, pending, facts)
        if filled is not None:
            ctx.clear_pending()
            inner = resolve(filled, facts=facts, context=ctx, source=source,
                            stt_confidence=None)
            inner.evidence.insert(0, f"직전 질문에 대한 답 '{res.text}'으로 완성: '{filled}'")
            return inner
        if not any(v in compact for v in MOVE_VERBS):
            # 짧은 답인데 선택지와 맞지 않는다 — 다시 묻는다(고르지 않는다).
            if len(compact) <= 12:
                return Resolution(res.text, decision="ASK",
                                  reason="선택지에 없는 답입니다 — " + _options_text(pending))
        ctx.clear_pending()   # 새 명령으로 본다

    # 1. 음성 불확실
    if source == "stt_final":
        if stt_confidence is not None and float(stt_confidence) < STT_MIN_CONFIDENCE:
            return Resolution(res.text, decision="ASK",
                              reason=f"잘 알아듣지 못했습니다('{res.text}') — 자재와 위치를 다시"
                                     " 말해 주세요")
    if _NUMBER_WORD.search(res.text) and re.search(r"번", res.text):
        return Resolution(res.text, decision="ASK",
                          reason="칸 번호를 확실히 듣지 못했습니다 — '컨베이어 2번 칸'처럼 숫자로"
                                 " 말해 주세요")

    # 2. 지원하지 않는 동작 — 가능한 동작으로 바꾸지 않는다
    for pattern, name in UNSUPPORTED:
        if pattern.search(res.text):
            return Resolution(res.text, decision="BLOCK",
                              reason=f"'{name}'은 지원하지 않는 동작입니다 — 지원하는 것은 자재를"
                                     " 원래 자리·컨베이어 칸으로 옮기는 것뿐입니다")
    if any(w in compact for w in POSITION_WORDS):
        return Resolution(res.text, decision="ASK",
                          reason="'맨 앞' 같은 위치는 칸 번호로 정해져 있지 않습니다 — 1·2·3번 칸 중"
                                 " 하나로 말해 주세요")

    # 2-1. "원상복귀" — 전체 복귀 목표, 또는 되묻기
    restored = _restore(res, facts, ctx)
    if restored is not None:
        return restored

    # 3. 글자 호칭 "A는", "B를" → "A자재"
    def letter(match: re.Match) -> str:
        return f"{match.group(1).upper()}자재"
    swapped = _LETTER.sub(letter, res.text)
    if swapped != res.text:
        res.evidence.append(f"글자 호칭을 자재 이름으로: '{res.text}' → '{swapped}'")
        res.text = swapped

    # 4~8. 맞바꾸기 · 위치 지칭 · 대명사 · 빈 칸 · 맨 번호(처음 걸린 것 하나)
    for step in (lambda r: _swap(r, facts), lambda r: _by_location(r, facts),
                 lambda r: _pronoun(r, facts, ctx), lambda r: _free_slot(r, facts, ctx),
                 lambda r: _bare_number(r, facts, ctx)):
        out = step(res)
        if out is not None:
            if out.decision:
                return out
            res = out
            break
    return _final_checks(res, facts, ctx)


def _final_checks(res: Resolution, facts: CellFacts, ctx) -> Resolution:
    """해석을 마친 문장에 목적지·자재가 빠졌으면 묻는다."""
    # 9. 해석했는데 목적지가 없다 — "그거 다시 가져다 놔"
    compact = _norm(res.text)
    if (res.applied and _mentioned(res.text, facts) and any(v in compact for v in MOVE_VERBS)
            and not re.search(r"컨베이어|\d+번|원래자리|원래위치|제자리|원위치|돌려놔|팔레트|"
                              r"비워|칸|자리로|위치로", compact)):
        return Resolution(res.text, evidence=res.evidence, decision="ASK",
                          reason="어디로 옮길까요? (원래 자리 / 컨베이어 1·2·3번 칸)")
    # 10. 옮기라는데 자재가 없다
    if (not _mentioned(res.text, facts) and any(v in compact for v in MOVE_VERBS)
            and not any(w in compact for w in ALL_WORDS)
            and re.search(r"컨베이어|\d+번칸|원래자리|제자리", compact)
            and res.applied):
        return _ask_material(res, facts, ctx, facts.names.keys(), "어느 자재를 옮길까요?")
    return res


def _restore_all_text(facts: CellFacts) -> str:
    """모든 자재를 각자의 원래 자리로 — 자재마다 목표를 적은 문장(계획기가 순서를 정한다)."""
    return ", ".join(f"{name}는 원래 자리로" for name in facts.names.values()) + " 돌려놔"


def _restore(res: Resolution, facts: CellFacts, ctx) -> Resolution | None:
    compact = _norm(res.text)
    if not any(w in compact for w in RESTORE_WORDS):
        return None
    everything = _restore_all_text(facts)
    away = [m for m, where in facts.location.items() if where != "origin"]
    focus = [m for m in (ctx.current_focus() if ctx else ()) if m in facts.names]
    says_all = any(w in compact for w in ("전부", "모두", "모든", "전체"))
    says_undo = any(w in compact for w in UNDO_MARKERS)
    # 직전 작업 하나만 되돌리라는 뜻일 수 있다: 되돌림 표지가 있거나, 이 세션에서 방금
    # 다룬 자재가 있고 그 밖의 자재도 원래 자리에 없다(둘의 결과가 다르다).
    ambiguous = says_undo or (not says_all and focus and any(m not in focus for m in away))
    if away and ambiguous:
        choices = focus or away
        label = _join([facts.names[m] for m in choices])
        options = {"all": everything}
        options.update({m: f"{facts.names[m]}를 원래 자리로 돌려놔" for m in choices})
        if ctx is not None:
            ctx.ask(everything, "restore", options)
        return Resolution(res.text, evidence=res.evidence, decision="ASK",
                          reason=f"방금 다룬 {label}만 원래 자리로 돌릴까요, 모든 자재를 원래"
                                 " 자리로 돌릴까요? ('전부' 또는 '"
                                 + f"{facts.names[choices[0]]}만'으로 답해 주세요)")
    res.evidence.append(f"'{res.text}' = 모든 자재를 각자의 원래 자리(팔레트)로"
                        + ("" if focus else " — 이 대화에 직전 작업이 없다"))
    res.text = everything
    return res


def _options_text(pending: Mapping[str, Any]) -> str:
    if pending.get("hole") == "restore":
        names = [text.split("를 ")[0] + "만" for key, text in pending["options"].items()
                 if key != "all"]
        return "가능한 답: " + ", ".join(["전부", *names])
    return "가능한 답: " + ", ".join(pending["options"])


def _answer_pending(answer: str, pending: Mapping[str, Any], facts: CellFacts) -> str | None:
    compact = _norm(answer)
    options = pending["options"]
    hole = pending["hole"]
    template = pending["template"]
    if hole == "slot":
        match = re.search(r"(\d+)번", compact) or re.fullmatch(r"(\d+)", compact)
        if match and f"slot_{match.group(1)}" in options:
            return template.replace("{slot}", _slot_word(f"slot_{match.group(1)}"))
    elif hole == "material":
        answer_fixed = _LETTER.sub(lambda m: f"{m.group(1).upper()}자재", answer)
        if re.fullmatch(r"[a-cA-C]", compact):
            answer_fixed = f"{compact.upper()}자재"
        chosen = [m for m in _mentioned(answer_fixed, facts) if m in options]
        if len(chosen) == 1:
            return template.replace("{material}", facts.names[chosen[0]])
    elif hole == "restore":
        if any(w in compact for w in ("전부", "모두", "모든", "전체")) or compact in ("다", "다요"):
            return options.get("all")
        answer_fixed = _LETTER.sub(lambda m: f"{m.group(1).upper()}자재", answer)
        if re.fullmatch(r"[a-cA-C](?:만)?", compact):
            answer_fixed = f"{compact[0].upper()}자재"
        chosen = [m for m in _mentioned(answer_fixed, facts) if m in options]
        if len(chosen) == 1:
            return options[chosen[0]]
    elif hole == "number_kind":
        if re.search(r"칸|컨베이어", compact) and not re.search(r"칸말고|컨베이어말고", compact):
            return options.get("칸")
        if re.search(r"팔레트|원래|제자리", compact):
            return options.get("팔레트")
    return None


def _ask_material(res: Resolution, facts: CellFacts, ctx, candidates, question: str,
                  template: str | None = None) -> Resolution:
    names = [facts.names[m] for m in candidates]
    if ctx is not None and template is not None:
        ctx.ask(template, "material", {m: facts.names[m] for m in candidates})
    return Resolution(res.text, evidence=res.evidence, decision="ASK",
                      reason=f"{question} ({_join(names)})")


def _swap(res: Resolution, facts: CellFacts) -> Resolution | None:
    compact = _norm(res.text)
    if not re.search(r"(?:자리|위치)(?:를|을)?(?:서로)?(?:좀)?(?:맞)?(?:바꿔|바꾸|교환)", compact):
        return None
    pair = _mentioned(res.text, facts)
    if len(pair) != 2:
        return None
    a, b = pair
    la, lb = facts.location.get(a, "origin"), facts.location.get(b, "origin")
    if la == "origin" and lb == "origin":
        return Resolution(res.text, evidence=res.evidence, decision="BLOCK",
                          reason="두 자재 모두 원래 팔레트에 있습니다 — 다른 팔레트에 놓는 동작은"
                                 " 지원하지 않아 자리를 바꿀 수 없습니다")
    where = lambda loc: "원래 자리로" if loc == "origin" else f"{_slot_word(loc)}에"  # noqa: E731
    text = f"{facts.names[a]}는 {where(lb)} 놓고 {facts.names[b]}는 {where(la)} 놓아줘"
    ev = [*res.evidence, f"자리 바꾸기: 관측 기록상 {facts.names[a]}={_place(la)},"
                         f" {facts.names[b]}={_place(lb)} → 서로의 위치를 목표로"]
    return Resolution(text, evidence=ev)


def _place(loc: str) -> str:
    return "원래 자리" if loc == "origin" else _slot_word(loc)


def _by_location(res: Resolution, facts: CellFacts) -> Resolution | None:
    text, compact = res.text, _norm(res.text)
    # "컨베이어 비워(줘)"
    if re.search(r"컨베이어(?:를|을|에있는(?:거|것|자재)(?:를|을)?)?(?:다|전부|모두)?(?:비워|치워)",
                 compact):
        on = facts.on_conveyor()
        if not on:
            return Resolution(text, evidence=res.evidence, decision="BLOCK",
                              reason="컨베이어에 자재가 없습니다")
        new = "랑 ".join(facts.names[m] for m in on) + "는 원래 자리로 돌려놔"
        return Resolution(new, evidence=[*res.evidence, "컨베이어 비우기: 관측 기록상 컨베이어에"
                                         f" {_join([facts.names[m] for m in on])}"])
    # "N번 칸 비워"
    match = re.search(r"(\d+)번(?:칸|자리|위치)(?:을|를)?비워", compact)
    if match:
        slot = f"slot_{match.group(1)}"
        holder = facts.holder(slot)
        if holder is None:
            return Resolution(text, evidence=res.evidence, decision="BLOCK",
                              reason=f"{_slot_word(slot)}은 비어 있습니다")
        return Resolution(f"{facts.names[holder]}를 원래 자리로 돌려놔",
                          evidence=[*res.evidence, f"{_slot_word(slot)} 비우기: 관측 기록상"
                                    f" 그 칸의 자재는 {facts.names[holder]}"])
    # "전부 원래대로", "다 제자리로" (자재 이름 없음)
    if (any(w in compact for w in ("전부", "모두", "다")) and "원래대로" in compact
            and not _mentioned(text, facts)):
        on = facts.on_conveyor()
        if not on:
            return Resolution(text, evidence=res.evidence, decision="BLOCK",
                              reason="원래 자리로 돌릴 자재가 없습니다(모두 원래 자리)")
        new = "랑 ".join(facts.names[m] for m in on) + "는 원래 자리로 돌려놔"
        return Resolution(new, evidence=[*res.evidence, "'전부 원래대로': 관측 기록상 컨베이어에"
                                         f" {_join([facts.names[m] for m in on])}"])
    # "N번 칸(에 있는) 거"
    pattern = re.compile(r"(?:컨베이어\s*)?(\d+)\s*번\s*(?:칸|자리|위치)?\s*(?:에\s*있는|의)?\s*"
                         r"(?:거|것|자재|물건|블록)(?!\s*자재)")
    match = pattern.search(text)
    if match and not re.match(r"\s*팔레트", text[match.start(1) + len(match.group(1)) + 1:]):
        slot = f"slot_{match.group(1)}"
        if slot in facts.slots:
            holder = facts.holder(slot)
            if holder is None:
                return Resolution(text, evidence=res.evidence, decision="BLOCK",
                                  reason=f"{_slot_word(slot)}은 비어 있습니다 — 옮길 자재가 없습니다")
            new = text[:match.start()] + facts.names[holder] + text[match.end():]
            return _continue(Resolution(new, evidence=[
                *res.evidence, f"{_slot_word(slot)}의 자재 = {facts.names[holder]}(관측 기록)"]),
                facts)
    # "남은 거"
    if re.search(r"남은(?:거|것|자재)", compact):
        rest = facts.at_origin()
        rest = [m for m in rest if m not in _mentioned(text, facts)]
        if len(rest) == 1:
            new = re.sub(r"남은\s*(?:거|것|자재)", facts.names[rest[0]], text)
            return _continue(Resolution(new, evidence=[
                *res.evidence, f"남은 자재 = {facts.names[rest[0]]}(컨베이어에 없는 자재 하나)"]),
                facts)
        return Resolution(text, evidence=res.evidence, decision="ASK",
                          reason="남은 자재가 여럿입니다 — 어느 자재인가요? ("
                                 + _join([facts.names[m] for m in rest]) + ")")
    return None


def _continue(res: Resolution, facts: CellFacts) -> Resolution:
    """자재를 정한 뒤 목적지(빈 칸·맨 번호)도 풀어 본다."""
    for step in (_free_slot, _bare_number):
        out = step(res, facts, None)
        if out is not None:
            return out
    return res


def _pronoun(res: Resolution, facts: CellFacts, ctx) -> Resolution | None:
    text, compact = res.text, _norm(res.text)
    if any(p in compact for p in ALSO_PRONOUNS):
        return Resolution(text, evidence=res.evidence, decision="ASK",
                          reason="'그것도·저것도'가 어느 자재인지 정할 수 없습니다 — 자재 이름으로"
                                 " 말해 주세요")
    used = next((p for p in PRONOUNS if p in compact), None)
    if used is None or _mentioned(text, facts):
        return None
    pattern = re.compile(r"(?:방금|아까)?\s*(?:옮긴\s*)?(?:그\s*)?(그거|그것|저거|저것|이거|이것|거)")
    returning = any(w in compact for w in RETURN_WORDS)
    focus = ctx.current_focus() if ctx else ()
    if focus:
        if len(focus) == 1:
            model = focus[0]
            new = pattern.sub(facts.names[model], text, count=1)
            return _continue(Resolution(new, evidence=[
                *res.evidence, f"'{used}' = {facts.names[model]}(직전 대화에서 다룬 자재)"]), facts)
        template = pattern.sub("{material}", text, count=1)
        return _ask_material(res, facts, ctx, focus,
                             "직전에 여러 자재를 다뤘습니다 — 어느 자재인가요?", template)
    # 대화 맥락이 없거나 만료됐다 — 관측 상태로 추측하지 않고 묻는다.
    if ctx is None:
        why = "이 대화의 맥락(세션)이 없어"
    elif ctx.focus_expired:
        why = "직전 대화가 오래돼(10분) 맥락이 만료되어"
    else:
        why = "직전 대화 맥락에 다룬 자재가 없어"
    candidates = facts.on_conveyor() if returning else facts.at_origin()
    template = pattern.sub("{material}", text, count=1)
    if ctx is not None and candidates:
        ctx.ask(template, "material", {m: facts.names[m] for m in candidates})
    listing = f" ({_join([facts.names[m] for m in candidates])})" if candidates else ""
    return Resolution(text, evidence=res.evidence, decision="ASK",
                      reason=f"{why} '{used}'가 어느 자재인지 정할 수 없습니다 — 자재 이름으로"
                             f" 말해 주세요{listing}")


def _free_slot(res: Resolution, facts: CellFacts, ctx) -> Resolution | None:
    text = res.text
    if not facts.slots:
        return None     # 칸이 설정되지 않은 셀 — 칸을 추론하지 않는다(기존 경로)
    match = re.search(r"(?:비어\s*있는|빈)\s*(?:칸|자리|위치|슬롯)", text)
    if match is None:
        return None
    mentioned = _mentioned(text, facts)
    free = facts.free_slots()
    if not free:
        return Resolution(text, evidence=res.evidence, decision="BLOCK",
                          reason="비어 있는 컨베이어 칸이 없습니다")
    if len(free) == 1:
        new = text[:match.start()] + _slot_word(free[0]) + text[match.end():]
        why = f"빈 칸 = {_slot_word(free[0])}(관측 기록"
        if facts.blocked:
            why += ", 사용 금지 " + _join([_slot_word(s) for s in sorted(facts.blocked)])
        return Resolution(new, evidence=[*res.evidence, why + ")"])
    if not mentioned:
        return None      # 자재부터 묻는다(아래 단계)
    template = text[:match.start()] + "{slot}" + text[match.end():]
    if ctx is not None:
        ctx.ask(template, "slot", {s: _slot_word(s) for s in free})
    return Resolution(text, evidence=res.evidence, decision="ASK",
                      reason="빈 칸이 " + "·".join(s.split("_")[-1] + "번" for s in free)
                             + " 여러 곳입니다 — 어느 칸에 놓을까요?")


def _bare_number(res: Resolution, facts: CellFacts, ctx) -> Resolution | None:
    text = res.text
    if not facts.slots:
        return None
    match = re.search(r"(?<![\d가-힣])(\d+)\s*번\s*(?=으로|로|에(?!서|있))", text)
    if match is None:
        return None
    before = text[:match.start()]
    after = text[match.end():]
    if re.search(r"(?:컨베이어|팔레트)\s*$", before) or re.match(r"\s*(?:칸|위치|자리|슬롯|팔레트)", after):
        return None
    mentioned = _mentioned(text, facts)
    if len(mentioned) != 1:
        return None
    model, number = mentioned[0], match.group(1)
    slot = f"slot_{number}"
    here = facts.location.get(model, "origin")
    options: dict[str, str] = {}
    if slot in facts.slots and here != slot:
        options["칸"] = text[:match.start()] + _slot_word(slot) + text[match.end():]
    if facts.origin_pallet_no.get(model) == number and here != "origin":
        options["팔레트"] = text[:match.start()] + "원래 자리" + text[match.end():]
    if len(options) == 1:
        kind, new = next(iter(options.items()))
        why = (f"'{number}번' = {_slot_word(slot)}" if kind == "칸"
               else f"'{number}번' = {number}번 팔레트({facts.names[model]}의 원래 자리)")
        other = ("(" + f"{number}번 팔레트는 {facts.names[model]}의 원래 자리가 아니거나 이미 그곳에 있음"
                 + ")") if kind == "칸" else "(컨베이어 칸으로는 이미 그 자리)"
        return Resolution(new, evidence=[*res.evidence, why + " " + other])
    if not options:
        return Resolution(text, evidence=res.evidence, decision="ASK",
                          reason=f"'{number}번'이 어디인지 정할 수 없습니다 — 컨베이어 칸 번호나"
                                 " '원래 자리'로 말해 주세요")
    if ctx is not None:
        ctx.ask(text, "number_kind", options)
    return Resolution(text, evidence=res.evidence, decision="ASK",
                      reason=f"'{number}번'이 컨베이어 {number}번 칸인가요, {number}번 팔레트"
                             f"({facts.names[model]}의 원래 자리)인가요?")


def facts_from(jobs, environment: Mapping[str, Any] | None) -> CellFacts:
    """시연 작업 실행기의 선언·기록 + 기억된 환경 → 해석 근거."""
    from server.sim_demo_commands import material_aliases, pallet_numbers
    from validation.conveyor_slots import record_slot
    from validation.simulation_demo_state import HELD_ON_TARGET, ON_PALLET

    names = {m: str(spec.get("korean") or m) for m, spec in jobs.materials.items()}
    aliases = material_aliases(jobs.workcell)
    numbers = {model: no for no, model in pallet_numbers(jobs.workcell).items()}
    origin_no = {m: numbers.get(spec.get("support_model")) for m, spec in jobs.materials.items()}
    location = {m: "origin" for m in names}
    state = jobs.state.status()
    for model, row in (state.get("objects") or {}).items():
        if model in location:
            location[model] = (record_slot(row) if row.get("state") == HELD_ON_TARGET
                               else row.get("pallet") if row.get("state") == ON_PALLET
                               else f"uncertain:{row.get('state')}")
    env = environment or {}
    return CellFacts(names=names, aliases=aliases, origin_pallet_no=origin_no,
                     slots=tuple(s.name for s in jobs.slots), location=location,
                     blocked=frozenset(env.get("blocked_slots") or ()),
                     unavailable=frozenset(env.get("unavailable_materials") or ()))
