# dashboard2 화면설계서 구현 명세 (2026-10-02, Opus 설계 → Sonnet 구현)

QA 실패 항목(`문서/QA_결과.md`)을 구현한다. 요구 원문은 `문서/기획서_템플릿(포스틱).md` 화면설계서 가~아,
`문서/상태매트릭스_포스틱.md`. 결정은 `문서/QA_결과.md` 5장(D1~D8).

## 절대 규칙
- **백엔드(`server/`, `core/`, `tests/` 등)는 한 글자도 고치지 않는다.** 이 작업은 `dashboard2/` 안에서만.
- **화면은 서버 값을 그대로 보여 준다(설계원칙 2).** 서버에 없는 값은 지어내지 말고 "데이터 없음"/"확인 안 됨"으로.
  **확인 안 됨은 정상이 아니다**(설계원칙 4) — 값이 없으면 "정상" 배지를 붙이지 않는다.
- `src/data.js`(목업)는 더 이상 import하지 않는다(마지막에 파일 삭제).
- 라이트 테마 토큰(`styles.css` :root 변수)만 쓴다. 새 색 하드코딩 금지(꼭 필요하면 :root에 변수 추가).
- 문구: 내부값 PASS/BLOCK/ASK, plan_id, reason_code를 1차 화면에 쓰지 않는다(용어표: 안전 확인됨/실행 차단/추가 확인 필요).
  UI 정지 버튼은 "즉시 정지". "실제 로봇 아님" 같은 상시 표지는 넣지 않는다.
- 접근성: 버튼은 `<button>`, 탭은 role="tab"/"tablist", 표는 `<table>` 또는 role="table", 필터 묶음은 role="group" + aria-label, 검색은 `<input type="search">`.
- 숫자 임계값은 서버 정책(`/v1/config` policies)에서 가져오고, 화면 고유 상수가 꼭 필요하면 이름 있는 상수 + 근거 주석.
- 각 단계 끝에 `npm run lint` 통과, `npx vite build --outDir "$TMP/build-<이름>"` 통과. Playwright는 돌리지 말 것(포트 충돌) — Opus가 돌린다.

## 서버에서 받을 수 있는 값 (실제 응답: `e2e/fixtures/*.json`)
| 경로 | 쓰는 곳 |
|---|---|
| `GET /health` | status, db.available/detail, robot.configured/robot_id/kind/is_simulated, features.planning/stt.available·detail, executions.simulated/real/unknown, running_execution_id, now |
| `GET /v1/config` | robot(profile·workcell·supported_skills·payload·work_radius), declared_robots, policies(safety·freshness·planning·stop·stt), features, session |
| `GET /v1/robots` | stop_diagnostics(available, tracked_goal_count, stop_latch_active, stop_latch_execution_id, is_simulated), robots{id: profile}, declared[](display_name, composite_profile_version, verified, complete, motion_ready, blocking_items, supported_skills, excluded_skills, arm/gripper/mounting) |
| `GET /v1/sim-demo` | running_job, recent_jobs[](job_id, action, action_label, material, slot_label, status, exit_code, started_at, result_status), materials[](model, korean, support_korean, slot_label, actions), recovery_required, pending_confirmation, goal, intent_available |
| `GET /v1/sim-view/state` | stale, joint_age_sec, pose_age_sec, joints(그리퍼 관절 포함) |
| 명령 | 기존 `src/simCommand.js` 그대로(`/v1/sim-demo/command`·`/confirm`·`/jobs/<id>`·`/stop`). source는 서버가 `text`·`stt_final`만 받는다 |

