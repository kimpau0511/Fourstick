// QA 자동 테스트(요구사항 추적표: 문서/QA_요구사항_추적표.md).
//   npm run qa        화면(ui) + 서버(api) 전체
//   npm run qa:ui     화면만 — 서버 응답을 가짜로 대체해 서버 없이 결정적으로 돈다
//   npm run qa:api    작업 셀 서버 직접 검사(읽기 + 로봇이 움직이지 않는 명령만). 서버가 꺼져 있으면 건너뛴다
// 테스트 제목 앞의 [ID]가 추적표의 테스트 ID다. 결과 요약: npm run qa:report
import { defineConfig } from '@playwright/test';

const PORT = 5181; // 개발 서버(5175)와 겹치지 않게 따로 띄운다

export default defineConfig({
  testDir: './e2e',
  outputDir: './e2e-results/artifacts',
  timeout: 30_000,
  fullyParallel: true,
  reporter: [['list'], ['json', { outputFile: 'e2e-results/results.json' }]],
  use: { baseURL: `http://localhost:${PORT}`, viewport: { width: 1440, height: 900 }, locale: 'ko-KR' },
  projects: [
    { name: 'ui', testMatch: /ui\/.*\.spec\.js/ },
    { name: 'api', testMatch: /api\/.*\.spec\.js/ },
  ],
  webServer: {
    command: `npx vite --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
