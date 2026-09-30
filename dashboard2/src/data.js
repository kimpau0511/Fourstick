// 피그마 v3(58:2) 목업 데이터. 백엔드와 연결된 값은 Gazebo 장면 영상과 명령 패널(simCommand.js)이다.

export const metrics = [
  { label: '공정 종합 가동률', value: '94.2%', badge: '+1.5%', tone: 'ok' },
  { label: '금일 총 목표 생산량', value: '1,840', unit: ' / 2,000', badge: '88%', tone: 'ok' },
  { label: '안전 검증 통과 지수', value: '100%', badge: '경고 2건', tone: 'warn' },
];

export const robots = [
  { name: '포스틱 Alpha', meta: 'RT-A01 · 6축 관절', state: '작업중', tone: 'info', task: '부품 정밀 적재 이송', progress: 0.7, load: '12.5 kg', temp: '38.4°C' },
  { name: '포스틱 Beta', meta: 'RT-B02 · 협동형', state: '위험 감속', tone: 'warn', task: '박스 실적 패키징', progress: 0.7, load: '4.2 kg', temp: '46.1°C', tempTone: 'warn' },
  { name: '포스틱 Gamma', meta: 'RT-C03 · 팔레타이저', state: '대기중', tone: 'idle', task: '지시 대기 중', progress: 0, load: '0.0 kg', temp: '31.2°C' },
];

export const history = [
  { id: '#0412', task: 'A구역 적재 파레트 비우고 B라인으로 정밀 이송', robot: '포스틱 Alpha', check: 'PASS (안전)', checkTone: 'ok', status: '완료', statusTone: 'ok' },
  { id: '#0411', task: '중량물 적재함 덤프 및 복귀 수동 조작 시퀀스', robot: '포스틱 Beta', check: 'PASS (안전)', checkTone: 'ok', status: '완료', statusTone: 'ok' },
  { id: '#0410', task: 'C라인 부품 정밀 조립 및 가동 테스트 수행', robot: '포스틱 Alpha', check: 'PASS (안전)', checkTone: 'ok', status: '완료', statusTone: 'ok' },
  { id: '#0409', task: '경로 상 미확인 장애물 회피 긴급 경로 재배치', robot: '포스틱 Beta', check: 'ADJUSTED (보정)', checkTone: 'warn', status: '정지', statusTone: 'danger' },
];

export const alerts = [
  { level: '위험', tone: 'danger', robot: '포스틱 Beta', ago: '1분 전', text: '안전 펜스 영역 2 진입 발생 - 동작 속도 20%로 긴급 자동 감속됨' },
  { level: '경고', tone: 'warn', robot: '포스틱 Beta', ago: '5분 전', text: '동작 관절 온도 위험 임계치 근접 (46°C) - 쿨링 팬 기동' },
  { level: '정보', tone: 'info', robot: '포스틱 Alpha', ago: '15분 전', text: '새로운 경로 이송 계획 안전 적합성 테스트 통과 완료' },
];

// ── 로봇 관리(58:268) ──
export const robotDetails = [
  { name: '포스틱 Alpha (선택됨)', meta: 'RT-A01 | 6축 다관절 로봇', state: '작업중', tone: 'info', selected: true, step: '정밀 부품 진공 안착 및 적재', progress: 0.95, barTone: 'info', battery: '94% (정상)', batteryTone: 'ok', signal: ['-54 dBm', '(우수)'] },
  { name: '포스틱 Beta', meta: 'RT-B02 | 협동형 로봇', state: '위험 감속', tone: 'warn', step: '완료 완충 박스 테이핑', progress: 0.45, barTone: 'warn', battery: '88% (정상)', batteryTone: 'ok', signal: ['-76 dBm', '(지연 위험)'], signalTone: 'warn' },
  { name: '포스틱 Gamma', meta: 'RT-C03 | 대형 팔레타이저', state: '대기중', tone: 'idle', step: '이송 지시 대기', progress: 0.005, barTone: 'idle', battery: '100% (완충)', signal: ['-48 dBm', '(매우 우수)'], signalTone: 'ok' },
];

export const tool = {
  title: '포스틱 Alpha (RT-A01) 장착 엔드 이펙터 및 도구 사양',
  name: '정밀 공압 진공 그리퍼',
  model: '모델명: VG-PR-09 | 흡착식 센서 내장',
  range: '정상 압착 압력 범위: 65 kPa',
  measured: '현재 실시간 측정 압력: 62.8 kPa',
  status: '도구 보정 상태 양호',
};

