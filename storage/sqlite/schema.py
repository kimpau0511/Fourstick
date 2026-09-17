"""SQLite 전용 스키마와 마이그레이션.

**이 파일과 같은 패키지 밖에는 SQL이 없다.** DB 방언을 한 곳에 모아 PostgreSQL
구현체를 추가할 때 대조 대상이 명확하도록 한다.

마이그레이션 원칙 (요구 13):
- 전진 전용(forward-only)이다. 되돌리는 스크립트를 두지 않는다 — 검증되지 않은
  롤백 경로는 데이터 손상 위험이 크다. 되돌려야 하면 백업에서 복구한다.
- 각 마이그레이션은 번호와 설명을 갖고 `schema_migrations`에 기록된다.
- 적용은 하나의 트랜잭션 안에서 이뤄진다. 실패하면 아무것도 적용되지 않는다.
- 저장 스키마 번호는 **Task Plan 계약 버전과 별개**다. 계약 버전은
  `core/constants.py`가, 저장 스키마 번호는 여기가 출처다. 둘을 같은 숫자로
  묶지 않는다 — 계약이 그대로여도 인덱스나 컬럼이 바뀔 수 있다.
- 계약 버전이 올라가 저장 형태가 바뀌면 마이그레이션을 추가하고, 기존 행의
  변환이 필요하면 변환과 변환 결과 동등성 검사를 함께 넣는다.

타입 선택 근거 (요구 12):
- ID·enum·JSON은 TEXT. Boolean은 INTEGER 0/1. 시각은 REAL(UTC epoch 초).
- DB의 타임스탬프·JSON·BOOLEAN 타입과 자동 증가 키를 쓰지 않는다.
- 외래키는 SQLite에서 기본 비활성이므로 연결마다 `PRAGMA foreign_keys = ON`을
  켠다(구현체 참고). PostgreSQL은 항상 활성이다.
"""

from __future__ import annotations

MIGRATION_0001 = """
CREATE TABLE requests (
    request_id      TEXT PRIMARY KEY,
    utterance       TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    created_at      REAL NOT NULL
);

CREATE TABLE plans (
    plan_id         TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES requests(request_id),
    plan_hash       TEXT NOT NULL,
    -- 원본 보존
    plan_json       TEXT NOT NULL,
    -- 검색용 복제 컬럼 (원본과 어긋나면 조회 시 거부한다)
    schema_version  TEXT NOT NULL,
    robot_id        TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    terminal_hold   TEXT,
    plan_created_at REAL NOT NULL,
    stored_at       REAL NOT NULL,
    UNIQUE (request_id, plan_hash)
);
CREATE INDEX idx_plans_request ON plans(request_id);
CREATE INDEX idx_plans_hash ON plans(plan_hash);

CREATE TABLE validations (
    validation_id   TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    plan_hash       TEXT NOT NULL,
    decision        TEXT NOT NULL,
    policy_id       TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    evaluated_at    REAL NOT NULL
);
CREATE INDEX idx_validations_plan ON validations(plan_id, evaluated_at);

CREATE TABLE validation_rules (
    validation_id   TEXT NOT NULL REFERENCES validations(validation_id),
    rule_code       TEXT NOT NULL,
    status          TEXT NOT NULL,
    reason          TEXT,
    message         TEXT NOT NULL,
    PRIMARY KEY (validation_id, rule_code)
);

CREATE TABLE permits (
    permit_id       TEXT PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    plan_hash       TEXT NOT NULL,
    granted         INTEGER NOT NULL,
    policy_id       TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    decided_at      REAL NOT NULL
);
CREATE INDEX idx_permits_plan ON permits(plan_id, decided_at);

CREATE TABLE permit_reasons (
    permit_id       TEXT NOT NULL REFERENCES permits(permit_id),
    seq             INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    detail          TEXT NOT NULL,
    PRIMARY KEY (permit_id, seq)
);

CREATE TABLE executions (
    execution_id    TEXT PRIMARY KEY,
    request_id      TEXT NOT NULL REFERENCES requests(request_id),
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id),
    plan_hash       TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,
    adapter_id      TEXT NOT NULL,
    robot_id        TEXT NOT NULL,
    profile_id      TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    policy_id       TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    started_at      REAL NOT NULL,
    UNIQUE (request_id, attempt_no)
);
CREATE INDEX idx_executions_request ON executions(request_id, attempt_no);
CREATE INDEX idx_executions_plan ON executions(plan_id);

CREATE TABLE state_transitions (
    execution_id    TEXT NOT NULL REFERENCES executions(execution_id),
    seq             INTEGER NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    reason          TEXT,
    occurred_at     REAL NOT NULL,
    PRIMARY KEY (execution_id, seq)
);

CREATE TABLE observations (
    execution_id    TEXT NOT NULL REFERENCES executions(execution_id),
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    observed_at     REAL NOT NULL,
    payload_json    TEXT NOT NULL,
    PRIMARY KEY (execution_id, seq)
);
CREATE INDEX idx_observations_kind ON observations(execution_id, kind);

CREATE TABLE execution_results (
    execution_id     TEXT NOT NULL REFERENCES executions(execution_id),
    seq              INTEGER NOT NULL,
    state            TEXT NOT NULL,
    request_accepted INTEGER NOT NULL,
    motion_completed INTEGER NOT NULL,
    target_reached   INTEGER NOT NULL,
    task_succeeded   INTEGER NOT NULL,
    verified         INTEGER NOT NULL,
    reason           TEXT,
    evidence_json    TEXT NOT NULL,
    recorded_at      REAL NOT NULL,
    PRIMARY KEY (execution_id, seq)
);
"""

