// 음성 입력 — 마이크 버튼 → STT 웹소켓(가짜 서버) → final만 명령으로 전송. 실제 마이크·서버는 쓰지 않는다.
import { expect, test } from '@playwright/test';
import { mockBackend } from '../mock.js';

test.use({
  permissions: ['microphone'],
  launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
});

const FINAL = 'A 자재를 컨베이어로 옮겨줘';
const panel = (page) => page.locator('section.command');

// 서버 프로토콜(server/routes/stt.py): session → (오디오 수신 중) partial → flush를 받으면 final.
async function mockStt(page) {
  await page.route('**/v1/sessions', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'qa-client' } }));
  const seen = { flush: 0 };
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    let partialSent = false;
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: 'stt_qa', sample_rate_hz: 16000 }));
    ws.onMessage((m) => {
      if (typeof m !== 'string') {
        if (!partialSent) { partialSent = true; ws.send(JSON.stringify({ kind: 'partial', text: 'A 자재를' })); }
        return;
      }
      if (JSON.parse(m).type === 'flush') {
        seen.flush += 1;
        ws.send(JSON.stringify({ kind: 'final', text: FINAL, raw_text: FINAL, confidence: 0.93 }));
      }
    });
  });
  return seen;
}

test.describe('음성 입력', () => {
  test('[UI-VOICE-01][SFR-001] 마이크를 누르면 음성 인식 표시(막대 3개)·aria-pressed·중간 전사가 보인다', async ({ page }) => {
    await mockBackend(page);
    await mockStt(page);
    await page.goto('/');
    const mic = panel(page).getByRole('button', { name: /음성 입력/ });
    await expect(mic).toHaveAttribute('aria-pressed', 'false');
    await mic.click();
    const live = panel(page).getByRole('button', { name: /음성 인식 중/ });
    await expect(live).toHaveAttribute('aria-pressed', 'true');
    await expect(live.locator('.voice-bars i')).toHaveCount(3);
    await expect(panel(page).locator('.cmd-partial')).toHaveText('A 자재를');
  });

  test('[UI-VOICE-02][SFR-001] final이 오면 stt_final로 1회만 보낸다(partial로는 보내지 않는다)', async ({ page }) => {
    const calls = await mockBackend(page);
    const seen = await mockStt(page);
    await page.goto('/');
    await panel(page).getByRole('button', { name: /음성 입력/ }).click();
    await expect(panel(page).locator('.cmd-partial')).toHaveText('A 자재를');
    expect(calls.filter((c) => c.path.endsWith('/command'))).toHaveLength(0);
    await panel(page).getByRole('button', { name: /음성 인식 중/ }).click();
    await expect.poll(() => calls.filter((c) => c.path.endsWith('/command')).length).toBe(1);
    const body = calls.find((c) => c.path.endsWith('/command')).body;
    expect(body).toMatchObject({ source: 'stt_final', utterance: FINAL, raw_transcript: FINAL, stt_confidence: 0.93 });
    expect(seen.flush).toBe(1);
  });

  test('[UI-VOICE-03][SFR-001] STT 기능이 꺼져 있으면 마이크가 잠기고 이유가 보인다', async ({ page }) => {
    const off = (base) => ({ ...base, features: { ...base.features, stt: { available: false, detail: '모델 없음' } } });
    await mockBackend(page, { overrides: { config: off, health: off } });
    await page.goto('/');
    await expect(panel(page).getByRole('button', { name: /음성 입력/ })).toBeDisabled();
    await expect(panel(page)).toContainText('음성 인식을 쓸 수 없습니다 — 모델 없음');
  });
});
