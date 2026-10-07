// 명령 패널 — 화면설계서 다(⑥-1~⑥-4, ⑦), 상태매트릭스 §4·§11, 피그마 CommandPanel 상태, 개발 중 결정(DEV-04)
import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend, sendCommand } from '../mock.js';

const panel = (page) => page.locator('section.command');

test.describe('명령 패널', () => {
  test('[UI-CMD-01][SFR-001·DEV-04] 대기: 텍스트 입력 + 입력칸 안 마이크 + 보내기, STEP 제목·Enter 안내 없음', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    const p = panel(page);
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
    await expect(p.getByRole('button', { name: /음성 입력/ })).toBeVisible();
    await expect(p.getByRole('button', { name: '보내기' })).toBeVisible();
    await expect(p).not.toContainText('STEP');
    await expect(p).not.toContainText('Enter로 보내기');
    // 마이크가 입력칸 영역 안에 있다
    // 서버 값이 오면 스킬 버튼이 생겨 입력칸이 밀린다 — 두 위치를 같은 순간에 잰다.
    await expect(p.getByRole('button', { name: '즉시 정지' })).toBeVisible();
    const [field, mic] = await p.evaluate((el) => [el.querySelector('#command-input'), el.querySelector('.mic-btn')]
      .map((n) => { const r = n.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height }; }));
    expect(mic.x).toBeGreaterThanOrEqual(field.x);
    expect(mic.x + mic.width).toBeLessThanOrEqual(field.x + field.width + 1);
    expect(mic.y + mic.height).toBeLessThanOrEqual(field.y + field.height + 1);
  });

  test('[UI-CMD-02][⑥-2·§4 ASK] 질문은 정보입력 요청 — 서버 질문 문장 + 답 입력칸, 승인 버튼 없음', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.ask });
    await page.goto('/');
    await sendCommand(page, '그거 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText(SAMPLES.ask.detail);
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
    await expect(p.getByRole('button', { name: /승인/ })).toHaveCount(0);
  });

  test('[UI-CMD-03][⑥-3 BLOCK] 차단은 실행 불가 통보 — 사유 문장, 우회 버튼 없음, "명령 수정"으로 입력 복귀', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.block });
    await page.goto('/');
    await sendCommand(page, '금지 칸으로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText(SAMPLES.block.detail);
    await expect(p.getByRole('button', { name: /승인|무시|그래도 실행/ })).toHaveCount(0);
    // 차단(BLOCK)이면 위험 판정 창이 패널을 덮는다 — 창의 "명령 수정"으로 입력에 돌아간다. 창에도 우회 버튼은 없다.
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('button', { name: /승인|무시|그래도 실행/ })).toHaveCount(0);
    await dialog.getByRole('button', { name: '명령 수정' }).click();
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
  });

  test('[UI-CMD-04][⑥-4·SFR-010] 실행 전 최종점검: 서버 요약 + 실행 승인/취소 + 남은 시간, 승인 시 decision → execute 각 1회', async ({ page }) => {
    const calls = await mockBackend(page, { plan: SAMPLES.plan(60), execute: SAMPLES.done });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText('A 자재를 컨베이어로 옮깁니다');
    await expect(p).toContainText(/\d+초 안에 승인하지 않으면 취소됩니다/);
    await p.getByRole('button', { name: '실행 승인' }).click();
    await expect(p).toContainText('작업 완료');
    expect(calls.filter((c) => c.path === '/v1/decision')).toHaveLength(1);
    expect(calls.find((c) => c.path === '/v1/decision').body).toMatchObject({ plan_id: 'qa-plan', plan_hash: 'qa-hash', decision: 'approve' });
    expect(calls.find((c) => c.path === '/v1/execute').body.approval_id).toBe('qa-approval');
  });

  test('[UI-CMD-05][§4 결과 만료] 확인 시간이 지나면 실행 승인 버튼이 잠긴다', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(1) });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p.getByRole('button', { name: '실행 승인' })).toBeDisabled({ timeout: 5000 });
    await expect(p).toContainText('확인 시간이 지났습니다');
  });

  test('[UI-CMD-05b][취소] 취소하면 decision은 reject이고 실행은 시작하지 않는다', async ({ page }) => {
    const calls = await mockBackend(page, { plan: SAMPLES.plan(60) });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await panel(page).getByRole('button', { name: '취소' }).click();
    await expect(panel(page)).toContainText('취소');
    expect(calls.find((c) => c.path === '/v1/decision').body).toMatchObject({ decision: 'reject' });
    expect(calls.some((c) => c.path === '/v1/execute')).toBe(false);
  });

  test('[UI-CMD-06][⑥-1·§11] 중간과정 3단계 체크리스트(동작 준비 → 안전 규칙 확인 → 가상 동작 확인)', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(60), execute: null, progress: [SAMPLES.step] });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    for (const step of ['동작 준비', '안전 규칙 확인', '가상 동작 확인']) await expect(p).toContainText(step);
  });

  test('[UI-CMD-07][⑦·§10] 대상 로봇 선택 Chip(1대면 기본 선택), 명령 직전 최종점검 카드에 로봇명 재명시', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(60) });
    await page.goto('/');
    const chip = panel(page).getByRole('group', { name: '대상 로봇' }).getByRole('button').first();
    await expect(chip).toBeVisible();
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    const name = (await chip.textContent()).trim();
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await expect(panel(page)).toContainText(`대상: ${name}`);
  });

  test('[UI-CMD-08][오류] 서버에 닿지 못하면 오류 카드 + 새 명령 입력', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/plan', (route) => route.abort());
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText('계획 요청 실패');
    await p.getByRole('button', { name: '새 명령 입력' }).click();
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
  });

  test('[UI-CMD-10][SFR-012] 터치 기반 스킬 버튼(No-Code fallback) — 누르면 해당 문장이 서버로 간다', async ({ page }) => {
    const calls = await mockBackend(page);
    await page.goto('/');
    // 서버가 지금 가능하다고 준 동작(고정 응답: A자재 이송)만 버튼이 된다.
    await panel(page).getByRole('group', { name: '스킬 버튼' }).getByRole('button', { name: 'A자재 → 컨베이어' }).click();
    await expect.poll(() => calls.filter((c) => c.path === '/v1/plan').length).toBe(1);
    expect(calls.find((c) => c.path === '/v1/plan').body).toMatchObject({ utterance: 'A자재를 컨베이어로 옮겨줘', session_id: 'qa-session' });
    expect(calls.some((c) => c.path.startsWith('/v1/sim-demo/'))).toBe(false);
  });

  test('[UI-CMD-11][⑥-2] ASK 카드에도 "답변 보내기" 버튼 — 입력이 있어야 켜지고, 누르면 답이 다시 간다', async ({ page }) => {
    const calls = await mockBackend(page, { plan: SAMPLES.ask });
    await page.goto('/');
    await sendCommand(page, '그거 옮겨줘');
    const send = panel(page).getByRole('button', { name: '답변 보내기' });
    await expect(send).toBeVisible();
    await expect(send).toBeDisabled(); // idle의 보내기와 같은 조건 — 빈 입력은 못 보낸다
    await panel(page).getByLabel('자연어 명령').fill('A 자재');
    await expect(send).toBeEnabled();
    await send.click();
    await expect.poll(() => calls.filter((c) => c.path === '/v1/plan').length).toBe(2);
    expect(calls.filter((c) => c.path === '/v1/plan')[1].body.utterance).toBe('A 자재');
  });

  test('[UI-CMD-12][⑥-4] 최종점검 카드: 서버 값이 있는 도구·판정 시각만 보인다', async ({ page }) => {
    await mockBackend(page, { plan: SAMPLES.plan(60) });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await expect(panel(page)).toContainText('도구: 그리퍼'); // 고정 응답 config.robot.has_gripper = true
    await expect(panel(page)).toContainText(/판정 시각 \d\d:\d\d:\d\d/);
  });

  test('[UI-CMD-13][⑥-4] 서버가 판정 시각을 주지 않으면 그 줄을 그리지 않는다', async ({ page }) => {
    const withoutTime = SAMPLES.plan(60);
    delete withoutTime.payload.plan.created_at;
    await mockBackend(page, { plan: withoutTime });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await expect(panel(page)).toContainText('실행 승인');
    await expect(panel(page)).not.toContainText('판정 시각');
  });
});