MIGRATION_0002 = """
-- 확정된 사용자 요청의 STT 메타데이터. 텍스트 입력 요청은 전부 NULL이다.
-- partial transcript와 PCM 오디오는 저장하지 않는다 — 평가용 원본 음성은
-- 일반 실행 기록과 분리해 파일로 두고 보존 여부를 설정으로 제어한다
-- (SttModelConfig.retain_eval_audio / eval_audio_dir).
ALTER TABLE requests ADD COLUMN transcript_confidence REAL;
ALTER TABLE requests ADD COLUMN needs_user_confirmation INTEGER;
ALTER TABLE requests ADD COLUMN stt_model TEXT;
ALTER TABLE requests ADD COLUMN stt_language TEXT;
ALTER TABLE requests ADD COLUMN stt_config_version TEXT;
ALTER TABLE requests ADD COLUMN stt_input_at REAL;
ALTER TABLE requests ADD COLUMN stt_confirmed_at REAL;
ALTER TABLE requests ADD COLUMN stt_latency_sec REAL;
"""

MIGRATION_0003 = """
-- STT 실행 시도를 요청에서 분리한다. 전진 전용 — 0002를 고치지 않고 덧붙인다.
--
-- 0002는 확정 transcript의 STT 메타데이터를 `requests`의 컬럼으로 두었다.
-- 그러면 재전사나 모델 교체 때 같은 행을 덮어써야 하고, 같은 음성을 여러
-- 구성으로 돌린 비교 결과를 담을 자리가 없다. 실행 시도는 본질적으로 여러
-- 개이므로 append-only 테이블로 옮긴다.
CREATE TABLE stt_inferences (
    stt_inference_id       TEXT PRIMARY KEY,
    session_id             TEXT    NOT NULL,
    -- 세션 안의 시도 번호. 재전사·모델 교체가 늘려간다.
    attempt_no             INTEGER NOT NULL,
    schema_version         TEXT    NOT NULL,
    created_at             REAL    NOT NULL,

    model_name             TEXT    NOT NULL,
    model_version          TEXT    NOT NULL,
    profile_id             TEXT    NOT NULL,
    profile_version        TEXT    NOT NULL,
    -- verified | candidate. 미검증 구성의 측정치를 검증된 것으로 읽지 않기 위해 남긴다.
    verification           TEXT    NOT NULL,
    device                 TEXT    NOT NULL,
    compute_type           TEXT    NOT NULL,
    language               TEXT    NOT NULL,

    -- 원자료를 각각 저장한다. rtf는 여기서 파생한 사본이다.
    audio_duration_ms      INTEGER NOT NULL,
    processing_duration_ms INTEGER NOT NULL,
    model_load_duration_ms INTEGER,
    rtf                    REAL,

    transcript             TEXT    NOT NULL,
    confidence             REAL    NOT NULL,
    confidence_metric      TEXT    NOT NULL,
    vad_speech_detected    INTEGER,
    vad_max_probability    REAL,
    final_adopted          INTEGER NOT NULL,
    reason_code            TEXT,
    request_id             TEXT,

    CHECK (attempt_no >= 1),
    CHECK (audio_duration_ms >= 0 AND processing_duration_ms >= 0),
    CHECK (confidence BETWEEN 0.0 AND 1.0),
    CHECK (final_adopted IN (0, 1)),
    CHECK (verification IN ('verified', 'candidate')),
    -- 채택된 시도는 반드시 요청에 연결되고, 실패 사유를 갖지 않는다.
    CHECK (final_adopted = 0 OR (request_id IS NOT NULL AND reason_code IS NULL)),
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

-- 같은 세션에서 시도 번호가 겹치지 않는다.
CREATE UNIQUE INDEX ux_stt_inferences_attempt ON stt_inferences(session_id, attempt_no);
-- 한 요청이 채택하는 시도는 하나다.
CREATE UNIQUE INDEX ux_stt_inferences_adopted ON stt_inferences(request_id)
    WHERE final_adopted = 1 AND request_id IS NOT NULL;
CREATE INDEX ix_stt_inferences_request ON stt_inferences(request_id);
CREATE INDEX ix_stt_inferences_profile ON stt_inferences(profile_id, created_at);

-- 요청은 채택한 실행 기록의 식별자만 갖는다.
-- 여기에는 외래키를 걸지 않는다. stt_inferences.request_id가 requests를 참조하고
-- 있어서 양쪽에 걸면 삽입 순서가 순환한다(요청 먼저 넣으면 실행 기록이 없고,
-- 실행 기록 먼저 넣으면 요청이 없다). 대신 저장소가 요청과 채택된 실행 기록을
-- 한 트랜잭션에서 함께 넣고, 채택 대상이 그 요청의 기록인지 검사한다.
ALTER TABLE requests ADD COLUMN selected_stt_inference_id TEXT;

-- 0002 컬럼의 기존 값은 버리지도, 없는 측정값을 채워 넣지도 않는다.
-- 옛 스키마에는 전사 본문·장치·compute_type·음성 길이·처리 시간이 아예 없어서
-- stt_inferences로 옮기면 값을 지어내야 한다. 그러므로 원본 8개 컬럼 그대로
-- 격리 테이블에 보존하고, 새 테이블에는 넣지 않는다.
CREATE TABLE stt_metadata_legacy_0002 (
    request_id              TEXT PRIMARY KEY,
    transcript_confidence   REAL,
    needs_user_confirmation INTEGER,
    stt_model               TEXT,
    stt_language            TEXT,
    stt_config_version      TEXT,
    stt_input_at            REAL,
    stt_confirmed_at        REAL,
    stt_latency_sec         REAL,
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

INSERT INTO stt_metadata_legacy_0002 (
    request_id, transcript_confidence, needs_user_confirmation, stt_model,
    stt_language, stt_config_version, stt_input_at, stt_confirmed_at, stt_latency_sec
)
SELECT request_id, transcript_confidence, needs_user_confirmation, stt_model,
       stt_language, stt_config_version, stt_input_at, stt_confirmed_at, stt_latency_sec
FROM requests
WHERE stt_config_version IS NOT NULL;

ALTER TABLE requests DROP COLUMN transcript_confidence;
ALTER TABLE requests DROP COLUMN needs_user_confirmation;
ALTER TABLE requests DROP COLUMN stt_model;
ALTER TABLE requests DROP COLUMN stt_language;
ALTER TABLE requests DROP COLUMN stt_config_version;
ALTER TABLE requests DROP COLUMN stt_input_at;
ALTER TABLE requests DROP COLUMN stt_confirmed_at;
ALTER TABLE requests DROP COLUMN stt_latency_sec;
"""

