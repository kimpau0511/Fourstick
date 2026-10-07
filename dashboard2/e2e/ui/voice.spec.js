// 호출어 음성 명령(2026-10-07) — 마이크 켜기/끄기 · "{이름}야" → TTS → 명령 → 해석 → 실행 확인 · 시간 초과 · 인식 실패 ·
// 긴급 정지. STT는 가짜 웹소켓 서버(호출어 대기 = mode=wake, 명령 = 일반 모드), TTS는 가짜 speechSynthesis다.
import { expect, test } from '@playwright/test';
import { SAMPLES, mockBackend } from '../mock.js';

test.use({
  permissions: ['microphone'],
  launchOptions: { args: ['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'] },
});

const panel = (page) => page.locator('section.command');
const voice = (page) => page.getByRole('group', { name: '음성 명령' });
const status = (page) => voice(page).locator('.voice-status');

// 가짜 TTS: 말한 문장을 기록하고 ms 뒤 끝낸다(헤드리스에는 음성이 없다). window.__speaking: 지금 재생 중인 문장.
async function fakeTts(page, ms = 50) {
  await page.addInitScript((dur) => {
    window.__spoken = [];
    window.__speaking = null;
    window.SpeechSynthesisUtterance = function Utter(text) { this.text = text; };
    let timer = null;
    let current = null;
    Object.defineProperty(window, 'speechSynthesis', { configurable: true, value: {
      cancel() { clearTimeout(timer); if (current) { const u = current; current = null; window.__speaking = null; u.onerror && u.onerror(); } },
      speak(u) { window.__spoken.push(u.text); window.__speaking = u.text; current = u;
        timer = setTimeout(() => { current = null; window.__speaking = null; u.onend && u.onend(); }, dur); },
    } });
  }, ms);
}

// 가짜 STT. 소켓을 모드별로 잡아 두고, 시험이 이벤트를 보낸다. 오디오 프레임 수·제어 메시지를 센다.
async function mockStt(page) {
  const stt = { wake: [], cmd: [], frames: { wake: 0, cmd: 0 }, control: [] };
  await page.route('**/v1/sessions', (route) => route.fulfill({ json: { session_id: 'qa-session', client_id: 'qa-client' } }));
  await page.routeWebSocket(/\/v1\/stt/, (ws) => {
    const mode = new URL(ws.url()).searchParams.get('mode') === 'wake' ? 'wake' : 'cmd';
    const entry = { ws, closed: false, epoch: 0 };
    stt[mode].push(entry);
    ws.onMessage((m) => {
      if (typeof m !== 'string') { stt.frames[mode] += 1; return; }
      const c = JSON.parse(m);
      if (c.type === 'abort' && mode === 'wake') entry.epoch += 1;     // 서버처럼 발화 세대를 올린다
      stt.control.push({ mode, ...c });
    });
    ws.onClose(() => { entry.closed = true; });
    ws.send(JSON.stringify({ kind: 'session', session_id: 'qa-session', stream_id: `stt_${mode}`, sample_rate_hz: 16000, mode }));
  });
  // 호출어 대기 소켓 이벤트에는 서버처럼 지금 세대(epoch)를 붙인다(일부러 옛 세대를 주면 그대로).
  stt.say = (mode, ev) => {
    const entry = stt[mode][stt[mode].length - 1];
    entry.ws.send(JSON.stringify(mode === 'wake' && ev.epoch === undefined ? { ...ev, epoch: entry.epoch } : ev));
  };
  stt.epoch = () => stt.wake[stt.wake.length - 1].epoch;
  return stt;
}

async function turnOn(page, stt) {
  await voice(page).getByRole('button', { name: '음성 명령 켜기' }).click();
  await expect.poll(() => stt.wake.length).toBe(1);
  await expect.poll(() => stt.frames.wake).toBeGreaterThan(0);
}

