// PWA(설치 가능 + 화면 껍데기 캐시) — public/manifest.webmanifest, public/sw.js, src/main.jsx, index.html.
// 서비스 워커 등록은 main.jsx가 build 결과(import.meta.env.PROD)에서만 한다. 테스트는 vite dev 서버에서 도는데,
// 그래서 "dev에서는 등록하지 않는다"는 약속은 그대로 검사하고, sw.js 동작(캐시 규칙)은 같은 파일을 페이지에서
// 직접 register해 진짜 브라우저 서비스 워커로 검사한다(sw.js를 흉내 내지 않는다).
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png', '/icon-512.png', '/icon-maskable-512.png']; // sw.js PRECACHE
const CACHE = 'forstick-shell-v1'; // sw.js CACHE

/** /sw.js를 등록하고, 이 페이지를 제어할 때까지 기다린다. */
async function installSw(page) {
  await page.evaluate(async () => {
    await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    if (!navigator.serviceWorker.controller) await new Promise((r) => navigator.serviceWorker.addEventListener('controllerchange', r, { once: true }));
  });
}

/** 지정한 캐시에 든 경로 목록(캐시가 없으면 빈 배열). */
const cachedPaths = (page, name = CACHE) => page.evaluate(async (n) => {
  if (!(await caches.has(n))) return [];
  return (await (await caches.open(n)).keys()).map((r) => new URL(r.url).pathname);
}, name);

test.describe('PWA', () => {
  test('[UI-PWA-01] index.html이 매니페스트를 달고, 매니페스트에 설치 필수 값(이름·start_url·display·192/512 아이콘)이 있다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute('href', '/manifest.webmanifest');
    const res = await page.request.get('/manifest.webmanifest');
    expect(res.ok()).toBe(true);
    const m = await res.json();
    expect(m.name).toBeTruthy();
    expect(m.start_url).toBe('/');
    expect(m.display).toBe('standalone');
    const sizes = m.icons.map((i) => i.sizes);
    expect(sizes).toEqual(expect.arrayContaining(['192x192', '512x512']));
    expect(m.icons.some((i) => i.purpose === 'maskable')).toBe(true);
    // 매니페스트가 가리키는 아이콘이 실제로 받아지고, 적힌 크기 그대로의 이미지다
    for (const icon of m.icons) {
      const r = await page.request.get(icon.src);
      expect(r.ok(), icon.src).toBe(true);
      if (icon.sizes !== 'any') {
        const [w, h] = icon.sizes.split('x').map(Number);
        const png = await r.body(); // PNG 헤더의 IHDR: 폭(16~19바이트), 높이(20~23바이트)
        expect([png.readUInt32BE(16), png.readUInt32BE(20)], icon.src).toEqual([w, h]);
      }
    }
  });

  test('[UI-PWA-02] dev 서버에서는 서비스 워커를 등록하지 않는다(HMR에 캐시가 끼지 않게 — main.jsx)', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.waitForLoadState('load');
    await expect(page.locator('.app:not(.preview) section.command')).toBeVisible();
    expect(await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).length)).toBe(0);
  });

  test('[UI-PWA-03] 설치되면 화면 껍데기 6개를 미리 담는다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await installSw(page);
    expect((await cachedPaths(page)).sort()).toEqual([...SHELL].sort());
  });

  test('[UI-PWA-04] 제어 API·상태(/v1/*, /health, 로그인 확인)는 캐시하지 않고 매번 서버 값을 받는다', async ({ page }) => {
    await mockBackend(page);
    // 같은 주소를 부를 때마다 값이 바뀌는 서버 — 캐시에서 나오면 값이 그대로다
    let n = 0;
    await page.route('**/v1/config', (route) => route.fulfill({ json: { seq: ++n } }));
    await page.goto('/');
    await installSw(page);
    const get = (path) => page.evaluate(async (p) => (await fetch(p)).json(), path);
    const first = (await get('/v1/config')).seq; // 화면 자신도 /v1/config를 부르므로 절대값이 아니라 증가를 본다
    const second = (await get('/v1/config')).seq;
    expect(second).toBeGreaterThan(first);
    await get('/health');
    await get('/v1/auth/me');
    await get('/v1/history');
    // 대조군: 같은 origin 정적 파일은 SW를 지나 캐시에 들어간다 — SW가 요청을 보고 있다는 증거
    await page.evaluate(() => fetch('/src/main.jsx').then((r) => r.text()));
    const paths = await cachedPaths(page);
    expect(paths).toContain('/src/main.jsx');
    expect(paths.filter((p) => p.startsWith('/v1/') || p.startsWith('/health'))).toEqual([]);
    // 어느 캐시에도 없다
    const all = await page.evaluate(async () => { const hits = []; for (const k of await caches.keys()) for (const r of await (await caches.open(k)).keys()) hits.push(new URL(r.url).pathname); return hits; });
    expect(all.filter((p) => p.startsWith('/v1/') || p.startsWith('/health'))).toEqual([]);
  });

  test('[UI-PWA-05] 오프라인: 서버에 닿지 않아도 화면 껍데기는 서비스 워커 캐시에서 열린다', async ({ page, context }) => {
    await mockBackend(page);
    await page.goto('/');
    await installSw(page);
    await context.setOffline(true);
    const res = await page.goto('/');
    expect(res.fromServiceWorker()).toBe(true);
    await expect(page.locator('#root')).toBeAttached();
    await expect(page).toHaveTitle('FORSTICK Control Panel');
    await context.setOffline(false);
  });

  test('[UI-PWA-06] 새 버전 활성화 때 이전 버전 캐시(CACHE 이름이 다른 것)를 지운다', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await page.evaluate(async () => { await (await caches.open('forstick-shell-v0')).put('/old', new Response('old')); });
    expect(await cachedPaths(page, 'forstick-shell-v0')).toEqual(['/old']);
    await installSw(page);
    await expect.poll(() => page.evaluate(() => caches.keys())).toEqual([CACHE]);
  });
});