MIGRATION_0004 = """
-- 전사 실행 경로 구분. 전진 전용 — 0003을 고치지 않고 덧붙인다.
--
-- 평가 실행기는 Transcriber를 직접 호출해서 VAD 게이트와 세션 시간 예산을
-- 지나지 않는다. 그 측정치가 운영 지연 통계에 섞이면 안 되므로 경로를 남긴다.
-- 기존 행은 모두 세션에서 만들어진 것이라 operational로 채운다(추정이 아니라
-- 0003 시점에는 평가 경로가 기록을 남기지 않았다는 사실에 근거한다).
ALTER TABLE stt_inferences ADD COLUMN execution_path TEXT
    NOT NULL DEFAULT 'operational';

CREATE INDEX ix_stt_inferences_path ON stt_inferences(execution_path, created_at);
"""

MIGRATION_0005 = """
-- 계획 생성(LLM) 시도 기록. append-only. 전진 전용.
--
-- 같은 request_id의 재시도는 기존 행을 고치지 않고 새 planning_attempt_id를
-- 받는다. 검색에 필요한 식별자와 상태는 컬럼으로 두고, 프롬프트·출력 원본은
-- 버전이 포함된 JSON 한 덩어리로 보존한다(구조가 자주 바뀐다).
--
-- API Key·토큰·인증 헤더·환경변수는 저장하지 않는다. payload는
-- planning/redaction.py를 지난 것만 들어오고, 레코드 불변식이 한 번 더 막는다.
CREATE TABLE planning_attempts (
    planning_attempt_id     TEXT PRIMARY KEY,
    request_id              TEXT    NOT NULL,
    attempt_no              INTEGER NOT NULL,
    schema_version          TEXT    NOT NULL,

    -- 공급자·모델·프롬프트·출력 스키마. STOP 우회는 모델을 부르지 않아 NULL이다.
    provider_id             TEXT,
    model_id                TEXT,
    prompt_template_version TEXT,
    output_schema_version   TEXT,

    -- 카탈로그 버전. 같은 발화가 다른 카탈로그에서 달라지는 것을 추적한다.
    resource_catalog_version TEXT   NOT NULL,
    skill_catalog_version    TEXT   NOT NULL,

    started_at              REAL    NOT NULL,
    ended_at                REAL    NOT NULL,
    duration_ms             INTEGER NOT NULL,

    status                  TEXT    NOT NULL,
    reason_code             TEXT,
    detail                  TEXT    NOT NULL DEFAULT '',

    -- 생성된 계획. plans에는 외래키를 걸지 않는다 — 계획 저장은 호출자 책임이고,
    -- 계획 저장이 실패해도 시도 기록은 남아야 한다.
    plan_id                 TEXT,
    plan_hash               TEXT,

    is_mock                 INTEGER NOT NULL DEFAULT 0,
    is_retry                INTEGER NOT NULL DEFAULT 0,
    previous_attempt_id     TEXT,
    stt_inference_id        TEXT,

    payload_version         TEXT,
    payload_json            TEXT,
    payload_truncated       INTEGER,
    payload_retention_days  INTEGER,

    CHECK (attempt_no >= 1),
    CHECK (duration_ms >= 0),
    CHECK (ended_at >= started_at),
    CHECK (status IN ('succeeded', 'failed', 'stop_bypass')),
    CHECK (is_mock IN (0, 1)),
    CHECK (is_retry IN (0, 1)),
    -- 성공한 시도는 계획과 공급자 식별자를 갖는다.
    CHECK (status <> 'succeeded' OR (
        plan_id IS NOT NULL AND plan_hash IS NOT NULL
        AND provider_id IS NOT NULL AND model_id IS NOT NULL
        AND prompt_template_version IS NOT NULL
        AND output_schema_version IS NOT NULL
        AND reason_code IS NULL
    )),
    -- 실패한 시도는 이유 코드를 갖는다.
    CHECK (status <> 'failed' OR reason_code IS NOT NULL),
    -- STOP 우회는 모델을 호출하지 않는다.
    CHECK (status <> 'stop_bypass' OR (
        provider_id IS NULL AND model_id IS NULL AND is_mock = 0
    )),
    CHECK (is_retry = 0 OR previous_attempt_id IS NOT NULL),
    CHECK (previous_attempt_id IS NULL OR previous_attempt_id <> planning_attempt_id),

    FOREIGN KEY (request_id) REFERENCES requests(request_id),
    FOREIGN KEY (previous_attempt_id) REFERENCES planning_attempts(planning_attempt_id),
    FOREIGN KEY (stt_inference_id) REFERENCES stt_inferences(stt_inference_id)
);

-- 같은 요청에서 시도 번호가 겹치지 않는다.
CREATE UNIQUE INDEX ux_planning_attempts_no ON planning_attempts(request_id, attempt_no);
-- 한 계획을 만든 시도는 하나다(계획에서 시도로 거꾸로 추적할 수 있게).
CREATE UNIQUE INDEX ux_planning_attempts_plan ON planning_attempts(plan_id)
    WHERE plan_id IS NOT NULL;
CREATE INDEX ix_planning_attempts_request ON planning_attempts(request_id, started_at);
-- 모델별 통계. is_mock을 함께 색인해 Mock을 섞지 않고 집계한다.
CREATE INDEX ix_planning_attempts_model ON planning_attempts(provider_id, model_id, is_mock);
"""