test.describe('호출어 음성 명령', () => {
  test('[UI-VOICE-01] 기본은 꺼짐, 켜면 호출어 대기(펄스)·끄면 정리된다', async ({ page }) => {
    await mockBackend(page);
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    const toggle = voice(page).getByRole('button', { name: '음성 명령 켜기' });
    await expect(toggle).toHaveAttribute('aria-pressed', 'false');
    await expect(status(page)).toContainText('음성 명령 꺼짐');
    await expect(voice(page)).toContainText('현재 호출어: 지니야');
    await expect(voice(page).locator('.voice-mic.pulse')).toHaveCount(0);
    expect(stt.wake.length).toBe(0);                                                  // 꺼져 있으면 듣지 않는다
    await turnOn(page, stt);
    await expect(voice(page).getByRole('button', { name: '음성 명령 끄기' })).toHaveAttribute('aria-pressed', 'true');
    await expect(status(page)).toContainText('호출어를 기다리고 있습니다. “지니야”라고 말씀하세요.');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    await expect(voice(page).locator('.voice-mic.pulse')).toHaveCount(1);
    await voice(page).getByRole('button', { name: '음성 명령 끄기' }).click();
    await expect(status(page)).toContainText('음성 명령 꺼짐');
    await expect.poll(() => stt.wake[0].closed).toBe(true);
    const frames = stt.frames.wake;
    await page.waitForTimeout(300);
    expect(stt.frames.wake).toBe(frames);                                              // 끈 뒤 오디오를 보내지 않는다
  });

  test('[UI-VOICE-02] 호출어 → "네, 말씀하세요." → 명령 → 분석 → 확인 카드, 실행 승인 전에는 실행하지 않는다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '안녕하세요', confidence: 0.9 });               // 호출어가 아니면 무시
    stt.say('wake', { kind: 'final', text: '알파야', confidence: 0.9 });                   // 다른 이름도 무시
    await page.waitForTimeout(300);
    expect(stt.cmd.length).toBe(0);
    stt.say('wake', { kind: 'final', text: '지니야.', confidence: 0.9 });
    await expect(status(page)).toContainText('듣는 중… 명령을 말씀하세요.');
    await expect.poll(() => page.evaluate(() => window.__spoken)).toEqual(['네, 말씀하세요.']);
    await expect.poll(() => stt.cmd.length).toBe(1);                                  // TTS가 끝난 뒤에야 명령 소켓을 연다
    await expect.poll(() => stt.frames.cmd).toBeGreaterThan(0);
    stt.say('cmd', { kind: 'speech_start' });
    stt.say('cmd', { kind: 'partial', text: 'A 자재를' });
    stt.say('cmd', { kind: 'state', state: 'finalizing' });
    await expect(status(page)).toContainText('TRANSCRIBING');
    stt.say('cmd', { kind: 'final', text: 'A 자재를 컨베이어로 옮겨줘', raw_text: 'A 자재를 컨베이어로 옮겨줘', confidence: 0.93 });
    await expect(voice(page)).toContainText('인식된 명령: “A 자재를 컨베이어로 옮겨줘”');
    await expect(status(page)).toContainText('CONFIRMING');
    await expect(panel(page)).toContainText('A 자재를 컨베이어로 옮깁니다');             // 해석 결과 문장
    await expect(panel(page).getByRole('button', { name: '실행 승인' })).toBeVisible();
    await expect(panel(page).getByRole('button', { name: '취소' })).toBeVisible();
    const commands = calls.filter((c) => c.path.endsWith('/command'));
    expect(commands).toHaveLength(1);
    expect(commands[0].body).toMatchObject({ utterance: 'A 자재를 컨베이어로 옮겨줘', source: 'stt_final' });
    expect(calls.filter((c) => c.path.endsWith('/confirm'))).toHaveLength(0);       // 확인 전에는 실행 요청이 없다
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });               // 확인 중에는 새 명령을 받지 않는다
    await page.waitForTimeout(300);
    expect(stt.cmd.length).toBe(1);
    await panel(page).getByRole('button', { name: '취소' }).click();
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    expect(calls.filter((c) => c.path.endsWith('/confirm')).filter((c) => c.body && c.body.action !== 'cancel')).toHaveLength(0);
  });

  test('[UI-VOICE-03] 실행 승인 → 실행 중 → 끝나면 호출어 대기로 돌아간다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.done] });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야 A 자재를 컨베이어로 옮겨줘', confidence: 0.9 }); // 한 번에 말해도 된다
    await expect(status(page)).toContainText('CONFIRMING');
    expect(calls.filter((c) => c.path.endsWith('/command'))[0].body).toMatchObject({ utterance: 'A 자재를 컨베이어로 옮겨줘' });
    await panel(page).getByRole('button', { name: '실행 승인' }).click();
    await expect.poll(() => calls.filter((c) => c.path.endsWith('/confirm')).length).toBe(1);
    await expect(status(page)).toContainText('WAKE_WORD_WAITING', { timeout: 15000 });
    await expect(voice(page)).toContainText('작업이 끝났습니다');
  });

  test('[UI-VOICE-10] 중간 전사가 "지니야"에서 멈추면 발화 끝을 기다리지 않고 받고, 그 발화는 서버에서 버린다', async ({ page }) => {
    await mockBackend(page);
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'partial', text: '지니야' });
    stt.say('wake', { kind: 'partial', text: '지니야 에이 자재' });                    // 말이 이어지면 기다린다
    await page.waitForTimeout(1600);
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    stt.say('wake', { kind: 'partial', text: '지니야' });
    await expect(status(page)).toContainText('LISTENING', { timeout: 3000 });
    // 첫 abort = 호출어 발화 버리기(그 뒤 '네, 말씀하세요.' 안내가 끝나면 한 번 더 버린다).
    await expect.poll(() => stt.control.filter((c) => c.mode === 'wake' && c.type === 'abort').length).toBeGreaterThanOrEqual(1);
  });

  test('[UI-VOICE-04] 호출어 뒤 제한 시간 안에 말이 없으면 호출어 대기로 돌아간다', async ({ page }) => {
    const calls = await mockBackend(page);
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });
    await expect(status(page)).toContainText('LISTENING');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING', { timeout: 12000 });
    await expect(voice(page)).toContainText('8초 안에 명령이 없어 호출어 대기로 돌아갑니다');
    await expect.poll(() => stt.cmd[0].closed).toBe(true);
    expect(calls.filter((c) => c.path.endsWith('/command'))).toHaveLength(0);
  });

  test('[UI-VOICE-05] 인식 실패·해석 실패는 오류를 보이고 호출어 대기로 돌아간다', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.block });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });
    await expect.poll(() => stt.cmd.length).toBe(1);
    stt.say('cmd', { kind: 'clarify', text: '어', confidence: 0.3 });
    await expect(status(page)).toContainText('ERROR');
    await expect(voice(page)).toContainText('잘 알아듣지 못했습니다');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING', { timeout: 5000 });
    stt.say('wake', { kind: 'final', text: '지니야, 이상한 데로 옮겨', confidence: 0.9 });
    await expect(status(page)).toContainText('ERROR');
    await expect(voice(page)).toContainText('명령을 실행할 수 없습니다');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING', { timeout: 5000 });
  });

  test('[UI-VOICE-06] 켜져 있으면 "정지"는 호출어 없이 바로 정지 → STOPPED, 복구 화면', async ({ page }) => {
    const calls = await mockBackend(page);
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'partial', text: '멈춰' });                                 // partial에서도 바로
    await expect(status(page)).toContainText('STOPPED');
    await expect.poll(() => calls.filter((c) => c.path.endsWith('/stop')).length).toBe(1);
    const box = voice(page).getByRole('group', { name: '정지 후 복구' });
    await expect(box).toBeVisible();
    await box.getByRole('button', { name: '호출어 대기로' }).click();
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    // 명령을 듣는 중에도 '긴급 정지'는 바로 처리한다.
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });
    await expect.poll(() => stt.cmd.length).toBe(1);
    stt.say('cmd', { kind: 'partial', text: '긴급 정지' });
    await expect(status(page)).toContainText('STOPPED');
    await expect.poll(() => calls.filter((c) => c.path.endsWith('/stop')).length).toBe(2);
    expect(calls.filter((c) => c.path.endsWith('/command'))).toHaveLength(0);
  });

  test('[UI-VOICE-07] 듣는 중에 끄면 녹음·대기 타이머·소켓을 모두 정리한다', async ({ page }) => {
    await mockBackend(page);
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });
    await expect.poll(() => stt.cmd.length).toBe(1);
    await voice(page).getByRole('button', { name: '음성 명령 끄기' }).click();
    await expect(status(page)).toContainText('음성 명령 꺼짐');
    await expect.poll(() => stt.cmd[0].closed && stt.wake[0].closed).toBe(true);
    await page.waitForTimeout(9000);                                                   // 명령 제한 시간이 지나도
    await expect(status(page)).toContainText('음성 명령 꺼짐');                         // 다시 켜지지 않는다
  });

  test('[UI-VOICE-08] STT를 쓸 수 없으면 켜기 버튼이 잠기고 이유가 보인다', async ({ page }) => {
    const off = (base) => ({ ...base, features: { ...base.features, stt: { available: false, detail: '모델 없음' } } });
    await mockBackend(page, { overrides: { config: off, health: off } });
    await page.goto('/');
    await expect(voice(page).getByRole('button', { name: '음성 명령 켜기' })).toBeDisabled();
    await expect(voice(page)).toContainText('음성 인식을 쓸 수 없습니다 — 모델 없음');
  });

  test('[UI-VOICE-09] 저장된 로봇 이름이 호출어가 된다', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.confirm(60), overrides: { robotName: '알파' } });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await expect(voice(page)).toContainText('현재 호출어: 알파야');
    await turnOn(page, stt);
    await expect(status(page)).toContainText('“알파야”라고 말씀하세요');
    stt.say('wake', { kind: 'final', text: '지니야', confidence: 0.9 });
    await page.waitForTimeout(300);
    expect(stt.cmd.length).toBe(0);
    stt.say('wake', { kind: 'final', text: '알파 야', confidence: 0.9 });
    await expect.poll(() => stt.cmd.length).toBe(1);
  });
});

