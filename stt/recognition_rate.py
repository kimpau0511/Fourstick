"""음성 인식률(글자 기준) — 사람이 확인한 정답 전사와 STT 출력을 비교한다 (2026-10-08).

정의
- CER = (치환 S + 삭제 D + 삽입 I) / 정답 글자 수 N   — 정규화한 문자열의 편집 거리. 1을 넘을 수 있다(삽입이 많을 때).
- 글자 기준 인식률 = max(0, 1 − CER) × 100
- 종합 = 전체 오류 수(ΣS+ΣD+ΣI) / 전체 정답 글자 수(ΣN). **사례별 %의 평균이 아니다.**

정답
- 정답은 **사람이 실제 발화를 듣고 확인한 전사**다(`reference_verified_by`가 있어야 한다).
  평가 대상 STT의 출력을 정답으로 쓰지 않는다(`reference_source`가 'stt'면 거절).
- 정답이 없으면 `pending`(평가 대기), 정답이 빈 문자열이면(정규화 뒤 0글자 포함) `not_evaluable`(평가 불가).

정규화(NORMALIZATION_VERSION) — 바꾸면 버전을 올리고, 다른 버전 결과와 합치지 않는다.
  ko-cer-1:
  1. 유니코드 NFKC(전각 → 반각, 호환 문자 통일)
  2. 라틴 문자 소문자화(A자재 = a자재)
  3. 공백 모두 제거(띄어쓰기는 평가하지 않는다)
  4. 문장부호·기호 제거: 유니코드 범주 P*(문장부호), S*(기호)
  5. 숫자·한글·라틴은 **그대로**. 숫자↔한글 읽기('2번'↔'이번'), 라틴↔한글 읽기('A'↔'에이')는
     바꾸지 않는다 — 그런 차이는 오류로 센다(의미가 같아 보여도 표기가 다르면 명령 해석에 영향을 준다).

용어 후보정
- `stt.command_normalization.normalize_command`(현장 용어 표기 통일)를 적용한 결과를 **따로** 계산한다.
  원본(`raw`)과 후보정(`corrected`)을 섞지 않는다. 화면에 보이는 값은 원본 기준이다.
"""

from __future__ import annotations

import unicodedata
from dataclasses import asdict, dataclass

NORMALIZATION_VERSION = "ko-cer-1"
#: 결과 상태
MEASURED, PENDING, NOT_EVALUABLE = "measured", "pending", "not_evaluable"
STATUS_LABELS = {MEASURED: "평가됨", PENDING: "평가 대기", NOT_EVALUABLE: "평가 불가"}


def normalize_for_cer(text: str | None) -> str:
    """ko-cer-1 정규화. 위 규칙 1~5."""
    s = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in s:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("P", "S"):
            continue
        out.append(ch.lower() if ch.isascii() else ch)
    return "".join(out)


@dataclass(frozen=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int
    reference_chars: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def cer(self) -> float | None:
        return None if self.reference_chars == 0 else self.errors / self.reference_chars

    @property
    def rate_percent(self) -> float | None:
        c = self.cer
        return None if c is None else max(0.0, 1.0 - c) * 100.0

    def to_dict(self) -> dict:
        return {**asdict(self), "errors": self.errors, "cer": self.cer, "rate_percent": self.rate_percent}


def edit_counts(reference: str, hypothesis: str) -> EditCounts:
    """정규화된 두 문자열의 최소 편집(레벤시테인)과 그 경로의 치환·삭제·삽입 수.

    같은 최소 거리의 경로가 여럿이면 치환 → 삭제 → 삽입 순으로 고른다(결정적). 오류 합은 경로와 무관하게 같다.
    """
    r, h = reference, hypothesis
    n, m = len(r), len(h)
    # dp[i][j] = r[:i] → h[:j] 최소 편집 거리
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if r[i - 1] == h[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j - 1] + cost, dp[i - 1][j] + 1, dp[i][j - 1] + 1)
    s = d = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (r[i - 1] != h[j - 1]):
            s += r[i - 1] != h[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            d += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return EditCounts(s, d, ins, n)


class ReferenceNotHumanVerified(ValueError):
    """정답이 사람이 확인한 전사가 아니다(평가 대상 STT 출력을 정답으로 쓰려 했다 등)."""


def score_case(reference: str | None, hypothesis: str | None, *, reference_source: str | None = "human") -> dict:
    """한 사례의 결과. reference=None → 평가 대기, 빈 정답 → 평가 불가."""
    if reference_source == "stt":
        raise ReferenceNotHumanVerified("평가 대상 STT의 출력은 정답이 될 수 없다")
    if reference is None:
        return {"status": PENDING, "status_label": STATUS_LABELS[PENDING], "counts": None}
    ref_n = normalize_for_cer(reference)
    hyp_n = normalize_for_cer(hypothesis)
    if not ref_n:
        return {"status": NOT_EVALUABLE, "status_label": STATUS_LABELS[NOT_EVALUABLE], "counts": None,
                "reference_normalized": ref_n, "hypothesis_normalized": hyp_n}
    c = edit_counts(ref_n, hyp_n)
    return {"status": MEASURED, "status_label": STATUS_LABELS[MEASURED], "counts": c.to_dict(),
            "reference_normalized": ref_n, "hypothesis_normalized": hyp_n}


def aggregate(cases) -> dict:
    """종합: 평가된 사례만, 전체 오류 수 / 전체 정답 글자 수. 사례별 %를 평균하지 않는다."""
    measured = [c for c in cases if c.get("status") == MEASURED]
    S = sum(c["counts"]["substitutions"] for c in measured)
    D = sum(c["counts"]["deletions"] for c in measured)
    I = sum(c["counts"]["insertions"] for c in measured)
    N = sum(c["counts"]["reference_chars"] for c in measured)
    total = EditCounts(S, D, I, N)
    return {"cases_total": len(cases), "cases_measured": len(measured),
            "cases_pending": sum(c.get("status") == PENDING for c in cases),
            "cases_not_evaluable": sum(c.get("status") == NOT_EVALUABLE for c in cases),
            **total.to_dict(), "normalization_version": NORMALIZATION_VERSION}
