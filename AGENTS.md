# forstick2 — 에이전트 작업 안내

Claude·Codex 등 모든 AI 에이전트가 세션 시작 때 읽는 문서다. 짧게 유지한다.
상세는 아래 문서 지도에서 필요한 것만 연다 — 전부 읽지 않는다.

## 이 프로젝트는

FR3 로봇 팔 + 2F-85 그리퍼 **Gazebo 작업 셀**(그리고 별도의 G1 휴머노이드 시뮬레이션)을
한국어 음성·텍스트 명령으로 움직인다. **현재 서비스는 시뮬레이션 전용**이다
(모든 실행 기록 `is_simulated=true`). FR3 실기 기준 데이터는 향후 확장용으로 보존만 한다.

핵심 가치: **LLM은 계획만 만들고, 실행 권한은 규칙 기반 검증기가 쥔다.**
그럴듯하게 틀린 실행보다 되묻기·차단이 낫다.

## 문서 지도

| 알고 싶은 것 | 볼 곳 |
|---|---|
| 무엇을·왜 만드나, 요구사항 ID, 성공 지표 | `docs/PRD.md` |
| 실행 방법·환경·웹 기능 | `README.md` |
| 계층 구조·요청 흐름 | `docs/ARCHITECTURE.md` |
| **왜** 이렇게 만들었나 (바꾸기 전 필독) | `docs/ADR.md` |
| 새 기능 진행 절차 (SDD) | `docs/SDD.md` |
| 기능별 스펙·진행 상태 | `specs/README.md` |
| G1 휴머노이드 | `humanoid/g1/README.md` |

코드 docstring의 `md/개발플랜.md`·`md/계획.md` 등은 **저장소에 없다**(`.gitignore`의 `md/`).
에이전트는 그 문서를 읽을 수 없다 — 내용을 추측하지 말고 docstring·코드를 근거로 삼는다.

## 작업 방식

- **애매하거나 잘 모르겠으면 항상 질문한다.** 코드·문서로 확인할 수 있는 건 직접 확인하고,
  그래도 남는 것만 묻는다. 추측으로 진행하지 않는다.
- 코딩 작업에는 `karpathy-guidelines` skill을 따른다 — 먼저 생각하기, 단순하게, 필요한 곳만 고치기,
  검증 가능한 목표. (Claude는 팀 플러그인으로, Codex는 `.agents/skills/`에서 읽는다.)
- 안전 동작·API·계층 계약 변경이나 동작이 모호한 새 기능은 **SDD**(`docs/SDD.md`),
  버그 수정·명확한 작은 변경은 **TDD**(재현 테스트 먼저), 오타·문서는 바로.
- 테스트는 기존 위치에 잇는다: `tests/unit`(순수 로직) · `tests/contract`(어댑터·core 계약) ·
  `tests/integration`(흐름·웹 API) · `tests/web`(JS 화면).
- 크기별 진행: 오타·주석은 바로 / 리팩터링은 요약 후 진행 / 새 엔드포인트·명령은
  설계 확인 후 / **안전 정책·로봇 구성·아키텍처 변경은 반드시 사람 확인 후**.
  "버그냐 결정이냐" 애매하면 결정으로 취급한다.
- 코딩 전에 검색: 비슷한 계약·검증기가 `core/`·`validation/`에 이미 있는지 먼저 본다.

## 제품 규칙은 ADR에

재검증·정보 부족 차단·STOP 우선·확인 카드·일반 경로 pick/place 차단 같은 안전 동작은
코딩 습관이 아니라 **제품 요구사항**이다. `docs/ADR.md`에 이유와 함께 있다.
`core/`·`validation/`·`server/`의 동작을 바꾸기 전에 관련 ADR을 읽고, 바꿔야 하면 ADR부터 고친다.
겉보기에 중복인 재검증도 요구사항이다 — "불필요한 에러 처리"로 보고 지우지 않는다.

## 규칙

- **이유 코드**: 공통 코드는 `core/reason_codes.py`가 유일한 출처 — `<category>.<reason>` 소문자
  (예: `capability.profile_incomplete`). 개별 안전 규칙 ID(`E-SEQ-001` 등)는 공통 코드로 매핑해 쓴다
  (`validation/safety_validator.py`의 `RULE_REASONS`). 다른 곳에서 문자열을 직접 만들지 않는다.
  기획서의 `A-SLOT`식 코드는 현재 쓰지 않는다.
- **환경변수**: `FORSTICK2_` 접두사 + 대문자. 예외: 테스트 전용은 `FORSTICK_`
  (`FORSTICK_TEST_TIMEOUT_SEC` 등). 값이 꼭 있어야 하면 기본값 없이 기동을 막는다.
- **경로**: 스크립트의 `/home/asd/...`는 예시 기본값 — 파일을 고치지 말고 환경변수로 덮는다.
  `config/profiles/*.json`의 경로는 측정 출처 기록이므로 바꾸지 않는다.
- Python 네이밍: 함수·변수 `snake_case`, 클래스 `PascalCase`, 상수 `UPPER_SNAKE_CASE`.
- 주석은 한국어로, **무엇이 아니라 왜**를 적는다. 우회·직관과 다른 선택엔 이유를 남긴다.
- API 키 등 비밀값을 에이전트 대화·터미널에 붙여넣지 않는다 — `.env`·환경변수로 넣는다.

## 명령어

```bash
./scripts/run_tests.sh                      # 전체 테스트 (기본 제한 60초)
node --test tests/web/*.test.mjs            # 웹 화면(JS)
./scripts/run_web_workcell.sh               # 웹 UI http://localhost:8092
```

테스트는 단순 명령으로 돌아가야 하고, 실패하면 0이 아닌 종료 코드를 낸다.

## 배운 점 (실수 → 규칙)

모든 작업에 해당하는 교정만 한 줄로 추가한다(사람 승인 후). 특정 결함은 회귀 테스트,
구조·결정은 해당 docs에 넣는다 — 위치 기준은 `docs/SDD.md` 환류 규칙.

- (아직 없음)
