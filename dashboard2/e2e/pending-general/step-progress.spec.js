import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend, sendCommand } from '../mock.js';

// 이송 실행 중 진행 표시(2026-10-07): 이송 한 번(계획 스텝 1~4)이 끝나기 전에 서버가 step_progress로
// 지난 스텝을 알리면 화면이 1→2→3→4 순서로 채운다. 최종 성공·실패는 step 결과가 정한다.

const PLAN = () => ({
  ok: true,
  payload: {
    request_id: 'p-request',
    plan: { plan_id: 'p-plan', plan_hash: 'p-hash', utterance: 'A자재를 컨베이어로 옮겨줘', created_at: Date.now() / 1000, ttl_sec: 300, steps: [
      { index: 1, skill: 'move', args: { to: 'loc_pallet_1' } },
      { index: 2, skill: 'pick', args: { object: 'mat_a', from: 'loc_pallet_1' } },
      { index: 3, skill: 'move', args: { to: 'loc_conveyor' } },
      { index: 4, skill: 'place', args: { object: 'mat_a', to: 'loc_conveyor' } },
      { index: 5, skill: 'home', args: {} },
    ] },
    validation: { decision: 'allow', detail: 'QA: 통과' },
  },
});
const progress = (done, no, label) => ({ type: 'step_progress', payload: { execution_id: 'p-exec', done_steps: done, current_step: done.length + 1, stage: { no, of: 12, label, reached: true } } });

test('[PRG-1] 이송 중 진행 단계가 1→2→3→4 순서로 차오르고, 실행 중 단계 이름을 보인다', async ({ page }) => {
  const calls = await mockBackend(page, { plan: PLAN, execute: null });
  await page.goto('/');
  await sendCommand(page, 'A자재를 컨베이어로 옮겨줘');
  const panel = page.locator('section.command');
  await panel.getByRole('button', { name: '실행 승인' }).click();
  await expect(panel).toContainText('실행 중');
  const items = panel.locator('ul.plan li');
  await expect(items).toHaveCount(5);
  await expect(panel.locator('ul.plan li.done')).toHaveCount(0);
  for (const [done, no, label] of [[[1], 4, '그리퍼 열기'], [[1, 2], 7, 'lift'], [[1, 2, 3], 10, '그리퍼 열기(해제)'], [[1, 2, 3, 4], 11, 'retreat']]) {
    calls.emit(progress(done, no, label));
    await expect(panel.locator('ul.plan li.done')).toHaveCount(done.length);
    await expect(items.nth(done.length - 1)).toHaveClass(/done/);
    await expect(items.nth(done.length)).not.toHaveClass(/done/);
    await expect(panel).toContainText(`${no}/12 ${label}`);
  }
  calls.finishExecution({ ...SAMPLES.done, steps: [1, 2, 3, 4, 5].map((index) => ({ index, skill: 'x', task_succeeded: true })) });
  await expect(panel).toContainText('작업 종료');
});

test('[PRG-2] 진행 표시 뒤 최종 결과가 실패면 실패가 이긴다', async ({ page }) => {
  const calls = await mockBackend(page, { plan: PLAN, execute: null });
  await page.goto('/');
  await sendCommand(page, 'A자재를 컨베이어로 옮겨줘');
  const panel = page.locator('section.command');
  await panel.getByRole('button', { name: '실행 승인' }).click();
  calls.emit(progress([1, 2, 3, 4], 11, 'retreat'));
  await expect(panel.locator('ul.plan li.done')).toHaveCount(4);
  calls.finishExecution({ ok: false, execution_id: 'p-exec', steps: [1, 2, 3, 4].map((index) => ({ index, skill: 'x', task_succeeded: false })), final: { task_succeeded: false } });
  await expect(panel).toContainText('작업 종료');
  await expect(panel.locator('ul.plan li.done')).toHaveCount(0);
});
