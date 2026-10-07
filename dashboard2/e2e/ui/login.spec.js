// 로그인(구글만) — 피그마 Light · Screens "로그인 /" 프레임, 결정 2026-10-06 Q1~Q10.
// 구글 GIS 스크립트는 가짜로 대체한다(window.__gsi로 팝업 결과를 고른다). 서버 약속은 src/auth.js 맨 위 주석.
import { expect, test } from '@playwright/test';
import { QA_USER, SAMPLES, mockBackend, startJob } from '../mock.js';

const GSI_STUB = `window.google = { accounts: { oauth2: { initCodeClient: (cfg) => ({ requestCode: () => {
  const m = window.__gsi || { code: 'qa-code' };
  if (m.hold) return;
  setTimeout(() => (m.error ? cfg.error_callback({ type: m.error }) : m.denied ? cfg.callback({ error: 'access_denied' }) : cfg.callback({ code: m.code })), 0);
} }) } } };`;

async function gsi(page, behavior = { code: 'qa-code' }) {
  await page.addInitScript((b) => { window.__gsi = b; }, behavior);
  await page.route('https://accounts.google.com/gsi/client', (route) => route.fulfill({ contentType: 'text/javascript', body: GSI_STUB }));
}

const card = (page) => page.locator('main.login-card');
// 로그인 뒤의 진짜 대시보드. 로그인 화면 뒤에도 미리보기(.app.preview)가 그려지므로 그것과 구분한다.
const dash = (page) => page.locator('.app:not(.preview) section.command');
const googleBtn = (page) => card(page).getByRole('button', { name: /Google|다시 시도|다른 계정/ });