test.describe('로봇 이름 설정', () => {
  test('[UI-NAME-01] 로봇 관리에서 이름을 저장하면 새로고침 뒤에도 유지되고 대시보드 호출어가 바뀐다', async ({ page }) => {
    const calls = await mockBackend(page);
    await page.goto('/#/robots');
    const card = page.getByRole('form', { name: '로봇 이름' });
    const input = card.getByLabel('로봇 이름');
    await expect(input).toHaveValue('지니');
    await expect(card).toContainText('현재 호출어: 지니야');
    for (const bad of ['', '   ', 'jini', '가나다라마']) {
      await input.fill(bad);
      await card.getByRole('button', { name: '저장' }).click();
      await expect(card.locator('.danger')).toBeVisible();
    }
    expect(calls.filter((c) => c.path === '/v1/settings/robot-name')).toHaveLength(0);   // 잘못된 값은 보내지 않는다
    await input.fill('알파');
    await card.getByRole('button', { name: '저장' }).click();
    await expect(card).toContainText('로봇 이름이 저장되었습니다.');
    await expect(card).toContainText('현재 호출어: 알파야');
    await expect(voice(page)).toContainText('현재 호출어: 알파야');                       // 즉시 반영
    await page.reload();
    await expect(page.getByRole('form', { name: '로봇 이름' }).getByLabel('로봇 이름')).toHaveValue('알파');
    await expect(voice(page)).toContainText('현재 호출어: 알파야');
  });
});