MIGRATION_0006 = """
-- 실제 LLM 호출 관측값과 검증 단계 기록. 전진 전용 — 0005를 고치지 않는다.
--
-- 설정값이 아니라 **서버가 알려준 값**을 남긴다. 설정과 서버가 다를 수 있고,
-- 기록에는 실제로 무엇이 답했는지가 남아야 한다. Mock 시도는 이 컬럼들이 NULL
-- 이므로 실제 호출 통계와 섞이지 않는다.
ALTER TABLE planning_attempts ADD COLUMN served_model_id TEXT;
ALTER TABLE planning_attempts ADD COLUMN server_version TEXT;
ALTER TABLE planning_attempts ADD COLUMN quantization TEXT;
ALTER TABLE planning_attempts ADD COLUMN structured_output INTEGER;
ALTER TABLE planning_attempts ADD COLUMN thinking_mode TEXT;
ALTER TABLE planning_attempts ADD COLUMN prompt_tokens INTEGER;
ALTER TABLE planning_attempts ADD COLUMN completion_tokens INTEGER;
-- 검증 단계별 결과. 단계 수가 늘어날 수 있어 JSON 배열로 둔다.
ALTER TABLE planning_attempts ADD COLUMN validation_stages_json TEXT;

CREATE INDEX ix_planning_attempts_served
    ON planning_attempts(served_model_id, is_mock, started_at);
"""

MIGRATION_0007 = """
-- 평가 실행 구분과 프롬프트 버전 비교용 컬럼. 전진 전용.
--
-- 기존 행의 경로는 **채워 넣지 않는다.** NULL은 "이 컬럼이 생기기 전의 기록"을
-- 뜻한다. 0006까지의 행이 평가였는지 운영이었는지 스키마가 단정할 근거가 없다
-- (실제로는 평가 실행기에서 나왔지만, 그 사실은 기록이 아니라 맥락이다).
ALTER TABLE planning_attempts ADD COLUMN execution_path TEXT;
ALTER TABLE planning_attempts ADD COLUMN evaluation_run_id TEXT;
ALTER TABLE planning_attempts ADD COLUMN evaluation_label TEXT;
ALTER TABLE planning_attempts ADD COLUMN evaluation_case_id TEXT;

-- 프롬프트 버전별 비교: 같은 사례를 라벨로 나눠 본다.
CREATE INDEX ix_planning_attempts_eval
    ON planning_attempts(evaluation_run_id, evaluation_label, evaluation_case_id);
CREATE INDEX ix_planning_attempts_prompt
    ON planning_attempts(prompt_template_version, model_id, execution_path);
"""