test.describe('로그인', () => {
  test('[UI-LOGIN-01] 로그인 전: 로그인 카드만 보이고 대시보드·서버 값은 부르지 않는다', async ({ page }) => {
    const asked = [];
    page.on('request', (r) => { const p = new URL(r.url()).pathname; if (p === '/health' || p.startsWith('/v1/sim-demo')) asked.push(p); });
    await mockBackend(page, { overrides: { auth: false } });
    await page.goto('/');
    await expect(card(page).getByRole('heading', { name: '로그인' })).toBeVisible();
    await expect(card(page)).toContainText('등록된 Google 계정으로 로그인하세요');
    await expect(googleBtn(page)).toHaveText('Google로 로그인');
    await expect(card(page)).toContainText('계정이 없나요? 관리자에게 등록을 요청하세요.');
    await expect(dash(page)).toHaveCount(0);
    expect(asked).toEqual([]);
    // 뒤 배경은 실제 대시보드(미리보기) — 원래 크기로 그려지고, 키보드·화면 읽기에서 빠진다
    const backdrop = page.locator('.login-backdrop');
    await expect(backdrop.locator('.app.preview header')).toBeVisible();
    await expect(backdrop).toHaveAttribute('inert', '');
    await expect(backdrop).toHaveAttribute('aria-hidden', 'true');
  });

  test('[UI-LOGIN-02] 구글 팝업 → 인가 코드를 서버로 → 등록 계정이면 대시보드, 사이드바에 서버가 준 계정', async ({ page }) => {
    await mockBackend(page, { overrides: { auth: false } });
    await gsi(page);
    let body = null;
    await page.route('**/v1/auth/google', async (route) => {
      body = route.request().postDataJSON();
      // 서버가 쿠키를 줬다면 이제 /v1/auth/me도 로그인으로 답한다 — 화면은 교환 뒤 이것을 다시 확인한다.
      await page.route('**/v1/auth/me', (r) => r.fulfill({ json: { user: QA_USER } }));
      return route.fulfill({ json: { user: QA_USER } });
    });
    await page.goto('/#/history');
    await googleBtn(page).click();
    await expect(dash(page)).toBeVisible();
    expect(body).toEqual({ code: 'qa-code' });
    await expect(page.locator('.nav-item.active')).toHaveAttribute('href', '#/history'); // 원래 가려던 화면(Q7)
    await expect(page.locator('.profile-btn')).toContainText(QA_USER.email);
  });

  for (const [id, setup, text, button] of [
    ['03', { status: 403, json: { reason_code: 'session.account_not_registered', email: 'nobody@example.com' } }, '이 계정(nobody@example.com)은 사용 권한이 없습니다. 관리자에게 등록을 요청하세요.', '다른 계정으로 로그인'],
    ['04', { status: 403, json: { reason_code: 'session.account_disabled' } }, '사용이 중지된 계정입니다. 관리자에게 문의하세요.', '다른 계정으로 로그인'],
    ['05', { status: 500, json: {} }, '로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.', '다시 시도'],
  ]) {
    test(`[UI-LOGIN-${id}] 서버 거절(${setup.status}${setup.json.reason_code ? ` ${setup.json.reason_code}` : ''}) — 피그마 문구 + 버튼`, async ({ page }) => {
      await mockBackend(page, { overrides: { auth: false } });
      await gsi(page);
      await page.route('**/v1/auth/google', (route) => route.fulfill(setup));
      await page.goto('/');
      await googleBtn(page).click();
      await expect(card(page).getByRole('alert')).toHaveText(text);
      await expect(googleBtn(page)).toHaveText(button);
      await expect(dash(page)).toHaveCount(0);
    });
  }

  test('[UI-LOGIN-06] 구글 창을 닫으면 "취소", 팝업이 막히면 "다시 시도"(흰 버튼)', async ({ page }) => {
    await mockBackend(page, { overrides: { auth: false } });
    await gsi(page, { error: 'popup_closed' });
    await page.goto('/');
    await googleBtn(page).click();
    await expect(card(page).getByRole('status')).toHaveText('로그인이 취소되었습니다.');
    await page.evaluate(() => { window.__gsi = { error: 'popup_failed_to_open' }; });
    await googleBtn(page).click();
    await expect(card(page).getByRole('alert')).toHaveText('브라우저가 로그인 창을 막았습니다. 팝업을 허용한 뒤 다시 시도하세요.');
    await expect(googleBtn(page)).toHaveText('다시 시도');
    await expect(googleBtn(page)).toHaveClass(/retry/);
  });

  test('[UI-LOGIN-07] 구글 창이 열린 동안: 안내 문구가 바뀌고 버튼은 잠긴 채 로딩 표시', async ({ page }) => {
    await mockBackend(page, { overrides: { auth: false } });
    await gsi(page, { hold: true });
    await page.goto('/');
    await googleBtn(page).click();
    await expect(card(page)).toContainText('열린 Google 창에서 로그인을 완료하세요');
    const btn = card(page).getByRole('button', { name: 'Google 인증 대기 중…' });
    await expect(btn).toBeDisabled();
    await expect(btn.locator('.spinner')).toBeVisible();
  });

  test('[UI-LOGIN-08] 로그인 확인 서버가 없거나 이상하면(404) 통과시키지 않는다 — 설계 원칙 4', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/auth/me', (route) => route.fulfill({ status: 404, body: 'not found' }));
    await page.goto('/');
    await expect(card(page).getByRole('alert')).toHaveText('로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.');
    await expect(dash(page)).toHaveCount(0);
  });

  test('[UI-LOGIN-09] 첫 접속: 서버 답이 오기 전엔 "로그인 상태 확인 중"', async ({ page }) => {
    await mockBackend(page);
    let release;
    const gate = new Promise((r) => { release = r; });
    await page.route('**/v1/auth/me', async (route) => { await gate; return route.fulfill({ json: { user: QA_USER } }); });
    await page.goto('/');
    await expect(page.getByRole('status').filter({ hasText: '로그인 상태 확인 중…' })).toBeVisible();
    release();
    await expect(dash(page)).toBeVisible();
  });

  test('[UI-LOGIN-10] 사용 중 세션 만료(401): 화면은 그대로, 다시 로그인 창 — 헤더 즉시 정지는 덮이지 않는다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(dash(page)).toBeVisible();
    await page.route((url) => url.pathname === '/v1/sim-demo', (route) => route.fulfill({ status: 401, json: { reason_code: 'session.expired' } }));
    const dialog = page.getByRole('alertdialog', { name: '로그인 시간이 지났습니다' });
    await expect(dialog).toBeVisible({ timeout: 10_000 });
    await expect(dialog).toContainText('즉시 정지는 다시 로그인하지 않아도 누를 수 있습니다');
    const estop = page.locator('header .estop');
    const hit = await estop.evaluate((el) => { const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); });
    expect(hit).toBe(true);
    await expect(dash(page)).toBeAttached(); // 대시보드는 언마운트되지 않는다
  });

  test('[UI-LOGIN-11] 작업이 없으면 프로필 메뉴 → 로그아웃 바로, 로그인 화면으로', async ({ page }) => {
    await mockBackend(page);
    let loggedOut = false;
    await page.route('**/v1/auth/logout', (route) => { loggedOut = true; return route.fulfill({ json: { ok: true } }); });
    await page.goto('/');
    await page.locator('.profile-btn').click();
    await page.getByRole('menuitem', { name: '로그아웃' }).click();
    await expect(card(page)).toBeVisible();
    expect(loggedOut).toBe(true);
  });

  test('[UI-LOGIN-12] 로봇 작업 중이면 로그아웃 확인 — 취소하면 그대로, 로그아웃해도 정지 요청은 안 보낸다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job, SAMPLES.run.job, SAMPLES.run.job, SAMPLES.run.job, SAMPLES.run.job, SAMPLES.run.job] });
    await page.goto('/');
    await startJob(page);
    await page.getByRole('dialog').getByRole('button', { name: '창 닫기' }).click();
    await page.locator('.profile-btn').click();
    await page.getByRole('menuitem', { name: '로그아웃' }).click();
    const confirm = page.getByRole('alertdialog', { name: '로그아웃할까요?' });
    await expect(confirm).toContainText('로그아웃해도 로봇 작업은 멈추지 않습니다');
    await confirm.getByRole('button', { name: '취소' }).click();
    await expect(confirm).toHaveCount(0);
    await page.locator('.profile-btn').click();
    await page.getByRole('menuitem', { name: '로그아웃' }).click();
    await page.getByRole('alertdialog').getByRole('button', { name: '로그아웃' }).click();
    await expect(card(page)).toBeVisible();
    expect(calls.some((c) => c.path.endsWith('/stop'))).toBe(false);
  });

  test('[UI-LOGIN-14] 교환은 됐는데 /v1/auth/me가 로그인이 아니면(쿠키 안 붙음) 들여보내지 않는다', async ({ page }) => {
    await mockBackend(page, { overrides: { auth: false } });
    await gsi(page);
    await page.route('**/v1/auth/google', (route) => route.fulfill({ json: { user: QA_USER } }));
    await page.goto('/');
    await googleBtn(page).click();
    await expect(card(page).getByRole('alert')).toHaveText('로그인 서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.');
    await expect(dash(page)).toHaveCount(0);
  });

  test('[UI-LOGIN-15] 서버 로그아웃이 실패하면 로그인 상태를 유지하고 알린다', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/auth/logout', (route) => route.fulfill({ status: 500, json: {} }));
    await page.goto('/');
    await page.locator('.profile-btn').click();
    await page.getByRole('menuitem', { name: '로그아웃' }).click();
    await expect(page.locator('.profile').getByRole('alert')).toHaveText('로그아웃을 서버에서 확인하지 못했습니다. 다시 시도하세요.');
    await expect(dash(page)).toBeVisible();
  });

  test('[UI-LOGIN-16] 서버에 진행 중 작업이 있으면(다른 탭이 시작했어도) 로그아웃 확인', async ({ page }) => {
    await mockBackend(page, { overrides: { simDemo: { running_job: { job_id: 'other-tab', status: 'running' } } } });
    await page.goto('/');
    await page.locator('.profile-btn').click();
    await page.getByRole('menuitem', { name: '로그아웃' }).click();
    await expect(page.getByRole('alertdialog', { name: '로그아웃할까요?' })).toBeVisible();
  });

  test('[UI-LOGIN-13] 820·390 폭: 카드가 화면 안에 있고 가로 스크롤이 없다', async ({ page }) => {
    await mockBackend(page, { overrides: { auth: false } });
    for (const width of [820, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.goto('/');
      const box = await card(page).boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(width);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    }
  });
});
