"""정지 표현 판정(2026-10-08 리뷰 10번) — 기대 동작을 먼저 고정한다.

- 정지·멈춰·긴급 정지·작업을 중지해 → 정지(STOP)
- 스톱워치처럼 다른 단어의 일부 → 정지 아님(NONE)
- 정지하지 마처럼 명확히 부정 → 그 표현만으로 정지하지 않음(NONE)
- '정지'라는 단어가 뭐야?처럼 인용·설명 → 정지 아님(NONE)
- A 옮겨, 아니 멈춰처럼 실제 정지 의도가 있으면 → 정지 우선(STOP)
- 애매하면 → 확인 요청(AMBIGUOUS) — 이동 계획을 만들지 않는다
- 2026-10-09 '그만': 명령 꼬리(…해·…둬)나 멈춤 동사가 붙으면 정지, '그만큼'·'그만하지 마'는 아님, 그 밖의 말이 이어지면 애매
  (봉인 평가 문장과 같은 예문은 쓰지 않는다 — 누설 검사)
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from planning.stop_intent import AMBIGUOUS, NONE, STOP, classify_stop  # noqa: E402

KEYWORDS = tuple(json.loads((ROOT / "examples/config/valid_stt_policy.json").read_text(encoding="utf-8"))["stop_keywords"])

CASES = {
    STOP: [
        "정지", "정지!", "정지해", "정지해줘", "정지해 주세요", "정지시켜", "로봇 정지", "지금 정지해",
        "멈춰", "멈춰!", "멈춰줘", "멈춰요", "로봇 멈춰", "멈춰 빨리", "멈추세요",
        "긴급 정지", "긴급정지", "비상 정지", "즉시 정지", "전체 정지해",
        "작업을 중지해", "중지", "작업 중지", "작업중지해", "중지해 주세요",
        "스톱", "스톱!", "스탑", "stop", "로봇 스톱해", "스탑해 줘",
        "A 옮겨, 아니 멈춰", "A자재 컨베이어로 옮기다가 멈춰", "정지 상태로 바꿔", "로봇 멈춰라", "정지시켜라",
        "정지하고 대기 위치로", "정지 후에 1번 팔레트로", "먼저 스톱 그 다음 홈으로", "스탑 바로", "정지 부탁드립니다", "정지하지 마, 아니 그냥 정지해", "잠깐 스탑",
        "그만해", "그만해요", "로봇 이제 그만해", "그만 좀 움직여요", "팔 그만 움직이세요", "그만둬", "작업 그만두세요",
        "그만!", "이제 그만.", "B 옮겨, 아니 그만해",
    ],
    NONE: [
        "스톱워치 켜 줘", "정지선 쪽으로 옮겨", "비정지 구간", "멈춤표",
        "정지하지 마", "정지하지 말고 계속해", "정지 없이 홈까지 가", "정지 말고 1번 팔레트로 이동", "멈추지 마", "멈춰 말고 계속해", "중지하지 마세요",
        "'정지'라는 단어가 뭐야?", "“멈춰”라는 말은 무슨 뜻이야", "정지란 무엇인가요",
        "A자재 컨베이어로 옮겨", "B 원래 자리로", "",
        "그만하지 마", "그만두지 마세요", "그만큼 더 옮겨줘", "'그만'이란 말은 무슨 뜻이야",
    ],
    AMBIGUOUS: [
        "정지 버튼 어디 있어?", "정지는 어떻게 해?", "정지?", "작업 중지는 언제 돼?", "\"스톱\"이라고 하면 멈춰?",
        "그만해도 돼?", "그만 컨베이어로 B자재 옮겨", "그만두면 어떻게 돼",
    ],
}


class StopIntentTest(unittest.TestCase):
    def test_expected_kinds(self):
        for kind, utterances in CASES.items():
            for u in utterances:
                with self.subTest(kind=kind, utterance=u):
                    self.assertEqual(classify_stop(u, KEYWORDS).kind, kind, classify_stop(u, KEYWORDS))

    def test_config_has_the_stop_words_used_above(self):
        for word in ("정지", "멈춰", "스톱", "스탑", "중지", "멈추", "그만"):
            self.assertIn(word, KEYWORDS)

    def test_every_clear_stop_command_from_existing_fixtures_still_stops(self):
        # 기존 시험·평가 자료에 정지로 들어 있던 명령이 빠지지 않는다(회귀).
        old = ["정지", "멈춰!", "로봇 멈춰", "스톱", "잠깐 스탑", "지금 정지해", "A 컨베이어로 옮기다가 멈춰"]
        for u in old:
            with self.subTest(u):
                self.assertEqual(classify_stop(u, KEYWORDS).kind, STOP)

    def test_reason_names_the_matched_expression(self):
        r = classify_stop("작업을 중지해", KEYWORDS)
        self.assertEqual(r.kind, STOP)
        self.assertIn("중지", r.matched)


if __name__ == "__main__":
    unittest.main()
