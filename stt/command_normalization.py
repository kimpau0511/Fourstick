"""한국어 로봇 명령의 표기 별칭만 정규화한다.

자재나 위치를 새로 고르지 않는다. 원문은 호출자가 별도로 보존한다.
"""

from __future__ import annotations

import re


def normalize_command(text: str) -> str:
    out = str(text or "").strip()
    # 긴 표현부터 처리한다. 지시 대명사 '저쪽'은 임의의 슬롯으로 바꾸지 않는다.
    for pattern, replacement in (
        # Whisper 전사에서 실측한 오인식(2026-09-24, faster-whisper small, 합성 음성).
        (r"\s*[.!?。]+$", ""),
        (r"컴비이어|컴베이어|컴비어|콤베이어|컨베어|컨비어|컨베이여|콘베이어", "컨베이어"),
        (r"자제(?=\s*(?:는|를|을|가|이|의|도|만|와|과|$|\s|[.,!?]))", "자재"),
        (r"(원위치|제자리)(?:로|에)(?=\s*(?:돌려|갖다|가져다|옮겨|놔|놓)|\s*$)", "원래 자리로"),
        (r"저쪽\s*벨트", "저쪽 컨베이어"),
        (r"컨베이어\s*벨트", "컨베이어"),
        (r"팔렛|파렛트", "팔레트"),
        (r"갖다\s*놔|가져다\s*놔|올려\s*놔|치워", "옮겨"),
        (r"돌려\s*놔", "돌려놔"),
        (r"원위치|제자리", "돌려놔"),
        (r"멈춰|정지|스톱|\bstop\b", "정지"),
        (r"(?<![가-힣])벨트", "컨베이어"),
        (r"(?<![가-힣a-zA-Z])에이(?=\s*자재)", "A"),
        (r"(?<![가-힣a-zA-Z])비(?=\s*자재)", "B"),
        (r"(?<![가-힣a-zA-Z])씨(?=\s*자재)", "C"),
        (r"(?<![가-힣a-zA-Z])시(?=\s*자재)", "C"),
    ):
        out = re.sub(pattern, replacement, out, flags=re.IGNORECASE)
    return out


#: STT 표기 보정(2026-10-08) — **철자만** 고친다(뜻을 바꾸는 동사·지시어 치환 없음). 일반 경로 계획 요청 전에 쓴다.
#: 실측 근거: faster-whisper 합성 음성 전사(2026-09-24)와 이 파일 위 목록. 원문은 요청 기록에 그대로 남긴다.
SPELLING_VERSION = "ko-stt-spelling-2"
#: 자재 이름 앞말(등록된 코드 글자·색). 이 바로 뒤에서만 '자재'의 오인식 표기를 고친다(다른 자리의 '차트'·'자태'는 그대로).
_MATERIAL_HEAD = r"(?:\b[A-Ca-c]|에이|비|씨|주황색?|오렌지색?|파란색?|파랑색?|하늘색?|블루|초록색?|녹색|연두색?|그린)"
_SPELLING = (
    (r"컴비이어|컴베이어|컴비어|콤베이어|컨비어|컨베이여|콘베이어|컨베이아", "컨베이어"),
    (r"자제(?=\s*(?:는|를|을|가|이|의|도|만|와|과|랑|$|\s|[.,!?]))", "자재"),
    (r"팔렛트|팔렛|파렛트|팔래트", "팔레트"),
    # 2026-10-08 합성 음성 dev(Windows Heami → large-v3-turbo) 실측: 'A자재'→'A차트·A자태·A자대·A차재·A차즈',
    # '하늘색 자재'→'하늘색 자택'. 등록된 자재 앞말 바로 뒤에서만 고친다.
    (rf"({_MATERIAL_HEAD})(\s?)(?:자태|자대|차재|차제|차트|자택|차즈|자 대)", r"\1\2자재"),
    # '1번 팔레트'→'한 번 팔레트'(합성 dev 실측). '팔레트' 바로 앞의 수사만 숫자로.
    (r"(?:한|일)\s*번\s*(?=팔레트)", "1번 "),
    (r"(?:두|이)\s*번\s*(?=팔레트)", "2번 "),
    (r"(?:세|삼)\s*번\s*(?=팔레트)", "3번 "),
)


def correct_stt_spelling(text: str) -> tuple[str, list[dict]]:
    """철자만 고친 문장과 바꾼 목록([{from, to}]). 바꿀 것이 없으면 원문 그대로."""
    out, changes = str(text or ""), []
    for pattern, replacement in _SPELLING:
        for m in re.finditer(pattern, out):
            to = m.expand(replacement)
            if m.group(0) != to:
                changes.append({"from": m.group(0), "to": to})
        out = re.sub(pattern, replacement, out)
    return out, changes
