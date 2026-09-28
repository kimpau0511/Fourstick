"""색깔로 자재 지정 — 정규화 · 셀 선언 · Gazebo 관측 대조.

"빨간 자재를 1번 칸으로", "파란색 자재는 빼고", "노란 거 먼저"처럼 **색으로**
가리킨 자재를 셀이 선언한 자재 id로 바꾼다. 바꾼 문장은 기존 해석·계획·확인·
실행 경로를 그대로 탄다.

- 색 낱말은 아래 **닫힌 표**로만 정규화한다(빨강·빨간색·적색 → red). 표에 없는
  낱말은 색으로 보지 않는다.
- 어느 자재가 어느 색인지는 **셀 설정 선언**(`resource_map[].korean_colors`)에서만
  온다. 코드가 RGB에서 색 이름을 짓지 않는다.
- 관측: 실행 중인 Gazebo의 장면 정보에서 자재의 표시 색(diffuse)을 읽어 설정의
  `color_rgba`와 대조한다. 다르면 그 자재를 색으로 가리킬 수 없다(되묻는다).
- 등록되지 않은 색, 같은 색 자재가 둘 이상, 관측·설정 불일치는 **고르지 않고**
  ASK다. LLM에 넘기지 않는다 — LLM이 색이나 자재 id를 정하지 못하게 한다.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

#: 정규 색 → 표면형. **닫힌 표**다. 선언 낱말도 이 표로 정규화한다.
COLOR_WORDS: Mapping[str, tuple[str, ...]] = {
    "red": ("빨강", "빨간", "빨간색", "빨강색", "적색", "레드", "붉은", "붉은색"),
    "orange": ("주황", "주황색", "오렌지", "오렌지색", "등색"),
    "yellow": ("노랑", "노란", "노란색", "노랑색", "황색", "옐로", "옐로우"),
    "green": ("초록", "초록색", "녹색", "그린"),
    "lime": ("연두", "연두색"),
    "blue": ("파랑", "파란", "파란색", "파랑색", "청색", "블루", "푸른", "푸른색"),
    "sky": ("하늘", "하늘색"),
    "purple": ("보라", "보라색", "자주", "자주색", "퍼플"),
    "pink": ("분홍", "분홍색", "핑크"),
    "white": ("흰", "흰색", "하얀", "하얀색", "백색", "화이트"),
    "black": ("검정", "검은", "검은색", "검정색", "흑색", "블랙"),
    "gray": ("회색", "그레이"),
    "brown": ("갈색", "브라운"),
}
COLOR_LABELS = {"red": "빨간색", "orange": "주황색", "yellow": "노란색",
                "green": "초록색", "lime": "연두색", "blue": "파란색",
                "sky": "하늘색", "purple": "보라색", "pink": "분홍색",
                "white": "흰색", "black": "검은색", "gray": "회색", "brown": "갈색"}
_SURFACE_TO_COLOR = {s: c for c, words in COLOR_WORDS.items() for s in words}
_SURFACES = sorted(_SURFACE_TO_COLOR, key=len, reverse=True)
#: 색 뒤에 오는 "자재를 가리키는" 명사. 이 꼴일 때만 자재 지칭으로 본다
#: ("주황 팔레트"는 자재가 아니다).
_NOUNS = r"(?:자재|자제|블록|박스|물건|상자|거|것|걸|건|게)"
_REFERENCE = re.compile(
    r"(" + "|".join(map(re.escape, _SURFACES)) + r")\s*(?:색\s*)?(?:의\s*)?(" + _NOUNS + r")")
#: "초록 팔레트", "파란색 트레이" — 팔레트를 색으로 가리킨다.
_PALLET_REFERENCE = re.compile(
    r"(" + "|".join(map(re.escape, _SURFACES)) + r")\s*(?:색\s*)?(?:의\s*)?(팔레트|트레이|받침)")
#: 명사 없이 조사가 붙은 색("주황이랑", "파랑은") — 옮기는 문장에서만 쓴다.
_BARE = re.compile(
    r"(?<![가-힣])(" + "|".join(map(re.escape, _SURFACES)) + r")(?:색)?"
    r"(?=\s*(?:이랑|랑|하고|과|와|은|는|을|를|이|가|만|도)(?![가-힣]*\s*(?:팔레트|트레이))"
    r"|\s+(?:자리|위치)\s*(?:를|을)?\s*(?:서로\s*)?(?:맞)?(?:바꿔|바꾸|교환))")
_ACTION = re.compile(r"옮겨|옮기|놔|놓|올려|돌려|바꿔|바꾸|교환|보내|갖다|가져다")
#: 관측 색과 설정 색의 채널별 허용 차.
RGBA_TOLERANCE = 0.02


def canonical_color(word: str) -> str | None:
    return _SURFACE_TO_COLOR.get(re.sub(r"\s+", "", str(word or "")).lower())


class ColorResolutionError(Exception):
    """색으로 가리킨 자재를 하나로 정할 수 없다. 항상 되묻는다(ASK)."""

    decision = "ASK"


@dataclass(frozen=True)
class MaterialColor:
    model: str
    korean: str
    colors: tuple[str, ...]            # 정규 색
    declared_rgba: tuple[float, ...] | None
    observed_rgba: tuple[float, ...] | None = None
    #: verified(관측=설정) · mismatch · unobserved(관측 수단 없음)
    status: str = "unobserved"

    def to_dict(self) -> dict:
        return {"model": self.model, "korean": self.korean,
                "colors": list(self.colors),
                "color_labels": [COLOR_LABELS.get(c, c) for c in self.colors],
                "declared_rgba": None if self.declared_rgba is None
                else list(self.declared_rgba),
                "observed_rgba": None if self.observed_rgba is None
                else list(self.observed_rgba),
                "status": self.status}


@dataclass
class ColorRegistry:
    materials: Mapping[str, MaterialColor]
    observed_at: float | None = None
    observation_detail: str = ""
    by_color: dict[str, list[str]] = field(default_factory=dict)
    #: 팔레트 모델 → 색. 팔레트 색은 **자기 자재와 같은 선언 색일 때만** 그 자재의
    #: 색 낱말을 물려받는다(코드가 RGB에서 색 이름을 짓지 않는다).
    pallets: Mapping[str, MaterialColor] = field(default_factory=dict)
    pallet_by_color: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_color = {}
        for model, item in self.materials.items():
            for color in item.colors:
                self.by_color.setdefault(color, []).append(model)
        self.pallet_by_color = {}
        for model, item in self.pallets.items():
            for color in item.colors:
                self.pallet_by_color.setdefault(color, []).append(model)

    @classmethod
    def from_workcell(cls, workcell: Mapping[str, Any],
                      observed: Mapping[str, Sequence[float]] | None = None,
                      *, observed_at: float | None = None,
                      detail: str = "") -> "ColorRegistry":
        items: dict[str, MaterialColor] = {}
        for row in workcell.get("resource_map", ()):
            model = row.get("gazebo_model")
            spec = (workcell.get("models") or {}).get(model) or {}
            if spec.get("kind") != "material":
                continue
            colors = []
            for word in row.get("korean_colors") or ():
                color = canonical_color(word)
                if color and color not in colors:
                    colors.append(color)
            declared = spec.get("color_rgba")
            declared = None if not declared else tuple(float(v) for v in declared)
            seen = None if observed is None else observed.get(model)
            seen = None if seen is None else tuple(float(v) for v in seen)
            if seen is None or declared is None:
                status = "unobserved"
            elif all(abs(a - b) <= RGBA_TOLERANCE for a, b in zip(seen[:3], declared[:3])):
                status = "verified"
            else:
                status = "mismatch"
            items[model] = MaterialColor(
                model=model, korean=str(row.get("korean") or model),
                colors=tuple(colors), declared_rgba=declared,
                observed_rgba=seen, status=status)
        pallets = _pallet_colors(workcell, items, observed)
        return cls(materials=items, observed_at=observed_at, observation_detail=detail,
                   pallets=pallets)

    def resolve_pallet(self, color: str, surface: str = "") -> MaterialColor:
        said = surface or COLOR_LABELS.get(color, color)
        models = self.pallet_by_color.get(color) or []
        if not models:
            known = ", ".join(
                f"{'/'.join(COLOR_LABELS.get(c, c) for c in item.colors)}={item.korean}"
                for item in self.pallets.values() if item.colors)
            raise ColorResolutionError(
                f"'{said}' 팔레트는 이 셀에 등록되어 있지 않습니다 — 등록된 팔레트 색:"
                f" {known or '없음'}. 팔레트 번호로 말해 주세요")
        if len(models) > 1:
            names = ", ".join(self.pallets[m].korean for m in models)
            raise ColorResolutionError(
                f"'{said}' 팔레트가 여럿입니다({names}) — 번호로 말해 주세요")
        item = self.pallets[models[0]]
        if item.status == "mismatch":
            raise ColorResolutionError(
                f"'{said}'로 등록된 {item.korean}의 시뮬레이터 표시 색이 설정과 다릅니다"
                f" (설정 {list(item.declared_rgba)}, 관측 {list(item.observed_rgba)}) —"
                " 팔레트 번호로 말해 주세요")
        return item

    def resolve(self, color: str, surface: str = "") -> MaterialColor:
        said = surface or COLOR_LABELS.get(color, color)
        models = self.by_color.get(color) or []
        if not models:
            known = ", ".join(
                f"{'/'.join(COLOR_LABELS.get(c, c) for c in item.colors)}={item.korean}"
                for item in self.materials.values() if item.colors)
            raise ColorResolutionError(
                f"'{said}' 자재는 이 셀에 등록되어 있지 않습니다 — 등록된 색: {known}."
                " 자재 이름으로 말해 주세요")
        if len(models) > 1:
            names = ", ".join(self.materials[m].korean for m in models)
            raise ColorResolutionError(
                f"'{said}' 자재가 여럿입니다({names}) — 어느 자재인지 이름으로 말해 주세요")
        item = self.materials[models[0]]
        if item.status == "mismatch":
            raise ColorResolutionError(
                f"'{said}'로 등록된 {item.korean}의 시뮬레이터 표시 색이 설정과 다릅니다"
                f" (설정 {list(item.declared_rgba)}, 관측 {list(item.observed_rgba)}) —"
                " 자재 이름으로 말해 주세요")
        return item

    def rewrite(self, text: str) -> tuple[str, list[dict]]:
        """색 지칭을 자재 이름으로 바꾼다. 못 정하면 ColorResolutionError."""
        substitutions: list[dict] = []

        def pallet(match: re.Match) -> str:
            surface = match.group(1)
            color = _SURFACE_TO_COLOR[surface]
            item = self.resolve_pallet(color, surface)
            substitutions.append({"said": match.group(0).strip(), "color": color,
                                  "color_label": COLOR_LABELS.get(color, color),
                                  "pallet": item.model, "korean": item.korean,
                                  "check": item.status})
            return item.korean

        text = _PALLET_REFERENCE.sub(pallet, str(text or ""))

        def swap(match: re.Match) -> str:
            surface, noun = match.group(1), match.group(2)
            color = _SURFACE_TO_COLOR[surface]
            item = self.resolve(color, surface)
            substitutions.append({"said": match.group(0).strip(), "color": color,
                                  "color_label": COLOR_LABELS.get(color, color),
                                  "material": item.model, "korean": item.korean,
                                  "check": item.status})
            # 뒤따르는 조사는 원문에 남는다("파란색 자재는" → "B자재는").
            tail = {"걸": "를", "건": "는", "게": "가"}.get(noun, "")
            return item.korean + tail

        rewritten = _REFERENCE.sub(swap, str(text or ""))
        if _ACTION.search(rewritten):
            # 옮기라는 문장에서는 "주황이랑", "파랑은"처럼 조사가 붙은 색도 자재 지칭이다.
            def bare(match: re.Match) -> str:
                surface = match.group(1)
                item = self.resolve(_SURFACE_TO_COLOR[surface], surface)
                substitutions.append({"said": surface, "color": item.colors[0],
                                      "color_label": COLOR_LABELS.get(
                                          _SURFACE_TO_COLOR[surface], surface),
                                      "material": item.model, "korean": item.korean,
                                      "check": item.status})
                return item.korean
            rewritten = _BARE.sub(bare, rewritten)
        return rewritten, substitutions

    def to_dict(self) -> dict:
        return {"observed_at": self.observed_at, "detail": self.observation_detail,
                "materials": [item.to_dict() for item in self.materials.values()],
                "pallets": [item.to_dict() for item in self.pallets.values()]}


def pallet_models(workcell: Mapping[str, Any]) -> list[str]:
    return [model for model, spec in (workcell.get("models") or {}).items()
            if (spec or {}).get("kind") == "pallet"]


def _pallet_colors(workcell: Mapping[str, Any], materials: Mapping[str, MaterialColor],
                   observed: Mapping[str, Sequence[float]] | None) -> dict[str, MaterialColor]:
    """팔레트 트레이 선언 색 == 그 팔레트에 선언된(프레임 부모) 자재 색이면 색 낱말을 잇는다."""
    from server.sim_demo_jobs import materials_from_workcell

    owner = {spec.get("support_model"): model
             for model, spec in materials_from_workcell(workcell).items()}
    korean = {row.get("gazebo_model"): row.get("korean") for row in workcell.get("resource_map", ())}
    out: dict[str, MaterialColor] = {}
    for model in pallet_models(workcell):
        spec = (workcell.get("models") or {}).get(model) or {}
        tray = next((part for part in spec.get("parts") or () if part.get("color_rgba")), None)
        declared = None if tray is None else tuple(float(v) for v in tray["color_rgba"])
        mat = materials.get(owner.get(model) or "")
        colors: tuple[str, ...] = ()
        if declared is not None and mat is not None and mat.declared_rgba is not None and all(
                abs(a - b) <= RGBA_TOLERANCE for a, b in zip(declared[:3], mat.declared_rgba[:3])):
            colors = mat.colors
        seen = None if observed is None else observed.get(model)
        seen = None if seen is None else tuple(float(v) for v in seen)
        if seen is None or declared is None:
            status = "unobserved"
        elif all(abs(a - b) <= RGBA_TOLERANCE for a, b in zip(seen[:3], declared[:3])):
            status = "verified"
        else:
            status = "mismatch"
        out[model] = MaterialColor(model=model, korean=str(korean.get(model) or model),
                                   colors=colors, declared_rgba=declared,
                                   observed_rgba=seen, status=status)
    return out


def stray_color_words(text: str) -> list[str]:
    """자재 지칭으로 바꾸지 못하고 남은 색 낱말. LLM에 넘기기 전에 본다."""
    compact = re.sub(r"\s+", "", str(text or ""))
    # 팔레트·트레이 색은 자재 지칭이 아니다.
    compact = re.sub("(" + "|".join(map(re.escape, _SURFACES)) + r")(?:색)?(?=팔레트|트레이|받침)",
                     "", compact)
    found = []
    for surface in _SURFACES:          # 긴 표면형부터 — "빨간색"을 "빨간"으로 또 세지 않는다
        if surface in compact:
            found.append(surface)
            compact = compact.replace(surface, " ")
    return found


# ── Gazebo 관측 ────────────────────────────────────────────────────
_SCENE_CACHE: dict[str, tuple[float, dict, str]] = {}


def observe_gazebo_colors(world: str, partition: str, models: Sequence[str], *,
                          ttl_sec: float = 30.0, timeout_sec: float = 5.0,
                          runner: Callable[..., Any] = subprocess.run,
                          clock: Callable[[], float] = time.time) -> tuple[dict, str]:
    """실행 중인 Gazebo 장면에서 자재의 표시 색(diffuse). 실패하면 빈 dict다."""
    key = f"{partition}/{world}"
    cached = _SCENE_CACHE.get(key)
    if cached is not None and clock() - cached[0] < ttl_sec:
        return cached[1], cached[2]
    import os
    env = dict(os.environ, GZ_PARTITION=partition)
    try:
        out = runner(["gz", "service", "-s", f"/world/{world}/scene/info",
                      "--reqtype", "gz.msgs.Empty", "--reptype", "gz.msgs.Scene",
                      "--timeout", str(int(timeout_sec * 1000)), "--req", ""],
                     capture_output=True, text=True, timeout=timeout_sec + 2, env=env)
        text = out.stdout or ""
    except Exception as exc:  # noqa: BLE001 — 관측 실패는 "관측 없음"이다
        return {}, f"관측 실패: {exc}"[:200]
    colors: dict[str, tuple[float, float, float, float]] = {}
    for model in models:
        start = text.find(f'name: "{model}"')
        if start < 0:
            continue
        match = re.search(r"diffuse \{\s*r: ([\d.eE+-]+)\s*g: ([\d.eE+-]+)\s*b: ([\d.eE+-]+)"
                          r"(?:\s*a: ([\d.eE+-]+))?", text[start:start + 4000])
        if match:
            r, g, b, a = match.groups()
            colors[model] = (float(r), float(g), float(b), float(a or 1.0))
    detail = "gazebo scene/info" if colors else "관측 결과에 자재 색이 없다"
    _SCENE_CACHE[key] = (clock(), colors, detail)
    return colors, detail
