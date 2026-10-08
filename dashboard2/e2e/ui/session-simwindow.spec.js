// 세션 만료 후 동작 + 시뮬레이션 창 바깥 클릭 닫기 중 기존 시험이 비워 둔 부분.
// 이미 있는 것: 만료 창이 뜨고 즉시 정지가 가려지지 않음(login.spec UI-LOGIN-10), 수동 열기·전체화면·아이콘·바깥 클릭 닫기(simulation-view.spec).
// 서버 약속은 src/auth.js 맨 위 주석. 정지를 로그인 없이 받는 일(server/auth.py is_public)은 백엔드 몫이라 여기선 화면이 정지 요청을 막지 않는지만 본다.
import { expect, test } from '@playwright/test';
import { QA_USER, SAMPLES, mockBackend, startJob } from '../mock.js';

const GSI_STUB = `window.google = { accounts: { oauth2: { initCodeClient: (cfg) => ({ requestCode: () => {
  setTimeout(() => cfg.callback({ code: 'qa-code' }), 0);
} }) } } };`;
const dash = (page) => page.locator('.app:not(.preview) section.command');
const card = (page) => page.locator('main.login-card');
const expiredDialog = (page) => page.getByRole('alertdialog', { name: '로그인 시간이 지났습니다' });
const stubGsi = (page) => page.route('https://accounts.google.com/gsi/client', (route) => route.fulfill({ contentType: 'text/javascript', body: GSI_STUB }));

/** 로그인된 채로 열고, 이후 /v1/sim-demo 상태 조회가 401이 되게 한다. state.expired를 false로 돌리면 다시 정상 응답. */
async function expireSession(page, hash = '', beforeExpire) {
  const calls = await mockBackend(page);
  await page.goto(`/${hash}`);
  await expect(dash(page)).toBeVisible();
  if (beforeExpire) await beforeExpire();
  const state = { expired: true };
  await page.route((url) => url.pathname === '/v1/sim-demo', (route) => (state.expired
    ? route.fulfill({ status: 401, json: { reason_code: 'session.expired' } }) : route.fallback()));
  await expect(expiredDialog(page)).toBeVisible({ timeout: 10_000 });
  return { calls, state };
}