## 0단계 — 구조 분리 + 데이터 층 (먼저, 혼자)
1. `vite.config.js` proxy에 `/health`, `/v1/robots`, `/v1/config` 추가(기존과 같은 옵션).
2. `src/server.js`: `useServer()` 훅. 주기 조회 — `/health`·`/v1/sim-demo`·`/v1/sim-view/state` 2초, `/v1/robots`·`/v1/config` 10초.
   반환 `{ health, config, robots, simDemo, simState, conn, alerts, robotStatus, refreshedAt }`.
   - `conn`: `{ status: 'ok'|'stale'|'down'|'connecting', lastReceivedAt, latencyMs(/health 왕복), ageSec }`.
     stale 기준 = `config.policies.freshness.environment_max_age_sec`(없으면 판단 불가 → 'connecting' 유지, 기본값을 지어내지 않는다).
     요청 실패가 이어지고 마지막 수신이 그 기준보다 오래되면 'down'.
   - `robotStatus`(로봇 1대 = /health robot_id): 상태매트릭스 §1 5단계 — `NO_DATA`(conn이 ok가 아니거나 simState 없음·stale),
     `CRITICAL`(stop_latch_active), `WARNING`(simDemo.recovery_required),
     `NOTICE`(simDemo.running_job 있음), 그 밖 `NORMAL`. `{ level, label, reasons[] }`.
   - `alerts`: 서버 상태에서만 만든다. `{ id, level: 'EMERGENCY'|'WARNING'|'TASK', robot, text, since }`.
     EMERGENCY: 정지 래치 활성. WARNING: db 사용 불가, planning/stt 사용 불가, 3D 관측 stale, recovery_required, 연결 끊김.
     TASK: 실행 중 작업(명령 패널 안에서만 쓰임). 상태가 사라지면 알림도 사라진다(생명주기 §8은 화면 상태로: 발생 → 확인 클릭 → 확인됨).
3. `App.jsx` 분리(동작 그대로 옮기기만): `src/components/{Sidebar,Header,CommandPanel,SimOverlay,DemoBar}.jsx`,
   `src/pages/{Home,Robots,History,Diagnostics,Settings}.jsx`. App은 `useServer()`를 한 번 불러 각 페이지·컴포넌트에 `server` prop으로 내린다.
4. `simCommand.js`: 이 탭에서 보낸 명령 기록 `log`(최근 50개, 메모리) 추가 — 항목마다
   `{ id, sentAt, text, robot, events:[{at, kind:'decision'|'confirm'|'cancel'|'job'|'result'|'stop'|'error', label, detail}] }`. 반환에 `log` 추가.
5. `e2e/mock.js`: `/health`·`/v1/config`·`/v1/robots`·`/v1/sim-demo`(GET)·`/v1/sim-view/state`를 fixtures로 응답.
   `mockBackend(page, { …, overrides: { health, robots, simDemo, simState } })`로 일부를 바꿀 수 있게(얕은 병합 함수 제공).
   기존 명령 응답 동작은 그대로.

## 1단계 — 병렬(파일이 겹치지 않게 나눔)

### A. 틀(Sidebar·Header·App 레이아웃·반응형) — 파일: `components/Sidebar.jsx`, `components/Header.jsx`, `App.jsx`, `styles.css`
- 사이드바 하단 **System status 위젯**(가-4·§15): 4행 ROS 2 / PLANNER / SAFETY PLC / LATENCY(진단 화면에서 행을 숨길 수 있다) + 헤더에 "N/M 정상"(M = 보이는 행 수, 숨긴 행이 있으면 위젯에 "숨김 K" 표시, 전부 숨기면 "상태 위젯 숨김").
  ROS 2 = robots.stop_diagnostics.available && health.robot.configured, PLANNER = features.planning.available,
  SAFETY PLC = 서버에 값 없음 → "수신 없음"(정상으로 세지 않음), LATENCY = conn.latencyMs(정상 기준은 stop.cancel_ack_timeout_sec*1000 미만).
  각 행 클릭 → `#/diagnostics?service=<id>`. 상태는 색+아이콘+텍스트(정상/경고/장애/수신 없음).
- 메뉴 "로봇 관리" 옆 배지 **"가동 N/전체"**(⑥·§11): 전체 = robots.robots 개수, 가동 = robotStatus가 NO_DATA·CRITICAL 아닌 수.
- 헤더: **연결 상태·데이터 수신 시각**(㉔·§7) — ok면 "수신 HH:MM:SS"(옅게), stale/down이면 "연결 끊김 · N초 전 데이터" 경고.
  **운전 모드·제어권**(㉕·§9) — "운전 모드: 시뮬레이션"(health.robot.is_simulated), "제어권: 정보 없음"(서버에 lease 없음).
- **긴급 배너**(⑤·§5·6): alerts 중 EMERGENCY가 있으면 헤더 아래 전체 폭 `role="alert"`, "긴급 N건 · 대표 1건".
  닫기 없음, "인지 확인" 누르면 축소(완전 제거 아님). 정지 버튼을 가리지 않는다(헤더 아래).
