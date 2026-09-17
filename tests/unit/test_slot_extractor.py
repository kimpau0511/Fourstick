"""슬롯 추출 검증 (md/개발플랜.md 3-03).

**모델을 쓰지 않는 경로만 본다.** 이 파일이 모델 없이 도는 것 자체가 3-03의
책임 분리가 이뤄졌다는 증거다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import json

from config.loader import load_resource_catalog
from core.reason_codes import ReasonCode
from core.resource_catalog import (
    CatalogError,
    ResourceCatalog,
    ResourceEntry,
    ResourceKind,
    normalize,
)
from planning.slot_extractor import check_plan_resources, extract_slots

CATALOG_FILE = ROOT / "examples" / "config" / "valid_resource_catalog.json"


def catalog() -> ResourceCatalog:
    return load_resource_catalog(json.loads(CATALOG_FILE.read_text(encoding="utf-8")))


def entry(rid, kind, name, aliases) -> ResourceEntry:
    return ResourceEntry(resource_id=rid, kind=kind, display_name=name, aliases=aliases)


class TestNormalization(unittest.TestCase):
    def test_spacing_and_case_do_not_change_the_key(self):
        self.assertEqual(normalize("1번 팔레트"), normalize("1번팔레트"))
        self.assertEqual(normalize("A자재"), normalize("a자재"))

    def test_full_width_digits_are_folded(self):
        self.assertEqual(normalize("１번 팔레트"), normalize("1번팔레트"))

    def test_particles_are_not_stripped(self):
        """조사를 규칙으로 벗기지 않는다 — 필요한 변형은 별칭으로 적는다."""
        self.assertNotEqual(normalize("팔레트에서"), normalize("팔레트"))


class TestCatalogContract(unittest.TestCase):
    def test_alias_clash_is_refused_at_load_time(self):
        with self.assertRaises(CatalogError) as ctx:
            ResourceCatalog(
                catalog_version="x",
                entries=(
                    entry("a", ResourceKind.LOCATION, "벨트", ("벨트",)),
                    entry("b", ResourceKind.OBJECT, "벨트", ("벨트",)),
                ),
            )
        self.assertIs(ctx.exception.reason, ReasonCode.CONFIG_INVALID)

    def test_same_alias_on_the_same_resource_is_fine(self):
        c = ResourceCatalog(
            catalog_version="x",
            entries=(entry("a", ResourceKind.LOCATION, "벨트", ("벨트", "벨 트")),),
        )
        self.assertEqual(c.resolve_alias("벨트"), "a")

    def test_display_name_must_be_reachable(self):
        with self.assertRaises(CatalogError):
            entry("a", ResourceKind.LOCATION, "1번 적재 위치", ("1번팔레트",))

    def test_unknown_resource_lookup_is_refused_not_guessed(self):
        with self.assertRaises(CatalogError) as ctx:
            catalog().get("loc_pallet_9")
        self.assertIs(ctx.exception.reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)

    def test_kinds_are_listed_from_the_catalog(self):
        c = catalog()
        self.assertEqual(
            c.ids_of_kind(ResourceKind.LOCATION),
            ("loc_pallet_1", "loc_pallet_2", "loc_conveyor"),
        )
        self.assertEqual(c.ids_of_kind(ResourceKind.OBJECT), ("obj_a", "obj_b"))


class TestExtraction(unittest.TestCase):
    def setUp(self):
        self.c = catalog()

    def extract(self, text, **kw):
        return extract_slots(text, self.c, **kw)

    def test_transfer_utterance_yields_object_and_two_locations_in_order(self):
        r = self.extract("1번 팔레트에서 A자재를 집어서 컨베이어에 올려줘")
        self.assertEqual(r.objects, ("obj_a",))
        self.assertEqual(r.locations, ("loc_pallet_1", "loc_conveyor"))
        self.assertEqual(r.resource_ids, ("loc_pallet_1", "obj_a", "loc_conveyor"))

    def test_spoken_variants_resolve_through_aliases(self):
        r = self.extract("일번 팔레트의 에이 자재를 두번째 팔레트로")
        self.assertEqual(r.objects, ("obj_a",))
        self.assertEqual(r.locations, ("loc_pallet_1", "loc_pallet_2"))

    def test_longer_alias_wins_over_a_shorter_overlapping_one(self):
        """'컨베이어 벨트'가 '벨트'로 잘리면 뜻이 달라진다."""
        r = self.extract("컨베이어 벨트에 놔줘")
        self.assertEqual([m.surface for m in r.matches], [normalize("컨베이어 벨트")])
        self.assertEqual(r.locations, ("loc_conveyor",))

    def test_unknown_words_are_not_mapped_to_similar_resources(self):
        r = self.extract("3번 팔레트에서 C자재를 집어")
        self.assertEqual(r.matches, ())

    def test_repeated_mention_is_kept_as_two_matches(self):
        r = self.extract("1번 팔레트에서 1번 팔레트로")
        self.assertEqual(r.locations, ("loc_pallet_1", "loc_pallet_1"))
        self.assertEqual(r.distinct(ResourceKind.LOCATION), ("loc_pallet_1",))

    def test_utterance_without_resources_is_not_an_error(self):
        """'홈으로', '정지'에는 리소스가 없다. 실패로 만들지 않는다."""
        r = self.extract("홈으로 보내줘")
        self.assertEqual(r.matches, ())
        self.assertFalse(r.stop_keyword_hit)

    def test_empty_utterance_yields_empty_result(self):
        r = self.extract("")
        self.assertEqual(r.matches, ())

    def test_stop_keyword_is_reported_but_not_acted_on_here(self):
        r = self.extract("지금 정지해", stop_keywords=("정지",))
        self.assertTrue(r.stop_keyword_hit)
        # 추출기는 계획을 만들지 않는다 — 판단은 조율자의 몫이다.
        self.assertFalse(hasattr(r, "plan"))

    def test_stop_keywords_are_injected_not_hardcoded(self):
        self.assertFalse(self.extract("지금 정지해").stop_keyword_hit)

    def test_match_positions_allow_order_comparison(self):
        r = self.extract("컨베이어에서 A자재를 1번 팔레트로")
        self.assertEqual([m.resource_id for m in r.matches],
                         ["loc_conveyor", "obj_a", "loc_pallet_1"])
        self.assertTrue(r.matches[0].start < r.matches[1].start < r.matches[2].start)


class TestPlanResourceCheck(unittest.TestCase):
    """계획 생성 뒤 대조 — 모델이 만들어 낸 리소스를 걸러낸다."""

    def setUp(self):
        self.c = catalog()

    def test_known_resources_pass(self):
        self.assertEqual(check_plan_resources(("loc_pallet_1", "obj_a"), self.c), ())

    def test_invented_resource_is_reported_not_corrected(self):
        issues = check_plan_resources(("loc_pallet_9",), self.c)
        self.assertEqual(len(issues), 1)
        self.assertIs(issues[0].reason, ReasonCode.PLAN_UNKNOWN_RESOURCE)
        self.assertIn("loc_pallet_9", issues[0].detail)

    def test_empty_argument_is_reported_as_incomplete(self):
        issues = check_plan_resources(("",), self.c)
        self.assertIs(issues[0].reason, ReasonCode.PLAN_SLOT_INCOMPLETE)


class TestResponsibilitySeparation(unittest.TestCase):
    """3-03 — 추출 모듈이 모델·네트워크·저장소를 모른다."""

    def test_extractor_does_not_import_a_provider_or_client(self):
        text = (ROOT / "planning" / "slot_extractor.py").read_text(encoding="utf-8")
        for banned in ("plan_provider", "requests", "httpx", "openai", "vllm",
                       "sqlite3", "PlanProvider"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, text)

    def test_provider_port_holds_no_extraction_rules(self):
        text = (ROOT / "planning" / "plan_provider.py").read_text(encoding="utf-8")
        for banned in ("normalize(", "find(", "alias_index"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, text)

    def test_catalog_has_no_robot_numbers(self):
        """카탈로그는 셀의 이름만 담는다. 좌표·치수·질량은 담지 않는다."""
        text = CATALOG_FILE.read_text(encoding="utf-8")
        for banned in ("joint", "radian", "kg", "meter", "_m\"", "pose", "xyz"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