MIGRATION_0008 = """
-- 작업자 승인·거부 기록. append-only. 전진 전용.
--
-- 재승인·재실행이 기존 기록을 덮지 않는다. 같은 계획을 다시 승인하면 새 행이
-- 쌓이고, 누가 언제 무엇을 보고 결정했는지가 남는다.
CREATE TABLE plan_approvals (
    approval_id             TEXT PRIMARY KEY,
    plan_id                 TEXT    NOT NULL,
    plan_hash               TEXT    NOT NULL,
    request_id              TEXT    NOT NULL,
    decision                TEXT    NOT NULL,
    decided_at              REAL    NOT NULL,
    schema_version          TEXT    NOT NULL,
    planning_attempt_id     TEXT,
    -- 승인 시점의 요청↔계획 리소스 일치 검증 결과.
    consistency_status      TEXT,
    consistency_reason_code TEXT,
    note                    TEXT    NOT NULL DEFAULT '',

    CHECK (decision IN ('approved', 'rejected')),
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id),
    FOREIGN KEY (request_id) REFERENCES requests(request_id)
);

CREATE INDEX ix_plan_approvals_plan ON plan_approvals(plan_id, decided_at);
CREATE INDEX ix_plan_approvals_request ON plan_approvals(request_id, decided_at);
"""

MIGRATION_0009 = """
-- 다중 세션 격리 (7단계 보강). 전진 전용.
--
-- 서버가 '현재 계획' 하나를 전역으로 들고 있던 구조를 없애고, 모든 작업을
-- 명시적 식별자로 조회한다. 그러려면 어떤 세션의 것인지가 기록에 있어야 한다.
--
-- 기존 행의 session_id는 **채우지 않는다.** NULL은 "세션 개념이 생기기 전의
-- 기록"을 뜻한다. 어느 세션의 것이었는지 추정할 근거가 없다.
CREATE TABLE sessions (
    session_id      TEXT PRIMARY KEY,
    created_at      REAL NOT NULL,
    last_seen_at    REAL NOT NULL,
    ended_at        REAL,
    status          TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    -- 어디서 만들어진 세션인지(브라우저/스크립트 등). 인증이 아니다.
    origin          TEXT NOT NULL DEFAULT '',

    CHECK (status IN ('active', 'ended', 'expired')),
    CHECK (ended_at IS NULL OR ended_at >= created_at),
    CHECK (last_seen_at >= created_at)
);
CREATE INDEX ix_sessions_status ON sessions(status, last_seen_at);

-- request ↔ session
ALTER TABLE requests ADD COLUMN session_id TEXT REFERENCES sessions(session_id);
CREATE INDEX ix_requests_session ON requests(session_id, created_at);

-- approval ↔ session·plan
ALTER TABLE plan_approvals ADD COLUMN session_id TEXT REFERENCES sessions(session_id);
CREATE INDEX ix_plan_approvals_session ON plan_approvals(session_id, decided_at);

-- execution ↔ session·approval, 그리고 실행 환경 구분
ALTER TABLE executions ADD COLUMN session_id TEXT REFERENCES sessions(session_id);
ALTER TABLE executions ADD COLUMN approval_id TEXT REFERENCES plan_approvals(approval_id);
-- 어댑터 종류와 시뮬레이션 여부. completed여도 실제 로봇 실행으로 집계하지
-- 않기 위해 실행 기록에 남긴다.
ALTER TABLE executions ADD COLUMN adapter_kind TEXT;
ALTER TABLE executions ADD COLUMN is_simulated INTEGER;
CREATE INDEX ix_executions_session ON executions(session_id, started_at);
CREATE INDEX ix_executions_simulated ON executions(is_simulated, started_at);
"""

#: (번호, 설명, SQL). 번호는 건너뛰지 않고 이어붙인다.
MIGRATION_0010 = """
-- 승인 시점의 로봇·Profile 버전 기록 (6-06 / 7-06). 전진 전용.
--
-- 계획에도 profile_id/profile_version이 있지만, 승인 기록이 스스로 "작업자가
-- 무엇을 보고 눌렀는지"를 담아야 한다. 승인 요청이 보낸 값과 저장된 계획의
-- 값을 대조한 결과가 이 열이다 — 대조에 실패하면 승인 자체가 성립하지 않으므로
-- 여기에는 일치한 값만 들어온다.
--
-- 기존 행은 채우지 않는다(NULL = 이 열이 생기기 전의 승인).
ALTER TABLE plan_approvals ADD COLUMN robot_id TEXT;
ALTER TABLE plan_approvals ADD COLUMN profile_id TEXT;
ALTER TABLE plan_approvals ADD COLUMN profile_version TEXT;
"""

