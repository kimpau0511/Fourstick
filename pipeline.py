"""
포스틱 L1~L2 파이프라인 공통 로직.
test_taskplan.py(CLI 테스트)와 api_server.py(FastAPI 서버) 양쪽에서 이 모듈을 가져다 쓴다.
"""

import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal, Union, Annotated, Optional

import requests
from pydantic import BaseModel, Field, ValidationError

# ============================================================
# Capability Profile — 이 로봇/설비가 실제로 알고 있는 위치·물건 목록.
# ⚠️ 지금은 테스트용 하드코딩. 나중에 실제 설비 등록 체계(DB/config)가
# 생기면 그걸 로드하는 걸로 교체.
# 이 목록이 L2 출력 스키마의 enum 제약으로 그대로 들어가서,
# LLM이 목록 밖의 위치/물건 이름을 "지어내는" 것을 구조적으로 차단한다.
# ============================================================

CAPABILITY_PROFILE = {
    "locations": ["1번 팔레트", "2번 팔레트", "3번 팔레트", "컨베이어"],
    "objects": ["A자재", "B자재", "C자재"],
}

LocationName = Literal[tuple(CAPABILITY_PROFILE["locations"])]
ObjectName = Literal[tuple(CAPABILITY_PROFILE["objects"])]

# ============================================================
# 감사 로그 — 성공/게이트 거부/오류 모든 시도를 JSONL로 남긴다.
# 성공한 계획만 남기면 감사 로그가 아니다 — 뭘 거부했는지도 추적 대상.
# ============================================================

AUDIT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_log.jsonl")

