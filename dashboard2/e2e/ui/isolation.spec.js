// 시험 격리(2026-10-08): 가짜 응답이 없는 API 요청은 브라우저에서 끊기고 기록된다 — 운영 서버로 나가지 않는다.
import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { LEAK_LOG, mockBackend } from '../mock.js';

test('[UI-ISO-01] 가짜 응답 없는 /v1 요청은 끊기고 기록된다', async ({ page }) => {
  await mockBackend(page);
  await page.goto('/');
  const outcome = await page.evaluate(() => fetch('/v1/__isolation_probe__').then(() => 'reached', () => 'blocked'));
  expect(outcome).toBe('blocked');
  expect(readFileSync(LEAK_LOG, 'utf8')).toContain('__isolation_probe__');
});