test.describe('세션 만료', () => {
  test('[UI-SESS-01] 만료 창이 떠 있어도 헤더 즉시 정지를 누르면 정지 요청이 서버로 나간다', async ({ page }) => {
    const { calls } = await expireSession(page);
    await page.locator('header .estop').click(); // 덮개가 가리면 Playwright가 클릭하지 못해 실패한다
    await expect.poll(() => calls.some((c) => c.method === 'POST' && c.path === '/v1/sim-demo/stop')).toBe(true);
  });

  test('[UI-SESS-02] 만료 창이 떠 있는 동안 명령 입력칸은 inert라 키보드로도 닿지 않는다', async ({ page }) => {
    await expireSession(page);
    const reachable = await page.getByLabel('자연어 명령').evaluate((el) => !el.closest('[inert]'));
    expect(reachable).toBe(false);
  });

  test('[UI-SESS-03] 로그인 API(/v1/auth/*)의 401은 만료로 보지 않고, 그 밖 경로(/health)의 401은 만료로 본다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(dash(page)).toBeVisible();
    await page.route('**/v1/auth/me', (route) => route.fulfill({ status: 401, json: {} }));
    await page.evaluate(() => fetch('/v1/auth/me').catch(() => {}));
    await page.waitForTimeout(500);
    await expect(expiredDialog(page)).toHaveCount(0);
    await page.route((url) => url.pathname === '/health', (route) => route.fulfill({ status: 401, json: {} }));
    await expect(expiredDialog(page)).toBeVisible({ timeout: 10_000 });
  });

  test('[UI-SESS-04] 만료 창에서 다시 로그인하면 창이 닫히고 보던 화면(#/history)과 입력하던 명령이 그대로다', async ({ page }) => {
    await stubGsi(page);
    const draft = '입력하던 명령';
    const { state } = await expireSession(page, '#/history', () => page.getByLabel('자연어 명령').fill(draft)); // 만료 전에 입력해 둔다
    const dialog = expiredDialog(page);
    await page.route('**/v1/auth/google', (route) => { state.expired = false; return route.fulfill({ json: { user: QA_USER } }); });
    await dialog.getByRole('button', { name: /Google/ }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.locator('.nav-item.active')).toHaveAttribute('href', '#/history');
    const reachable = await page.getByLabel('자연어 명령').evaluate((el) => !el.closest('[inert]'));
    expect(reachable).toBe(true);
    await expect(page.getByLabel('자연어 명령')).toHaveValue(draft); // 입력하던 내용이 사라지지 않는다
  });

  test('[UI-SESS-05] 만료 창에서 등록 안 된 계정(403)으로 다시 로그인하면 창은 남고 거절 문구가 보인다', async ({ page }) => {
    await stubGsi(page);
    await expireSession(page);
    await page.route('**/v1/auth/google', (route) => route.fulfill({ status: 403, json: { reason_code: 'session.account_not_registered', email: 'nobody@example.com' } }));
    const dialog = expiredDialog(page);
    await dialog.getByRole('button', { name: /Google/ }).click();
    await expect(dialog.getByRole('alert')).toContainText('nobody@example.com');
    await expect(dialog).toBeVisible();
  });

  test('[UI-SESS-06] 다른 탭에서 로그아웃하면 이 탭도 로그인 화면으로 간다', async ({ page, context }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(dash(page)).toBeVisible();
    const other = await context.newPage();
    await mockBackend(other);
    await other.goto('/');
    await other.locator('.profile-btn').click();
    await other.locator('.profile').getByRole('button', { name: '로그아웃' }).click();
    await expect(card(other)).toBeVisible();
    await expect(card(page)).toBeVisible();
    await expect(dash(page)).toHaveCount(0);
  });
});

test.describe('시뮬레이션 창', () => {
  test('[UI-SIMWIN-01] 작업 중 바깥을 눌러 닫아도 정지 요청은 없고, 같은 상태에서 다시 뜨지 않으며, 버튼으로 다시 열 수 있다', async ({ page }) => {
    await page.route('**/v1/**', (route) => route.fulfill({ status: 503, json: {} }));
    const calls = await mockBackend(page, { command: SAMPLES.confirm(), jobs: [SAMPLES.run.job] });
    await page.goto('/');
    await startJob(page);
    const dialog = page.getByRole('dialog', { name: '가상 동작 확인 중', exact: true });
    await expect(dialog).toBeVisible();
    const polls = () => calls.filter((c) => c.path.includes('/jobs/')).length;
    const before = polls();
    await page.locator('.scrim').click({ position: { x: 5, y: 5 } }); // 창 바깥(덮개) 모서리
    await expect(dialog).toHaveCount(0);
    await expect.poll(polls, { timeout: 20_000 }).toBeGreaterThanOrEqual(before + 3); // 작업 상태 조회가 3번 더 돈 뒤에도
    await expect(dialog).toHaveCount(0); // 다시 뜨지 않는다
    expect(calls.some((c) => c.path.endsWith('/stop'))).toBe(false);
    await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expect(dialog).toBeVisible();
  });

  test('[UI-SIMWIN-02] 명령 패널의 "시뮬레이션 보기"는 명령 없이 열리고, 열린 창 뒤의 헤더 즉시 정지가 정지 요청을 보낸다', async ({ page }) => {
    await page.route('**/v1/**', (route) => route.fulfill({ status: 503, json: {} }));
    const calls = await mockBackend(page);
    await page.goto('/');
    await page.locator('section.command').getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
    await expect(page.getByRole('dialog', { name: '시뮬레이션 보기', exact: true })).toBeVisible();
    expect(calls.filter((c) => c.path.endsWith('/command'))).toEqual([]);
    await page.locator('header .estop').click();
    await expect.poll(() => calls.some((c) => c.path === '/v1/sim-demo/stop')).toBe(true);
  });
});