export const toolHistory = [
  { at: '2024.10.15 08:30', tool: '진공 흡착 패드 교체', note: '소모성 고무 패드 마모로 인한 정밀 진공 패드 전량 교체 조치 완료', by: '이영수 사원' },
  { at: '2024.09.28 14:15', tool: '2지 병렬 기계식 그리퍼', note: '전체 관절 힌지 윤활유 도포 및 파지 토크 테스트 검증 정상 완료', by: '김민우 조장' },
  { at: '2024.08.12 11:00', tool: '3D 비전 카메라 모듈', note: '부품 인식 오차율 감소를 위한 레이저 캘리브레이션 정밀 보정 작업 실행', by: '외부 기술팀' },
];

// ── 기록 및 로그(58:465) ──
export const commandLog = [
  { id: '#0412', text: '"A구역 적재 파레트 비우고 Beta 로봇을 B라인으로 즉시 이동시켜줘."', approver: '김민우 조장', sim: 'PASS (안전)', simTone: 'ok', adjust: '정밀 보정 98%', status: '작업 성공', statusTone: 'ok' },
  { id: '#0411', text: '"Beta 로봇 구동 토크 감지 후 조인트 2번 온도 냉각 작동 시퀀스 실행 요청"', approver: '자동화 제어팀', sim: 'PASS (안전)', simTone: 'ok', adjust: '쿨러 가동 100%', status: '작업 성공', statusTone: 'ok' },
  { id: '#0410', text: '"Gamma 팔레타이저 작업 긴급 중지하고 부품 정렬 상태 재탐색할 것"', approver: '김민우 조장', sim: 'PASS (안전)', simTone: 'ok', adjust: '경로 재배치', status: '작업 성공', statusTone: 'ok' },
  { id: '#0409', text: '"안전 가이드라인 라인 내 적재 박스 적재 불일치 강제 자동 보정 시작"', approver: '시스템 비상', sim: 'ADJUSTED (보정)', simTone: 'warn', adjust: '센서 무시 감지', adjustTone: 'warn', status: '긴급 정지', statusTone: 'danger' },
];

// 피그마 v3 메모: 데이터 불일치를 고치지 않고 "확인 필요"로 표시한다.
export const commandLogNote = '※ 확인 필요: #0412 입력 문장의 로봇(Beta)이 현황 화면의 담당 로봇(Alpha)과 다릅니다. 시안 데이터 확인 후 통일하세요.';

// ── 실시간 진단(58:617) ──
export const engineMetrics = [
  { label: '자연어 계획 처리 속도', value: '120 ms', badge: '매우 빠름', tone: 'ok' },
  { label: '센서 데이터 동기화율', value: '99.98%', badge: '안정적', tone: 'ok' },
  { label: '미들웨어 지연 시간', value: '1.4 ms', badge: '지연 없음', tone: 'ok' },
];

export const issues = [
  { tag: '관절온도 높음', tone: 'warn', title: '포스틱 Beta 모터 온도 상승 감지 (동작 보정 상태)', ago: '최근 감지: 5분 전', body: 'Beta 로봇의 2번 회전 관절 동작온도가 한계 임계점(46°C)에 임박했습니다. 일시적 수동 감속 모드가 활성화되었으며, 자체 보정 쿨링 시스템이 가동되고 있습니다.', action: '- 쿨링 기동 10분 후 온도가 40°C 이하로 낮아지지 않을 경우, 수동 셧다운 후 메커니컬 가동부 윤활 점검을 수행하십시오.' },
  { tag: '안전 위협', tone: 'danger', title: '안전 감지 펜스 2 구역 침범 및 긴급 제동 보정', ago: '최근 감지: 15분 전', body: '스마트 비전 감지 결과 안전 구역 바운더리 내에 비인가 물체(적재 박스 낙하 추정)가 식별되었습니다. 협동 로봇 충돌 감지 방지를 위해 일시 속도가 20%로 긴급 조정되었습니다.', action: '- 작업 명령 패널에서 비상 정지(EMERGENCY STOP)를 대기한 상태에서, 해당 구역 내 하적 물품을 정량 제거하고 레이저 라인 센서를 초기화하십시오.' },
];
