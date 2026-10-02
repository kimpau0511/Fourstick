// 공통 레이아웃·홈·로봇 관리·기록·진단·설정 — 화면설계서 가~아, 상태매트릭스, 용어 매핑표
import { expect, test } from '@playwright/test';
import { fixture, mockBackend } from '../mock.js';

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

  test('[UI-LAYOUT-03][가-3] 명령 패널 폭을 드래그로 조절할 수 있다', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('separator', { name: /명령 패널/ })).toBeVisible();
  });

  test('[UI-LAYOUT-04][가-4·§15] 사이드바 System status 위젯 4행(ROS 2·PLANNER·SAFETY PLC·LATENCY) + "N/4 정상"', async ({ page }) => {
    await page.goto('/');
    const side = page.locator('aside.sidebar');
    for (const row of ['ROS 2', 'PLANNER', 'SAFETY PLC', 'LATENCY']) await expect(side).toContainText(row);
    await expect(side).toContainText(/\d\/4 정상/);
  });
});

test.describe('현황(홈)', () => {
  test('[UI-HOME-01][①·§1] 로봇 상태는 색+모양 이중 구분(●◆▲■?)', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('.robot').first().locator('[data-shape]')).toBeVisible();
  });

  test('[UI-HOME-02][①·용어표] 로봇 카드 1차 표시는 셀 위치·별칭(모델명 UR5e/FR3 아님)', async ({ page }) => {
    await page.goto('/');
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
    await expect(page.getByRole('alert').filter({ hasText: /긴급/ })).toBeVisible(); // 축소될 뿐 사라지지 않는다
    await page.locator('header').getByRole('button', { name: /즉시 정지/ }).click({ trial: true }); // 가려지지 않는다
  });

  test('[UI-HOME-06][⑥·§11] 사이드바 로봇 관리 배지 "가동 N/전체"', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('nav.nav')).toContainText(/가동\s*\d+\s*\/\s*\d+/);
  });
});

test.describe('비상 정지·용어', () => {
  test('[UI-ESTOP-01][SFR-009·DEV-06] 정지 버튼은 시뮬레이션 창이 열려 있어도 보이고 눌린다', async ({ page }) => {
    await page.goto('/');
    await page.locator('.demo-bar').getByRole('button', { name: '안전 검사 중' }).click();
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

  test('[UI-ROBOTS-03][⑬-2] 상세 탭 3개: 개요·도구 장착 이력·프로파일 버전', async ({ page }) => {
    await page.goto('/#/robots');
    // 2026-10-02 사용자 결정: 상세 탭은 우측 창의 '상세' 버튼을 눌러야 펼친다.
    const more = page.getByRole('button', { name: '상세', exact: true });
    await expect(more).toHaveAttribute('aria-expanded', 'false');
    await expect(page.getByRole('tab')).toHaveCount(0);
    await more.click();
    for (const tab of ['개요', '도구 장착 이력', '프로파일 버전']) await expect(page.getByRole('tab', { name: tab })).toBeVisible();
  });
  test('[UI-ROBOTS-04][2026-10-02 결정] 우측 창은 좌측 목록과 겹치는 항목(이름·셀·도구·운용 가능 여부)을 되풀이하지 않는다', async ({ page }) => {
    await page.goto('/#/robots');
    const detail = page.getByLabel('로봇 상세');
    await expect(detail).toContainText('현재 상태');
    for (const dup of ['현재 도구', '현재 셀', '운용 가능 여부']) await expect(detail).not.toContainText(dup);
  });
});

test.describe('기록', () => {
  test('[UI-RECORD-01][⑮·§14] 필터 두 축 분리: 검증 결과 / 실행 결과', async ({ page }) => {
    await page.goto('/#/history');
    await expect(page.getByRole('group', { name: /검증 결과/ })).toBeVisible();
    await expect(page.getByRole('group', { name: /실행 결과/ })).toBeVisible();
  });

  test('[UI-RECORD-02][⑯-4] 명령 원문·ID 텍스트 검색', async ({ page }) => {
    await page.goto('/#/history');
    await expect(page.getByRole('searchbox')).toBeVisible();
  });

  test('[UI-RECORD-03][⑭·⑯-2] 명령 1건 상세 타임라인(계획→시뮬레이션→허가→실행)', async ({ page }) => {
    await page.goto('/#/history');
    await page.getByRole('table').getByRole('row').nth(1).click(); // 첫 데이터 행(머리 행 다음)
    for (const step of ['계획', '시뮬레이션', '허가', '실행']) await expect(page.locator('.col-main')).toContainText(step);
    await expect(page.locator('.col-main')).toContainText(/정지 요청 없음|STOP/);
  });
});

test.describe('진단·설정·공통', () => {
  test('[UI-DIAG-01][⑰·⑲-2·§15] 서비스 상태 목록(정상/경고/장애/수신없음)', async ({ page }) => {
    await page.goto('/#/diagnostics');
    await expect(page.locator('.col-main')).toContainText(/ROS 2|PLANNER/);
    await expect(page.locator('.col-main')).toContainText(/마지막 정상|최근 점검/);
  });

  test('[UI-DIAG-02][⑲-5] 진단자료 내보내기', async ({ page }) => {
    await page.goto('/#/diagnostics');
    await expect(page.getByRole('button', { name: /내보내기/ })).toBeVisible();
  });

  test('[UI-SET-01][⑳·§16] 설정 3구역: 개인 / 운영 / 안전·권한', async ({ page }) => {
    await page.goto('/#/settings');
    const main = page.locator('.col-main');
    await expect(main).toContainText('개인');
    await expect(main).toContainText('운영');
    await expect(main).toContainText(/안전\s*·\s*권한/);
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
