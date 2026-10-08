"""음성 인식률(글자 기준) 계산·저장·표시값 선택 시험 — STT·서버 없이."""

import json
import tempfile
import unittest
from pathlib import Path

from stt.recognition_rate import (MEASURED, NORMALIZATION_VERSION, NOT_EVALUABLE, PENDING, ReferenceNotHumanVerified,
                                  aggregate, edit_counts, normalize_for_cer, score_case)
from stt.recognition_store import (InvalidDataset, display_summary, load_manifest, model_key, save_result)

KEY = {"model_name": "large-v3-turbo", "profile_id": "turbo-cuda-float16", "config_version": "turbo-gpu-1.0",
       "device": "cuda", "compute_type": "float16", "language": "ko", "vad_threshold": 0.5}


class CerTest(unittest.TestCase):
    def counts(self, ref, hyp):
        return score_case(ref, hyp)["counts"]

    def test_exact(self):
        c = self.counts("A자재를 컨베이어로", "A자재를 컨베이어로")
        self.assertEqual((c["errors"], c["cer"], c["rate_percent"]), (0, 0.0, 100.0))

    def test_substitution(self):
        c = self.counts("컨베이어로", "컨베이여로")          # 5글자 중 1 치환
        self.assertEqual((c["substitutions"], c["deletions"], c["insertions"]), (1, 0, 0))
        self.assertAlmostEqual(c["cer"], 0.2)
        self.assertAlmostEqual(c["rate_percent"], 80.0)

    def test_deletion(self):
        c = self.counts("원래자리로", "원래자로")
        self.assertEqual((c["substitutions"], c["deletions"], c["insertions"]), (0, 1, 0))

    def test_insertion(self):
        c = self.counts("정지", "정지해")
        self.assertEqual((c["substitutions"], c["deletions"], c["insertions"]), (0, 0, 1))
        self.assertAlmostEqual(c["cer"], 0.5)

    def test_cer_over_100_percent_floors_rate(self):
        c = self.counts("정지", "고맙습니다 정말 감사합니다")
        self.assertGreater(c["cer"], 1.0)                 # 원본 CER은 그대로 보존(1 초과)
        self.assertEqual(c["rate_percent"], 0.0)          # 인식률은 0 밑으로 내려가지 않는다

    def test_empty_reference_not_evaluable(self):
        self.assertEqual(score_case("", "고맙습니다")["status"], NOT_EVALUABLE)
        self.assertEqual(score_case(" .!? ", "x")["status"], NOT_EVALUABLE)   # 정규화 뒤 0글자

    def test_missing_reference_pending(self):
        self.assertEqual(score_case(None, "아무 말")["status"], PENDING)

    def test_stt_output_cannot_be_reference(self):
        with self.assertRaises(ReferenceNotHumanVerified):
            score_case("A자재", "A자재", reference_source="stt")

    def test_normalization(self):
        self.assertEqual(normalize_for_cer("A 자재를,  컨베이어로!"), "a자재를컨베이어로")
        self.assertEqual(normalize_for_cer("Ａ자재"), "a자재")                 # NFKC 전각 → 반각
        self.assertEqual(score_case("A 자재를 옮겨.", "a자재를옮겨")["counts"]["errors"], 0)
        # 숫자·라틴을 한글 읽기로 바꾸지 않는다 — 다르면 오류
        self.assertGreater(score_case("2번 팔레트", "이번 팔레트")["counts"]["errors"], 0)
        self.assertGreater(score_case("A자재", "에이자재")["counts"]["errors"], 0)
        self.assertEqual(NORMALIZATION_VERSION, "ko-cer-1")

    def test_aggregate_is_total_errors_over_total_chars(self):
        cases = [score_case("가나다라마바사아자차", "가나다라마바사아자차"),   # 10글자 0오류 = 100%
                 score_case("가나", "")]                                    # 2글자 2오류 = 0%
        agg = aggregate(cases + [score_case(None, "x"), score_case("", "y")])
        self.assertEqual((agg["errors"], agg["reference_chars"]), (2, 12))
        self.assertAlmostEqual(agg["rate_percent"], (1 - 2 / 12) * 100)    # 83.3% — 단순 평균(50%)이 아니다
        self.assertEqual((agg["cases_measured"], agg["cases_pending"], agg["cases_not_evaluable"]), (2, 1, 1))

    def test_edit_counts_deterministic(self):
        self.assertEqual(edit_counts("abc", "abc").errors, 0)
        self.assertEqual(edit_counts("", "ab").insertions, 2)


