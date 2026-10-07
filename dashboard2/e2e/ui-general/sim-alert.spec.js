// 일반 경로: 오류 안내의 조치는 서버 사유 코드로만 고른다(추측 없음). 서버 응답은 가짜다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

async function send(page, text) {
  await page.getByLabel('자연어 명령').fill(text);
  await page.getByRole('button', { name: '보내기' }).click();
}

test('[UI-GSAL-01] 계획 모델 없음(503 plan.llm_unavailable): 서버 사유 그대로 + 코드에 맞는 조치 + 상세 보기의 코드', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => route.fulfill({ status: 503, json: { ok: false, reason_code: 'plan.llm_unavailable',
    detail: '계획 생성을 사용할 수 없다: 모델 서버를 확인할 수 없다: [plan.llm_timeout] 응답이 60.0s를 넘겼다' } }));
  await page.goto('/');
  await send(page, 'A자재를 컨베이어로 옮겨줘');
  const box = page.getByRole('alert', { name: '오류 안내' });
  await expect(box).toContainText('원인계획 생성을 사용할 수 없다: 모델 서버를 확인할 수 없다');
  await expect(box).toContainText('조치계획 모델 서버 상태를 확인해 주세요');
  await box.getByText('상세 보기').click();
  await expect(box).toContainText('코드plan.llm_unavailable');
});

test('[UI-GSAL-02] 기하 검사 확인 필요(geometry.environment_unavailable)는 경고 톤 안내와 다시 보내기 조치', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => route.fulfill({ json: { ok: true, request_id: 'r', executable: false, stop_latch: { cleared: true },
    plan: { plan_id: 'p', plan_hash: 'h', steps: [] }, safety: { decision: 'allow', rules: [] },
    validation: { decision: 'ask', reason_code: 'geometry.environment_unavailable', detail: '기하 검사를 완료하지 못했다 — 검사하지 않은 상태를 안전으로 보지 않는다' } } }));
  await page.goto('/');
  await send(page, 'A자재를 컨베이어로 옮겨줘');
  await page.getByRole('button', { name: '시뮬레이션 보기', exact: true }).click();
  const box = page.getByRole('alert', { name: '오류 안내' });
  await expect(box).toContainText('확인 필요');
  await expect(box).toContainText('원인기하 검사를 완료하지 못했다');
  await expect(box).toContainText('조치잠시 뒤 다시 보내 주세요');
  await expect(box).toHaveClass(/warn/);
});

test('[UI-GSAL-03] 작업 실패로 시뮬레이션 창이 닫혀도 명령 패널에 원인·조치가 남는다(결과 표시와 중복 없이)', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => route.fulfill({ json: { ok: true, request_id: 'r', executable: true, stop_latch: { cleared: true },
    plan: { plan_id: 'p', plan_hash: 'h', ttl_sec: 600, steps: [{ index: 1, skill: 'move', args: { target: 'loc_pallet_1' } }] },
    safety: { decision: 'allow', rules: [] }, validation: { decision: 'allow', detail: '' } } }));
  await page.route('**/v1/decision', (route) => route.fulfill({ json: { ok: true, approval_id: 'a' } }));
  await page.route('**/v1/execute', async (route) => { await new Promise((r) => { setTimeout(r, 600); });
    return route.fulfill({ status: 409, json: { ok: false, execution_id: 'exec-1', interrupted: 'exec.unverifiable',
      final: { state: 'failed', reason_code: 'exec.unverifiable', evidence: { detail: '놓은 뒤 자재 위치를 관측하지 못했다' } } } }); });
  await page.goto('/');
  await send(page, 'A자재를 컨베이어로 옮겨줘');
  await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
  const card = page.locator('section.command .cmd-card');
  await expect(card).toContainText('실행 중단 (exec.unverifiable)', { timeout: 8000 });
  await page.keyboard.press('Escape');
  const close = page.getByRole('button', { name: '창 닫기' });
  if (await close.count()) await close.click();
  await expect(page.getByRole('dialog')).toHaveCount(0);                  // 창은 닫혀 있다
  const guide = card.getByRole('note', { name: '오류 원인과 조치' });
  await expect(guide).toContainText('원인놓은 뒤 자재 위치를 관측하지 못했다');
  await expect(guide).toContainText('조치자재 위치를 확인하고, 필요하면 복구한 뒤 다시 시도해 주세요');
  await guide.getByText('상세 보기').click();
  await expect(guide).toContainText('코드exec.unverifiable');
});

test('[UI-GSAL-04] 차단 사유가 카드 제목에 이미 있으면 원인은 되풀이하지 않고 조치·상세만', async ({ page }) => {
  await mockBackend(page);
  await page.route('**/v1/sessions**', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'c1' } }));
  await page.route('**/v1/plan', (route) => route.fulfill({ status: 503, json: { ok: false, reason_code: 'plan.llm_unavailable', detail: '계획 생성을 사용할 수 없다' } }));
  await page.goto('/');
  await send(page, 'A자재를 컨베이어로 옮겨줘');
  const card = page.locator('section.command .cmd-card');
  const guide = card.getByRole('note', { name: '오류 원인과 조치' });
  await expect(guide).toContainText('조치계획 모델 서버 상태를 확인해 주세요');
  await expect(guide).not.toContainText('원인');
  await expect(card.locator('.cmd-title')).toHaveText('계획 생성을 사용할 수 없다');
  await expect(guide.locator('p')).toHaveCount(1);                        // 보이는 줄은 조치 하나(원인은 제목에 이미 있음)
});
