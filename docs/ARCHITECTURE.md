# 아키텍처

코드에서 확인한 구조만 적는다. 각 모듈의 상세 계약은 해당 파일 docstring이 원본이다.
구조가 바뀌면 이 문서도 같은 작업에서 고친다.

## 디렉터리 (계층)

```
core/         계약 — TaskPlan·ReasonCode·Profile·정책·STOP 계약. 다른 계층에 의존하지 않는다
planning/     발화 → Task Plan. 슬롯 추출(규칙) → 모델 초안(vLLM) → 카탈로그 대조
validation/   규칙 기반 검증 — 안전 판정·요청↔계획 일치·실행 직전 재검증(Permit)·pick/place 관문
robots/       Robot Adapter — base 인터페이스, fake(개발용), fr3_gazebo, g1_gazebo, moveit, hardware(경계만)
stt/          스트리밍 STT — VAD(Silero) + faster-whisper, WebSocket 세션
storage/      Repository 인터페이스 + SQLite 구현. 모든 저장은 여기를 지난다
server/       웹 서버 — asgi(HTTP·WS) → api(요청 흐름) → runtime(조립). sim_demo_* 는 시연 경로
html/         웹 UI (Three.js 3D 화면 포함)
humanoid/g1/  G1 휴머노이드 제어기 — FR3와 별개 Gazebo 파티션·파이썬 환경
config/       로봇·그리퍼·장착·작업 셀 프로필 (측정값 + 출처)
scripts/      실행·측정·E2E 검증 스크립트
tests/        unit · contract · integration · model · web
```

## 일반 명령 흐름 (`/v1/plan` → `/v1/execute`)

`server/api.py`가 순서와 저장을 지킨다.

```
발화(텍스트 / STT final)
  → planning/pipeline.py
      1. 슬롯 추출 (모델 없음)
      2. 정지 키워드면 모델을 건너뛰고 stop 계획 생성 (요청 표현일 뿐, 실제 정지는 아래 STOP 경로)
      3. PlanProvider(vLLM)에 초안 요청 — 모델 호출은 여기 한 곳
      4. 초안 리소스를 ResourceCatalog와 대조 (모델이 지어낸 위치 차단)
      5. 스킬을 SkillCatalog·Profile과 대조
      6. TaskPlan 생성  (모델이 "확인 필요"라 하면 계획 대신 되묻기)
  → validation/safety_validator.py   규칙별 PASS/BLOCK/INSUFFICIENT_DATA/N/A → ALLOW·ASK·BLOCK
  → validation/request_plan_consistency.py   요청↔계획 리소스 일치
  → 사용자 승인 (/v1/decision)
  → validation/execution_permit.py   실행 요청 시점에 전부 다시 판정 (ASK도 불허)
  → Robot Adapter 실행 → 결과 저장
```

- 계획 생성 성공 ≠ 실행 승인. 실행은 같은 세션·요청·계획의 유효한 승인 기록이 있어야 한다.
- 서버는 '현재 계획'을 추정하지 않는다 — session_id·plan_id·plan_hash·approval_id 등 명시 식별자로만 조회.
- 일반 경로의 집기·놓기는 차단된다(`capability.profile_incomplete`).

## 시뮬레이션 시연 흐름 (`/v1/sim-demo/*`)

활성 작업 셀이 시뮬레이션일 때만 열린다.

```
발화 → server/sim_demo_commands.py (규칙 해석)
     → 모호하면 server/sim_demo_intent.py (Qwen은 JSON 분류만: intent·material_id·confidence)
     → server/sim_demo_confirm.py 확인 카드 (만료·취소·상태 변경 = 작업 0건)
     → server/sim_demo_jobs.py 가 scripts/demo_workcell_pick_place.sh 를 별도 프로세스로 실행
```

- 한 번에 하나의 시연 작업만(`server/cell_execution.py` 실행 권한).
- 정지는 정지 요청 파일 → 스크립트가 기존 STOP 절차로 멈춘다. 프로세스를 죽이지 않는다.

## STOP

위 계획 흐름과 **별개의 경로**다. 검증·승인·허가 단계를 거치지 않는다.

```
/v1/stop → 어댑터 STOP 계약(core/stop_contract.py) → exec.stopped | exec.stop_unconfirmed
```

정지 확인이 안 되면 `exec.stop_unconfirmed`이며 성공으로 표시하지 않는다. 래치 해제는 `/v1/stop/release`.
시연 경로의 정지는 정지 요청 파일을 거쳐 스크립트의 STOP 절차로 간다(위 참고).

## 주요 엔드포인트

| 경로 | 모듈 |
|---|---|
| `/v1/sessions` · `/v1/config` | `server/routes/session.py` |
| `/v1/plan` | `server/routes/planning.py` |
| `/v1/decision` · `/v1/execute` · `/v1/stop` · `/v1/state` | `server/routes/execution.py` |
| `/v1/sim-demo/*` | `server/routes/sim_demo.py` |
| `/v1/scene` · `/v1/scene/stream`(WS) | `server/routes/scene.py` · `server/asgi.py` |
| `/v1/sim-view` (3D, 읽기 전용) | `server/routes/sim_view.py` |
| `/v1/humanoid` | `server/routes/humanoid.py` |
| `/v1/stt`(WS) · `/v1/events` | `server/asgi.py` |
| `/v1/robots` | `server/routes/robot.py` |

## 새 로봇 추가 지점

`robots/base/robot_adapter.py` 인터페이스를 구현하고 `robots/registry.py`에 등록한다.
어댑터 적합성은 `tests/contract/test_adapter_conformance.py`로 확인한다.
