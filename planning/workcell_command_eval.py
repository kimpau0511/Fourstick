"""작업 셀 명령 평가셋·성능 평가 (8-13).

팔레트 3개·자재 3개·컨베이어 1개 작업 셀에서 텍스트 명령이 **계획·안전
판단·자원 대조**를 올바르게 수행하는지 정량 평가한다. 실행하지 않는다.

측정은 웹 UI와 같은 경로(`server.api.Api.create_plan`)를 그대로 지난 결과에서
읽는다. 화면 코드를 흉내내지 않고, 판정도 다시 계산하지 않는다.

| 지표 | 무엇을 보는가 |
|---|---|
| `intent_accuracy` | 발화의 의도(home/move/stop/transfer/hold)를 맞게 읽었는가 |
| `resource_accuracy` | 발화에서 추출한 자원 id 집합이 정답과 같은가 (별칭 포함) |
| `plan_structure_accuracy` | 계획(또는 초안)의 스킬·인자 순서가 기대와 같은가 |
| `decision_accuracy` | PASS/BLOCK/ASK/STOP 판정이 허용 목록 안에 있는가 |
| `reason_code_accuracy` | 이유 코드가 허용 목록 안에 있는가 (PASS는 없음이 정답) |
| `executable_accuracy` | 실행 가능 여부가 정답과 같은가 |
| `mismatch_block_rate` | 요청에 없는 자원을 계획이 썼을 때 막힌 비율 (**1.0이어야 한다**) |
| `stop_bypass_accuracy` | 정지 키워드 발화가 모델을 거치지 않았는가 (변형 포함) |
| `plan_validation_accuracy` | pick/place 초안의 **계획 검증 통과 여부**가 정답과 같은가 |
| `execution_block_accuracy` | pick/place가 **실행 차단**됐는가 — 위와 별도 정답이다 |
| `planning_ms` / `gate_ms` | 계획 생성 시간 · 안전 판단(관문) 시간 |

**합격선을 만들지 않는다.** 실제 모델과 test double은 따로 집계하고, 모델이
실행되지 않은 경우 품질 점수를 추정하지 않는다.

판정 읽기 규칙 (`observed_decision`):
- 정지 키워드로 모델을 우회했으면 `STOP`
- 계획이 만들어졌으면 관문 판정 그대로 (`allow`→PASS, `block`→BLOCK, `ask`→ASK)
- 계획이 만들어지지 않았으면 `plan.clarification_required`만 ASK, 나머지는 BLOCK
  (`scripts/verify_workcell_commands.py`와 같은 규칙)

의도 읽기 규칙 (`derive_intent`): 우회→stop, pick+place→transfer,
pick만→hold, move 있음→move, home만→home, 확인 요청→clarify, 그 외→none.
정답 의도와 다르더라도 **확인 요청(clarify)이 허용된 사례에서 되물은 것은
의도 오류로 세지 않는다** — 의도를 잘못 확정한 것이 아니라 확정하지 않은 것이다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from core.reason_codes import ReasonCode
from core.resource_catalog import ResourceKind
from core.termination import TerminationRequirement
from planning.output_parser import ClarificationNeeded, draft_from_output
from planning.plan_provider import PlanningContext, PlanningMode
from planning.prompt import OUTPUT_SCHEMA_VERSION, PROMPT_TEMPLATE_VERSION

DECISIONS = ("PASS", "BLOCK", "ASK", "STOP")
INTENTS = ("home", "move", "stop", "transfer", "hold", "clarify", "unsupported", "none")
SPLITS = ("dev", "sealed")
PLAN_VALIDATION_EXPECTATIONS = ("verified", "not_verified")
#: 계획이 리소스를 담는 인자.
_RESOURCE_ARGS = ("to", "from", "object")


# ── 평가셋 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ExpectedStep:
    skill: str
    args: Mapping[str, str] = field(default_factory=dict)

    def key(self) -> tuple[str, tuple[tuple[str, str], ...]]:
        return (self.skill, tuple(sorted(self.args.items())))


@dataclass(frozen=True)
class Expectation:
    """발화 하나의 정답. 값을 만들지 않고 파일에서 읽는다."""

    intent: str
    resources: tuple[str, ...]
    #: 정확히 일치해야 하는 계획. None이면 `core_steps`나 구조 채점 없음.
    steps: tuple[ExpectedStep, ...] | None
    #: 순서대로 포함돼야 하는 핵심 스텝(pick/place 초안용).
    core_steps: tuple[ExpectedStep, ...] | None
    #: 허용 판정. 첫 항목이 기대 판정(혼동 행렬 행)이다.
    decisions: tuple[str, ...]
    #: 허용 이유 코드. 비어 있으면 "이유 코드 없음"이 정답(PASS).
    reason_codes: tuple[str, ...]
    #: 실행 가능 정답. None이면 "관측 판정이 PASS일 때만 실행 가능"이 정답이다
    #: (허용 판정이 여럿인 사례에서 쓴다).
    executable: bool | None
    stop_bypass: bool
    #: pick/place 초안의 계획 검증 기대. None이면 채점하지 않는다.
    plan_validation: str | None = None
    terminal_hold: str | None = None

    @property
    def primary_decision(self) -> str:
        return self.decisions[0]


@dataclass(frozen=True)
class EvalUtterance:
    id: str
    scenario: str
    category: str
    split: str
    utterance: str
    expect: Expectation
    note: str = ""


@dataclass(frozen=True)
class Scenario:
    id: str
    category: str
    title: str
    description: str
    #: 이 시나리오의 발화가 공통으로 따르는 기대. 발화별 기대의 기본값이다.
    defaults: Mapping[str, Any] = field(default_factory=dict)


def _steps_from(rows) -> tuple[ExpectedStep, ...] | None:
    if rows is None:
        return None
    return tuple(ExpectedStep(skill=r["skill"], args=dict(r.get("args") or {})) for r in rows)


def expectation_from(raw: Mapping[str, Any]) -> Expectation:
    decisions = raw["decision"]
    if isinstance(decisions, str):
        decisions = [decisions]
    reasons = raw.get("reason_codes")
    if reasons is None:
        reasons = []
    return Expectation(
        intent=raw["intent"],
        resources=tuple(raw.get("resources") or ()),
        steps=_steps_from(raw.get("steps")),
        core_steps=_steps_from(raw.get("core_steps")),
        decisions=tuple(decisions),
        reason_codes=tuple(reasons),
        executable=(None if raw.get("executable") is None else bool(raw["executable"])),
        stop_bypass=bool(raw.get("stop_bypass", False)),
        plan_validation=raw.get("plan_validation"),
        terminal_hold=raw.get("terminal_hold"),
    )


def load_scenarios(path: Path) -> tuple[Scenario, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(
        Scenario(
            id=row["id"], category=row["category"], title=row["title"],
            description=row.get("description", ""),
            defaults=dict(row.get("defaults") or {}),
        )
        for row in data["scenarios"]
    )


def load_set(path: Path, scenarios: Sequence[Scenario], split: str) -> tuple[EvalUtterance, ...]:
    """발화 파일을 읽는다. 시나리오 기본 기대 위에 발화별 기대를 덮는다."""
    by_id = {s.id: s for s in scenarios}
    out: list[EvalUtterance] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        scenario = by_id[row["scenario"]]
        merged = dict(scenario.defaults)
        merged.update(row.get("expect") or {})
        out.append(
            EvalUtterance(
                id=row["id"], scenario=scenario.id, category=scenario.category,
                split=split, utterance=row["utterance"],
                expect=expectation_from(merged), note=row.get("note", ""),
            )
        )
    return tuple(out)


def validate_dataset(
    scenarios: Sequence[Scenario], dev: Sequence[EvalUtterance],
    sealed: Sequence[EvalUtterance], *, catalog_ids: set[str],
    known_reason_codes: set[str] | None = None,
) -> list[str]:
    """평가셋 자체의 결함을 찾는다. 빈 목록이 정상이다."""
    problems: list[str] = []
    reason_values = known_reason_codes or {r.value for r in ReasonCode}
    scenario_ids = {s.id for s in scenarios}
    if len(scenario_ids) != len(scenarios):
        problems.append("시나리오 id가 중복된다")
    seen_ids: set[str] = set()
    seen_text: dict[str, str] = {}
    used_scenarios: set[str] = set()
    for row in list(dev) + list(sealed):
        if row.id in seen_ids:
            problems.append(f"{row.id}: 발화 id 중복")
        seen_ids.add(row.id)
        text = "".join(row.utterance.split())
        if text in seen_text:
            problems.append(f"{row.id}: 발화 중복({seen_text[text]}과 같다)")
        seen_text[text] = row.id
        if row.scenario not in scenario_ids:
            problems.append(f"{row.id}: 없는 시나리오 {row.scenario}")
        used_scenarios.add(row.scenario)
        if row.split not in SPLITS:
            problems.append(f"{row.id}: split 값 {row.split!r}")
        e = row.expect
        if e.intent not in INTENTS:
            problems.append(f"{row.id}: intent {e.intent!r}")
        if not e.decisions or any(d not in DECISIONS for d in e.decisions):
            problems.append(f"{row.id}: decision {e.decisions!r}")
        for code in e.reason_codes:
            if code not in reason_values:
                problems.append(f"{row.id}: 없는 이유 코드 {code}")
        for rid in e.resources:
            if rid not in catalog_ids:
                problems.append(f"{row.id}: 셀 카탈로그에 없는 자원 {rid}")
        for steps in (e.steps, e.core_steps):
            for step in steps or ():
                for value in step.args.values():
                    if value not in catalog_ids:
                        problems.append(f"{row.id}: 스텝 인자에 없는 자원 {value}")
        if e.plan_validation is not None and e.plan_validation not in PLAN_VALIDATION_EXPECTATIONS:
            problems.append(f"{row.id}: plan_validation {e.plan_validation!r}")
        if "PASS" in e.decisions and e.reason_codes and len(e.decisions) == 1:
            problems.append(f"{row.id}: PASS인데 이유 코드가 있다")
        if e.executable is True and "PASS" not in e.decisions:
            problems.append(f"{row.id}: 실행 가능인데 PASS가 허용되지 않는다")
        if e.stop_bypass and "STOP" not in e.decisions:
            problems.append(f"{row.id}: 정지 우회 기대인데 STOP이 허용되지 않는다")
        if e.steps is None and e.core_steps is None and "PASS" in e.decisions and not e.stop_bypass:
            problems.append(f"{row.id}: PASS 기대인데 기대 계획이 없다")
    dev_text = {"".join(r.utterance.split()) for r in dev}
    for row in sealed:
        if "".join(row.utterance.split()) in dev_text:
            problems.append(f"{row.id}: 봉인셋 발화가 개발셋에도 있다")
    for sid in scenario_ids - used_scenarios:
        problems.append(f"시나리오 {sid}에 발화가 없다")
    for sid in scenario_ids:
        if not any(r.scenario == sid for r in sealed):
            problems.append(f"시나리오 {sid}에 봉인셋 발화가 없다")
    return problems


def leak_scan(sealed: Sequence[EvalUtterance], roots: Sequence[Path],
              *, exclude: Sequence[Path] = ()) -> list[tuple[str, str]]:
    """봉인 발화가 프롬프트·카탈로그·테스트·스크립트·화면에 박혀 있는지 본다.

    공백을 무시하고 문자열 포함으로 비교한다. 봉인 발화 문자열 자체를 이 함수가
    어디에도 쓰지 않는다 — 이 파일에 봉인 발화를 적지 않는다.
    """
    suffixes = {".py", ".js", ".mjs", ".json", ".jsonl", ".html", ".md", ".txt", ".sh"}
    excluded = {Path(p).resolve() for p in exclude}
    texts: list[tuple[Path, str]] = []
    for root in roots:
        root = Path(root)
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for f in files:
            if not f.is_file() or f.suffix not in suffixes:
                continue
            if any(str(f.resolve()).startswith(str(x)) for x in excluded):
                continue
            if "__pycache__" in f.parts or ".venv" in f.parts:
                continue
            try:
                texts.append((f, "".join(f.read_text(encoding="utf-8").split())))
            except (OSError, UnicodeDecodeError):
                continue
    hits: list[tuple[str, str]] = []
    for row in sealed:
        needle = "".join(row.utterance.split())
        for f, body in texts:
            if needle in body:
                hits.append((row.id, str(f)))
    return hits


# ── 관측 ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Observation:
    """`Api.create_plan` 결과에서 읽은 사실. 판정을 다시 계산하지 않는다."""

    ok: bool
    bypassed_model: bool
    decision: str
    reason_code: str | None
    executable: bool
    slots: tuple[str, ...]
    steps: tuple[ExpectedStep, ...]
    terminal_hold: str | None
    clarification: str | None
    consistency: Mapping[str, Any] | None
    plan_validation: Mapping[str, Any] | None
    geometry_decision: str | None
    planning_ms: float | None
    gate_ms: float | None
    total_ms: float
    provider_id: str | None
    model_id: str | None
    served_model_id: str | None
    is_mock: bool | None
    plan_hash: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    detail: str = ""

    @property
    def used_resources(self) -> tuple[str, ...]:
        out: list[str] = []
        for step in self.steps:
            for key in _RESOURCE_ARGS:
                value = step.args.get(key)
                if value and value not in out:
                    out.append(value)
        if self.terminal_hold and self.terminal_hold not in out:
            out.append(self.terminal_hold)
        return tuple(out)

    @property
    def has_pick_place(self) -> bool:
        return any(s.skill in ("pick", "place") for s in self.steps)

    @property
    def mismatch_observed(self) -> bool:
        """계획(초안 포함)이 발화에서 확인되지 않은 자원을 썼는가."""
        confirmed = set(self.slots)
        return any(r not in confirmed for r in self.used_resources)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "bypassed_model": self.bypassed_model,
            "decision": self.decision, "reason_code": self.reason_code,
            "executable": self.executable, "slots": list(self.slots),
            "steps": [{"skill": s.skill, "args": dict(s.args)} for s in self.steps],
            "terminal_hold": self.terminal_hold, "clarification": self.clarification,
            "consistency_status": None if not self.consistency else self.consistency.get("status"),
            "only_in_plan": [] if not self.consistency else list(self.consistency.get("only_in_plan") or ()),
            "plan_validation": None if self.plan_validation is None else {
                "available": self.plan_validation.get("available"),
                "plan_verified": self.plan_validation.get("plan_verified"),
                "execution_allowed": self.plan_validation.get("execution_allowed"),
                "reason_codes": list(self.plan_validation.get("reason_codes") or ()),
                "checks_passed": self.plan_validation.get("checks_passed"),
                "stage_count": self.plan_validation.get("stage_count"),
                "snapshot_id": (self.plan_validation.get("snapshot") or {}).get("snapshot_id")
                if isinstance(self.plan_validation.get("snapshot"), dict) else None,
            },
            "geometry_decision": self.geometry_decision,
            "planning_ms": self.planning_ms, "gate_ms": self.gate_ms,
            "total_ms": self.total_ms,
            "provider_id": self.provider_id, "model_id": self.model_id,
            "served_model_id": self.served_model_id, "is_mock": self.is_mock,
            "plan_hash": self.plan_hash,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "mismatch_observed": self.mismatch_observed,
            "detail": self.detail,
        }


def observed_decision(payload: Mapping[str, Any], *, bypassed: bool) -> str:
    if bypassed:
        return "STOP"
    if payload.get("ok"):
        value = ((payload.get("validation") or {}).get("decision") or "").lower()
        return {"allow": "PASS", "block": "BLOCK", "ask": "ASK"}.get(value, "BLOCK")
    if payload.get("reason_code") == ReasonCode.PLAN_CLARIFICATION_REQUIRED.value:
        return "ASK"
    return "BLOCK"


def derive_intent(obs: Observation) -> str:
    if obs.bypassed_model:
        return "stop"
    skills = [s.skill for s in obs.steps]
    if not skills:
        if obs.reason_code == ReasonCode.PLAN_CLARIFICATION_REQUIRED.value:
            return "clarify"
        if obs.reason_code in (ReasonCode.PLAN_UNSUPPORTED_SKILL.value,
                               ReasonCode.ROBOT_SKILL_UNSUPPORTED.value):
            return "unsupported"
        return "none"
    if "pick" in skills and "place" in skills:
        return "transfer"
    if "pick" in skills or obs.terminal_hold:
        return "hold"
    if "place" in skills:
        return "transfer"
    if "move" in skills:
        return "move"
    if all(s == "home" for s in skills):
        return "home"
    if "stop" in skills:
        return "stop"
    return "none"


def observation_from_payload(
    payload: Mapping[str, Any], *, bypassed: bool, planning_ms: float | None,
    gate_ms: float | None, total_ms: float, attempt: Mapping[str, Any] | None,
    geometry_decision: str | None = None,
) -> Observation:
    """create_plan 결과와 시도 기록에서 관측을 만든다."""
    ok = bool(payload.get("ok"))
    if ok:
        plan = payload.get("plan") or {}
        steps = tuple(
            ExpectedStep(skill=s["skill"], args=dict(s.get("args") or {}))
            for s in plan.get("steps") or ()
        )
        terminal_hold = plan.get("terminal_hold")
        validation = payload.get("validation") or {}
        reason = validation.get("reason_code")
        executable = bool(payload.get("executable"))
        consistency = payload.get("consistency")
        plan_hash = plan.get("plan_hash")
        clarification = payload.get("clarification")
        detail = validation.get("detail") or ""
        geometry = ((validation.get("geometry") or {}).get("decision")
                    if geometry_decision is None else geometry_decision)
    else:
        steps = tuple(
            ExpectedStep(skill=s["skill"], args=dict(s.get("args") or {}))
            for s in payload.get("draft_steps") or ()
        )
        terminal_hold = None
        reason = payload.get("reason_code")
        executable = False
        consistency = None
        plan_hash = None
        clarification = payload.get("clarification")
        detail = payload.get("detail") or ""
        geometry = None
    if ok:
        # 성공 응답은 슬롯을 자원 대조(consistency.request_resources)에 담는다.
        rows = (payload.get("consistency") or {}).get("request_resources") or ()
    else:
        rows = (payload.get("slots") or {}).get("matches") or ()
    slots = tuple(m.get("resource_id") for m in rows if m.get("resource_id"))
    distinct: list[str] = []
    for rid in slots:
        if rid not in distinct:
            distinct.append(rid)
    attempt = attempt or {}
    return Observation(
        ok=ok, bypassed_model=bypassed,
        decision=observed_decision(payload, bypassed=bypassed),
        reason_code=reason, executable=executable, slots=tuple(distinct),
        steps=steps, terminal_hold=terminal_hold, clarification=clarification,
        consistency=consistency, plan_validation=payload.get("plan_validation"),
        geometry_decision=geometry,
        planning_ms=planning_ms, gate_ms=gate_ms, total_ms=total_ms,
        provider_id=attempt.get("provider_id"), model_id=attempt.get("model_id"),
        served_model_id=attempt.get("served_model_id"),
        is_mock=attempt.get("is_mock"), plan_hash=plan_hash,
        prompt_tokens=attempt.get("prompt_tokens"),
        completion_tokens=attempt.get("completion_tokens"),
        detail=detail,
    )


# ── 채점 ───────────────────────────────────────────────────────────────────
def _subsequence(core: Sequence[ExpectedStep], actual: Sequence[ExpectedStep]) -> bool:
    keys = [s.key() for s in actual]
    pos = 0
    for step in core:
        k = step.key()
        while pos < len(keys) and keys[pos] != k:
            pos += 1
        if pos >= len(keys):
            return False
        pos += 1
    return True


@dataclass(frozen=True)
class CaseResult:
    case: EvalUtterance
    obs: Observation

    @property
    def derived_intent(self) -> str:
        return derive_intent(self.obs)

    @property
    def intent_ok(self) -> bool:
        derived = self.derived_intent
        if derived == self.case.expect.intent:
            return True
        return derived == "clarify" and "ASK" in self.case.expect.decisions

    @property
    def resources_ok(self) -> bool:
        return set(self.obs.slots) == set(self.case.expect.resources)

    @property
    def structure_ok(self) -> bool | None:
        e = self.case.expect
        if e.steps is not None:
            if not self.obs.steps:
                return False
            same = [s.key() for s in self.obs.steps] == [s.key() for s in e.steps]
            return same and self.obs.terminal_hold == e.terminal_hold
        if e.core_steps is not None:
            if not self.obs.steps:
                return False
            return _subsequence(e.core_steps, self.obs.steps)
        return None

    @property
    def decision_ok(self) -> bool:
        return self.obs.decision in self.case.expect.decisions

    @property
    def reason_ok(self) -> bool:
        e = self.case.expect
        if self.obs.decision == "STOP":
            return e.stop_bypass
        if self.obs.reason_code is None:
            # 이유 코드 없음은 PASS의 모양이다. PASS가 허용된 사례에서만 정답이다.
            return "PASS" in e.decisions and self.obs.decision == "PASS"
        return self.obs.reason_code in e.reason_codes

    @property
    def executable_ok(self) -> bool:
        want = self.case.expect.executable
        if want is None:
            want = self.obs.decision == "PASS"
        return self.obs.executable == want

    @property
    def stop_ok(self) -> bool:
        return self.obs.bypassed_model == self.case.expect.stop_bypass

    @property
    def plan_validation_ok(self) -> bool | None:
        want = self.case.expect.plan_validation
        if want is None:
            return None
        if not self.obs.has_pick_place:
            # pick/place 초안이 없으면(확인 요청, 이동만 만든 경우) 펼칠 계획이
            # 없다. 채점 대상이 아니다 — 그 문제는 판정·의도 채점이 잡는다.
            return None
        pv = self.obs.plan_validation
        if not pv or not pv.get("available"):
            return False
        verified = bool(pv.get("plan_verified"))
        return verified if want == "verified" else not verified

    @property
    def execution_block_ok(self) -> bool | None:
        """pick/place 사례: 실행이 실제로 막혔는가. 계획 검증과 별도 정답."""
        if self.case.expect.plan_validation is None or not self.obs.has_pick_place:
            return None
        return (not self.obs.executable
                and self.obs.decision in ("BLOCK", "ASK")
                and (self.obs.plan_validation or {}).get("execution_allowed") is not True)

    @property
    def mismatch_blocked(self) -> bool | None:
        if not self.obs.mismatch_observed:
            return None
        return self.obs.decision != "PASS"

    @property
    def all_ok(self) -> bool:
        checks = [self.intent_ok, self.resources_ok, self.decision_ok,
                  self.reason_ok, self.executable_ok, self.stop_ok]
        for v in (self.structure_ok, self.plan_validation_ok, self.execution_block_ok):
            if v is not None:
                checks.append(v)
        return all(checks)

    def problems(self) -> list[str]:
        out: list[str] = []
        e = self.case.expect
        if not self.intent_ok:
            out.append(f"의도: 기대 {e.intent}, 관측 {self.derived_intent}")
        if not self.resources_ok:
            out.append(f"자원: 기대 {sorted(e.resources)}, 관측 {sorted(self.obs.slots)}")
        if self.structure_ok is False:
            out.append("계획 구조가 기대와 다르다")
        if not self.decision_ok:
            out.append(f"판정: 기대 {'/'.join(e.decisions)}, 관측 {self.obs.decision}")
        if not self.reason_ok:
            out.append(f"이유: 기대 {list(e.reason_codes) or '없음'}, 관측 {self.obs.reason_code}")
        if not self.executable_ok:
            out.append(f"실행 가능: 기대 {'판정에 따름' if e.executable is None else e.executable},"
                       f" 관측 {self.obs.executable}")
        if not self.stop_ok:
            out.append(f"정지 우회: 기대 {e.stop_bypass}, 관측 {self.obs.bypassed_model}")
        if self.plan_validation_ok is False:
            pv = self.obs.plan_validation or {}
            out.append(f"계획 검증: 기대 {e.plan_validation}, 관측 "
                       f"available={pv.get('available')} verified={pv.get('plan_verified')}")
        if self.execution_block_ok is False:
            out.append("pick/place 실행 차단이 관측되지 않았다")
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.case.id, "scenario": self.case.scenario,
            "category": self.case.category, "split": self.case.split,
            "utterance": self.case.utterance,
            "expected": {
                "intent": self.case.expect.intent,
                "resources": list(self.case.expect.resources),
                "decision": list(self.case.expect.decisions),
                "reason_codes": list(self.case.expect.reason_codes),
                "executable": self.case.expect.executable,
                "stop_bypass": self.case.expect.stop_bypass,
                "plan_validation": self.case.expect.plan_validation,
            },
            "observed": self.obs.to_dict(),
            "derived_intent": self.derived_intent,
            "checks": {
                "intent": self.intent_ok, "resources": self.resources_ok,
                "structure": self.structure_ok, "decision": self.decision_ok,
                "reason": self.reason_ok, "executable": self.executable_ok,
                "stop_bypass": self.stop_ok,
                "plan_validation": self.plan_validation_ok,
                "execution_block": self.execution_block_ok,
                "mismatch_blocked": self.mismatch_blocked,
            },
            "all_ok": self.all_ok,
            "problems": self.problems(),
        }


def _rate(values: Sequence[bool | None]) -> float | None:
    rows = [v for v in values if v is not None]
    return (sum(1 for v in rows if v) / len(rows)) if rows else None


def quantiles(values: Sequence[float]) -> dict | None:
    if not values:
        return None
    ordered = sorted(values)

    def q(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        pos = p * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

    return {"n": len(ordered), "min": ordered[0], "median": q(0.5),
            "p90": q(0.9), "max": ordered[-1], "mean": sum(ordered) / len(ordered)}


@dataclass
class RunSummary:
    """한 공급자·한 세트의 집계. 실제 모델과 double을 섞지 않는다."""

    provider_label: str
    is_mock: bool
    split: str
    results: list[CaseResult] = field(default_factory=list)

    def add(self, result: CaseResult) -> None:
        self.results.append(result)

    def metrics(self) -> dict:
        r = self.results
        return {
            "intent_accuracy": _rate([x.intent_ok for x in r]),
            "resource_accuracy": _rate([x.resources_ok for x in r]),
            "plan_structure_accuracy": _rate([x.structure_ok for x in r]),
            "decision_accuracy": _rate([x.decision_ok for x in r]),
            "reason_code_accuracy": _rate([x.reason_ok for x in r]),
            "executable_accuracy": _rate([x.executable_ok for x in r]),
            "mismatch_block_rate": _rate([x.mismatch_blocked for x in r]),
            "mismatch_observed_count": sum(1 for x in r if x.obs.mismatch_observed),
            "stop_bypass_accuracy": _rate([x.stop_ok for x in r]),
            "plan_validation_accuracy": _rate([x.plan_validation_ok for x in r]),
            "execution_block_accuracy": _rate([x.execution_block_ok for x in r]),
            "all_checks_pass_rate": _rate([x.all_ok for x in r]),
            "case_count": len(r),
        }

    def confusion(self) -> dict:
        """기대 판정(첫 항목) × 관측 판정."""
        table = {row: {col: 0 for col in DECISIONS} for row in DECISIONS}
        for x in self.results:
            table[x.case.expect.primary_decision][x.obs.decision] += 1
        return table

    def by_category(self) -> dict:
        out: dict[str, dict] = {}
        for x in self.results:
            bucket = out.setdefault(x.case.category, {"n": 0, "all_ok": 0, "decision_ok": 0})
            bucket["n"] += 1
            bucket["all_ok"] += int(x.all_ok)
            bucket["decision_ok"] += int(x.decision_ok)
        return out

    def timings(self) -> dict:
        planned = [x.obs.planning_ms for x in self.results
                   if x.obs.planning_ms is not None and not x.obs.bypassed_model]
        gate = [x.obs.gate_ms for x in self.results if x.obs.gate_ms is not None]
        total = [x.obs.total_ms for x in self.results]
        return {
            "planning_ms": quantiles(planned),
            "gate_ms": quantiles(gate),
            "total_ms": quantiles(total),
            # 이 평가는 실행하지 않는다. 실행 시간은 별도 지표 자리만 둔다.
            "gazebo_execution_ms": None,
        }

    def failures(self, *, repro_prefix: str) -> list[dict]:
        out: list[dict] = []
        for x in self.results:
            if x.all_ok:
                continue
            out.append({
                "id": x.case.id, "scenario": x.case.scenario,
                "category": x.case.category, "utterance": x.case.utterance,
                "problems": x.problems(),
                "expected": x.to_dict()["expected"],
                "observed_decision": x.obs.decision,
                "observed_reason_code": x.obs.reason_code,
                "observed_steps": [{"skill": s.skill, "args": dict(s.args)} for s in x.obs.steps],
                "observed_slots": list(x.obs.slots),
                "clarification": x.obs.clarification,
                "detail": x.obs.detail,
                "repro": f"{repro_prefix} --only {x.case.id}",
            })
        return out

    def to_dict(self, *, repro_prefix: str) -> dict:
        return {
            "provider_label": self.provider_label,
            "is_mock": self.is_mock,
            "split": self.split,
            "metrics": self.metrics(),
            "confusion": self.confusion(),
            "by_category": self.by_category(),
            "timings": self.timings(),
            "failures": self.failures(repro_prefix=repro_prefix),
            "cases": [x.to_dict() for x in self.results],
        }


def repeat_agreement(first: Sequence[CaseResult], second: Sequence[CaseResult]) -> dict:
    """같은 세트를 두 번 돌렸을 때 판정·계획이 같은 비율."""
    by_id = {x.case.id: x for x in second}
    same_decision = 0
    same_plan = 0
    compared = 0
    differing: list[str] = []
    for x in first:
        y = by_id.get(x.case.id)
        if y is None:
            continue
        compared += 1
        d = x.obs.decision == y.obs.decision and x.obs.reason_code == y.obs.reason_code
        p = [s.key() for s in x.obs.steps] == [s.key() for s in y.obs.steps]
        same_decision += int(d)
        same_plan += int(p)
        if not (d and p):
            differing.append(x.case.id)
    return {
        "compared": compared,
        "decision_agreement": (same_decision / compared) if compared else None,
        "plan_agreement": (same_plan / compared) if compared else None,
        "differing": differing,
    }


# ── test double ─────────────────────────────────────────────────────────────
@dataclass
class RuleDoubleProvider:
    """규칙 기반 deterministic test double. **실제 모델이 아니다.**

    슬롯 개수와 몇 개의 표현으로만 초안을 만든다. `is_mock=True`가 붙고 집계는
    실제 모델과 섞이지 않는다. 품질 점수를 대신하지 않는다 — 평가 하니스와
    관문이 동작하는지 확인하는 기준선이다.
    """

    termination: TerminationRequirement | None = None
    home_words: tuple[str, ...] = ("홈", "복귀", "안전 위치", "안전위치", "원위치")
    hold_words: tuple[str, ...] = ("들고 있", "잡고 있", "쥐고 있")
    label: str = "double-rule"

    @property
    def provider_id(self) -> str:
        return self.label

    @property
    def prompt_template_version(self) -> str:
        return PROMPT_TEMPLATE_VERSION

    @property
    def output_schema_version(self) -> str:
        return OUTPUT_SCHEMA_VERSION

    @property
    def is_mock(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        return "double-rule-based"

    def _steps(self, context: PlanningContext) -> tuple[list[dict], str | None]:
        slots = context.slots
        objects = slots.distinct(ResourceKind.OBJECT)
        locations = slots.distinct(ResourceKind.LOCATION)
        text = context.utterance
        hold = any(w in text for w in self.hold_words)
        if not objects and not locations:
            if any(w in text for w in self.home_words):
                return [], None
            raise ClarificationNeeded("대상 위치나 자재를 확인할 수 없습니다")
        if not objects:
            return [{"skill": "move", "args": {"to": loc}} for loc in locations], None
        if len(locations) >= 2:
            return [
                {"skill": "move", "args": {"to": locations[0]}},
                {"skill": "pick", "args": {"object": objects[0], "from": locations[0]}},
                {"skill": "move", "args": {"to": locations[-1]}},
                {"skill": "place", "args": {"object": objects[0], "to": locations[-1]}},
            ], None
        if len(locations) == 1 and hold:
            return [
                {"skill": "move", "args": {"to": locations[0]}},
                {"skill": "pick", "args": {"object": objects[0], "from": locations[0]}},
            ], objects[0]
        raise ClarificationNeeded("자재를 어디에서 어디로 옮길지 확인할 수 없습니다")

    def generate(self, context: PlanningContext):
        steps, terminal_hold = self._steps(context)
        if self.termination is not None and self.termination.final_skill \
                and self.termination.final_skill in context.allowed_skills \
                and terminal_hold is None:
            steps.append({"skill": self.termination.final_skill, "args": {}})
        if not steps:
            raise ClarificationNeeded("만들 수 있는 계획이 없습니다")
        return draft_from_output(
            {"output_schema_version": OUTPUT_SCHEMA_VERSION, "result": "plan",
             "steps": steps, "terminal_hold": terminal_hold},
            provider_id=self.provider_id, model_id=self.model_name,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            output_schema_version=OUTPUT_SCHEMA_VERSION,
            mode=PlanningMode.FUNCTION_CALLING, latency_sec=0.0, is_mock=True,
            notes={"rule": "slot-count"},
        )


@dataclass
class SubstitutingDoubleProvider(RuleDoubleProvider):
    """**일부러 틀리는** double. 요청에 없는 자원을 계획에 넣는다.

    확인 요청 대신 카탈로그의 첫 위치를 골라 넣는다 — 모델이 없는 위치를
    비슷한 위치로 치환하는 실패를 재현한다. 관문(E-REQ-001)이 이것을 전부
    막는지 재는 데만 쓴다. `is_mock=True`.
    """

    label: str = "double-substituting"

    @property
    def model_name(self) -> str:
        return "double-substituting"

    def _steps(self, context: PlanningContext):
        slots = context.slots
        objects = slots.distinct(ResourceKind.OBJECT)
        locations = list(slots.distinct(ResourceKind.LOCATION))
        text = context.utterance
        if not objects and not locations and any(w in text for w in self.home_words):
            return [], None
        fallback = [loc for loc in context.allowed_locations if loc not in locations]
        if not objects:
            target = locations[0] if locations else fallback[0]
            return [{"skill": "move", "args": {"to": target}}], None
        while len(locations) < 2 and fallback:
            locations.append(fallback.pop(0))
        return [
            {"skill": "move", "args": {"to": locations[0]}},
            {"skill": "pick", "args": {"object": objects[0], "from": locations[0]}},
            {"skill": "move", "args": {"to": locations[-1]}},
            {"skill": "place", "args": {"object": objects[0], "to": locations[-1]}},
        ], None


# ── 보고서 ─────────────────────────────────────────────────────────────────
def _fmt(value, spec="{:.3f}") -> str:
    return "없음" if value is None else spec.format(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# 작업 셀 명령 평가 — {report['run_id']}")
    add("")
    add("> **시뮬레이터·계획 단계 평가다.** 실제 로봇에 연결하지 않았고 명령을 보내지"
        " 않았다. pick/place는 계획 검증과 실행 차단을 **따로** 채점하며, 어느 쪽도"
        " 실제 하드웨어 검증이나 pick/place 실행 허용을 뜻하지 않는다.")
    add("")
    add("## 버전")
    add("")
    add("| 항목 | 값 |")
    add("|---|---|")
    for key, value in report["versions"].items():
        add(f"| `{key}` | `{value}` |")
    add("")
    for run in report["runs"]:
        m = run["metrics"]
        add(f"## {run['provider_label']} · {run['split']} 세트"
            f" ({'test double' if run['is_mock'] else '실제 모델'})")
        add("")
        if run["is_mock"]:
            add("> test double 결과다. **실제 모델 성능으로 읽지 않는다.**")
            add("")
        add("| 지표 | 값 |")
        add("|---|---|")
        for key in ("intent_accuracy", "resource_accuracy", "plan_structure_accuracy",
                    "decision_accuracy", "reason_code_accuracy", "executable_accuracy",
                    "mismatch_block_rate", "stop_bypass_accuracy",
                    "plan_validation_accuracy", "execution_block_accuracy",
                    "all_checks_pass_rate"):
            add(f"| {key} | {_fmt(m.get(key))} |")
        add(f"| mismatch_observed_count | {m.get('mismatch_observed_count')} |")
        add(f"| case_count | {m.get('case_count')} |")
        add("")
        t = run["timings"]
        add("| 시간(ms) | n | median | p90 | max |")
        add("|---|---|---|---|---|")
        for key in ("planning_ms", "gate_ms", "total_ms"):
            q = t.get(key)
            if q:
                add(f"| {key} | {q['n']} | {q['median']:.0f} | {q['p90']:.0f} | {q['max']:.0f} |")
            else:
                add(f"| {key} | 0 | — | — | — |")
        add("| gazebo_execution_ms | — | 측정하지 않음(이 평가는 실행하지 않는다) | | |")
        add("")
        add("혼동 행렬 (행 = 기대 판정, 열 = 관측 판정)")
        add("")
        add("| 기대 \\ 관측 | " + " | ".join(DECISIONS) + " |")
        add("|---|" + "---|" * len(DECISIONS))
        for row in DECISIONS:
            add(f"| {row} | " + " | ".join(str(run["confusion"][row][c]) for c in DECISIONS) + " |")
        add("")
        add("분류별")
        add("")
        add("| 분류 | n | 전체 통과 | 판정 일치 |")
        add("|---|---|---|---|")
        for name, b in run["by_category"].items():
            add(f"| {name} | {b['n']} | {b['all_ok']} | {b['decision_ok']} |")
        add("")
        fails = run["failures"]
        add(f"실패 사례 {len(fails)}건")
        add("")
        for f in fails:
            add(f"- **{f['id']}** ({f['category']}) “{f['utterance']}”")
            for p in f["problems"]:
                add(f"  - {p}")
            add(f"  - 관측: {f['observed_decision']} / {f['observed_reason_code']}"
                + (f" / 질문: {f['clarification']}" if f.get("clarification") else ""))
            add(f"  - 재현: `{f['repro']}`")
        add("")
    if report.get("repeat"):
        add("## 재현성 (같은 세트 2회)")
        add("")
        for label, r in report["repeat"].items():
            add(f"- {label}: 비교 {r['compared']}건 · 판정 일치 {_fmt(r['decision_agreement'])}"
                f" · 계획 일치 {_fmt(r['plan_agreement'])}"
                + (f" · 다른 사례 {r['differing']}" if r["differing"] else ""))
        add("")
    if report.get("voice"):
        add("## 음성 평가")
        add("")
        add(f"- {report['voice']['status']}")
        add("")
    add("## 남는 것")
    add("")
    add("- `real_hardware_ready=false` · `real_hardware_verified=false` — 이 평가는 바꾸지 않는다.")
    add("- 일반 API pick/place는 `capability.profile_incomplete`로 계속 차단된다.")
    return "\n".join(lines) + "\n"