MIGRATION_0011 = """
-- 실행 환경 값의 모순을 저장에서 막는다 (7단계 보강). 전진 전용.
--
-- 규칙:
--   * adapter_kind = 'fake'      -> is_simulated = 1 (Fake는 반드시 simulated)
--   * 실제 어댑터(fake 아님)     -> is_simulated = 0은 **연결이 확인된 경우만**
--     (environment_confirmed_at NOT NULL). 근거가 없으면 NULL(unknown)이다.
--   * adapter_kind가 없으면      -> is_simulated도 NULL (근거 없는 옛 기록)
--
-- SQLite는 기존 표에 CHECK를 추가할 수 없다. 표를 다시 만드는 방법은
-- state_transitions·observations·execution_results가 executions를 참조하므로
-- 외래키 강제 아래에서 DROP이 실패한다(실측). 그래서 **트리거**로 같은 규칙을
-- 강제한다 — 모순된 행은 INSERT·UPDATE 시점에 거부된다.
ALTER TABLE executions ADD COLUMN environment_confirmed_at REAL;

CREATE TRIGGER trg_executions_env_insert
BEFORE INSERT ON executions
WHEN NOT (
        (IFNULL(NEW.adapter_kind, '') = 'fake' AND IFNULL(NEW.is_simulated, -1) = 1)
     OR (IFNULL(NEW.adapter_kind, '') <> ''
         AND IFNULL(NEW.adapter_kind, '') <> 'fake'
         AND (IFNULL(NEW.is_simulated, -1) = 1
              OR (IFNULL(NEW.is_simulated, -1) = 0
                  AND NEW.environment_confirmed_at IS NOT NULL)
              OR NEW.is_simulated IS NULL))
     OR (IFNULL(NEW.adapter_kind, '') = '' AND NEW.is_simulated IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'adapter_kind와 실행 환경 값이 모순된다');
END;

CREATE TRIGGER trg_executions_env_update
BEFORE UPDATE ON executions
WHEN NOT (
        (IFNULL(NEW.adapter_kind, '') = 'fake' AND IFNULL(NEW.is_simulated, -1) = 1)
     OR (IFNULL(NEW.adapter_kind, '') <> ''
         AND IFNULL(NEW.adapter_kind, '') <> 'fake'
         AND (IFNULL(NEW.is_simulated, -1) = 1
              OR (IFNULL(NEW.is_simulated, -1) = 0
                  AND NEW.environment_confirmed_at IS NOT NULL)
              OR NEW.is_simulated IS NULL))
     OR (IFNULL(NEW.adapter_kind, '') = '' AND NEW.is_simulated IS NULL)
)
BEGIN
    SELECT RAISE(ABORT, 'adapter_kind와 실행 환경 값이 모순된다');
END;
"""

MIGRATION_0012 = """
-- 검증 실행 이력 append-only (6-04·6-05)와 승인의 검증 조건 묶기.
--
-- 환경 데이터 본문이나 기하 모델은 여기 복사하지 않는다. snapshot 식별자와
-- hash로만 추적한다(md/저장소_설계.md "원본 보존과 검색 가능 필드").
CREATE TABLE validation_runs (
    validation_run_id   TEXT PRIMARY KEY,
    session_id          TEXT REFERENCES sessions(session_id),
    request_id          TEXT NOT NULL REFERENCES requests(request_id),
    plan_id             TEXT NOT NULL REFERENCES plans(plan_id),
    plan_hash           TEXT NOT NULL,
    -- 실행 직전 재검증이면 그 실행 식별자. 계획 단계 검증이면 NULL이다.
    execution_id        TEXT REFERENCES executions(execution_id),
    -- 무엇이 검사했는가: safety / capability / geometry / consistency ...
    validator_kind      TEXT NOT NULL,
    validator_id        TEXT NOT NULL,
    validator_version   TEXT NOT NULL,
    robot_id            TEXT,
    profile_id          TEXT,
    profile_version     TEXT,
    policy_id           TEXT,
    policy_version      TEXT,
    snapshot_id         TEXT,
    snapshot_version    TEXT,
    snapshot_hash       TEXT,
    -- 검사에 필요한 입력이 모두 있었는가. ALLOW의 전제다.
    input_complete      INTEGER NOT NULL,
    decision            TEXT NOT NULL,
    reason_code         TEXT,
    detail              TEXT NOT NULL DEFAULT '',
    started_at          REAL NOT NULL,
    finished_at         REAL NOT NULL,
    duration_ms         INTEGER NOT NULL,
    schema_version      TEXT NOT NULL,

    CHECK (decision IN ('allow', 'block', 'ask')),
    CHECK (input_complete IN (0, 1)),
    -- 입력이 불완전한 통과를 저장에서 막는다.
    CHECK (NOT (decision = 'allow' AND input_complete = 0)),
    CHECK (finished_at >= started_at),
    CHECK (duration_ms >= 0)
);
CREATE INDEX ix_validation_runs_plan ON validation_runs(plan_id, started_at);
CREATE INDEX ix_validation_runs_request ON validation_runs(request_id, started_at);
CREATE INDEX ix_validation_runs_kind ON validation_runs(validator_kind, decision);
CREATE INDEX ix_validation_runs_execution ON validation_runs(execution_id);

-- 승인을 검증 당시 조건에 묶는다. 하나라도 달라지면 재검증·재승인이다.
ALTER TABLE plan_approvals ADD COLUMN policy_id TEXT;
ALTER TABLE plan_approvals ADD COLUMN policy_version TEXT;
ALTER TABLE plan_approvals ADD COLUMN snapshot_id TEXT;
ALTER TABLE plan_approvals ADD COLUMN snapshot_version TEXT;
ALTER TABLE plan_approvals ADD COLUMN snapshot_hash TEXT;
ALTER TABLE plan_approvals ADD COLUMN validation_run_id TEXT
    REFERENCES validation_runs(validation_run_id);
CREATE INDEX ix_plan_approvals_validation ON plan_approvals(validation_run_id);
"""

