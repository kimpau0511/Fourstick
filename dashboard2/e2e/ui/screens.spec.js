// 공통 레이아웃·홈·로봇 관리·기록·진단·설정 — 화면설계서 가~아, 상태매트릭스, 용어 매핑표
import { readFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import { SAMPLES, fixture, mockBackend, sendCommand, startJob } from '../mock.js';

test.beforeEach(async ({ page }) => { await mockBackend(page); });

const ROUTES = ['home', 'robots', 'history', 'diagnostics', 'settings'];

test.describe('공통 레이아웃', () => {
  test('[UI-LAYOUT-01][가-1] 좌측 메뉴 5개: 현황·로봇 관리·기록·진단·설정', async ({ page }) => {
    await page.goto('/');
    const nav = page.locator('nav.nav a');
    await expect(nav).toHaveCount(5);
    const labels = await nav.allTextContents();
    for (const [i, want] of [/홈|현황/, /로봇/, /기록/, /진단/, /설정/].entries()) expect(labels[i]).toMatch(want);
  });

  test('[UI-LAYOUT-02][가-2] 모든 화면이 우측 명령 패널을 공유한다', async ({ page }) => {
    for (const r of ROUTES) {
      await page.goto(`/#/${r}`);
      await expect(page.locator('section.command'), r).toBeVisible();
    }
  });

  test('[UI-LAYOUT-03][가-3] 명령 패널 폭을 키보드(←/→)로 조절하고 320~560으로 제한된다', async ({ page }) => {
    await page.goto('/');
    const sep = page.getByRole('separator', { name: /명령 패널/ });
    const cmdW = () => page.locator('.app').evaluate((el) => el.style.getPropertyValue('--cmd-w'));
    await expect(sep).toHaveAttribute('aria-valuenow', '400');
    await sep.focus();
    await page.keyboard.press('ArrowLeft'); // 왼쪽 = 패널이 넓어진다
    await expect(sep).toHaveAttribute('aria-valuenow', '416');
    expect(await cmdW()).toBe('416px');
    for (let i = 0; i < 20; i++) await page.keyboard.press('ArrowLeft');
    await expect(sep).toHaveAttribute('aria-valuenow', '560'); // 최대
    for (let i = 0; i < 40; i++) await page.keyboard.press('ArrowRight');
    await expect(sep).toHaveAttribute('aria-valuenow', '320'); // 최소
    expect(await cmdW()).toBe('320px');
  });

  test('[UI-LAYOUT-04][가-4·§15] 사이드바 System status 위젯 3행(ROS 2·PLANNER·LATENCY) + "N/3 정상" — SAFETY PLC는 서버 신호가 없어 삭제(2026-10-07)', async ({ page }) => {
    await page.goto('/');
    const side = page.locator('aside.sidebar');
    for (const row of ['ROS 2', 'PLANNER', 'LATENCY']) await expect(side).toContainText(row);
    await expect(side).not.toContainText('SAFETY PLC');
    await expect(side).toContainText(/\d\/3 정상/);
  });
});

test.describe('현황(홈)', () => {
  test('[UI-HOME-01][①·§1] 로봇 상태는 색+모양 이중 구분(●◆▲■?)', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.robot').first().locator('[data-shape]')).toBeVisible();
  });

  test('[UI-HOME-02][①·용어표] 로봇 카드 1차 표시는 셀 위치·별칭(모델명 UR5e/FR3 아님)', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.robot-head strong').first()).toBeVisible(); // 로그인 확인 뒤에 대시보드가 그려진다
    const titles = await page.locator('.robot-head strong').allTextContents();
    expect(titles.length).toBeGreaterThan(0);
    for (const t of titles) expect(t).not.toMatch(/UR5e|FR3/);
  });

  test('[UI-HOME-03][②·§2] 안전 3종(비상정지·보호정지·가드) 개별 표시 영역이 있다', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.col-main')).toContainText(/보호정지|가드/);
  });

  test('[UI-HOME-04][③·§3] 정상 범위 연속값은 원시 수치 없이 "정상" 배지만', async ({ page }) => {
    await page.goto('/');
    // 목업의 Alpha 관절 온도 38.4°C는 정상 범위 — 수치가 카드에 보이면 규칙 위반
    await expect(page.locator('.robot').first()).not.toContainText('38.4°C');
  });

  test('[UI-HOME-05][④⑤·§5·6] 긴급 배너(긴급 N건 집계, 닫기 없음, 정지 버튼 비가림)', async ({ page }) => {
    // 긴급 상황 = 서버의 정지 래치 활성(IMPLEMENTATION_SPEC alerts). 기본 고정 데이터엔 없어서 여기서 켠다.
    const stop = fixture('robots').stop_diagnostics;
    await mockBackend(page, { overrides: { robots: { stop_diagnostics: { ...stop, stop_latch_active: true } } } });
    await page.goto('/');
    const banner = page.getByRole('alert').filter({ hasText: /긴급\s*\d+건/ });
    await expect(banner).toBeVisible();
    await expect(banner.getByRole('button', { name: /닫기|×/ })).toHaveCount(0);
    await banner.getByRole('button', { name: /인지 확인/ }).click();
    await expect(page.getByRole('alert').filter({ hasText: /긴급\s*\d+건/ })).toBeVisible(); // 축소될 뿐 사라지지 않는다
    await expect(page.getByRole('button', { name: '다시 펼치기' })).toBeVisible(); // 접힌 뒤에도 되돌릴 수 있다
    await page.getByRole('button', { name: '다시 펼치기' }).click();
    await expect(page.getByRole('button', { name: /인지 확인/ })).toBeVisible();
    await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click({ trial: true }); // 가려지지 않는다
  });

  test('[UI-HOME-07][①] 홈 로봇 카드 진행 막대는 이 화면이 따라가는 작업일 때만 그린다(서버 running_job에는 진행률이 없다)', async ({ page }) => {
    const running = { job_id: 'qa-job', action: 'transfer', action_label: '컨베이어로 이송', status: 'running' };
    await mockBackend(page, { overrides: { simDemo: { running_job: running } } });
    await page.goto('/');
    await expect(page.locator('.robot').first()).toContainText('컨베이어로 이송'); // 다른 탭에서 시작한 작업 — 막대 없음
    await expect(page.locator('.robot').getByRole('progressbar')).toHaveCount(0);
  });

  test('[UI-HOME-08][①] 이 화면에서 시작한 작업이면 서버 progress(reached/전체)로 진행 막대를 그린다', async ({ page }) => {
    const running = { job_id: 'qa-job', action: 'transfer', action_label: '컨베이어로 이송', status: 'running' };
    await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job], overrides: { simDemo: { running_job: running } } });
    await page.goto('/');
    await sendCommand(page, 'A 자재를 컨베이어로 옮겨줘');
    await page.locator('section.command').getByRole('button', { name: '실행 승인' }).click();
    const bar = page.locator('.robot').getByRole('progressbar', { name: '작업 진행률' });
    await expect(bar).toBeVisible();
    await expect(bar).toHaveAttribute('aria-valuenow', '33'); // 3단계 중 1단계 도달
  });

  test('[UI-HOME-06][⑥·§11] 사이드바 로봇 관리 배지 "가동 N/전체"', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('nav.nav')).toContainText(/가동\s*\d+\s*\/\s*\d+/);
  });
});

