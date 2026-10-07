// 반복 작업(2026-10-07): 접고 펼침 · 자재 체크 · 횟수 · 미리보기 → 승인 → 시작 · 진행 상태 · 실행 중 잠금 ·
// 현재 회차 후 종료 · 새로고침 뒤 같은 실행 · 시뮬레이션 보기 일시정지/재개/취소. 서버 응답은 가짜다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

const RUN = (state, extra = {}) => ({ run: { run_id: 'rep_1', state, active: ['running', 'pausing', 'paused', 'resuming'].includes(state),
  count: 3, label: state === 'paused' ? '2/3회 · B자재 원래 자리 복귀 일시정지' : '2/3회 · B자재 복귀 중', finish_after_round: false,
  materials: [{ id: 'mat_a', name: 'A자재' }, { id: 'mat_b', name: 'B자재' }], reason: null, ...extra } });

async function setup(page, { run = { run: null }, preview } = {}) {
  await mockBackend(page);
  const calls = [];
  const state = { run };
  const json = (route, body, status = 200) => route.fulfill({ status, json: body });
  await page.route('**/v1/sessions**', (route) => json(route, { session_id: 'qa-session', client_id: 'c1' }));
  await page.route((url) => url.pathname.startsWith('/v1/sim-demo/repeat'), (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (method === 'GET') return json(route, state.run);
    const body = route.request().postDataJSON();
    calls.push({ path, body });
    if (path.endsWith('/preview')) return json(route, preview ?? { ok: true, token: 'tok-1', count: 3, total_steps: 12,
      sequence: ['A자재 컨베이어로 이송', 'A자재 원래 자리 복귀', 'B자재 컨베이어로 이송', 'B자재 원래 자리 복귀'], problems: [] });
    if (path.endsWith('/start')) { state.run = RUN('running', { label: '1/3회 · A자재 이송 중' }); return json(route, state.run, 202); }
    if (path.endsWith('/finish-after-round')) { state.run = RUN('running', { finish_after_round: true }); return json(route, state.run); }
    if (path.endsWith('/pause')) { state.run = RUN('paused'); return json(route, state.run); }
    if (path.endsWith('/resume')) { state.run = RUN('running'); return json(route, state.run); }
    if (path.endsWith('/verify')) {
      const ok = state.verifyOk;
      state.run = RUN('interrupted', { label: '0/3회 · 중단됨(확인 필요)', lock_held: !ok, reason: ok ? '상태 확인 완료 — 잠금을 풀었습니다' : state.run.run.reason,
        check: { ok, notes: [], checks: [{ name: 'process', ok, detail: ok ? '실행 프로세스 없음' : '실행 프로세스가 아직 동작 중입니다' }] } });
      state.verifyOk = true;
      return json(route, state.run);
    }
    if (path.endsWith('/cancel')) { state.run = RUN('cancelled', { label: '1/3회 · 취소됨', reason: '사용자가 반복을 취소했습니다' }); return json(route, state.run); }
    return json(route, {}, 404);
  });
  await page.goto('/');
  return { calls, state };
}
const panel = (page) => page.locator('section.command');
const repeat = (page) => page.getByRole('region', { name: '반복 작업' });

test('[UI-REP-01] 접고 펼치며, 자재·횟수를 고르고, 미리보기를 승인해야 시작한다', async ({ page }) => {
  const { calls } = await setup(page);
  await expect(repeat(page).getByRole('checkbox')).toHaveCount(0);                    // 접혀 있다
  await repeat(page).getByRole('button', { name: /반복 작업/ }).click();
  await expect(repeat(page).getByRole('checkbox')).toHaveCount(3);
  const start = repeat(page).getByRole('button', { name: '반복 시작' });
  await expect(start).toBeDisabled();                                                  // 자재를 고르지 않았다
  await repeat(page).getByRole('checkbox', { name: 'A자재' }).check();
  await repeat(page).getByRole('checkbox', { name: 'B자재' }).check();
  await repeat(page).getByRole('spinbutton').fill('0');
  await expect(start).toBeDisabled();
  await expect(repeat(page)).toContainText('1~100 사이의 정수');
  await repeat(page).getByRole('spinbutton').fill('3');
  await start.click();
  const confirm = repeat(page).getByRole('group', { name: '반복 작업 확인' });
  await expect(confirm).toContainText('3회 × [A자재 컨베이어로 이송 → A자재 원래 자리 복귀 → B자재 컨베이어로 이송 → B자재 원래 자리 복귀] · 총 12동작');
  expect(calls.map((c) => c.path)).toEqual(['/v1/sim-demo/repeat/preview']);         // 승인 전에는 시작하지 않는다
  expect(calls[0].body).toEqual({ session_id: 'qa-session', materials: ['mat_a', 'mat_b'], count: '3' });
  await confirm.getByRole('button', { name: '승인하고 시작' }).click();
  await expect.poll(() => calls.map((c) => c.path)).toEqual(['/v1/sim-demo/repeat/preview', '/v1/sim-demo/repeat/start']);
  expect(calls[1].body).toEqual({ session_id: 'qa-session', token: 'tok-1' });
  await expect(repeat(page)).toContainText('1/3회 · A자재 이송 중');
});