def result(*, key=KEY, source="real", split="final", purpose="official", ref_source="human", by="tester",
           created=1.0, rate_errors=1, n=10, norm=NORMALIZATION_VERSION, dataset="ds"):
    case = {"id": "c1", "reference_source": ref_source, "reference_verified_by": by,
            "raw": {"status": MEASURED, "counts": {}}}
    agg = {"errors": rate_errors, "reference_chars": n, "rate_percent": (1 - rate_errors / n) * 100,
           "cases_measured": 1, "cer": rate_errors / n, "substitutions": rate_errors, "deletions": 0, "insertions": 0}
    return {"model_key": key, "dataset": {"id": dataset, "source_type": source, "split": split}, "cases": [case],
            "aggregate_raw": agg, "normalization_version": norm, "purpose": purpose, "created_at": created}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unmeasured_when_empty(self):
        s = display_summary(KEY, root=self.tmp)
        self.assertEqual((s["status"], s["label"], s["rate_percent"]), ("unmeasured", "음성 인식률 미측정", None))

    def test_only_matching_real_final_human(self):
        other = dict(KEY, model_name="small")
        for r, i in ((result(key=other, rate_errors=0), 1), (result(split="dev", rate_errors=0), 3),
                     (result(purpose="functional", rate_errors=0), 4),
                     (result(ref_source="tts_script", rate_errors=0), 5), (result(by=None, rate_errors=0), 6),
                     (result(norm="ko-cer-0", rate_errors=0), 7)):
            save_result({**r, "run_id": f"skip{i}"}, root=self.tmp)
        self.assertEqual(display_summary(KEY, root=self.tmp)["status"], "unmeasured")
        save_result({**result(rate_errors=1, n=13), "run_id": "ok"}, root=self.tmp)
        s = display_summary(KEY, root=self.tmp)
        self.assertEqual(s["status"], "measured")
        self.assertEqual(s["label"], "음성 인식률 92.3%")
        self.assertEqual(s["note"], "글자 기준 평가 결과이며 현재 발화의 정확도를 뜻하지 않습니다.")

    def synth(self, *, run_id, created, errors=1, n=10, split="final", noise=None, key=KEY, norm=NORMALIZATION_VERSION):
        r = result(source="synthetic", split=split, purpose="functional", ref_source="tts_script", by=None,
                   created=created, rate_errors=errors, n=n, key=key, norm=norm, dataset=f"synth-{run_id}")
        r["cases"][0]["noise"] = noise
        r["dataset"]["synth_engine"] = ["windows-sapi:Microsoft Heami Desktop"]
        save_result({**r, "run_id": run_id}, root=self.tmp)

    def test_synthetic_fallback_is_labelled_and_never_noisy_dev_or_other_model(self):
        self.synth(run_id="noisy", created=5.0, errors=0, noise={"kind": "white", "snr_db": 10})
        self.synth(run_id="dev", created=6.0, errors=0, split="dev")
        self.synth(run_id="other", created=7.0, errors=0, key=dict(KEY, compute_type="int8"))
        self.synth(run_id="oldnorm", created=8.0, errors=0, norm="ko-cer-0")
        self.assertEqual(display_summary(KEY, root=self.tmp)["status"], "unmeasured")
        self.synth(run_id="clean-old", created=1.0, errors=2, n=10)
        self.synth(run_id="clean-new", created=2.0, errors=3, n=28)
        s = display_summary(KEY, root=self.tmp)
        self.assertEqual((s["status"], s["source"]), ("measured", "synthetic"))
        self.assertEqual(s["label"], "음성 인식률 89.3%")          # 가장 최근 한 실행만(합치지 않음). 라벨은 그대로
        self.assertIn("합성 음성", s["note"])
        self.assertIn("실제 사용자 음성", s["note"])
        # 실제 녹음 평가가 생기면 그쪽이 우선
        save_result({**result(rate_errors=1, n=13, created=0.5), "run_id": "real"}, root=self.tmp)
        s = display_summary(KEY, root=self.tmp)
        self.assertEqual((s["source"], s["label"]), ("real", "음성 인식률 92.3%"))

    def test_latest_run_not_merged(self):
        save_result({**result(rate_errors=5, created=1.0), "run_id": "old"}, root=self.tmp)
        save_result({**result(rate_errors=1, created=2.0, dataset="ds2"), "run_id": "new"}, root=self.tmp)
        s = display_summary(KEY, root=self.tmp)
        self.assertEqual(s["basis"]["run_id"], "new")
        self.assertAlmostEqual(s["rate_percent"], 90.0)   # 두 실행을 합치지 않는다

    def test_mock_purpose_not_storable(self):
        with self.assertRaises(ValueError):
            save_result({**result(), "purpose": "mock"}, root=self.tmp)

    def test_model_key_from_config_object(self):
        class C:  # noqa: D401
            pass
        c = C()
        for k, v in KEY.items():
            setattr(c, k, v)
        self.assertEqual(model_key(c), KEY)

    def manifest(self, rows):
        p = self.tmp / "m.jsonl"
        p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        return p

    def test_manifest_rules(self):
        base = {"id": "a", "audio": "a.wav", "source_type": "real", "split": "final", "reference": "정지",
                "reference_source": "human", "reference_verified_by": "kim"}
        self.assertEqual(len(load_manifest(self.manifest([base]))), 1)
        bad = [dict(base, reference_source="stt"), dict(base, reference_verified_by=None),
               dict(base, reference_source="tts_script"),
               dict(base, source_type="synthetic", reference_source="tts_script"),   # 합성인데 엔진 없음
               dict(base, split="test")]
        for row in bad:
            with self.subTest(row=row), self.assertRaises(InvalidDataset):
                load_manifest(self.manifest([row]))
        with self.assertRaises(InvalidDataset):  # 실제·합성 섞임
            load_manifest(self.manifest([base, dict(base, id="b", source_type="synthetic", synth_engine="x",
                                                    reference_source="tts_script")]))
        pending = dict(base, id="p", reference=None, reference_source=None, reference_verified_by=None)
        self.assertEqual(len(load_manifest(self.manifest([base, pending]))), 2)   # 정답 없는 사례(평가 대기) 허용