- **명령 패널 폭 조절**(가-3): 본문과 명령 패널 사이 `role="separator" aria-label="명령 패널 폭 조절" aria-orientation="vertical"` 드래그(+키보드 ←/→).
  최소 320px, 최대 560px, 기본 400px(근거: 피그마 400). 최소폭에서도 입력칸·보내기 보임.
- **반응형**(반응형 결정 2026-09-30): `min-width:1280px` 제거. 1024~1279: 사이드바 64px 아이콘 레일(글자 숨김, aria-label 유지),
  명령 패널은 오른쪽 서랍(헤더의 "명령 패널" 버튼으로 열고 닫음). 768~1023: 같고 로봇 카드 1열. 표는 가로 스크롤 없이 핵심 열(ID·내용·상태)만,
  나머지는 행 펼침. 즉시 정지는 모든 폭에서 헤더에 보인다. DEMO 바는 본문 흐름 안(겹치지 않게).

### B. 명령 패널 — 파일: `components/CommandPanel.jsx`, `simCommand.js`, `components/command.css`(새로, App에서 import)
- **3단계 체크리스트**(⑥-1·§11): `동작 준비 → 안전 규칙 확인 → 가상 동작 확인`을 한 줄로. 보내는 중 = 1단계 진행,
  판정 도착(CONFIRM) = 1·2 완료, 승인 후 작업 실행 중 = 3단계 진행, 작업 종료 = 3단계 완료. ASK/BLOCK이면 체크리스트 대신 질문/차단 카드.
  완료 체크, 현재 진행 표시, 미시작 중립색. 퍼센트 바는 보조로만.
- **대상 로봇 Chip**(⑦·§10): robots.robots에서 Chip 나열(이름은 역할 우선: supported_skills에 pick/place 있으면 "이송 로봇", 모델명은 title로 보조).
  로봇이 1대면 기본 선택. 최종점검 카드에 "대상: <로봇 이름>"을 다시 적는다.
- **스킬 버튼(No-Code fallback)**(SFR-012): 입력칸 위 Chip 버튼 — 서버 `simDemo.materials`의 `actions`가 true인 것만
  ("A자재 → 컨베이어"(transfer), "A자재 원래 자리로"(return)) + "즉시 정지". 누르면 해당 문장을 **보낸다**(source:'text').
  서버가 'button' 입력을 따로 받지 않으므로 기록 구분은 안 됨 — 코드 주석에 남긴다.
- **연결 끊김이면 보내기·실행 승인 잠금**(㉔·§7): server.conn.status가 'ok'가 아니면 disabled + 이유 문구.

### C. 현황(홈)·로봇 관리 — 파일: `pages/Home.jsx`, `pages/Robots.jsx`, `pages/home.css`, `pages/robots.css`
- 홈 지표 3칸(서버 값): 시뮬레이션 실행 누적(health.executions.simulated), 확인 안 된 실행(executions.unknown), 계획 모델 상태(features.planning).
- **로봇 카드**(①·§1·§2·§3): 서버 로봇 1대. 제목 = 역할 별칭("이송 로봇"), 보조 = 작업 셀 id·모델(display_name)은 작게.
  상태 배지 = robotStatus 색+모양(`data-shape`: ● NORMAL, ◆ NOTICE, ▲ WARNING, ■ CRITICAL, ? NO_DATA). 현재 스킬/작업(simDemo.running_job.action_label), 진행률, 그리퍼 상태(simState.joints의 그리퍼 관절값 → "그리퍼 관절 0.12 rad" 원시값은 정상일 때 숨김: 정상이면 "정상"만).
  **안전 3종**: 서버에 비상정지·보호정지·가드 신호가 없다 → 카드에 "안전 신호(비상정지·보호정지·가드): 확인 안 됨" 한 줄(정상 배지 아님).
  연속값(온도·부하): 서버에 없음 → 표시하지 않음.
