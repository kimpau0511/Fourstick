"""정지 표현 판정(2026-10-08 리뷰 10번) — 발화가 정지를 요구하는지 세 갈래로 나눈다.

STOP      분명한 정지 명령이 하나라도 있다. 다른 해석보다 먼저다('A 옮겨, 아니 멈춰').
NONE      정지 낱말이 없거나, 있어도 다른 낱말의 일부('스톱워치')·명확한 부정('정지하지 마')·
          인용·설명('정지'라는 말)뿐이다. 그 표현만으로는 정지하지 않는다.
AMBIGUOUS 정지 낱말을 명령으로도 설명으로도 확정할 수 없다('정지 버튼 어디 있어?').
          호출자는 이동 계획을 만들지 않고 확인을 요청한다.

정지 낱말은 설정(`stt_policy.stop_keywords`)에서 받는다. 여기 있는 것은 낱말 **뒤에 붙는 꼴**(명령형 어미·
조사·부정)과 **앞에 붙어도 되는 말**(긴급·비상…)뿐이다 — 새 정지 낱말을 코드에 두지 않는다.
판정은 정규화 전 원문(띄어쓰기·따옴표·문장부호)을 본다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STOP, NONE, AMBIGUOUS = "stop", "none", "ambiguous"

# 정지 낱말 바로 앞에 붙어 써도 같은 정지 명령인 말('긴급정지', '작업중지').
_COMPOUND_PREFIX = ("긴급", "비상", "즉시", "전체", "작업", "일단", "당장", "모두", "전부", "로봇", "이제")
_QUOTES_OPEN = "'\"“‘「『"
_QUOTES_CLOSE = "'\"”’」』"
# 끝·공백·문장부호(물음표 제외 — 물음표는 따로 본다).
_END = r"(?=$|[\s.,!~…)\]」』'\"”’])"
# 낱말(정지·중지·스톱·스탑) 바로 뒤에 붙어도 같은 낱말로 보는 꼬리 — 명령형·연결형('하고'·'한 뒤')·조사.
# 이 밖의 한글이 붙으면 다른 낱말의 일부다('정지선'·'스톱워치').
_GLUED_TAIL = re.compile(
    r"(?:해줘|해주세요|해요|해라|하라|하세요|하십시오|합니다|해|하고|한|할|시켜줘|시켜주세요|시켜라|시키세요|시키고|시켜|"
    r"는|은|가|이|를|을|도|의|에|로|으로|만|후|부터|까지)?(?![가-힣])")
# '멈추' 어간의 꼬리('멈추세요'·'멈추어'·'멈추라'·'멈추고').
_ROOT_TAIL = re.compile(r"(?:세요|십시오|어라|어요|어|라|고)(?![가-힣])")
# 이미 명령형인 낱말(멈춰)의 꼬리.
_IMPERATIVE_TAIL = re.compile(r"(?:라|줘|요|주세요|봐)?(?![가-힣])")
# 부정: '정지하지 마/말고/않', '멈추지 마', '멈춰 말고', '정지 없이'.
_NEGATION = re.compile(r"^\s?(?:하지|시키지|지)\s?(?:마|말|않)|^\s?(?:말고|없이)")
# 인용·설명: 낱말 바로 뒤의 '라는/란/이란/라고/이라고/이라는'('멈춰라'의 '라'는 명령형이라 넣지 않는다).
_MENTION = re.compile(r"^(?:이?라는|이?란|이?라고)(?![가-힣])|^(?:이?라는|이?라고)")
# '그만'(2026-10-09): 붙은 꼬리('…해'·'…둬' 꼴), 또는 띄어 쓴 다음 말이 멈추라는 동사(_GEUMAN_VERB)일 때만 명령이다.
# (봉인 평가 문장과 같은 예문은 코드에 적지 않는다 — 누설 검사.)
# 그 밖의 말이 이어지면('그만 컨베이어로 …') 애매하다. '그만큼'처럼 다른 한글이 붙으면 다른 낱말이다.
_GEUMAN_TAIL = re.compile(
    r"(?:해줘|해주세요|해요|해라|하라|하세요|하십시오|하자|해|둬요|둬라|두세요|두십시오|둬)?(?![가-힣])")
_GEUMAN_VERB = re.compile(
    r"^\s+(?:좀\s+)?(?:움직여(?:요|라)?|움직이세요|움직이십시오|움직이라|움직이고|멈춰(?:요|라)?|해(?:요|라)?|하세요|"
    r"하라|하십시오|둬(?:요)?|두세요)(?![가-힣])")
_GEUMAN_NEGATION = re.compile(r"^\s?(?:하지|두지)\s?(?:마|말|않)")
# 물음: 낱말 뒤 문장에 물음표나 묻는 말이 있다 — 명령인지 확정하지 않는다.
_QUESTION = re.compile(r"[?？]|어디|어떻게|언제|왜|무슨|뭐|무엇")


@dataclass(frozen=True)
class StopIntent:
    kind: str
    matched: tuple[str, ...] = ()
    reasons: tuple[str, ...] = field(default=())

    @property
    def is_stop(self) -> bool:
        return self.kind == STOP


def _grammar(keyword: str) -> str:
    """설정 낱말의 꼴: imperative(멈춰) | root(멈추) | hada(정지·중지) | geuman(그만) | noun(스톱·stop 등 나머지)."""
    if keyword == "그만":
        return "geuman"
    if keyword.endswith("춰"):
        return "imperative"
    if keyword.endswith("추"):
        return "root"
    if keyword in ("정지", "중지"):
        return "hada"
    return "noun"


def _occurrence(text: str, start: int, keyword: str) -> tuple[str, str]:
    """원문 text[start:]의 낱말 하나 → (판정, 이유)."""
    end = start + len(keyword)
    before = text[:start]
    after = text[end:]
    latin = keyword.isascii()
    # 1) 다른 낱말의 일부인가(앞). 한글은 '긴급정지'처럼 정해 둔 앞말만 붙어도 된다.
    if before and re.search(r"[가-힣A-Za-z0-9]$", before):
        if latin and re.search(r"[A-Za-z0-9]$", before):
            return NONE, "다른 낱말의 일부(앞)"
        if not latin:
            glued = re.search(r"[가-힣]+$", before)
            if not (glued and glued.group(0).endswith(_COMPOUND_PREFIX)):
                return NONE, "다른 낱말의 일부(앞)"
    # 2) 인용(따옴표로 감쌈)·설명('라는').
    quoted = bool(before) and before[-1] in _QUOTES_OPEN and bool(after) and after[0] in _QUOTES_CLOSE
    if quoted or _MENTION.match(after):
        return NONE, "인용·설명"
    # 3) 낱말에 붙은 꼬리. 맞는 꼬리가 없으면 다른 낱말의 일부다(뒤).
    grammar = _grammar(keyword)
    if latin:
        if re.match(r"[A-Za-z0-9]", after):
            return NONE, "다른 낱말의 일부(뒤)"
        tail = re.match(r"(?:해줘|해|하고|한)?(?![가-힣])", after)
    elif grammar == "imperative":
        tail = _IMPERATIVE_TAIL.match(after)
    elif grammar == "geuman":
        if _GEUMAN_NEGATION.match(after):
            return NONE, "부정"
        tail = _GEUMAN_TAIL.match(after)
        if tail is None:
            # '그만해도 돼?'·'그만두면'처럼 활용이 이어진다 — 묻는 말이면 애매, 아니면 다른 낱말·말꼴.
            return (AMBIGUOUS, "물음") if _QUESTION.search(after) else (NONE, "다른 낱말의 일부(뒤)")
        rest = after[tail.end():]
        if _QUESTION.search(rest):
            return AMBIGUOUS, "물음"
        if tail.end() == 0 and rest.strip(" .,!~…") and not _GEUMAN_VERB.match(rest):
            # 띄어 쓴 '그만' 뒤에 멈추라는 동사가 아닌 말이 온다('그만 컨베이어로 옮겨') — 정지인지 확정하지 않는다.
            return AMBIGUOUS, "뒤 말이 멈춤 동사가 아님"
        return STOP, "명령"
    elif grammar == "root":
        if _NEGATION.match(after):
            return NONE, "부정"
        tail = _ROOT_TAIL.match(after)
        if tail is None:
            return NONE, "명령형이 아님"
    else:
        if _NEGATION.match(after):
            return NONE, "부정"
        tail = _GLUED_TAIL.match(after)
    if tail is None:
        return NONE, "다른 낱말의 일부(뒤)"
    rest = after[tail.end():]
    # 4) 부정('멈춰 말고'·'정지 없이')·물음('정지 버튼 어디 있어?').
    if _NEGATION.match(after) or _NEGATION.match(rest):
        return NONE, "부정"
    if _QUESTION.search(rest) or _QUESTION.search(after[:tail.end()]):
        return AMBIGUOUS, "물음"
    # 5) 그 밖은 정지 명령이다 — 뒤에 다른 말('지금 바로'·'그다음 컨베이어로')이 이어져도 정지가 먼저다.
    return STOP, "명령"


def classify_stop(utterance: str | None, keywords) -> StopIntent:
    text = (utterance or "").strip()
    if not text:
        return StopIntent(NONE)
    found: list[tuple[int, str, str, str]] = []
    lowered = text.lower()
    taken: set[int] = set()
    for keyword in sorted({str(k) for k in keywords if str(k).strip()}, key=len, reverse=True):
        needle = keyword.lower()
        start = lowered.find(needle)
        while start >= 0:
            if start not in taken:
                kind, why = _occurrence(text, start, text[start:start + len(keyword)])
                found.append((start, keyword, kind, why))
                taken.update(range(start, start + len(keyword)))
            start = lowered.find(needle, start + 1)
    if not found:
        return StopIntent(NONE)
    found.sort()
    stops = [f for f in found if f[2] == STOP]
    if stops:
        return StopIntent(STOP, tuple(f[1] for f in stops), tuple(f"{f[1]}: {f[3]}" for f in found))
    if any(f[2] == AMBIGUOUS for f in found):
        return StopIntent(AMBIGUOUS, tuple(f[1] for f in found if f[2] == AMBIGUOUS),
                          tuple(f"{f[1]}: {f[3]}" for f in found))
    return StopIntent(NONE, (), tuple(f"{f[1]}: {f[3]}" for f in found))


AMBIGUOUS_STOP_MESSAGE = ("정지하라는 말인지 확실하지 않습니다 — 멈추려면 '정지'라고 말하거나 화면의 즉시 정지를 누르세요."
                          " 이동 계획은 만들지 않았습니다")
