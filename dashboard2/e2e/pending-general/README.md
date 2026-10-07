# 보류한 일반 모드 화면 시험 (2026-10-07 병합)

feature/login-google 병합에서 명령 흐름은 팀원 쪽(`src/planCommand.js`, 기본 시연 모드·`VITE_COMMAND_MODE=general`이면 일반 경로)을 기준으로 했다.
여기 시험들은 그 전의 일반 모드 구현(`src/generalCommand.js`)과 예전 `e2e/mock.js`(plan/decision/execute 모의)에 맞춰 쓴 것이라
지금 화면 흐름에서는 맞지 않는다. 지우지 않고 보관한다 — 테스트 실행 대상(`e2e/ui`, `e2e/ui-general`, `e2e/api`)이 아니다.

- return-button.spec.js — 복귀 버튼(일반 계획 API·승인·취소·NOOP·차단 표시·상태 재조회)
- step-progress.spec.js — 이송 중 진행 단계(step_progress 이벤트) 1→4 표시
- general-command.spec.js · general-live.spec.js — 일반 계획 승인·취소·정지
- fullscreen.spec.js · motion-speed.spec.js — 전체화면·이동 속도 카드 삭제 뒤 정지 연결

다시 쓰려면 planCommand.js 흐름과 지금 mock.js(command/confirm/jobs)에 맞게 옮긴다.