test('[UI-REP-02] 시작할 수 없으면 이유를 보이고 시작 요청을 보내지 않는다', async ({ page }) => {
  const { calls } = await setup(page, { preview: { ok: false, problems: ['B자재이(가) 원래 자리가 아니라 컨베이어에 있습니다 — 움직이지 않습니다'], sequence: [] } });
  await repeat(page).getByRole('button', { name: /반복 작업/ }).click();
  await repeat(page).getByRole('checkbox', { name: 'B자재' }).check();
  await repeat(page).getByRole('button', { name: '반복 시작' }).click();
  await expect(repeat(page).getByRole('alert')).toContainText('움직이지 않습니다');
  expect(calls.some((c) => c.path.endsWith('/start'))).toBe(false);
});

test('[UI-REP-03] 진행 중이면 상태를 보이고 자재·횟수·시작·다른 명령을 잠그며, 회차 후 종료를 보낸다', async ({ page }) => {
  const { calls } = await setup(page, { run: RUN('running') });
  await expect(repeat(page)).toContainText('2/3회 · B자재 복귀 중');                   // 접혀 있어도 상태는 보인다
  await repeat(page).getByRole('button', { name: /반복 작업/ }).click();
  await expect(repeat(page).getByRole('checkbox', { name: 'A자재' })).toBeDisabled();
  await expect(repeat(page).getByRole('spinbutton')).toBeDisabled();
  await expect(repeat(page).getByRole('button', { name: '반복 시작' })).toBeDisabled();
  await expect(panel(page)).toContainText('반복 작업이 진행 중입니다 — 끝나거나 취소한 뒤 명령하세요');
  await expect(panel(page).getByRole('button', { name: 'A자재 → 컨베이어' })).toBeDisabled();
  await repeat(page).getByRole('button', { name: '현재 회차 후 종료' }).click();
  await expect.poll(() => calls.map((c) => c.path)).toEqual(['/v1/sim-demo/repeat/rep_1/finish-after-round']);
  await expect(repeat(page)).toContainText('이번 회차 후 종료');
});

test('[UI-REP-04] 새로고침해도 같은 실행을 서버에서 다시 보고 새로 시작하지 않는다', async ({ page }) => {
  const { calls } = await setup(page, { run: RUN('running') });
  await expect(repeat(page)).toContainText('2/3회 · B자재 복귀 중');
  await page.reload();
  await expect(repeat(page)).toContainText('2/3회 · B자재 복귀 중');
  expect(calls).toEqual([]);
});

test('[UI-REP-05] 시뮬레이션 보기의 일시정지·재개·반복 취소는 이 반복을 제어한다', async ({ page }) => {
  const { calls } = await setup(page, { run: RUN('running') });
  await page.getByRole('button', { name: '시뮬레이션 보기' }).click();
  const view = page.getByRole('dialog').first();
  await expect(view).toContainText('반복 작업: 2/3회 · B자재 복귀 중');
  await view.getByRole('button', { name: /일시정지/ }).click();
  await expect(view).toContainText('2/3회 · B자재 원래 자리 복귀 일시정지');
  await view.getByRole('button', { name: /재개/ }).click();
  await expect.poll(() => calls.map((c) => c.path)).toEqual(['/v1/sim-demo/repeat/rep_1/pause', '/v1/sim-demo/repeat/rep_1/resume']);
  await view.getByRole('button', { name: /일시정지/ }).click();
  await view.getByRole('button', { name: '반복 취소' }).click();
  await expect(repeat(page)).toContainText('1/3회 · 취소됨');
});

test('[UI-REP-06] 재시작으로 중단된 반복은 확인 전까지 명령을 잠그고, 확인이 통과해야 잠금을 푼다', async ({ page }) => {
  const { calls, state } = await setup(page, { run: RUN('interrupted', { label: '0/3회 · 중단됨(확인 필요)', lock_held: true,
    reason: '서버가 다시 시작돼 반복을 이어서 하지 않습니다 — 작업 셀 잠금을 유지합니다' }) });
  state.verifyOk = false;
  const box = page.getByRole('alert', { name: '중단된 반복 작업 확인' });
  await expect(box).toContainText('작업 셀 잠금 유지 중');
  await expect(panel(page)).toContainText('상태 확인 후 잠금 해제’를 먼저 하세요');
  await expect(panel(page).getByRole('button', { name: 'A자재 → 컨베이어' })).toBeDisabled();
  await box.getByRole('button', { name: '상태 확인 후 잠금 해제' }).click();
  await expect(box).toContainText('✗ 실행 프로세스가 아직 동작 중입니다');
  await expect(box).toContainText('잠금을 유지했습니다');
  await box.getByRole('button', { name: '상태 확인 후 잠금 해제' }).click();
  await expect(box).toHaveCount(0);
  await expect(panel(page)).not.toContainText('상태 확인 후 잠금 해제’를 먼저 하세요');
  expect(calls.map((c) => c.path)).toEqual(['/v1/sim-demo/repeat/rep_1/verify', '/v1/sim-demo/repeat/rep_1/verify']);
});
