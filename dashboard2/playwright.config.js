// QA 자동 테스트(요구사항 추적표: 문서/QA_요구사항_추적표.md).
//   npm run qa        화면(ui) + 서버(api) 전체
//   npm run qa:ui     화면만 — 서버 응답을 가짜로 대체해 서버 없이 결정적으로 돈다
//   npm run qa:api    작업 셀 서버 직접 검사(읽기 + 로봇이 움직이지 않는 명령만). 서버가 꺼져 있으면 건너뛴다
// 테스트 제목 앞의 [ID]가 추적표의 테스트 ID다. 결과 요약: npm run qa:report
import { defineConfig } from '@playwright/test';

// 개발 서버(5175)와 겹치지 않게 따로 띄운다. 여러 작업을 동시에 돌릴 땐 QA_PORT로 바꾼다.
const PORT = Number(process.env.QA_PORT || 5181);
const GENERAL_PORT = PORT + 1; // 일반 경로 화면(VITE_COMMAND_MODE=general) 검사용

export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e-results/artifacts',
  timeout: 30_000,
  fullyParallel: true,
  reporter: [['list'], ['json', { outputFile: 'e2e-results/results.json' }]],
  use: { baseURL: `http://localhost:${PORT}`, viewport: { width: 1440, height: 900 }, locale: 'ko-KR' },
  projects: [
    { name: 'ui', testMatch: /ui\/.*\.spec\.js/ },
    { name: 'ui-general', testMatch: /ui-general\/.*\.spec\.js/, use: { baseURL: `http://localhost:${GENERAL_PORT}` } },
    { name: 'api', testMatch: /api\/.*\.spec\.js/ },
  ],
  // 명령 경로를 환경변수로 고정한다 — 이 PC의 .env.local(VITE_COMMAND_MODE=general)이 테스트를 바꾸지 않게(이미 있는 환경변수가 .env보다 우선).
  webServer: [
    { command: `npx vite --port ${PORT} --strictPort`, url: `http://localhost:${PORT}`, reuseExistingServer: false, timeout: 60_000,
      env: { VITE_COMMAND_MODE: 'sim' } },
    { command: `npx vite --port ${GENERAL_PORT} --strictPort`, url: `http://localhost:${GENERAL_PORT}`, reuseExistingServer: false, timeout: 60_000,
      env: { VITE_COMMAND_MODE: 'general' } },
  ],
});