if __name__ == "__main__":
    unittest.main()


class RouteTest(unittest.TestCase):
    """GET /v1/stt/recognition-rate — 파일만 읽는다. 계획·승인·실행 API를 부르지 않는다."""

    def run_route(self, runtime, method="GET"):
        import asyncio
        import os
        from types import SimpleNamespace
        from server.routes import stt_eval

        class NoApi:  # 제어 API를 건드리면 바로 실패
            def __getattr__(self, name):
                raise AssertionError(f"평가 조회가 제어 API {name}을 불렀다")
        ctx = SimpleNamespace(api=NoApi(), runtime=runtime, config=None, hub=None, read_body=None)
        os.environ["FORSTICK2_STT_RECOGNITION_DIR"] = str(self.tmp)
        try:
            return asyncio.run(stt_eval.handle(ctx, method, "/v1/stt/recognition-rate", None, {}))
        finally:
            os.environ.pop("FORSTICK2_STT_RECOGNITION_DIR", None)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_route(self):
        from types import SimpleNamespace
        status, _, body = self.run_route(SimpleNamespace(stt_model_config=None))
        self.assertIn("음성 인식률 미측정".encode(), body)
        cfg = SimpleNamespace(**KEY)
        self.assertIn("음성 인식률 미측정".encode(), self.run_route(SimpleNamespace(stt_model_config=cfg))[2])
        save_result({**result(rate_errors=1, n=13), "run_id": "ok"}, root=self.tmp)
        status, _, body = self.run_route(SimpleNamespace(stt_model_config=cfg))
        self.assertEqual(status, 200)
        self.assertIn("음성 인식률 92.3%".encode(), body)
        self.assertIsNone(self.run_route(SimpleNamespace(stt_model_config=cfg), method="POST"))