test.describe('비상 정지·용어', () => {
  test('[UI-ESTOP-01][SFR-009·DEV-06] 정지 버튼은 시뮬레이션 창이 열려 있어도 보이고 눌린다', async ({ page }) => {
    await mockBackend(page, { command: SAMPLES.confirm(60), jobs: [SAMPLES.run.job] });
    await page.goto('/');
    await startJob(page);
    await expect(page.locator('.sim-card')).toBeVisible();
    const stop = page.locator('header').getByRole('button', { name: /정지/ });
    await expect(stop).toBeVisible();
    await stop.click({ trial: true }); // 가려져 있으면 실패한다
  });

  test('[UI-ESTOP-02][㉓·용어표 D4] UI STOP 버튼 문구는 "즉시 정지"(비상정지는 물리 E-stop 상태 표시 전용)', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header').getByRole('button', { name: /즉시 정지/ })).toBeVisible();
  });

  test('[UI-TERM-01][용어표] 화면 1차 문구에 내부값 PASS/BLOCK/ASK를 쓰지 않는다', async ({ page }) => {
    for (const r of ROUTES) {
      await page.goto(`/#/${r}`);
      const text = await page.locator('.col-main').innerText();
      expect(text, r).not.toMatch(/\b(PASS|BLOCK|ASK)\b/);
    }
  });
});

