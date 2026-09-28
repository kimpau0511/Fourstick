"""한국어 음성 명령 별칭: 원문 보존과 표기 정규화만 검사한다."""

import unittest

from stt.command_normalization import normalize_command


class CommandNormalizationTest(unittest.TestCase):
    def test_alias_examples(self):
        examples = (
            ("에이 자재를 1번 팔렛에서 벨트로 갖다 놔", "A 자재를 1번 팔레트에서 컨베이어로 옮겨"),
            ("비 자재를 컨베이어로 올려 놔", "B 자재를 컨베이어로 옮겨"),
            ("씨 자재 치워", "C 자재 옮겨"),
            ("A 자재 원위치", "A 자재 돌려놔"),
            ("B 자재 제자리", "B 자재 돌려놔"),
            ("스톱", "정지"),
            ("멈춰", "정지"),
            ("저쪽 벨트", "저쪽 컨베이어"),
        )
        for raw, expected in examples:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_command(raw), expected)

    def test_observed_whisper_misrecognitions(self):
        """합성 음성 E2E에서 실측한 전사(2026-09-24)."""
        examples = (
            ("A 자재를 컴비이어 1번에 옮겨 줘.", "A 자재를 컨베이어 1번에 옮겨 줘"),
            ("B 자제는 컴베이어 2번. C 자제는 컴베이어 3번에 놓아줘.",
             "B 자재는 컨베이어 2번. C 자재는 컨베이어 3번에 놓아줘"),
            ("빨간 자제 옮겨줘.", "빨간 자재 옮겨줘"),
            ("시 자재를 제자리로 돌려놔.", "C 자재를 원래 자리로 돌려놔"),
            ("그다음 그거 제자리로", "그다음 그거 원래 자리로"),       # 문장 끝(2026-09-25)
        )
        for raw, expected in examples:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_command(raw), expected)

    def test_restraint_word_is_not_material(self):
        self.assertEqual(normalize_command("과속을 자제해"), "과속을 자제해")

    def test_no_material_or_location_is_invented(self):
        self.assertEqual(normalize_command("저쪽으로 옮겨"), "저쪽으로 옮겨")
        self.assertEqual(normalize_command("비가 온다"), "비가 온다")