MIGRATION_0013 = """
-- 조합형 로봇 Profile 기록 (8-02). append-only.
--
-- 팔·그리퍼·장착 Profile의 식별자와 버전, 자산 manifest 버전, 출처 commit과
-- checksum, 시뮬레이션/실제 구분, 확인 상태와 미확인 항목을 함께 보존한다.
-- **기존 실행·승인 기록을 새 Profile 버전으로 갱신하지 않는다** — 새 버전은
-- 새 행이고, 승인은 Profile 버전에 묶여 있어 자동으로 무효가 된다.
CREATE TABLE robot_profiles (
    composite_profile_id        TEXT NOT NULL,
    composite_profile_version   TEXT NOT NULL,
    display_name                TEXT NOT NULL,
    arm_profile_id              TEXT NOT NULL,
    arm_profile_version         TEXT NOT NULL,
    gripper_profile_id          TEXT,
    gripper_profile_version     TEXT,
    mounting_profile_id         TEXT,
    mounting_profile_version    TEXT,
    asset_manifest_version      TEXT NOT NULL,
    source_commit               TEXT NOT NULL DEFAULT '',
    source_checksum             TEXT NOT NULL DEFAULT '',
    environment                 TEXT NOT NULL,
    verified                    INTEGER NOT NULL,
    supported_skills            TEXT NOT NULL,
    unverified_items            TEXT NOT NULL,
    recorded_at                 REAL NOT NULL,
    schema_version              TEXT NOT NULL,

    PRIMARY KEY (composite_profile_id, composite_profile_version),
    CHECK (environment IN ('simulation', 'real')),
    CHECK (verified IN (0, 1)),
    -- 미확인 항목이 남아 있는데 verified로 기록할 수 없다.
    CHECK (NOT (verified = 1 AND unverified_items <> '[]'))
);
CREATE INDEX ix_robot_profiles_arm
    ON robot_profiles(arm_profile_id, arm_profile_version);
CREATE INDEX ix_robot_profiles_gripper
    ON robot_profiles(gripper_profile_id, gripper_profile_version);
"""

MIGRATION_0014 = """
-- 시뮬레이터 검증 실행 기록 (8-03~8-06). append-only.
--
-- 목표와 관측을 **따로** 담는다. 명령 전송 성공(command_accepted)과 목표 도달
-- (goal_reached)을 한 열에 합치지 않는다. 파지 확인 결과도 근거와 함께 남긴다.
-- 시뮬레이션 실행이므로 is_simulated는 항상 1이고, 실제 실행 집계에 섞이지
-- 않는다(`execution_environment_counts`와 별개 표다).
CREATE TABLE sim_verification_runs (
    verification_id          TEXT PRIMARY KEY,
    recorded_at              REAL NOT NULL,
    step                     TEXT NOT NULL,
    -- Profile·자산 버전
    composite_profile_id     TEXT NOT NULL,
    composite_profile_version TEXT NOT NULL,
    arm_profile_id           TEXT NOT NULL,
    arm_profile_version      TEXT NOT NULL,
    gripper_profile_id       TEXT,
    gripper_profile_version  TEXT,
    mounting_profile_id      TEXT,
    mounting_profile_version TEXT,
    asset_manifest_version   TEXT NOT NULL,
    -- 환경 버전
    ros_distro               TEXT NOT NULL,
    gazebo_version           TEXT NOT NULL,
    controller_versions      TEXT NOT NULL,
    -- 실행 환경 구분
    adapter_kind             TEXT NOT NULL,
    arm_only                 INTEGER NOT NULL,
    is_simulated             INTEGER NOT NULL,
    -- 목표와 관측
    target_json              TEXT NOT NULL,
    observed_json            TEXT NOT NULL,
    aperture_target          REAL,
    aperture_observed        REAL,
    contact_json             TEXT,
    grasp_json               TEXT,
    -- 5축 결과와 이유
    command_accepted         INTEGER NOT NULL,
    motion_completed         INTEGER,
    target_reached           INTEGER,
    task_succeeded           INTEGER,
    state                    TEXT NOT NULL,
    reason_code              TEXT,
    -- 연결 식별자(앱 경로를 지난 경우에만 채운다)
    validation_run_id        TEXT,
    approval_id              TEXT,
    execution_id             TEXT,
    attempt_no               INTEGER,
    detail                   TEXT NOT NULL DEFAULT '',
    schema_version           TEXT NOT NULL,

    CHECK (is_simulated = 1),
    CHECK (arm_only IN (0, 1)),
    CHECK (command_accepted IN (0, 1)),
    -- 목표 도달이 아니면 작업 성공일 수 없다.
    CHECK (NOT (task_succeeded = 1 AND target_reached = 0))
);
CREATE INDEX ix_sim_verification_profile
    ON sim_verification_runs(composite_profile_id, composite_profile_version);
CREATE INDEX ix_sim_verification_step
    ON sim_verification_runs(step, recorded_at);
"""