test.describe('로봇 관리', () => {
  test('[UI-ROBOTS-01][⑬-1] 목록 + 선택 시 우측 상세 패널 구조', async ({ page }) => {
    await page.goto('/#/robots');
    await expect(page.getByRole('table', { name: /로봇/ }).or(page.getByRole('list', { name: /로봇/ }))).toBeVisible();
  });

  test('[UI-ROBOTS-02][⑫·§12] 운용 가능 여부는 결과값으로 표시, 켜고 끄는 토글 없음', async ({ page }) => {
    await page.goto('/#/robots');
    await expect(page.getByRole('switch')).toHaveCount(0);
    await expect(page.getByRole('checkbox')).toHaveCount(0);
    await expect(page.locator('.col-main')).toContainText(/운용 가능|운용 불가/);
  });

  test('[UI-ROBOTS-06][2026-10-07 요청] 목록 머리글은 기록 화면과 같은 "로봇", 운용 가능은 초록·운용 불가는 빨강', async ({ page }) => {
    await page.goto('/#/robots');
    const table = page.getByRole('table', { name: '로봇 목록' });
    await expect(table.locator('thead th').first()).toHaveText('로봇');
    const cell = table.locator('tbody td').getByText(/^운용 (가능|불가)$/);
    const text = await cell.textContent();
    await expect(cell).toHaveCSS('color', text === '운용 가능' ? 'rgb(6, 95, 70)' : 'rgb(168, 25, 25)');
  });

  test('[UI-ROBOTS-03][⑬-2] 상세 탭 3개: 개요·도구 장착 이력·프로파일 버전', async ({ page }) => {
    await page.goto('/#/robots');
    // 2026-10-02 사용자 결정: 상세 탭은 우측 창의 '상세' 버튼을 눌러야 펼친다.
    const more = page.getByRole('button', { name: '상세', exact: true });
    await expect(more).toHaveAttribute('aria-expanded', 'false');
    // 화면 탭(로봇 목록 / 구성 · 작업 셀)은 늘 보이므로 상세 패널 안의 탭만 센다(2026-10-06).
    const detail = page.getByLabel('로봇 상세');
    await expect(detail.getByRole('tab')).toHaveCount(0);
    await more.click();
    for (const tab of ['개요', '도구 장착 이력', '프로파일 버전']) await expect(detail.getByRole('tab', { name: tab })).toBeVisible();
  });
  test('[UI-ROBOTS-04][2026-10-02 결정] 우측 창은 좌측 목록과 겹치는 항목(이름·셀·도구·운용 가능 여부)을 되풀이하지 않는다', async ({ page }) => {
    await page.goto('/#/robots');
    const detail = page.getByLabel('로봇 상세');
    await expect(detail).toContainText('현재 상태');
    for (const dup of ['현재 도구', '현재 셀', '운용 가능 여부']) await expect(detail).not.toContainText(dup);
  });
});