- 알림 목록: server.alerts(EMERGENCY·WARNING)만. 없으면 "현재 알림 없음".
- 최근 작업 표: simDemo.recent_jobs(시각·자재·작업·결과). 결과 문구는 사람 말(RESULT_LABELS·action_label), 내부값 노출 금지.
- **로봇 관리**(⑪~⑬·§12·13): 왼쪽 목록(`<table aria-label="로봇 목록">`: 이름·모델·셀·현재 도구·운용 여부·관리자 중지) + 행 선택 시 오른쪽 상세 패널.
  상세 상단 고정: 기본정보·현재 도구(config.robot.has_gripper → "그리퍼 장착", 이름은 서버 값이 있을 때만)·현재 셀·지원 스킬·**운용 가능 여부**(결과값: health.robot.configured && !stop_latch_active && conn ok && 3D 관측 신선 → "운용 가능", 아니면 "운용 불가 — <사유>"). 토글 없음.
  **주의: `robots.declared[]`는 실기(실물) 준비용 프로필이다 — 지금 시뮬레이션 로봇이 아니다. blocking_items·verified·complete 같은 실기 준비도는 화면에 보이지 않는다.** 로봇 정보는 `robots.robots[id]`(시뮬레이션 프로필)와 `/v1/config` robot에서 가져온다.
  탭 3개(`role="tablist"`): **개요 / 도구 장착 이력 / 프로파일 버전**. 도구 이력: 서버에 없음 → "기록 없음". 프로파일 버전: `robots.robots[id].profile_version`(시뮬레이션 프로필)과 config.robot.profile_id/version(날짜 역순은 값 없으면 생략).
  관리자 중지("새 작업 차단")는 권한·API가 없어 렌더링하지 않는다.

### D. 기록·진단·설정 — 파일: `pages/History.jsx`, `pages/Diagnostics.jsx`, `pages/Settings.jsx`, `pages/records.css`
- **기록**(⑭~⑯·§14): 데이터 = simDemo.recent_jobs(서버) + cmd.log(이 탭). 목록 `<table>` 열: 요청 시각·로봇·명령 요약·요청자("이 화면")·검증 결과·실행 결과.
  필터 두 묶음 `role="group" aria-label="검증 결과"`(전체/안전 확인됨/실행 차단/추가 확인 필요), `aria-label="실행 결과"`(전체/성공/실패/STOP),
  검증=실행 차단이면 실행 결과 필터 비활성. 조회 범위(로봇·날짜) 묶음. `<input type="search">` 원문·ID 부분일치. 요약 "안전 확인됨 · STOP 조건 적용 중".
  행 클릭 → 상세 **세로 타임라인**: 계획 → 시뮬레이션(안전 확인) → 허가(승인/취소) → 실행 → 정지(있을 때만 붉은 마름모 노드, 없으면 "정지 요청 없음").
  서버 기록(recent_jobs)은 명령 원문이 없으므로 "명령 원문: 서버 기록 없음", 실행 결과만.
- **진단**(⑰~⑲·§15): 서비스 목록(문제 있는 것 위로): ROS 2/로봇, PLANNER(계획), STT, DB, 3D 관측, 장면 카메라 — 상태(정상/경고/장애/수신 없음)·최근 점검 시각·마지막 정상 시각·지속 시간·최근 오류. 값은 server(useServer) 수집분.
  실패 이력: 이 화면이 열린 뒤 관측한 상태 변화 이벤트 목록(메모리). 원시 샘플: 문제가 있을 때만 "문제 전후 원시 샘플 보기" 버튼(자동으로 펼치지 않음).
  **진단자료 내보내기**(⑲-5): 오른쪽 위 "진단자료 내보내기" 버튼 → 작업 행(대기→생성 중→완료/실패) → 완료되면 JSON 다운로드 버튼(브라우저 Blob; 서버 API 아님 — 주석).
- **설정**(⑳~㉒·§16·17): 왼쪽 서브메뉴 3구역 **개인 / 운영 / 안전·권한**.
  개인: 저장 루틴(localStorage — try/catch, 이 브라우저만) 목록 + 상세, "새 루틴", 즐겨찾기 필터, 삭제 확인.
  운영: "사용자 계정은 인증 도입 뒤 제공" 빈 상태(인증 보류 결정). 
  안전·권한: 적용 중 정책 조회 전용(config.policies 각 항목·policy_version) + "조회 전용 · 변경은 안전관리자 권한 필요" + 변경 상태머신 범례(작성 중 → 검사 중 → 승인 필요 → 적용 예약 → 사용 중; 현재 "사용 중"). 편집 버튼은 렌더링하지 않는다.
