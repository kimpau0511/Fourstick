// 시뮬레이션 창·개발 중 결정·반응형 — 추적표 5장(DEV-01~12), 화면설계서 ⑧
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.beforeEach(async ({ page }) => { await mockBackend(page); });

async function openSim(page, state = '안전 검사 중') {
  await page.goto('/');
  await page.locator('.demo-bar').getByRole('button', { name: state }).click();
  await expect(page.locator('.sim-card')).toBeVisible();
}

test('[UI-SIM-02][⑧·§4] 영상(3D·카메라)을 못 받으면 빈 그림 대신 이유를 적는다', async ({ page }) => {
  await openSim(page);
  await expect(page.locator('.sim-card')).toContainText(/쓸 수 없음|연결하는 중|끊김/);
});

test('[UI-SIM-03][DEV-01] 3D 모델을 못 받으면 Gazebo 영상 경로로 대신한다', async ({ page }) => {
  await openSim(page);
  await expect(page.locator('.sim-head > div small')).toContainText('Gazebo');
});

test('[UI-SIM-01][⑧ D3] 시뮬레이션 창에 "SIMULATION — 실시간 아님" 표기', async ({ page }) => {
  await openSim(page);
  await expect(page.locator('.sim-card')).toContainText(/SIMULATION|실시간 아님/);
});

test('[UI-DEV-01][DEV-02] 라이트 테마: 페이지 바탕 #e4e9f0, 카드 #e8edf3', async ({ page }) => {
  await page.goto('/');
  expect(await page.evaluate(() => getComputedStyle(document.body).backgroundColor)).toBe('rgb(228, 233, 240)');
  expect(await page.locator('.card').first().evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(232, 237, 243)');
});

test('[UI-DEV-02][DEV-03] "실제 로봇 아님" 상시 표지가 없다', async ({ page }) => {
  for (const r of ['home', 'robots', 'history', 'diagnostics', 'settings']) {
    await page.goto(`/#/${r}`);
    await expect(page.locator('body'), r).not.toContainText('실제 로봇 아님');
  }
});

test('[UI-DEV-03][DEV-05] 위험 판정 창 영상 칸에 문구 없음 · 설정 정책 문장에 "(안)" 없음', async ({ page }) => {
  await openSim(page, '위험 판정');
  await expect(page.locator('.sim-video')).not.toContainText('BLOCKED');
  await page.locator('.demo-bar').getByRole('button', { name: '기본' }).click();
  await page.goto('/#/settings');
  await expect(page.locator('.policy')).not.toContainText('(안)');
});

test('[UI-DEV-04][DEV-09 D2] 화면 값은 서버에서 온다 — 목업 데이터 모듈을 쓰지 않는다', async () => {
  const app = readFileSync(new URL('../../src/App.jsx', import.meta.url), 'utf-8');
  expect(app).not.toMatch(/from '\.\/data\.js'/);
});

test('[UI-DEV-05][DEV-12] 개발 서버는 localhost에만 열린다(인증 없음)', async () => {
  const cfg = readFileSync(new URL('../../vite.config.js', import.meta.url), 'utf-8');
  expect(cfg).toMatch(/host:\s*'localhost'/);
});

for (const width of [1024, 820]) {
  test(`[UI-RESP-01][DEV-07] 폭 ${width}px에서 가로 스크롤 없음, 비상 정지 보임`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/');
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await expect(page.locator('header').getByRole('button', { name: /정지/ })).toBeInViewport();
  });
}

test('[UI-RESP-02][DEV-08] 태블릿(1024px): 사이드바는 아이콘 레일(≤80px)', async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.goto('/');
  const box = await page.locator('aside.sidebar').boundingBox();
  expect(box.width).toBeLessThanOrEqual(80);
});
