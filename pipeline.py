"""
포스틱 L1~L2 파이프라인 공통 로직.
gazebo_robot/taskplan_bridge.py(CLI 테스트)와 api_server.py(FastAPI 서버) 양쪽에서 이 모듈을 가져다 쓴다.
"""

import hashlib
import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Literal, Union, Annotated, Optional

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
# 크기 기준 자동 회전 — 기본 10MB x 5개(최대 약 50MB). 무기한 누적 방지.
# 회전되어 밀려난 과거분은 audit_log.jsonl.1, .2 ... 로 보관됨(삭제 아님).
AUDIT_LOG_MAX_BYTES = int(os.environ.get("FORSTICK_AUDIT_LOG_MAX_BYTES", 10 * 1024 * 1024))
AUDIT_LOG_BACKUP_COUNT = int(os.environ.get("FORSTICK_AUDIT_LOG_BACKUP_COUNT", 5))

_audit_logger = logging.getLogger("forstick.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.propagate = False
if not _audit_logger.handlers:
    _audit_handler = RotatingFileHandler(
        AUDIT_LOG_PATH, maxBytes=AUDIT_LOG_MAX_BYTES,
        backupCount=AUDIT_LOG_BACKUP_COUNT, encoding="utf-8",
    )
    _audit_handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_logger.addHandler(_audit_handler)

def write_audit_log(event: str, reason_code: Optional[str], utterance: str,
                     confidence: float, detail: Optional[dict] = None):
    """계획 생성·거부·실행·STOP 이벤트를 JSONL 한 줄로 기록한다.
    RotatingFileHandler가 파일 쓰기 자체의 스레드 안전성을 보장한다."""
    entry = {
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "reason_code": reason_code,
        "utterance": utterance,
        "stt_confidence": confidence,
        "detail": detail or {},
    }
    _audit_logger.info(json.dumps(entry, ensure_ascii=False))

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
        # "11번 팔레트"가 "1번 팔레트"를 부분 문자열로 포함해버리는 오탐을
        # 막기 위해, 매칭 시작 위치 바로 앞이 숫자면 그 매칭은 버린다.
        m = re.search(r"(?<!\d)" + re.escape(loc), text)
        if m:
            found.append((m.start(), loc))
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
    if not slots.from_location or not slots.to_location:
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

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MoveArgs(StrictModel):
    target: LocationName

class PickArgs(StrictModel):
    object: ObjectName
    from_: LocationName | None = Field(None, alias="from")

class PlaceArgs(StrictModel):
    object: ObjectName
    to: LocationName

class EmptyArgs(StrictModel):
    pass

class HomeStep(StrictModel):
    skill: Literal["home"]
    args: EmptyArgs

class MoveStep(StrictModel):
    skill: Literal["move"]
    args: MoveArgs

class PickStep(StrictModel):
    skill: Literal["pick"]
    args: PickArgs

class PlaceStep(StrictModel):
    skill: Literal["place"]
    args: PlaceArgs

class StopStep(StrictModel):
    skill: Literal["stop"]
    args: EmptyArgs

Step = Annotated[
    Union[HomeStep, MoveStep, PickStep, PlaceStep, StopStep],
    Field(discriminator="skill"),
]

class TaskPlanDraft(StrictModel):
    intent: Literal["transfer"]
    steps: list[Step] = Field(min_length=1)

schema = TaskPlanDraft.model_json_schema()

MODEL_ID = os.environ.get("FORSTICK_MODEL_ID", "/home/asd/models/exaone-3.5-7.8b-awq")

SYSTEM_PROMPT = (
    "너는 산업용 로봇의 작업 계획을 세우는 도우미다. "
    "반드시 home, move, pick, place, stop 다섯 개 스킬만 조합해서 계획을 세운다. "
    "위치나 물건 이름은 절대 지어내지 말고, 사용자 지시문에 언급된 이름만 그대로 사용한다. "
    "불필요한 중간 이동 없이 최소 단계로 구성한다."
)

# ============================================================
# Task Plan v1.0 봉투(envelope) — 백엔드/통합 계층
# L2가 만든 intent+steps를 다른 레이어(L3~L5)가 받을 최종 규격으로 감싼다.
# ⚠️ 필드명은 기획서 초안 기준 추정치 — 원본 기획서와 한 번 대조 필요.
# ============================================================

SCHEMA_VERSION = "1.0"
ROBOT_ID = "robot-01"          # 임시값. 실제 로봇/설비 등록 체계 나오면 교체
PLAN_TTL_SECONDS = 300          # 계획 유효시간(초). 만료 후 실행 계층에서 거부해야 함
STT_MIN_CONFIDENCE = float(os.environ.get("FORSTICK_STT_MIN_CONFIDENCE", "0.35"))

def compute_plan_hash(intent: str, steps: list) -> str:
    """intent+steps를 정규화된 JSON으로 직렬화한 뒤 SHA-256 해시.
    다운스트림(L3~L5)에서 이 해시로 계획이 중간에 변조되지 않았는지 검증할 수 있다."""
    canonical = json.dumps(
        {"intent": intent, "steps": steps},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def wrap_task_plan(draft: dict, utterance: str, confidence: float, model_id: str,
                   robot_id: str = ROBOT_ID) -> dict:
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
        "robot_id": robot_id,
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


def check_plan_matches_slots(plan: dict, slots: Slots) -> list[dict]:
    """LLM 계획이 결정론적으로 추출한 단일 이송 슬롯과 같은지 확인한다."""
    steps = plan.get("steps", [])
    picks = [s for s in steps if s.get("skill") == "pick"]
    places = [s for s in steps if s.get("skill") == "place"]
    violations = []

    if len(picks) != 1 or len(places) != 1:
        violations.append({
            "code": "E-SEM-001",
            "message": "단일 이송 계획에는 pick/place가 각각 정확히 1개여야 함",
        })
        return violations

    pick_args = picks[0].get("args", {}) or {}
    place_args = places[0].get("args", {}) or {}
    expected = (slots.object, slots.from_location, slots.to_location)
    actual = (pick_args.get("object"), pick_args.get("from"), place_args.get("to"))
    if actual != expected or place_args.get("object") != slots.object:
        violations.append({
            "code": "E-SEM-002",
            "message": "생성된 계획의 물체·출발지·도착지가 원래 명령과 다름",
        })
    if any(s.get("skill") == "stop" for s in steps):
        violations.append({
            "code": "E-SEM-003",
            "message": "일반 작업 계획에 STOP 스킬을 포함할 수 없음",
        })
    return violations


def validate_task_plan_for_execution(task_plan: dict, expected_robot_id: str,
                                     now: Optional[datetime] = None) -> list[dict]:
    """실행 직전 단일 진입점에서 형식·동일성·유효기간·안전규칙을 재검증한다."""
    violations = []
    try:
        validated = TaskPlanDraft.model_validate({
            "intent": task_plan.get("intent"),
            "steps": task_plan.get("steps"),
        })
        steps = validated.model_dump(mode="json", by_alias=True)["steps"]
    except (AttributeError, ValidationError) as exc:
        return [{"code": "E-EXEC-SCHEMA", "message": str(exc)}]

    if task_plan.get("schema_version") != SCHEMA_VERSION:
        violations.append({"code": "E-EXEC-VERSION", "message": "지원하지 않는 Task Plan 버전"})
    if task_plan.get("robot_id") != expected_robot_id:
        violations.append({"code": "E-EXEC-ROBOT", "message": "현재 실행 로봇과 계획의 robot_id가 다름"})
    if task_plan.get("plan_hash") != compute_plan_hash(task_plan.get("intent"), steps):
        violations.append({"code": "E-EXEC-HASH", "message": "Task Plan 내용과 plan_hash가 다름"})

    try:
        expires_at = datetime.fromisoformat(task_plan["expires_at"].replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        if expires_at <= current:
            violations.append({"code": "E-EXEC-EXPIRED", "message": "Task Plan 유효기간이 만료됨"})
    except (KeyError, AttributeError, TypeError, ValueError):
        violations.append({"code": "E-EXEC-EXPIRY", "message": "expires_at이 없거나 잘못됨"})

    from safety_guard import check_plan_safety
    violations.extend(check_plan_safety({**task_plan, "steps": steps}))
    return violations

# ============================================================
# 파이프라인 본체 — 프린트하지 않고 결과를 dict로 반환한다.
# CLI(taskplan_bridge.py)와 API 서버(api_server.py) 양쪽에서 이 반환값을
# 각자 방식(콘솔 출력 vs HTTP 응답)으로 소비한다.
#
# 반환 형태:
#   성공  : {"status": "success", "task_plan": {...}, "slots": {...}}
#   게이트: {"status": "gated", "reason_code": "...", "message": "...", "slots": {...}}
#   오류  : {"status": "error", "reason_code": "...", "message": "..."}
# ============================================================

def process_utterance(raw_text: str, confidence: float, robot_id: str = ROBOT_ID) -> dict:
    slots = extract_slots(raw_text)

    # STOP은 LLM과 일반 계획 게이트를 거치지 않는 별도 우선 경로다.
    if slots.action == "stop":
        return {
            "status": "stop_requested",
            "reason_code": "USER_STOP",
            "message": "정지 요청을 실행기로 전달합니다.",
            "slots": vars(slots),
        }

    if confidence < STT_MIN_CONFIDENCE:
        write_audit_log("GATED", "A-STT-CONFIDENCE", raw_text, confidence)
        return {
            "status": "gated",
            "reason_code": "A-STT-CONFIDENCE",
            "message": "음성 인식 신뢰도가 낮습니다. 다시 말씀해 주세요.",
            "slots": vars(slots),
        }

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

    if slots.quantity != 1:
        write_audit_log("GATED", "A-QUANTITY", raw_text, confidence, {"slots": vars(slots)})
        return {
            "status": "gated",
            "reason_code": "A-QUANTITY",
            "message": "현재는 한 번에 자재 1개만 처리할 수 있습니다.",
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
        resp.raise_for_status()
        result = resp.json()
    except (requests.RequestException, ValueError) as e:
        write_audit_log("ERROR", "LLM_UNREACHABLE", raw_text, confidence, {"error": str(e)})
        return {"status": "error", "reason_code": "LLM_UNREACHABLE",
                "message": f"vLLM 서버에 연결할 수 없음: {e}"}

    if not isinstance(result.get("choices"), list) or not result["choices"]:
        write_audit_log("ERROR", "NO_CHOICES", raw_text, confidence,
                         {"raw_response": str(result)[:500]})
        return {"status": "error", "reason_code": "NO_CHOICES",
                "message": "vLLM 서버가 정상 응답을 주지 않음.", "raw": str(result)[:500]}

    choice = result["choices"][0]
    content = (choice.get("message") or {}).get("content") if isinstance(choice, dict) else None
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None

    if not isinstance(content, str):
        write_audit_log("ERROR", "INVALID_LLM_RESPONSE", raw_text, confidence,
                        {"raw_response": str(result)[:500]})
        return {"status": "error", "reason_code": "INVALID_LLM_RESPONSE",
                "message": "vLLM 응답에 문자열 content가 없음."}

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError) as e:
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

    draft = validated.model_dump(mode="json", by_alias=True)
    semantic_violations = check_plan_matches_slots(draft, slots)
    if semantic_violations:
        write_audit_log("REJECTED", semantic_violations[0]["code"], raw_text, confidence, {
            "violations": semantic_violations,
        })
        return {
            "status": "rejected",
            "reason_code": semantic_violations[0]["code"],
            "message": "생성된 계획이 원래 명령과 일치하지 않아 거부됨",
            "violations": semantic_violations,
        }

    task_plan = wrap_task_plan(draft, raw_text, confidence, MODEL_ID, robot_id=robot_id)

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