MIGRATION_0015 = """
-- MoveIt2 계획·충돌 검사 관측을 시뮬레이터 검증 기록에 잇는다 (8-07). append-only.
--
-- 규칙:
--   * **기존 행을 갱신하지 않는다.** 새 검증은 새 행이다.
--   * 시뮬레이션 결과는 여전히 is_simulated=1이다 — real 실행 집계와 섞지 않는다.
--   * 환경 데이터 본문을 넣지 않는다. planning scene은 id·hash·시각만 남긴다.
--   * 충돌 검사 판정은 allow/block/ask 중 하나다. SQLite는 기존 표에 CHECK를
--     추가할 수 없으므로 트리거로 강제한다(0011과 같은 방법).
ALTER TABLE sim_verification_runs ADD COLUMN moveit_config_version TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN kinematics_solver TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN kinematics_solver_version TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN planning_scene_snapshot_id TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN planning_scene_snapshot_version TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN planning_scene_snapshot_hash TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN planning_scene_checked_at REAL;
ALTER TABLE sim_verification_runs ADD COLUMN collision_decision TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN collision_reason_code TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN geometry_validator_id TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN geometry_validator_version TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN commanded_joints_json TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN observed_joints_json TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN target_pose_json TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN observed_pose_json TEXT;
ALTER TABLE sim_verification_runs ADD COLUMN sim_fixture_version TEXT;

CREATE TRIGGER trg_sim_verification_collision_insert
BEFORE INSERT ON sim_verification_runs
WHEN NEW.collision_decision IS NOT NULL
 AND NEW.collision_decision NOT IN ('allow', 'block', 'ask')
BEGIN
    SELECT RAISE(ABORT, '충돌 검사 판정은 allow/block/ask 중 하나여야 한다');
END;

-- ALLOW에는 검사한 환경이 있어야 한다. snapshot 없는 통과를 저장에서 막는다.
CREATE TRIGGER trg_sim_verification_allow_needs_scene
BEFORE INSERT ON sim_verification_runs
WHEN NEW.collision_decision = 'allow'
 AND (NEW.planning_scene_snapshot_id IS NULL
      OR NEW.planning_scene_snapshot_hash IS NULL
      OR NEW.planning_scene_checked_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, '충돌 검사 통과에는 planning scene snapshot이 필요하다');
END;

CREATE INDEX ix_sim_verification_scene
    ON sim_verification_runs(planning_scene_snapshot_hash);
"""


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "요청·계획·검증·허가·실행·이력 초기 스키마", MIGRATION_0001),
    (2, "확정 요청의 STT 메타데이터 컬럼 추가", MIGRATION_0002),
    (3, "STT 실행 기록을 append-only 테이블로 분리", MIGRATION_0003),
    (4, "전사 실행 경로(운영/평가) 구분 컬럼 추가", MIGRATION_0004),
    (5, "계획 생성 시도 append-only 기록 추가", MIGRATION_0005),
    (6, "실제 LLM 호출 관측값·검증 단계 기록 추가", MIGRATION_0006),
    (7, "평가 실행 구분과 프롬프트 버전 비교 컬럼 추가", MIGRATION_0007),
    (8, "작업자 승인·거부 append-only 기록 추가", MIGRATION_0008),
    (9, "세션 격리와 실행 환경 구분 컬럼 추가", MIGRATION_0009),
    (10, "승인 시점의 로봇·Profile 버전 기록 추가", MIGRATION_0010),
    (11, "실행 환경 값 모순 차단(트리거)과 연결 확인 시각 추가", MIGRATION_0011),
    (12, "검증 실행 이력과 승인의 검증 조건 묶기 추가", MIGRATION_0012),
    (13, "조합형 로봇 Profile 기록 추가", MIGRATION_0013),
    (14, "시뮬레이터 검증 실행 기록 추가", MIGRATION_0014),
    (15, "MoveIt2 계획·충돌 검사 관측 연결 추가", MIGRATION_0015),
)

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at  REAL NOT NULL
);
"""

LATEST_VERSION: int = MIGRATIONS[-1][0]
