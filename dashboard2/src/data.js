// 피그마 v3(58:2) 목업 데이터. 백엔드와 연결된 값은 Gazebo 장면 영상뿐이다.

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

export const command = {
  utterance: '"A구역 적재 파레트 비우고 Beta 로봇을 B라인으로 즉시 이동시켜줘."',
  target: '대상 로봇: Beta · 구역: A구역 → B라인',
  plan: [
    { done: true, text: 'A구역 적재물 무게 중심 및 그리퍼 파지 압력 계산' },
    { done: false, text: 'Alpha-Beta 로봇 간 협동 이송 경로 동적 모델링 중' },
  ],
  checks: ['• 이 계획의 충돌 검증: SAFE (범위 한정)', '• Beta 감속·온도 위험 미해결 · 재확인 필요'],
  holdReason: '사유: STEP 2 계획 수립 진행 중 · Beta 안전 펜스 2 경고 미해결',
};