class VoiceOutcomeLogTest(unittest.TestCase):
    """계획 요청의 음성 결과 기록 — 사용자가 고쳤으면 'user_edited'(사용자 수정 발생). STT 오인식으로 확정하지 않는다."""

    def log(self, payload, result):
        from types import SimpleNamespace
        from server.routes.planning import _log_voice_outcome
        tmp = Path(tempfile.mkdtemp())
        ctx = SimpleNamespace(config=SimpleNamespace(db_path=tmp / "web.sqlite3"))
        _log_voice_outcome(ctx, payload, result)
        p = tmp / "voice_outcomes.jsonl"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def test_kinds(self):
        stt = {"stt_request_id": "s1", "stt_text": "A차트 옮겨줘", "stt_confidence": 0.8}
        edited = self.log({"utterance": "B자재 옮겨줘", "stt": {**stt, "edited": True}}, {"ok": False})
        self.assertEqual((edited["kind"], edited["edited"]), ("user_edited", True))
        self.assertNotIn("stt_corrected", json.dumps(edited))
        self.assertEqual(self.log({"utterance": "A차트 옮겨줘", "stt": stt}, {"ok": True})["kind"], "planned")
        self.assertEqual(self.log({"utterance": "A차트 옮겨줘", "stt": stt}, {"ok": False})["kind"], "not_planned")
        self.assertIsNone(self.log({"utterance": "텍스트 명령"}, {"ok": True}))   # 음성이 아니면 기록하지 않는다


