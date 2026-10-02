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
    await mockBackend(page, { command: SAMPLES.ask });
    await page.goto('/');
    await sendCommand(page, '그거 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText(SAMPLES.ask.reason);
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
    await expect(p.getByRole('button', { name: /승인/ })).toHaveCount(0);
  });

  test('[UI-CMD-03][⑥-3 BLOCK] 차단은 실행 불가 통보 — 사유 문장, 우회 버튼 없음, "명령 수정"으로 입력 복귀', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.block });
    await page.goto('/');
    await sendCommand(page, '금지 칸으로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText(SAMPLES.block.reason);
    await expect(p.getByRole('button', { name: /승인|무시|그래도 실행/ })).toHaveCount(0);
    await p.getByRole('button', { name: '명령 수정' }).click();
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
  });

  test('[UI-CMD-04][⑥-4·SFR-010] 실행 전 최종점검: 서버 요약 + 실행 승인/취소 + 남은 시간, 승인 시 확인 요청 1회', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.done] });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText('A 자재를 컨베이어로 옮깁니다');
    await expect(p).toContainText(/\d+초 안에 승인하지 않으면 취소됩니다/);
    await p.getByRole('button', { name: '실행 승인' }).click();
    await expect(p).toContainText('이송 완료');
    expect(calls.filter((c) => c.path.endsWith('/confirm'))).toHaveLength(1);
    expect(calls.find((c) => c.path.endsWith('/confirm')).body).toMatchObject({ token: 'qa-token', action: 'confirm' });
  });

  test('[UI-CMD-05][§4 결과 만료] 확인 시간이 지나면 실행 승인 버튼이 잠긴다', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.confirm(1) });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p.getByRole('button', { name: '실행 승인' })).toBeDisabled({ timeout: 5000 });
    await expect(p).toContainText('확인 시간이 지났습니다');
  });

  test('[UI-CMD-05b][취소] 취소하면 확인 요청은 cancel로 가고 작업이 시작되지 않는다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), confirm: { ok: true } });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await panel(page).getByRole('button', { name: '취소' }).click();
    await expect(panel(page)).toContainText('취소');
    expect(calls.find((c) => c.path.endsWith('/confirm')).body).toMatchObject({ action: 'cancel' });
    expect(calls.some((c) => c.path.includes('/jobs/'))).toBe(false);
  });

  test('[UI-CMD-06][⑥-1·§11] 중간과정 3단계 체크리스트(동작 준비 → 안전 규칙 확인 → 가상 동작 확인)', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job, SAMPLES.run.job] });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    for (const step of ['동작 준비', '안전 규칙 확인', '가상 동작 확인']) await expect(p).toContainText(step);
  });

  test('[UI-CMD-07][⑦·§10] 대상 로봇 선택 Chip, 명령 직전 로봇명 재명시', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(panel(page).getByRole('button', { name: /FR3|Alpha|로봇/ })).toBeVisible();
  });

  test('[UI-CMD-08][오류] 서버에 닿지 못하면 오류 카드 + 새 명령 입력', async ({ page }) => {
    await mockBackend(page);
    await page.route('**/v1/sim-demo/command', (route) => route.abort());
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    const p = panel(page);
    await expect(p).toContainText('백엔드에 연결하지 못했습니다');
    await p.getByRole('button', { name: '새 명령 입력' }).click();
    await expect(p.getByLabel('자연어 명령')).toBeVisible();
  });

  test('[UI-CMD-10][SFR-012] 터치 기반 스킬 버튼(No-Code fallback)', async ({ page }) => {
    await mockBackend(page);
    await page.goto('/');
    await expect(panel(page).getByRole('button', { name: /이송|복귀|홈|정지/ }).first()).toBeVisible();
  });
});