// 계획 음성 안내 → 음성 승인/취소(2026-10-07). 시연 경로(가짜 서버) — 확인 카드의 승인·취소와 같은 /v1/sim-demo/confirm을 쓴다.
const spoken = (page) => page.evaluate(() => window.__spoken);
const confirms = (calls) => calls.filter((c) => c.path.endsWith('/confirm'));
const listening = (page) => expect(status(page)).toContainText('“네·진행해” 또는 “아니·취소해”라고 말씀하세요', { timeout: 8000 });

async function toApproval(page, stt, command = '지니야 A 자재를 컨베이어로 옮겨줘') {
  await turnOn(page, stt);
  stt.say('wake', { kind: 'final', text: command, confidence: 0.9 });
  await expect(status(page)).toContainText('CONFIRMING');
  await listening(page);
}

test.describe('계획 음성 안내 → 음성 승인', () => {
  test('[UI-VAPP-01] 명령 확인 → 계획 안내(자재·목적지) → "네, 진행해" → 서버가 받은 뒤 "작업을 시작하겠습니다"', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job, SAMPLES.done] });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    const said = await spoken(page);
    expect(said).toContain('요청하신 명령을 확인했습니다.');
    expect(said.find((t) => t.startsWith('다음과 같이 작업을 진행하려고 합니다.'))).toBe(
      '다음과 같이 작업을 진행하려고 합니다. A 자재를 컨베이어로 옮깁니다. 놓을 자리는 컨베이어 1번 칸입니다. 이대로 작업을 진행할까요?');
    expect(confirms(calls)).toHaveLength(0);
    stt.say('wake', { kind: 'final', text: '네, 진행해', confidence: 0.9 });
    await expect.poll(() => confirms(calls).map((c) => c.body.action)).toEqual(['confirm']);
    await expect.poll(() => spoken(page)).toContain('네, 작업을 시작하겠습니다.');
    await expect(panel(page).getByRole('button', { name: '실행 승인' })).toHaveCount(0);
  });

  test('[UI-VAPP-02] "아니, 취소해" → 같은 취소 API → 취소 확정 뒤 "작업을 취소하겠습니다"', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60), confirm: { decision: 'CANCELLED' } });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    stt.say('wake', { kind: 'final', text: '아니 취소해', confidence: 0.9 });
    await expect.poll(() => confirms(calls).map((c) => c.body.action)).toEqual(['cancel']);
    await expect.poll(() => spoken(page)).toContain('알겠습니다. 작업을 취소하겠습니다.');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    expect(await spoken(page)).not.toContain('네, 작업을 시작하겠습니다.');
  });

  test('[UI-VAPP-03] 불명확·긍정/부정이 섞인 말·신뢰도 낮은 "네"는 실행하지 않고 다시 묻는다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    for (const text of ['글쎄요', '진행하지 마', '네 아니 취소']) {
      const before = (await spoken(page)).filter((t) => t === '작업을 진행할지 취소할지 다시 말씀해 주세요.').length;
      stt.say('wake', { kind: 'final', text, confidence: 0.9 });
      await expect.poll(async () => (await spoken(page)).filter((t) => t === '작업을 진행할지 취소할지 다시 말씀해 주세요.').length).toBe(before + 1);
      await listening(page);
    }
    stt.say('wake', { kind: 'clarify', text: '네', confidence: 0.4 });                 // 낮은 신뢰도 '네'로는 실행하지 않는다
    await expect.poll(async () => (await spoken(page)).filter((t) => t.startsWith('작업을 진행할지')).length).toBe(4);
    expect(confirms(calls)).toHaveLength(0);
    await listening(page);
    stt.say('wake', { kind: 'final', text: '응', confidence: 0.9 });
    await expect.poll(() => confirms(calls).map((c) => c.body.action)).toEqual(['confirm']);
  });

  test('[UI-VAPP-04] 무응답이면 카드 만료와 함께 취소 — 자동 실행 없음', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(4) });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    await expect.poll(() => spoken(page), { timeout: 10000 }).toContain('확인 시간이 지나 작업을 취소했습니다.');
    await expect(status(page)).toContainText('WAKE_WORD_WAITING');
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });                   // 만료 뒤 '네'는 승인이 아니다
    await page.waitForTimeout(500);
    expect(confirms(calls)).toHaveLength(0);
  });

  test('[UI-VAPP-05] 음성 승인과 버튼이 겹쳐도 승인 요청은 한 번', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await page.route('**/v1/sim-demo/confirm', async (route) => {             // 서버 응답을 늦춰 겹치게 한다
      calls.push({ method: 'POST', path: '/v1/sim-demo/confirm', body: route.request().postDataJSON() });
      await new Promise((r) => { setTimeout(r, 800); });
      return route.fulfill({ json: SAMPLES.run });
    });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });
    await panel(page).getByRole('button', { name: '실행 승인' }).click({ timeout: 2000 }).catch(() => {});
    stt.say('wake', { kind: 'final', text: '진행해', confidence: 0.9 });
    await expect.poll(() => spoken(page)).toContain('네, 작업을 시작하겠습니다.');
    await page.waitForTimeout(500);
    expect(confirms(calls)).toHaveLength(1);
  });

  test('[UI-VAPP-06] 안내 음성 중에는 마이크를 보내지 않고, 안내 전·중 발화(옛 세대)는 승인으로 쓰지 않는다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await fakeTts(page, 1500);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야 A 자재를 컨베이어로 옮겨줘 응 진행해', confidence: 0.9 }); // 명령 속 '응 진행해'
    await expect.poll(() => page.evaluate(() => window.__speaking || ''), { timeout: 8000 }).toContain('이대로 작업을 진행할까요?');
    await expect(voice(page)).toContainText('안내 음성 재생 중 — 이 동안은 음성 ‘정지’를 듣지 않습니다');
    const frames = stt.frames.wake;
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });                   // 안내 중 들린 자기 목소리
    await page.waitForTimeout(600);
    expect(stt.frames.wake).toBe(frames);                                              // 재생 중 마이크 프레임 없음
    const old = stt.epoch();
    await listening(page);
    expect(stt.epoch()).toBeGreaterThan(old);                                          // 안내가 끝나면 그 사이 발화를 버린다
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9, epoch: old });       // 늦게 도착한 옛 세대 결과
    await page.waitForTimeout(500);
    expect(confirms(calls)).toHaveLength(0);
    await expect(voice(page)).not.toContainText('안내 음성 재생 중');
  });

  test('[UI-VAPP-07] 승인 대기 중 정지어가 먼저 — "네, 정지"도 승인이 아니라 정지', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    stt.say('wake', { kind: 'final', text: '네, 정지', confidence: 0.9 });
    await expect(status(page)).toContainText('STOPPED');
    await expect.poll(() => calls.filter((c) => c.path.endsWith('/stop')).length).toBe(1);
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });
    await page.waitForTimeout(500);
    expect(confirms(calls)).toHaveLength(0);
  });

  test('[UI-VAPP-08] 서버가 거부하면 시작했다고 말하지 않고 사유를 안내한다 · 차단(BLOCK)은 승인 단계로 가지 않는다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.confirm(60) });
    await page.route('**/v1/sim-demo/confirm', (route) => {
      calls.push({ method: 'POST', path: '/v1/sim-demo/confirm', body: route.request().postDataJSON() });
      return route.fulfill({ status: 409, json: { detail: '정지 래치가 걸려 있습니다' } });
    });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await toApproval(page, stt);
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });
    await expect.poll(async () => (await spoken(page)).find((t) => t.startsWith('작업을 시작하지 못했습니다.')) || '').toContain('정지 래치가 걸려 있습니다');
    expect(await spoken(page)).not.toContain('네, 작업을 시작하겠습니다.');
  });

  test('[UI-VAPP-09] 차단(BLOCK)·되묻기(ASK)는 계획 승인으로 넘어가지 않고 사유·질문을 안내한다', async ({ page }) => {
    const calls = await mockBackend(page, { command: SAMPLES.block });
    await fakeTts(page);
    const stt = await mockStt(page);
    await page.goto('/');
    await turnOn(page, stt);
    stt.say('wake', { kind: 'final', text: '지니야 이상한 데로 옮겨', confidence: 0.9 });
    await expect.poll(async () => (await spoken(page)).find((t) => t.startsWith('작업을 진행할 수 없습니다.')) || '').toContain('목적지가 사용 금지 자리라');
    expect((await spoken(page)).some((t) => t.includes('이대로 작업을 진행할까요?'))).toBe(false);
    stt.say('wake', { kind: 'final', text: '네', confidence: 0.9 });
    await page.waitForTimeout(400);
    expect(confirms(calls)).toHaveLength(0);
  });
});