class UtteranceCheckTest(unittest.TestCase):
    """이번 발화 인식률(사용자 확인 기준) — 확정 한 번당 계산·저장, 같은 발화는 파일 하나."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def confirm(self, uid, stt, ref, **kw):
        from stt.utterance_check import confirm
        return confirm(utterance_id=uid, stt_text=stt, reference=ref, stt_request_id="req1", model_key=KEY,
                       root=self.tmp, **kw)

    def test_unedited_confirm_is_100(self):
        r = self.confirm("utt_aaaaaaaa", "A자재를 컨베이어로", "A자재를 컨베이어로")
        self.assertEqual((r["score"]["status"], r["score"]["counts"]["rate_percent"]), (MEASURED, 100.0))
        self.assertEqual(r["label"], "이번 발화 인식률 100.0% · 사용자 확인 기준")
        self.assertFalse(r["audio_stored"])

    def test_edited_confirm(self):
        r = self.confirm("utt_bbbbbbbb", "A자태를 컨베이어로.", "A자재를 컨베이어로")
        c = r["score"]["counts"]
        self.assertEqual((c["substitutions"], c["deletions"], c["insertions"], c["reference_chars"]), (1, 0, 0, 9))
        self.assertEqual(r["label"], "이번 발화 인식률 88.9% · 사용자 확인 기준")

    def test_empty_reference_not_evaluable(self):
        r = self.confirm("utt_cccccccc", "고맙습니다", " . ")
        self.assertEqual(r["score"]["status"], NOT_EVALUABLE)
        self.assertIn("평가 불가", r["label"])

    def test_reconfirm_same_utterance_is_one_record(self):
        self.confirm("utt_dddddddd", "A차트 옮겨줘", "A차트 옮겨줘", now=1.0)
        r = self.confirm("utt_dddddddd", "A차트 옮겨줘", "A자재 옮겨줘", now=2.0)
        self.assertEqual(len(list(self.tmp.glob("*.json"))), 1)
        self.assertEqual((r["confirm_count"], r["first_confirmed_at"], r["confirmed_at"]), (2, 1.0, 2.0))
        self.assertEqual(r["history"][0]["reference"], "A차트 옮겨줘")
        self.assertEqual(r["reference"], "A자재 옮겨줘")

    def test_same_id_different_stt_rejected_and_bad_id(self):
        from stt.utterance_check import InvalidCheck
        self.confirm("utt_eeeeeeee", "첫 발화", "첫 발화")
        with self.assertRaises(InvalidCheck):
            self.confirm("utt_eeeeeeee", "다른 발화", "다른 발화")
        for bad in ("short", "../../etc", "a b c d e f g h", None):
            with self.subTest(bad=bad), self.assertRaises(InvalidCheck):
                self.confirm(bad, "x", "x")


class UtteranceOwnershipTest(unittest.TestCase):
    """세션 소유권: 서버가 STT 요청 기록(실제 저장소)과 대조한다. 클라이언트가 보낸 STT 문장은 쓰지 않는다."""

    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from storage.sqlite.repository import SqliteRepository
        from stt.streaming_session import StreamingSTTSession
        from stt.transcription_guard import TranscriptionGuard
        from stt.whisper_backend import Transcript
        from test_stt_session import FRAME, MODEL_CONFIG, SR, ScriptedTranscriber, ScriptedVad, make_policy

        self.tmp = Path(tempfile.mkdtemp())
        self.repo = SqliteRepository(now=0.0)
        self.addCleanup(self.repo.close)
        from storage.records import SessionRecord, SessionStatus
        for sid in ("sessA", "sessB"):   # 실제 흐름처럼 세션이 있어야 STT 요청이 저장된다(외래 키)
            self.repo.create_session(SessionRecord(session_id=sid, created_at=1.0, last_seen_at=1.0,
                                                   status=SessionStatus.ACTIVE, schema_version="1.0"))
        policy = make_policy()

        def stt_request(req_id, owner, text):
            s = StreamingSTTSession(
                request_id=req_id, policy=policy, vad=ScriptedVad((True,)),
                transcriber=ScriptedTranscriber([Transcript(text, 0.9)]), sample_rate_hz=SR,
                schema_version="1.0", repository=self.repo, session_id=f"stream_{req_id}", owner_session_id=owner,
                model_config=MODEL_CONFIG, model_version="test", model_load_sec=1.0, confidence_metric="t",
                clock=lambda: 0.0, guard=TranscriptionGuard(policy, clock=lambda: 0.0))
            s.start(at_utc=100.0)
            s.feed_audio(FRAME, at_sec=0.1, at_utc=100.1)
            final = [e for e in s.flush(at_sec=0.5, at_utc=100.5) if e.kind.value == "final"][0]
            assert final.persisted, final
            return final.text
        self.text_a = stt_request("reqA0001", "sessA", "A자태를 컨베이어로")
        stt_request("reqB0001", "sessB", "B자재 옮겨줘")

    def call(self, method, session, req_id, reference=None, **extra):
        import asyncio
        from types import SimpleNamespace
        from server.api import ApiError
        from server.routes import stt_eval

        class Api:
            def require_session(self, sid):
                if sid not in ("sessA", "sessB"):
                    raise ApiError(404, None, "세션 없음")

            def __getattr__(self, name):
                raise AssertionError(f"발화 확정이 제어 API {name}을 불렀다")

        async def body(_):
            return {"session_id": session, "stt_request_id": req_id, "reference": reference, **extra}
        ctx = SimpleNamespace(api=Api(), runtime=SimpleNamespace(stt_model_config=SimpleNamespace(**KEY), repository=self.repo),
                              config=SimpleNamespace(db_path=self.tmp / "web.sqlite3"), hub=None, read_body=body)
        try:
            status, _, out = asyncio.run(stt_eval.handle(ctx, method, "/v1/stt/utterance-check", None,
                                                         {"session_id": session, "stt_request_id": req_id}))
            return status, json.loads(out)
        except ApiError as exc:
            return exc.status, {"detail": str(exc)}

    def test_server_record_is_used_not_client_text(self):
        status, d = self.call("POST", "sessA", "reqA0001", "A자재를 컨베이어로",
                              stt_text="A자재를 컨베이어로", utterance_id="utt_forged_id")   # 위조 시도
        self.assertEqual(status, 200)
        self.assertEqual(d["stt_text"], self.text_a)                 # 서버 저장소의 수정 전 STT
        self.assertEqual(d["utterance_id"], "utt_reqA0001")           # 서버가 만든 발화 id
        self.assertLess(d["rate_percent"], 100.0)                     # 위조한 '같은 문장'이 아니라 실제 STT와 비교
        saved = json.loads((self.tmp / "stt_utterance_checks" / "utt_reqA0001.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["session_id"], saved["stt_text_source"]), ("sessA", "server_record"))
        self.assertFalse((self.tmp / "stt_utterance_checks" / "utt_forged_id.json").exists())

    def test_other_session_cannot_read_confirm_or_overwrite(self):
        self.assertEqual(self.call("POST", "sessA", "reqA0001", "A자재를 컨베이어로")[0], 200)
        self.assertEqual(self.call("POST", "sessB", "reqA0001", "아무거나")[0], 403)         # 확정·덮어쓰기
        self.assertEqual(self.call("GET", "sessB", "reqA0001")[0], 403)                       # 조회
        status, d = self.call("GET", "sessA", "reqA0001")
        self.assertEqual((status, d["confirm_count"]), (200, 1))                              # 덮어써지지 않음
        saved = json.loads((self.tmp / "stt_utterance_checks" / "utt_reqA0001.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["reference"], "A자재를 컨베이어로")
        # 저장소 기록을 거치지 않은 직접 덮어쓰기도 거부
        from stt.utterance_check import OwnershipError, confirm
        with self.assertRaises(OwnershipError):
            confirm(utterance_id="utt_reqA0001", stt_text=saved["stt_text"], reference="x", stt_request_id="reqA0001",
                    model_key=KEY, root=self.tmp / "stt_utterance_checks", session_id="sessB")

    def test_same_session_reconfirm_is_one_record(self):
        self.call("POST", "sessA", "reqA0001", "A자태를 컨베이어로")
        status, d = self.call("POST", "sessA", "reqA0001", "A자재를 컨베이어로")
        self.assertEqual((status, d["confirm_count"]), (200, 2))
        self.assertEqual(len(list((self.tmp / "stt_utterance_checks").glob("*.json"))), 1)

    def test_unknown_request_bad_session_and_unconfirmed(self):
        self.assertEqual(self.call("POST", "sessA", "reqZZZZ01", "x")[0], 404)
        self.assertEqual(self.call("POST", "sessX", "reqA0001", "x")[0], 404)               # 없는 세션
        self.assertEqual(self.call("POST", "sessA", "../../etc", "x")[0], 400)
        status, d = self.call("GET", "sessA", "reqA0001")
        self.assertEqual((status, d["status"]), (200, "unconfirmed"))

    def test_text_request_is_not_an_utterance(self):
        from storage.records import RequestRecord
        self.repo.save_request(RequestRecord(request_id="reqT0001", utterance="텍스트 명령", schema_version="1.0",
                                             created_at=1.0, session_id="sessA"))
        self.assertEqual(self.call("POST", "sessA", "reqT0001", "텍스트 명령")[0], 403)

