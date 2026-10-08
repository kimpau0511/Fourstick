"""이번 발화 인식률 — 사용자가 확인한 실제 발화 기준 (2026-10-08).

흐름: 음성 확정 결과(STT, 수정 전)를 입력칸에 넣는다 → 사용자가 **실제로 말한 내용**과 맞게 고친다 →
'실제 발화 내용으로 확정'을 누른다 → 수정 전 STT 결과와 확정 정답을 비교해 이 발화의 인식률을 계산·저장한다.
계획·승인·실행과 무관하다(이 모듈은 파일만 쓴다).

- 계산은 `stt.recognition_rate.score_case`(정규화 ko-cer-1, CER, 인식률 = max(0, 1−CER)×100)를 그대로 쓴다.
- 고치지 않아도 사용자가 **명시적으로 확정**해야 100%가 된다(확정 없이 점수를 만들지 않는다).
- 빈 정답(정규화 뒤 0글자) = 평가 불가.
- 같은 발화(utterance_id)는 파일 하나 — 다시 확정하면 덮어쓰고 이전 정답은 `history`에 남긴다(중복 집계 없음).
- 원본 음성은 저장하지 않는다. 합성·외부 평가 결과(`stt/recognition_store.py`)와 다른 폴더·스키마다.

세션 소유권(2026-10-08 보완): **클라이언트가 보낸 원본 STT를 믿지 않는다.** 서버가 STT 요청 id로 저장소의 요청 기록을 찾아
(1) 그 요청을 만든 세션이 호출한 세션과 같은지, (2) 음성(STT) 요청인지 확인하고, 수정 전 STT 문장은 **저장소 기록**을 쓴다.
발화 id는 서버가 STT 요청 id에서 만든다. 다른 세션의 기록은 조회·확정·덮어쓰기 모두 거부(OwnershipError).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from stt.recognition_rate import MEASURED, NORMALIZATION_VERSION, score_case

SCHEMA = "forstick2.stt_utterance_check/1"
BASIS = "사용자 확인 기준"
_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class InvalidCheck(ValueError):
    pass


class NotFound(LookupError):
    pass


class OwnershipError(PermissionError):
    pass


def utterance_id_for(stt_request_id: str) -> str:
    if not isinstance(stt_request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,72}", stt_request_id):
        raise InvalidCheck("stt_request_id 형식이 아니다")
    return f"utt_{stt_request_id}"


def owned_stt(repository, *, session_id: str, stt_request_id: str) -> dict:
    """저장소의 STT 요청 기록(서버가 가진 원본). 없으면 NotFound, 다른 세션이거나 음성 요청이 아니면 OwnershipError."""
    utterance_id_for(stt_request_id)
    try:
        req = repository.get_request(stt_request_id)
    except Exception:  # noqa: BLE001 — 저장소의 '없음' 예외 종류에 기대지 않는다
        raise NotFound("STT 요청 기록을 찾을 수 없다") from None
    if not req.session_id or req.session_id != session_id:
        raise OwnershipError("다른 세션의 발화 기록이다")
    if not req.selected_stt_inference_id:
        raise OwnershipError("음성(STT) 요청 기록이 아니다")
    raw = None
    try:
        raw = repository.get_stt_inference(req.selected_stt_inference_id).raw_transcript
    except Exception:  # noqa: BLE001
        raw = None
    return {"stt_request_id": req.request_id, "stt_text": req.utterance, "stt_raw_text": raw,
            "stt_inference_id": req.selected_stt_inference_id, "session_id": req.session_id}


def read_owned(*, root: Path, session_id: str, stt_request_id: str) -> dict | None:
    """저장된 확정 기록(같은 세션만). 없으면 None, 다른 세션 기록이면 OwnershipError."""
    path = Path(root) / f"{utterance_id_for(stt_request_id)}.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("session_id") != session_id:
        raise OwnershipError("다른 세션의 발화 기록이다")
    return record


def check_dir(db_path: Path) -> Path:
    return Path(db_path).parent / "stt_utterance_checks"


def label_of(score: Mapping[str, Any]) -> str:
    if score.get("status") == MEASURED:
        return f"이번 발화 인식률 {score['counts']['rate_percent']:.1f}% · {BASIS}"
    return "이번 발화 인식률 평가 불가 · 빈 정답"


def confirm(*, utterance_id: str, stt_text: str, reference: str, stt_request_id: str | None,
            model_key: Mapping[str, Any] | None, root: Path, now: float | None = None,
            session_id: str | None = None, stt_raw_text: str | None = None) -> dict:
    """확정 한 번. 같은 utterance_id면 기존 기록을 갱신한다(이전 정답은 history). 저장한 기록을 돌려준다.
    `stt_text`는 **서버 저장소의 STT 문장**을 넘긴다(라우트가 `owned_stt`로 찾는다). session_id가 다른 기존 기록은 덮어쓰지 않는다."""
    if not isinstance(utterance_id, str) or not _ID.match(utterance_id):
        raise InvalidCheck("utterance_id 형식이 아니다")
    if not isinstance(stt_text, str) or not isinstance(reference, str):
        raise InvalidCheck("stt_text·reference는 문자열이어야 한다")
    now = time.time() if now is None else now
    score = score_case(reference, stt_text, reference_source="human")
    path = Path(root) / f"{utterance_id}.json"
    prev = None
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = None
    if prev is not None and prev.get("session_id") != session_id:
        raise OwnershipError("다른 세션의 발화 기록은 덮어쓰지 않는다")
    if prev is not None and prev.get("stt_text") != stt_text:
        # 같은 발화 id인데 STT 결과가 다르다 — 다른 발화를 덮어쓰지 않는다.
        raise InvalidCheck("같은 발화 id에 다른 STT 결과가 왔다 — 새 발화는 새 id로")
    history = list((prev or {}).get("history") or [])
    if prev is not None:
        history.append({"reference": prev.get("reference"), "confirmed_at": prev.get("confirmed_at"),
                        "status": (prev.get("score") or {}).get("status")})
    record = {
        "schema": SCHEMA, "utterance_id": utterance_id, "stt_request_id": stt_request_id, "session_id": session_id,
        "stt_text": stt_text, "stt_text_source": "server_record" if session_id else "caller", "stt_raw_text": stt_raw_text,
        "reference": reference, "reference_source": "human", "basis": BASIS,
        "model_key": dict(model_key) if model_key else None, "normalization_version": NORMALIZATION_VERSION,
        "score": score, "label": label_of(score),
        "first_confirmed_at": (prev or {}).get("first_confirmed_at") or now, "confirmed_at": now,
        "confirm_count": len(history) + 1, "history": history, "audio_stored": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return record
