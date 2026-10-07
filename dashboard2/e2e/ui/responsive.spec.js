// 반응형(2026-09-30 결정) 회귀 검사: 화면마다 카드 밖으로 넘치는 요소와 가로 스크롤이 없어야 한다.
import { expect, test } from '@playwright/test';
import { mockBackend, SAMPLES } from '../mock.js';
test('[UI-RESP-05][DEV-07] 폭 820·1280·1440에서 어떤 화면도 카드 밖으로 넘치거나 가로 스크롤이 생기지 않는다', async ({ page }) => {
  test.setTimeout(90000);
  await mockBackend(page, { plan: SAMPLES.plan(60) });
  const out = [];
  for (const w of [820, 1280, 1440]) {
    await page.setViewportSize({ width: w, height: 900 });
    for (const p of ['home', 'robots', 'history', 'diagnostics', 'settings']) {
      await page.goto('/#/' + p); await page.waitForTimeout(1300);
      const r = await page.evaluate(() => {
        const bad = new Set();
        const docOver = document.documentElement.scrollWidth - window.innerWidth;
        for (const el of document.querySelectorAll('.app *')) {
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed' || el.closest('.col-side:not(.open)')) continue;
          if (cs.clipPath && cs.clipPath !== 'none') continue;
          const box = el.closest('.card, .sidebar, .header, .command');
          if (!box || box === el) continue;
          const a = el.getBoundingClientRect(), b = box.getBoundingClientRect();
          if (a.width === 0) continue;
          let clipped = false; for (let x = el.parentElement; x && x !== box; x = x.parentElement) { const o = getComputedStyle(x).overflowX; if (o !== 'visible') { clipped = true; break; } }
          if (clipped) continue;
          const over = Math.max(a.right - b.right, b.left - a.left);
          if (over > 1) bad.add(`${el.tagName.toLowerCase()}.${[...el.classList].join('.')}「${(el.textContent || '').trim().slice(0, 16)}」 +${Math.round(over)}px`);
        }
        return { docOver, bad: [...bad].slice(0, 4) };
      });
      if (r.docOver > 0 || r.bad.length) out.push(`W${w} ${p}: hscroll=${r.docOver} ${r.bad.join(' | ')}`);
    }
  }
  expect(out).toEqual([]);
});