test.describe('기록', () => {
  // 목 서버 recent_jobs(고정 응답) 4건: 성공 3(복구·이송·정합) · 정지됨 1(복귀 중 정지). 이 탭에서 보낸 명령은 없다.
  const historyRows = (page) => page.getByRole('table', { name: '명령 기록' }).locator('tbody tr');

  test('[UI-RECORD-01][⑮·§14] 필터 두 축 분리: 검증 결과 / 실행 결과 — 누르면 행이 줄고, 실행 차단이면 실행 결과는 잠긴다', async ({ page }) => {
    await page.goto('/#/history');
    const verify = page.getByRole('group', { name: /검증 결과/ });
    const exec = page.getByRole('group', { name: /실행 결과/ });
    await expect(verify).toBeVisible();
    await expect(exec).toBeVisible();
    await expect(historyRows(page)).toHaveCount(4);
    await exec.getByRole('button', { name: 'STOP' }).click();
    await expect(historyRows(page)).toHaveCount(1);
    await exec.getByRole('button', { name: '성공' }).click();
    await expect(historyRows(page)).toHaveCount(3);
    await verify.getByRole('button', { name: '실행 차단' }).click();
    for (const button of await exec.getByRole('button').all()) await expect(button).toBeDisabled(); // 차단된 명령은 실행 결과가 없다
  });

  test('[UI-RECORD-02][⑯-4] 명령 원문·ID 텍스트 검색 — 입력하면 행 수가 줄어든다', async ({ page }) => {
    await page.goto('/#/history');
    const search = page.getByRole('searchbox');
    await expect(search).toBeVisible();
    await expect(historyRows(page)).toHaveCount(4);
    await search.fill('이송');
    await expect(historyRows(page)).toHaveCount(1);
    await search.fill('simjob_5d6d97a38a78'); // 작업 ID로도 찾는다
    await expect(historyRows(page)).toHaveCount(1);
    await search.fill('없는 명령 zzz');
    await expect(historyRows(page)).toHaveCount(0);
    await expect(page.locator('.col-main')).toContainText('조건에 맞는 기록이 없습니다');
  });

  test('[UI-RECORD-03][⑭·⑯-2] 명령 1건 상세 타임라인(계획→시뮬레이션→허가→실행) — 행을 눌러 연다', async ({ page }) => {
    await page.goto('/#/history');
    await expect(page.getByRole('region', { name: '명령 상세' })).toHaveCount(0);
    await historyRows(page).first().click(); // 가장 최근 작업(복구)
    const detail = page.getByRole('region', { name: '명령 상세' });
    for (const step of ['계획', '시뮬레이션', '허가', '실행']) await expect(detail.locator('.timeline')).toContainText(step);
    await expect(detail.locator('.timeline')).toContainText('작업 ID simjob_1b05a6710b83'); // 서버 작업이면 시작 시각·작업 ID
    await expect(detail.locator('.timeline')).toContainText(/정지 요청 없음|STOP/);
  });
});

