"""자재를 색 이름으로 부르는 경로 검증.

색 이름의 출처는 **셀 설정의 선언 하나**다
(`config/workcell/fr3_2f85_workcell.json`의 `resource_map[].korean_colors`).
두 경로가 같은 선언을 쓰는지 본다.

- 일반 계획 생성 경로: 자원 카탈로그 별칭 → 슬롯 추출
- 시뮬레이션 시연 명령 경로: `server.sim_demo_commands.material_aliases`

모델의 `color_rgba`에서 한국어 색 이름을 만들어 내지 않는다 — 코드가 RGB를
낱말로 옮기면 그건 선언이 아니라 추측이다. 여기서도 그 규칙을 확인한다.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from config.loader import load_resource_catalog  # noqa: E402
from core.resource_catalog import ResourceKind  # noqa: E402
from planning.slot_extractor import extract_slots  # noqa: E402
from server.sim_demo_jobs import materials_from_workcell  # noqa: E402
from server.sim_demo_commands import (  # noqa: E402
    PASS_THROUGH,
    RUN,
    material_aliases,
    parse_command,
)

WORKCELL = json.loads((ROOT / "config/workcell/fr3_2f85_workcell.json")
                      .read_text(encoding="utf-8"))
CATALOG = load_resource_catalog(json.loads(
    (ROOT / "config/workcell/fr3_2f85_workcell_resource_catalog.json")
    .read_text(encoding="utf-8")))


def declared_colors() -> dict[str, tuple[str, ...]]:
    """자원 id → 셀 설정이 선언한 색 이름. 선언이 없으면 빈 항목이다."""
    return {row["resource_id"]: tuple(row.get("korean_colors") or ())
            for row in WORKCELL["resource_map"]}


class TestDeclaration(unittest.TestCase):
    def test_every_material_declares_at_least_one_color_name(self):
        colors = declared_colors()
        materials = [row["resource_id"] for row in WORKCELL["resource_map"]
                     if (WORKCELL["models"].get(row["gazebo_model"]) or {}
                         ).get("kind") == "material"]
        self.assertTrue(materials)
        for rid in materials:
            with self.subTest(rid=rid):
                self.assertTrue(colors.get(rid), f"{rid}에 색 이름 선언이 없다")

    def test_color_names_do_not_collide_between_materials(self):
        seen: dict[str, str] = {}
        for rid, words in declared_colors().items():
            for word in words:
                owner = seen.setdefault(word, rid)
                self.assertEqual(owner, rid,
                                 f"색 이름 {word!r}이 {owner}와 {rid}를 함께 가리킨다")


class TestPlanningPath(unittest.TestCase):
    """자원 카탈로그 별칭 → 슬롯 추출. 계획 생성이 쓰는 경로다."""

    def test_declared_color_names_resolve_to_the_material(self):
        for rid, words in declared_colors().items():
            if not words:
                continue
            for word in words:
                with self.subTest(rid=rid, word=word):
                    slots = extract_slots(f"{word} 자재를 컨베이어로 옮겨줘", CATALOG)
                    found = [m.resource_id for m in slots.matches
                             if m.kind is ResourceKind.OBJECT]
                    self.assertEqual(found, [rid])

    def test_color_word_without_material_noun_is_not_an_object(self):
        # "주황"만으로는 자재가 되지 않는다 — 색 별칭은 `<색> 자재` 꼴뿐이다.
        slots = extract_slots("주황 팔레트로 이동해줘", CATALOG)
        self.assertEqual([m.resource_id for m in slots.matches
                          if m.kind is ResourceKind.OBJECT], [])

    def test_letter_names_still_work(self):
        slots = extract_slots("A 자재를 컨베이어로 옮겨줘", CATALOG)
        self.assertEqual([m.resource_id for m in slots.matches
                          if m.kind is ResourceKind.OBJECT], ["mat_a"])


class TestSimDemoPath(unittest.TestCase):
    """시연 명령 라우터. LLM을 거치지 않는 경로다."""

    def test_aliases_come_from_the_declaration(self):
        aliases = material_aliases(WORKCELL)
        by_model = {row["gazebo_model"]: row for row in WORKCELL["resource_map"]}
        for model, names in aliases.items():
            for word in by_model[model].get("korean_colors") or ():
                with self.subTest(model=model, word=word):
                    self.assertIn(f"{word}자재", names)

    def test_transfer_and_return_by_color(self):
        by_model = {row["gazebo_model"]: row for row in WORKCELL["resource_map"]}
        for model, row in by_model.items():
            for word in row.get("korean_colors") or ():
                with self.subTest(model=model, word=word):
                    transfer = parse_command(f"{word} 자재를 컨베이어로 옮겨줘", WORKCELL)
                    self.assertEqual(
                        (transfer["decision"], transfer["intent"], transfer["material"]),
                        (RUN, "transfer", model))
                    back = parse_command(f"{word} 자재를 원래 자리로 돌려놔", WORKCELL)
                    self.assertEqual(
                        (back["decision"], back["intent"], back["material"]),
                        (RUN, "return", model))

    def test_status_payload_carries_the_declared_color_names(self):
        # 화면이 "무엇으로 부를 수 있는지" 보여주려면 선언이 그대로 실려야 한다.
        materials = materials_from_workcell(WORKCELL)
        for row in WORKCELL["resource_map"]:
            words = row.get("korean_colors")
            if not words:
                continue
            with self.subTest(model=row["gazebo_model"]):
                self.assertEqual(materials[row["gazebo_model"]]["korean_colors"],
                                 list(words))

    def test_color_word_alone_is_not_a_material_mention(self):
        parsed = parse_command("주황 팔레트로 이동해줘", WORKCELL)
        self.assertEqual(parsed["decision"], PASS_THROUGH)
        self.assertIsNone(parsed["material"])

    def test_undeclared_color_is_not_invented_from_rgb(self):
        # 자재 색(color_rgba)은 선언돼 있지만 "빨강"은 선언에 없다.
        self.assertTrue(any((row.get("color_rgba") for row in WORKCELL["models"].values()
                             if row.get("kind") == "material")))
        aliases = material_aliases(WORKCELL)
        self.assertFalse([m for m, names in aliases.items()
                          if any("빨강" in n or "빨간" in n for n in names)])


if __name__ == "__main__":
    unittest.main()
