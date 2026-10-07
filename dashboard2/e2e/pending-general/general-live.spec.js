import { expect, test } from '@playwright/test';

// 명시적으로 요청한 연동 확인에서만 실행한다. 실행/STOP 요청은 브라우저에서 차단한다.
test('실제 PC1 API로 계획 생성과 거절만 확인', async ({ page, request }) => {
  test.skip(process.env.GENERAL_LIVE_CHECK !== '1', '실제 API 연동 확인을 명시한 경우에만 실행');
  const forbidden = [];
  await page.route(/\/v1\/(execute|stop)(\/|\?|$)/, (route) => { forbidden.push(route.request().url()); return route.abort(); });
  const cell = await (await request.get('/v1/sim-demo')).json();
  const material = cell.materials.find((m) => m.actions?.transfer || m.actions?.return);
  expect(material).toBeTruthy();
  const text = material.actions.transfer ? `${material.korean}를 컨베이어로 옮겨줘` : `${material.korean}를 원래 자리로 돌려놔`;
  await page.goto('/');
  const planResponse = page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/plan');
  await page.getByLabel('자연어 명령').fill(text);
  await page.getByRole('button', { name: '보내기', exact: true }).click();
  const plan = await (await planResponse).json();
  expect(plan.ok).toBe(true);
  await expect(page.getByRole('list', { name: '작업 계획' })).toBeVisible();
  await page.screenshot({ path: '/tmp/forstick2-general-dashboard.png', fullPage: true });
  const rejection = page.waitForResponse((r) => new URL(r.url()).pathname === '/v1/decision');
  await page.locator('section.command').getByRole('button', { name: '취소', exact: true }).click();
  expect((await (await rejection).json()).ok).toBe(true);
  await expect(page.locator('section.command')).toContainText('계획을 취소했습니다');
  expect(forbidden).toEqual([]);
});