test.describe('진단·설정·공통', () => {
  test('[UI-DIAG-01][⑰·⑲-2·§15] 서비스 상태 목록(정상/경고/장애/수신없음)', async ({ page }) => {
    await page.goto('/#/diagnostics');
    await expect(page.locator('.col-main')).toContainText(/ROS 2|PLANNER/);
    await expect(page.locator('.col-main')).toContainText(/마지막 정상|최근 점검/);
  });

  test('[UI-DIAG-02][⑲-5] 진단자료 내보내기 — 완료 행·다운로드, 해제한 항목은 payload에서 빠지고 전부 해제하면 버튼이 잠긴다', async ({ page }) => {
    await page.goto('/#/diagnostics');
    const exportBtn = page.getByRole('button', { name: '진단자료 내보내기' });
    const items = page.getByRole('group', { name: '내보낼 항목' });
    await expect(exportBtn).toBeVisible();
    for (const name of ['연결 상태', '서비스 상태', '상태 변화 이력', '서버 상태', '정책', '최근 작업']) {
      await expect(items.getByRole('checkbox', { name: new RegExp(name) })).toBeChecked(); // 기본 전부 체크
    }
    await items.getByRole('checkbox', { name: '정책' }).uncheck();
    await exportBtn.click();
    const jobs = page.getByRole('table', { name: '진단자료 내보내기 작업' });
    await expect(jobs).toContainText('완료');
    const [download] = await Promise.all([page.waitForEvent('download'), jobs.getByRole('button', { name: /JSON 다운로드/ }).click()]);
    const payload = JSON.parse(readFileSync(await download.path(), 'utf-8'));
    expect(payload.included).not.toContain('policies');
    expect(payload.included).toContain('services');
    expect(payload).not.toHaveProperty('policies');
    expect(payload).toHaveProperty('services');
    for (const checkbox of await items.getByRole('checkbox').all()) await checkbox.uncheck();
    await expect(exportBtn).toBeDisabled();
  });

  test('[UI-DIAG-03][⑲-3] 서비스 행의 원시 샘플은 마지막 정상과 현재를 함께 보인다(본 적 없으면 그렇게 적는다)', async ({ page }) => {
    await page.goto('/#/diagnostics');
    const row = page.locator('#svc-camera'); // 모의 서버가 장면 카메라를 주지 않아(503) 늘 '수신 없음'
    await row.getByRole('button', { name: '문제 전후 원시 샘플 보기' }).click();
    await expect(row.locator('pre')).toContainText('마지막 정상');
    await expect(row.locator('pre')).toContainText('이 화면이 본 적 없음');
    await expect(row.locator('pre')).toContainText('현재');
  });

  test('[UI-SET-01][⑳·§16] 설정 3구역: 개인 / 운영 / 안전·권한', async ({ page }) => {
    await page.goto('/#/settings');
    const main = page.locator('.col-main');
    await expect(main).toContainText('개인');
    await expect(main).toContainText('운영');
    await expect(main).toContainText(/안전\s*·\s*권한/);
  });

  test('[UI-SET-03][㉒-2] 저장 루틴: 검색·기본 대상·사용 스킬·적용 가능 로봇 열', async ({ page }) => {
    await page.goto('/#/settings');
    await page.getByRole('button', { name: '새 루틴' }).click();
    await page.getByRole('textbox', { name: '이름' }).fill('컨베이어 이송');
    await page.getByRole('textbox', { name: '명령 문장' }).fill('A자재를 컨베이어로 옮겨줘');
    const table = page.getByRole('table', { name: '저장 루틴 목록' });
    // 상세 창이 옆에 열리면 표 카드가 좁아져 이 열들은 행 펼침으로 들어간다(반응형) — 열이 있는지만 본다.
    for (const col of ['사용 스킬', '적용 가능 로봇']) await expect(table.getByRole('columnheader', { name: col, includeHidden: true })).toBeAttached();
    const row = table.locator('tbody tr').first();
    await expect(row).toContainText('이송'); // 화면 추정(실행 때 서버가 다시 판정)
    await expect(row).toContainText('실행 시 선택');
    const target = page.getByRole('combobox', { name: '기본 대상' });
    await target.selectOption({ index: 1 }); // 0 = 지정 안 함, 1 = 서버 로봇
    const label = await target.evaluate((el) => el.selectedOptions[0].textContent);
    await expect(row).toContainText(label);
    await expect(row).not.toContainText('실행 시 선택');
    const search = page.getByRole('searchbox', { name: '루틴 검색' });
    await search.fill('컨베이어');
    await expect(table.locator('tbody tr')).toHaveCount(1);
    await search.fill('없는 루틴 zzz');
    await expect(page.locator('.col-main')).toContainText('검색 결과가 없습니다');
  });

  test('[UI-SET-02][㉑·§17] 안전 임계값 변경은 상태머신(작성 중→검사 중→승인 필요→적용 예약→사용 중)', async ({ page }) => {
    await page.goto('/#/settings');
    // 상태머신은 '안전·권한' 구역의 요구다(㉒-4) — 그 구역을 연 뒤 본다.
    await page.getByRole('button', { name: /안전\s*·\s*권한/ }).click();
    await expect(page.locator('.col-main')).toContainText(/승인 필요|적용 예약/);
  });

  test('[UI-COMMON-01][㉔·§7] 연결 상태·데이터 수신 시각 전역 표시', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('header')).toContainText(/수신|연결/);
  });

  test('[UI-COMMON-02][㉕·§9] 제어권·운전 모드 표시', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('body')).toContainText(/제어권|제어중|운전 모드|REMOTE|원격/);
  });
});