def write_audit_log(event: str, reason_code: Optional[str], utterance: str,
                     confidence: float, detail: Optional[dict] = None):
    """event: 'SUCCESS' | 'GATED' | 'ERROR'. 한 줄 = 이벤트 하나(JSONL)."""
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "reason_code": reason_code,
        "utterance": utterance,
        "stt_confidence": confidence,
        "detail": detail or {},
    }
    with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def print_audit_log_tail(n: int = 5):
    """최근 n개 감사 로그 항목을 콘솔에 출력 (동작 확인용 편의 함수)."""
    if not os.path.exists(AUDIT_LOG_PATH):
        print(f"[감사로그] 아직 로그 파일이 없음: {AUDIT_LOG_PATH}")
        return
    with open(AUDIT_LOG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    print(f"[감사로그] 최근 {min(n, len(lines))}건 (전체 {len(lines)}건, 파일: {AUDIT_LOG_PATH}):")
    for line in lines[-n:]:
        entry = json.loads(line)
        print(f"  - {entry['logged_at']} [{entry['event']}/{entry['reason_code']}] {entry['utterance']!r}")

# ============================================================
# L1: 입력 처리 (음성/텍스트 → 텍스트 → 슬롯 추출)
# ============================================================

def transcribe_text_stub(raw_input: str) -> tuple[str, float]:
    """텍스트를 직접 입력할 때 쓰는 통과용 스텁 (마이크 없이 텍스트로 테스트할 때 사용).
    신뢰도는 항상 1.0으로 고정."""
    return raw_input, 1.0

_stt_model = None

def _get_stt_model():
    """faster-whisper 모델을 최초 1회만 로드해서 재사용."""
    global _stt_model
    if _stt_model is None:
        from faster_whisper import WhisperModel
        # GPU는 vLLM이 이미 거의 다 쓰고 있으므로(VRAM 8GB 빠듯) STT는 CPU로 돌린다.
        _stt_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _stt_model

# STT가 "1번 팔레트", "A자재", "컨베이어" 같은 도메인 전용 단어를 일반 단어로
# 잘못 알아듣는 문제(예: "1번"→"이번", "컨베이어"→"컴퓨터")를 줄이기 위한
# Whisper용 initial_prompt. Whisper 계열 모델은 프롬프트에 있는 단어를
# 실제 발화에서 더 잘 알아듣는 경향이 있음(모델 재학습 없이 어휘를
# "힌트"로 주는 표준적인 방법). Capability Profile이 바뀌면 이 프롬프트도
# 자동으로 같이 바뀌도록 CAPABILITY_PROFILE에서 직접 만든다.
_STT_INITIAL_PROMPT = (
    "다음은 창고 로봇 작업 지시 음성입니다. "
    + ", ".join(CAPABILITY_PROFILE["locations"] + CAPABILITY_PROFILE["objects"])
    + " 같은 위치와 물건 이름이 자주 나옵니다. "
    "집어서, 옮겨줘, 올려줘, 놔줘, 정지, 멈춰, 그만 같은 표현도 자주 나옵니다."
)

def transcribe_audio(audio_path: str) -> tuple[str, float]:
    """faster-whisper로 녹음 파일을 텍스트로 변환.
    confidence는 세그먼트별 avg_logprob(로그확률)을 확률값으로 환산한
    평균으로 근사한 값 — 정확한 통계적 신뢰도는 아니고 대략적인 지표.
    initial_prompt로 Capability Profile 어휘(위치/물건 이름)를 힌트로 줘서,
    "1번"→"이번", "컨베이어"→"컴퓨터"처럼 도메인 용어를 일반 단어로
    잘못 알아듣는 걸 줄인다."""
    model = _get_stt_model()
    segments, info = model.transcribe(
        audio_path,
        language="ko",
        initial_prompt=_STT_INITIAL_PROMPT,
        vad_filter=True,  # 녹음 앞뒤 무음 구간에서 엉뚱한 텍스트를 지어내는(hallucination) 것 방지
    )
    segments = list(segments)
    text = "".join(seg.text for seg in segments).strip()
    if segments:
        avg_logprob = sum(seg.avg_logprob for seg in segments) / len(segments)
        confidence = math.exp(avg_logprob)
    else:
        confidence = 0.0
    return text, confidence

class Slots:
    def __init__(self):
        self.action: Optional[str] = None
        self.object: Optional[str] = None
        self.from_location: Optional[str] = None
        self.to_location: Optional[str] = None
        self.quantity: int = 1

    def __repr__(self):
        return (f"Slots(action={self.action!r}, object={self.object!r}, "
                f"from={self.from_location!r}, to={self.to_location!r}, qty={self.quantity})")

def _find_locations_with_role(text: str) -> tuple[Optional[str], Optional[str]]:
    """CAPABILITY_PROFILE에 있는 위치 이름을 문장에서 직접 찾고, 뒤따르는
    조사/문맥으로 출발지(from)인지 도착지(to)인지 판단한다.

    이전 버전은 "OO에서 OO를 집어서 OO에 올려줘"처럼 정확한 문장 구조(조사+
    동사)를 정규식으로 강제해서, 표현이 조금만 달라도(예: "컨베이어로 옮겨줘",
    "팔레트에 있는 A자재 꺼내") 슬롯을 아예 못 뽑았다. 위치 이름 자체는
    Capability Profile 안의 고정된 4개 문자열이므로, 문장 구조를 강제하는
    대신 그 문자열이 어디 있는지 직접 찾는 게 훨씬 다양한 표현에 강하다."""
    found = []
    for loc in CAPABILITY_PROFILE["locations"]:
        idx = text.find(loc)
        if idx != -1:
            found.append((idx, loc))
    found.sort()

    src, dst = None, None
    for idx, loc in found:
        after = text[idx + len(loc): idx + len(loc) + 15]
        if re.match(r"\s*에서", after):
            if src is None:
                src = loc
        elif re.match(r"\s*(으로|로)\b", after):
            # "~로/으로"는 거의 항상 방향(도착지)을 뜻한다.
            if dst is None:
                dst = loc
        elif re.match(r"\s*에\s*있", after):
            # "~에 있는" 식 서술은 방향을 단정하지 않고, 아래 순서 보완에 맡긴다
            # (예: "1번 팔레트에 있는 A자재" — 여기서 "에"는 도착지가 아니라
            # 그냥 A자재가 지금 어디 있는지를 설명하는 것뿐).
            pass
        elif re.match(r"\s*에", after) and re.search(
            r"(옮기|올리|놓|놔|두|둬|가져|나르|전달|내리|실어|싣)", after
        ):
            # "~에 [뭔가를] 놔/올려/실어" 처럼 동사가 바로 안 붙고 목적어가
            # 끼어 있어도, 뒤쪽에 놓기/싣기 계열 동사가 나오면 도착지로 본다.
            if dst is None:
                dst = loc

    # 조사로 역할을 못 정한 나머지는 언급 순서로 보완 (먼저 나온 쪽이 출발지).
    remaining = [loc for _, loc in found if loc != src and loc != dst]
    if src is None and remaining:
        src, remaining = remaining[0], remaining[1:]
    if dst is None and remaining:
        dst = remaining[-1]

    return src, dst

def extract_slots(text: str) -> Slots:
    s = Slots()

    m = re.search(r"(\d+)\s*개", text)
    if m:
        s.quantity = int(m.group(1))

    if any(k in text for k in ["정지", "멈춰", "스톱", "그만"]):
        s.action = "stop"
        return s

    s.from_location, s.to_location = _find_locations_with_role(text)

    # 물건도 마찬가지로 Capability Profile 목록에 있는 이름을 문장에서 직접
    # 찾는다 — "집어서"/"꺼내서"/"가져다가"/"실어서" 등 어떤 동사를 쓰든
    # 물건 이름 자체만 언급되면 인식됨.
    for obj in CAPABILITY_PROFILE["objects"]:
        if obj in text:
            s.object = obj
            break

    if s.object or s.from_location or s.to_location:
        s.action = "transfer"

    return s

def check_slots_complete(slots: Slots) -> Optional[str]:
    """필수 슬롯이 부족하면 되물음 사유 코드를 반환. 충분하면 None."""
    if not slots.object:
        return "A-SLOT"
    if not slots.from_location and not slots.to_location:
        return "A-SLOT"
    return None

def check_slots_known(slots: Slots) -> Optional[str]:
    """슬롯 값이 채워져 있어도 Capability Profile 목록에 없는 값이면 거부.
    (실제로 STT 오인식으로 "2번 팔레트"가 "팔레트"로 깨져서 들어왔을 때,
    LLM이 enum 제약 때문에 엉뚱한 값(예: 출발지·도착지 둘 다 컨베이어)으로
    조용히 대체해버리는 걸 방지하기 위한 방어선. check_slots_complete는
    "비어있는지"만 보고, 이 함수는 "목록에 실제로 있는 값인지"를 본다."""
    if slots.object and slots.object not in CAPABILITY_PROFILE["objects"]:
        return "A-UNKNOWN-OBJ"
    if slots.from_location and slots.from_location not in CAPABILITY_PROFILE["locations"]:
        return "A-UNKNOWN-LOC"
    if slots.to_location and slots.to_location not in CAPABILITY_PROFILE["locations"]:
        return "A-UNKNOWN-LOC"
    return None

# ============================================================
# L2: Task Plan 스키마 (원자 스킬 5종)
# target/object/from/to는 Capability Profile의 enum으로 제약된다.
# ============================================================

class MoveArgs(BaseModel):
    target: LocationName
    speed: str | None = None

class PickArgs(BaseModel):
    object: ObjectName
    from_: LocationName | None = Field(None, alias="from")

class PlaceArgs(BaseModel):
    object: ObjectName
    to: LocationName
    orientation: str | None = None

class EmptyArgs(BaseModel):
    pass

class HomeStep(BaseModel):
    skill: Literal["home"]
    args: EmptyArgs

class MoveStep(BaseModel):
    skill: Literal["move"]
    args: MoveArgs

class PickStep(BaseModel):
    skill: Literal["pick"]
    args: PickArgs

class PlaceStep(BaseModel):
    skill: Literal["place"]
    args: PlaceArgs

class StopStep(BaseModel):
    skill: Literal["stop"]
    args: EmptyArgs

Step = Annotated[
    Union[HomeStep, MoveStep, PickStep, PlaceStep, StopStep],
    Field(discriminator="skill"),
]

class TaskPlanDraft(BaseModel):
    intent: str
    steps: list[Step]

schema = TaskPlanDraft.model_json_schema()

MODEL_ID = "/home/asd/models/exaone-3.5-7.8b-awq"

SYSTEM_PROMPT = (
    "너는 산업용 로봇의 작업 계획을 세우는 도우미다. "
    "반드시 home, move, pick, place, stop 다섯 개 스킬만 조합해서 계획을 세운다. "
    "위치나 물건 이름은 절대 지어내지 말고, 사용자 지시문에 언급된 이름만 그대로 사용한다. "
    "불필요한 중간 이동 없이 최소 단계로 구성한다."
)

def normalize_plan(plan: dict) -> dict:
    """LLM 출력은 신뢰하지 않는다 — 마지막 스텝이 정확히 home 스킬인지
    코드로 강제 검사/보정한다. 프롬프트 지시만으로는 보장이 안 되기 때문."""
    steps = plan.get("steps", [])
    if not steps or steps[-1].get("skill") != "home":
        steps.append({"skill": "home", "args": {}})
        plan["steps"] = steps
    return plan

# ============================================================
# Task Plan v1.0 봉투(envelope) — 백엔드/통합 계층
# L2가 만든 intent+steps를 다른 레이어(L3~L5)가 받을 최종 규격으로 감싼다.
# ⚠️ 필드명은 기획서 초안 기준 추정치 — 원본 기획서와 한 번 대조 필요.
# ============================================================

SCHEMA_VERSION = "1.0"
ROBOT_ID = "robot-01"          # 임시값. 실제 로봇/설비 등록 체계 나오면 교체
PLAN_TTL_SECONDS = 300          # 계획 유효시간(초). 만료 후 실행 계층에서 거부해야 함

def compute_plan_hash(intent: str, steps: list) -> str:
    """intent+steps를 정규화된 JSON으로 직렬화한 뒤 SHA-256 해시.
    다운스트림(L3~L5)에서 이 해시로 계획이 중간에 변조되지 않았는지 검증할 수 있다."""
    canonical = json.dumps(
        {"intent": intent, "steps": steps},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def wrap_task_plan(draft: dict, utterance: str, confidence: float, model_id: str) -> dict:
    """L2 결과(intent+steps)를 Task Plan v1.0 전체 스키마로 감싼다."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=PLAN_TTL_SECONDS)

    return {
        "plan_id": str(uuid.uuid4()),
        "plan_hash": compute_plan_hash(draft["intent"], draft["steps"]),
        "schema_version": SCHEMA_VERSION,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "utterance": utterance,
        "robot_id": ROBOT_ID,
        "intent": draft["intent"],
        "steps": draft["steps"],
        "ref_versions": {
            "schema_version": SCHEMA_VERSION,
            "model_id": model_id,
        },
        "audit": {
            "source_layer": "L1+L2",
            "stt_confidence": confidence,
        },
    }

# ============================================================
# 파이프라인 본체 — 프린트하지 않고 결과를 dict로 반환한다.
# CLI(test_taskplan.py)와 API 서버(api_server.py) 양쪽에서 이 반환값을
# 각자 방식(콘솔 출력 vs HTTP 응답)으로 소비한다.
#
# 반환 형태:
#   성공  : {"status": "success", "task_plan": {...}, "slots": {...}}
#   게이트: {"status": "gated", "reason_code": "...", "message": "...", "slots": {...}}
#   오류  : {"status": "error", "reason_code": "...", "message": "..."}
# ============================================================

def process_utterance(raw_text: str, confidence: float) -> dict:
    slots = extract_slots(raw_text)

    reason = check_slots_complete(slots)
    if reason:
        write_audit_log("GATED", reason, raw_text, confidence, {"slots": vars(slots)})
        return {
            "status": "gated",
            "reason_code": reason,
            "message": "어떤 물건을 어디서 어디로 옮길지 다시 말씀해 주세요.",
            "slots": vars(slots),
        }

    reason = check_slots_known(slots)
    if reason:
        write_audit_log("GATED", reason, raw_text, confidence, {"slots": vars(slots)})
        return {
            "status": "gated",
            "reason_code": reason,
            "message": "몇 번 팔레트인지, 정확한 위치를 다시 말씀해 주세요.",
            "slots": vars(slots),
        }

    prompt = (
        f"작업 지시: {raw_text}\n"
        f"추출된 슬롯 - 동작: {slots.action}, 대상: {slots.object}, "
        f"출발: {slots.from_location}, 도착: {slots.to_location}, 수량: {slots.quantity}\n"
        "위 슬롯을 참고해서 작업 계획을 세워줘."
    )

    try:
        resp = requests.post(
            "http://localhost:8000/v1/chat/completions",
            json={
                "model": MODEL_ID,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "task_plan", "schema": schema},
                },
                # 모델이 정상 종료 못하고 반복 생성하며 폭주하는 걸 막기 위한 안전장치.
                "max_tokens": 1024,
                "temperature": 0,
            },
            timeout=60,
        )
    except requests.RequestException as e:
        write_audit_log("ERROR", "LLM_UNREACHABLE", raw_text, confidence, {"error": str(e)})
        return {"status": "error", "reason_code": "LLM_UNREACHABLE",
                "message": f"vLLM 서버에 연결할 수 없음: {e}"}

    result = resp.json()

    if "choices" not in result:
        write_audit_log("ERROR", "NO_CHOICES", raw_text, confidence,
                         {"raw_response": str(result)[:500]})
        return {"status": "error", "reason_code": "NO_CHOICES",
                "message": "vLLM 서버가 정상 응답을 주지 않음.", "raw": str(result)[:500]}

    choice = result["choices"][0]
    content = choice["message"]["content"]
    finish_reason = choice.get("finish_reason")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        write_audit_log("ERROR", "JSON_DECODE_ERROR", raw_text, confidence,
                         {"error": str(e), "content_len": len(content), "finish_reason": finish_reason})
        return {"status": "error", "reason_code": "JSON_DECODE_ERROR",
                "message": str(e), "finish_reason": finish_reason}

    try:
        # Capability Profile enum 검증. vLLM 구조화 출력이 스키마를 어겼을 경우
        # (모델/vLLM 버그 등으로) 여기서 마지막 방어선으로 걸러진다.
        validated = TaskPlanDraft.model_validate(parsed)
    except ValidationError as e:
        write_audit_log("ERROR", "SCHEMA_VALIDATION_ERROR", raw_text, confidence,
                         {"error": str(e)})
        return {"status": "error", "reason_code": "SCHEMA_VALIDATION_ERROR", "message": str(e)}

    draft = normalize_plan(validated.model_dump(mode="json", by_alias=True))
    task_plan = wrap_task_plan(draft, raw_text, confidence, MODEL_ID)

    # L3 Safety Guard — 여기까지 통과한 계획이라도 다시 처음부터 독립 검사한다.
    # (import는 함수 안에서: safety_guard.py가 pipeline.py를 가져다 쓰므로
    # 모듈 최상단에서 서로 import하면 순환참조가 생긴다)
    from safety_guard import check_plan_safety
    violations = check_plan_safety(task_plan)
    if violations:
        write_audit_log("REJECTED", violations[0]["code"], raw_text, confidence, {
            "plan_id": task_plan["plan_id"],
            "violations": violations,
        })
        return {
            "status": "rejected",
            "reason_code": violations[0]["code"],
            "message": "Task Plan이 안전 규칙을 위반해 거부됨",
            "violations": violations,
            "task_plan": task_plan,
        }

    write_audit_log("SUCCESS", None, raw_text, confidence, {
        "plan_id": task_plan["plan_id"],
        "plan_hash": task_plan["plan_hash"],
        "skills": [step["skill"] for step in task_plan["steps"]],
    })

    return {"status": "success", "task_plan": task_plan, "slots": vars(slots)}
