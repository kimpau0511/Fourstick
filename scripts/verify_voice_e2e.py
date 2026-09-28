"""음성 E2E — 음성 파일 → STT(WebSocket) → 시연 명령 → 확인 → Gazebo → 관측.

    # 음성 파일 만들기(인터넷 필요, gTTS). 이미 있으면 건너뛴다.
    python scripts/verify_voice_e2e.py --generate
    # 실행 중인 서버(8094)로 검증. websockets 필요.
    FORSTICK2_SIM_PICK_PLACE_DEMO=1 python scripts/verify_voice_e2e.py

브라우저가 하는 일을 그대로 따른다: 세션 발급 → `/v1/stt?session_id=` 에 PCM16을
보내고 `flush` → `final` 전사 → `POST /v1/sim-demo/command`(source=stt_final,
raw_transcript, stt_confidence) → 확인 카드/목표 확인 → 실행 → reconcile 관측.

실제 마이크가 아니다. 합성 음성(gTTS)이라 억양·잡음·마이크 특성은 검증하지 않는다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
FIXTURES = ROOT / "fixtures/stt/voice_commands"
MANIFEST = FIXTURES / "manifest.json"
CONTEXT_FIXTURES = ROOT / "fixtures/stt/voice_context"
#: 맥락 대화(순서대로). (id, 문장, 허용 판정, 기대 [동작, 자재, 도착] 또는 None, Gazebo 실행, 잡음 dB)
CONTEXT_CASES = (
    ("vc1", "파란 거 2번으로", ("CONFIRM", "CONFIRM_GOAL"), ["transfer", "material_b", "slot_2"], True, None),
    ("vc2", "그다음 그거 제자리로", ("CONFIRM", "CONFIRM_GOAL"), ["return", "material_b", "origin"], True, None),
    ("vc3", "주황 거 빈 칸에", ("ASK",), None, False, None),
    ("vc4", "3번", ("CONFIRM", "CONFIRM_GOAL"), ["transfer", "material_a", "slot_3"], True, None),
    ("vc5", "그거 제자리로", ("CONFIRM", "CONFIRM_GOAL"), ["return", "material_a", "origin"], True, None),
    # 잡음: 바르게 해석하거나 되물어야 한다(STT clarify 포함). 다른 자재·칸이면 실패.
    # 실행하지 않는다.
    ("vc6", "C 자재를 컨베이어 1번에 놓아줘", ("ASK", "STT_CLARIFY", "CONFIRM", "CONFIRM_GOAL"),
     ["transfer", "material_c", "slot_1"], False, -6),
    ("vc7", "C 자재를 컨베이어 1번에 놓아줘", ("ASK", "STT_CLARIFY", "CONFIRM", "CONFIRM_GOAL"),
     ["transfer", "material_c", "slot_1"], False, -12),
    ("vc8", "C 자재를 컨베이어 1번에 놓아줘", ("ASK", "STT_CLARIFY", "CONFIRM", "CONFIRM_GOAL"),
     ["transfer", "material_c", "slot_1"], False, -18),
)

#: (id, 문장, 범주, 기대 판정, 기대 자재, Gazebo에서 실행하는가)
CASES = (
    ("v01", "에이 자재를 컨베이어 1번에 옮겨줘", "normal·letter_reading", "CONFIRM",
     "material_a", True),
    ("v02", "비 자재는 컨베이어 2번, 씨 자재는 컨베이어 3번에 놓아줘", "multi_goal",
     "CONFIRM_GOAL", None, True),
    ("v03", "파란색 자재를 원래 자리로 돌려놔", "color", "CONFIRM", "material_b", True),
    ("v04", "저거 저쪽에 좀 놔줘", "ambiguous", "ASK", None, False),
    ("v05", "에이 자재를 3번 팔레트로 옮겨", "dangerous_wrong_pallet", "BLOCK", None, False),
    ("v06", "빨간 자재 옮겨줘", "unregistered_color", "ASK", None, False),
    ("v07", "2번 칸 고장났어", "environment", "ENVIRONMENT", None, False),
    ("v08", "멈춰", "stop", "STOP", None, False),
    ("v09", "씨 자재를 제자리로 돌려놔", "letter_reading·return", "CONFIRM",
     "material_c", True),
)


def generate() -> int:
    from gtts import gTTS

    FIXTURES.mkdir(parents=True, exist_ok=True)
    rows = []
    for case_id, text, category, decision, material, run in CASES:
        mp3 = FIXTURES / f"{case_id}.mp3"
        if not mp3.is_file():
            gTTS(text=text, lang="ko").save(str(mp3))
            print(f"생성 {mp3.name}: {text}")
        rows.append({"id": case_id, "text": text, "category": category,
                     "expected_decision": decision, "expected_material": material,
                     "run_in_gazebo": run, "audio": mp3.name,
                     "source": "gTTS(ko) 합성 음성 — 실제 마이크 녹음 아님"})
    MANIFEST.write_text(json.dumps({"schema": "forstick2.voice_commands/1",
                                    "cases": rows}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return 0


def generate_context() -> int:
    from gtts import gTTS

    CONTEXT_FIXTURES.mkdir(parents=True, exist_ok=True)
    rows = []
    for case_id, text, allowed, plan, run, noise_db in CONTEXT_CASES:
        clean = CONTEXT_FIXTURES / f"{case_id}.mp3"
        same = next((CONTEXT_FIXTURES / f"{c[0]}.mp3" for c in CONTEXT_CASES
                     if c[1] == text and (CONTEXT_FIXTURES / f"{c[0]}.mp3").is_file()), None)
        if not clean.is_file():
            if same is not None:
                clean.write_bytes(same.read_bytes())    # 같은 문장은 같은 음성(잡음만 다르다)
            else:
                gTTS(text=text, lang="ko").save(str(clean))
        audio = clean
        if noise_db is not None:
            audio = CONTEXT_FIXTURES / f"{case_id}_noise.wav"
            if not audio.is_file():
                # 분홍 잡음을 음성보다 {noise_db} dB 낮게 섞는다(합성 — 실제 환경 잡음 아님).
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(clean), "-f", "lavfi",
                                "-i", "anoisesrc=color=pink:amplitude=0.5:seed=7",
                                "-filter_complex",
                                f"[1]volume={10 ** (noise_db / 20):.3f}[n];"
                                "[0][n]amix=inputs=2:duration=first:normalize=0",
                                "-ar", "16000", "-ac", "1", str(audio)], check=True)
        rows.append({"id": case_id, "text": text, "audio": audio.name,
                     "allowed": list(allowed), "plan": plan, "run_in_gazebo": run,
                     "noise_db": noise_db,
                     "source": "gTTS(ko) 합성 음성" + (" + 합성 분홍 잡음" if noise_db else "")})
    (CONTEXT_FIXTURES / "manifest.json").write_text(json.dumps(
        {"schema": "forstick2.voice_context/1", "cases": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return 0


def pcm16(path: Path, rate: int) -> bytes:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
                          "-ar", str(rate), "-f", "s16le", "-"],
                         capture_output=True, check=True)
    # 끝에 1초 침묵 — 발화 종료(VAD)가 스스로 확정되게 한다.
    return out.stdout + b"\x00\x00" * rate


async def transcribe(base_ws: str, session_id: str, audio: Path) -> dict:
    import websockets

    events = []
    async with websockets.connect(f"{base_ws}/v1/stt?session_id={session_id}",
                                  max_size=None) as ws:
        hello = json.loads(await ws.recv())
        if hello.get("kind") != "session":
            return {"error": hello}
        rate = int(hello["sample_rate_hz"])
        data = pcm16(audio, rate)
        chunk = rate // 10 * 2          # 100 ms
        started = time.monotonic()
        for i in range(0, len(data), chunk):
            await ws.send(data[i:i + chunk])
        await ws.send(json.dumps({"type": "flush"}))
        final = None
        deadline = time.monotonic() + 120
        while final is None and time.monotonic() < deadline:
            try:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            except asyncio.TimeoutError:
                break
            events.append(event.get("kind"))
            if event.get("kind") == "final":
                final = event
            elif event.get("kind") == "error":
                return {"error": event, "events": events}
        await ws.send(json.dumps({"type": "close"}))
    return {"final": final, "events": events,
            "stt_sec": round(time.monotonic() - started, 2),
            "audio_sec": round(len(data) / 2 / rate, 2)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8094")
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--no-gazebo", action="store_true")
    parser.add_argument("--suite", choices=("commands", "context"), default="commands")
    args = parser.parse_args()
    if args.generate:
        return generate() if args.suite == "commands" else generate_context()
    if os.environ.get("FORSTICK2_SIM_PICK_PLACE_DEMO") != "1":
        print("거부: FORSTICK2_SIM_PICK_PLACE_DEMO=1을 명시하지 않았다.", file=sys.stderr)
        return 2
    from verify_arrangement_e2e import Client, log, reset

    client = Client(args.base)
    if args.suite == "context":
        return run_context(client, args)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))["cases"]
    status = client.status()
    materials = [m["model"] for m in status.get("materials") or []]
    results: list[dict] = []
    reset(client, materials, results, "음성 전 초기화")
    _, session = client.call("POST", "/v1/sessions", {})
    session_id = session.get("session_id")
    base_ws = args.base.replace("http", "ws", 1)
    for case in manifest:
        heard = asyncio.run(transcribe(base_ws, session_id, FIXTURES / case["audio"]))
        final = heard.get("final") or {}
        text = final.get("text") or ""
        row = {"id": case["id"], "category": case["category"], "said": case["text"],
               "heard": text, "raw_heard": final.get("raw_text"),
               "stt_confidence": final.get("confidence"),
               "stt_sec": heard.get("stt_sec"), "audio_sec": heard.get("audio_sec"),
               "expected": case["expected_decision"]}
        if not text:
            row.update(passed=False, detail=f"final 없음: {heard.get('error') or heard.get('events')}")
            results.append(row)
            log(f"[{case['id']}] STT 실패 {row['detail']}")
            continue
        started = time.monotonic()
        payload = client.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "stt_final", "utterance": text,
            "raw_transcript": final.get("raw_text") or text,
            "stt_confidence": final.get("confidence")})[1]
        row["command_sec"] = round(time.monotonic() - started, 2)
        decision = payload.get("decision")
        material = payload.get("material")
        row.update(decision=decision, material=material, reason=payload.get("reason"))
        # 위험 명령은 실행되지 않으면(BLOCK·ASK) 차단이다.
        allowed = (("BLOCK", "ASK") if case["category"].startswith("dangerous")
                   else (case["expected_decision"],))
        ok = decision in allowed and (
            case["expected_material"] is None or material == case["expected_material"])
        log(f"[{case['id']}] '{case['text']}' → STT '{text}' ({final.get('confidence')})"
            f" → {decision} {material or ''} {payload.get('reason') or ''}")
        if ok and case["run_in_gazebo"] and not args.no_gazebo:
            executed = execute(client, payload)
            row["gazebo"] = executed
            ok = executed.get("passed", False)
            log(f"    Gazebo {executed}")
        elif decision in ("CONFIRM", "CONFIRM_GOAL"):
            cancel(client, payload)
        if decision == "ENVIRONMENT":
            client.call("POST", "/v1/sim-demo/environment",
                        {"blocked_slots": [], "unavailable_materials": []})
        row["passed"] = ok
        results.append(row)
    reset(client, materials, results, "음성 후 초기화")
    passed = sum(1 for r in results if r.get("passed"))
    report = {"schema": "forstick2.voice_e2e/1", "is_simulated": True,
              "audio_source": "gTTS(ko) 합성 음성 — 실제 마이크 아님",
              "passed": passed, "total": len(results), "results": results}
    out = ROOT / f"reports/workcell/voice_e2e_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"===== 음성 {passed}/{len(results)} 통과 · {out}")
    return 0 if passed == len(results) else 1


def run_context(client, args) -> int:
    """맥락 대화 — 같은 세션에서 순서대로 말한다(앞 문장의 해석이 뒤 문장의 근거)."""
    from verify_arrangement_e2e import log, reset
    from eval_sim_commands import normalized_plan

    manifest = json.loads((CONTEXT_FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    status = client.status()
    materials = [m["model"] for m in status.get("materials") or []]
    results: list[dict] = []
    reset(client, materials, results, "음성 맥락 전 초기화")
    _, session = client.call("POST", "/v1/sessions", {})
    base_ws = args.base.replace("http", "ws", 1)
    for case in manifest["cases"]:
        heard = asyncio.run(transcribe(base_ws, session.get("session_id"),
                                       CONTEXT_FIXTURES / case["audio"]))
        final = heard.get("final") or {}
        text = final.get("text") or ""
        payload = client.call("POST", "/v1/sim-demo/command", {
            "mode": "simulation_demo", "source": "stt_final", "utterance": text,
            "raw_transcript": final.get("raw_text") or text,
            "stt_confidence": final.get("confidence")})[1] if text else {}
        # STT가 전사 대신 다시 말해 달라고 했다(clarify) — 명령을 만들지 않은 되묻기다.
        decision = payload.get("decision") or (
            "STT_CLARIFY" if "clarify" in (heard.get("events") or []) else None)
        plan = normalized_plan(payload) if payload else None
        ok = decision in case["allowed"]
        if ok and decision in ("CONFIRM", "CONFIRM_GOAL") and case["plan"]:
            ok = plan == [case["plan"]]
        evidence = (payload.get("interpretation") or {}).get("evidence") or []
        log(f"[{case['id']}] '{case['text']}' → STT '{text}' ({final.get('confidence')})"
            f" → {decision} {plan or ''} {payload.get('reason') or ''}"
            + (f" · 근거 {evidence}" if evidence else ""))
        row = {"id": case["id"], "said": case["text"], "heard": text,
               "stt_confidence": final.get("confidence"), "noise_db": case["noise_db"],
               "decision": decision, "plan": plan, "reason": payload.get("reason"),
               "evidence": evidence}
        if ok and case["run_in_gazebo"] and decision in ("CONFIRM", "CONFIRM_GOAL"):
            row["gazebo"] = execute(client, payload)
            ok = row["gazebo"].get("passed", False)
            log(f"    Gazebo {row['gazebo'].get('passed')} {row['gazebo'].get('problems')}")
        elif decision in ("CONFIRM", "CONFIRM_GOAL"):
            cancel(client, payload)
        row["passed"] = ok
        results.append(row)
    reset(client, materials, results, "음성 맥락 후 초기화")
    passed = sum(1 for r in results if r.get("passed"))
    out = ROOT / f"reports/workcell/voice_context_{time.strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps({"schema": "forstick2.voice_context_e2e/1", "is_simulated": True,
                               "audio_source": "gTTS(ko) 합성 음성(+합성 잡음) — 실제 사람 목소리 아님",
                               "passed": passed, "total": len(results), "results": results},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"===== 음성 맥락 {passed}/{len(results)} 통과 · {out}")
    return 0 if passed == len(results) else 1


def cancel(client, payload) -> None:
    confirmation = payload.get("confirmation") or {}
    if confirmation.get("kind") == "goal":
        client.call("POST", f"/v1/sim-demo/goals/{confirmation['goal_id']}/confirm",
                    {"action": "cancel"})
    elif confirmation.get("token"):
        client.call("POST", "/v1/sim-demo/confirm",
                    {"token": confirmation["token"], "action": "cancel"})


def execute(client, payload) -> dict:
    """확인을 누르고(브라우저의 확인 버튼) 끝까지 본 뒤 관측으로 판정한다."""
    from verify_arrangement_e2e import observe

    confirmation = payload.get("confirmation") or {}
    started = time.monotonic()
    if confirmation.get("kind") == "goal":
        goal_id = confirmation["goal_id"]
        client.call("POST", f"/v1/sim-demo/goals/{goal_id}/confirm", {"action": "confirm"})
        final = client.wait_goal(goal_id)
        expected = (final.get("reasoning") or {}).get("expected")
        done = final.get("status") == "completed"
    else:
        status, result = client.call("POST", "/v1/sim-demo/confirm",
                                     {"token": confirmation.get("token"), "action": "confirm"})
        job = result.get("job") or {}
        if not job.get("job_id"):
            return {"passed": False, "detail": f"확인 실패 {status} {result.get('reason')}"}
        finished = client.wait_job(job["job_id"])
        done = finished.get("exit_code") == 0
        expected = None
    seen = observe(client, expected)
    return {"passed": bool(done and seen["passed"]),
            "elapsed_sec": round(time.monotonic() - started, 1),
            "observed": seen.get("observed"), "problems": seen.get("problems")}


if __name__ == "__main__":
    sys.exit(main())