test.describe('서버 이력(/v1/history)', () => {
  const now = Math.floor(Date.now() / 1000);
  const history = {
    commands: [
      { request_id: 'req-1', utterance: '팔레트 1의 A자재를 집어 컨베이어에 놓아줘', created_at: now - 120, decision: 'allow', execution_id: 'exe-1', execution_started_at: now - 110, final_state: 'stopped' },
      { request_id: 'req-2', utterance: '금지 구역으로 옮겨', created_at: now - 300, decision: 'block', execution_id: null, execution_started_at: null, final_state: null },
      { request_id: 'req-3', utterance: '관측 끊긴 실행', created_at: now - 400, decision: 'allow', execution_id: 'exe-3', execution_started_at: now - 390, final_state: 'unknown' },
    ],
    sim_jobs: [
      { job_id: 'simjob_file1', action: 'transfer', action_label: '컨베이어로 이송', material: 'material_a', status: 'simulation_transfer_completed', written_at: new Date((now - 600) * 1000).toISOString() },
      { job_id: 'simjob_file2', action: 'transfer', action_label: '컨베이어로 이송', material: 'material_c', status: 'some_internal_code', written_at: new Date((now - 700) * 1000).toISOString() },
      // 이력 API가 한국어 이름을 못 준 작업(action_label = 내부 이름)
      { job_id: 'simjob_file3', action: 'spot', action_label: 'spot', material: 'material_b', status: 'spot_arrived', written_at: new Date((now - 800) * 1000).toISOString() },
    ],
  };

  test('[UI-RECORD-04][⑭·§14] 서버 요청 기록과 작업 결과 파일이 기록 표에 나오고, 판정·상태는 화면 용어로 바뀐다', async ({ page }) => {
    await mockBackend(page, { overrides: { history, simDemo: { action_labels: { ...fixture('simDemo').action_labels, spot: '지정 위치로 옮기기' } } } });
    await page.goto('/#/history');
    const table = page.getByRole('table', { name: '명령 기록' });
    const row = (name) => table.getByRole('row', { name });
    await expect(row(/A자재를 집어 컨베이어에 놓아줘/)).toContainText('안전 확인됨');
    await expect(row(/A자재를 집어 컨베이어에 놓아줘/)).toContainText('정지 확인됨');
    await expect(row(/금지 구역으로 옮겨/)).toContainText('실행 차단');
    await expect(row(/금지 구역으로 옮겨/)).toContainText('실행 없음');
    await expect(row(/관측 끊긴 실행/)).toContainText('확인 안 됨'); // unknown을 성공으로 바꾸지 않는다
    await expect(table).toContainText('A자재'); // 결과 파일 작업 — 자재 한국어 이름
    await expect(table).toContainText('결과 확인 안 됨'); // 표에 없는 결과 코드는 성공·실패로 지어내지 않는다
    await expect(table).not.toContainText('some_internal_code');
    await expect(row(/지정 위치로 옮기기 · B자재/)).toContainText('지정 위치 도착'); // 내부 이름 spot 대신 서버 한국어 이름·결과
    await expect(table).not.toContainText(/\bspot\b/);
    await expect(table).not.toContainText(/\ballow\b|\bblock\b|\bask\b/);
    // STOP 필터는 서버가 정지를 확인한 요청만 남긴다
    await page.getByRole('group', { name: '실행 결과' }).getByRole('button', { name: 'STOP' }).click();
    // 기본 fixture의 '복귀 중 정지' 작업 + 이 요청 — 정지를 확인한 것만 남고 차단·확인 안 됨 요청은 빠진다
    await expect(table.locator('tbody tr:not(.row-detail)')).toHaveCount(2);
    await expect(row(/금지 구역으로 옮겨/)).toHaveCount(0);
    await expect(row(/관측 끊긴 실행/)).toHaveCount(0);
    await row(/A자재를 집어 컨베이어에 놓아줘/).click();
    const timeline = page.locator('.timeline');
    await expect(timeline).toContainText('명령 원문: 팔레트 1의 A자재를 집어 컨베이어에 놓아줘');
    await expect(timeline.locator('li[data-state="stop"]')).toHaveCount(1);
  });

  test('[UI-RECORD-05][⑭] 이 화면에서 보낸 명령이 서버 요청 기록에도 있으면 한 줄만, 이력 조회 실패는 표시', async ({ page }) => {
    const sent = '두 번 나오면 안 되는 명령';
    await mockBackend(page, { command: { decision: 'BLOCK', reason: 'QA' }, overrides: { history: { commands: [{ request_id: 'req-x', utterance: sent, created_at: Math.floor(Date.now() / 1000), decision: 'block', execution_id: null, final_state: null }] } } });
    await page.goto('/');
    await sendCommand(page, sent);
    await page.getByRole('dialog').getByRole('button', { name: '명령 수정' }).click();
    await page.goto('/#/history');
    await expect(page.getByRole('table', { name: '명령 기록' }).getByRole('row', { name: new RegExp(sent) })).toHaveCount(1);
    await expect(page.getByRole('table', { name: '명령 기록' }).getByRole('row', { name: new RegExp(sent) })).toContainText('실행 없음'); // 차단은 실행 자체가 없다
    await page.route('**/v1/history**', (route) => route.fulfill({ status: 500, json: {} }));
    await page.reload();
    await expect(page.getByText(/서버 이력 조회 실패/)).toBeVisible({ timeout: 15000 });
  });
});
