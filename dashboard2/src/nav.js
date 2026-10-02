import settings from './assets/settings.svg';
import chartLine from './assets/chart-line.svg';
import circleX from './assets/circle-x.svg';
import house from './assets/house.svg';
import logs from './assets/logs.svg';

export const NAV = [
  { id: 'home', label: '홈', icon: house, title: '종합 관제 대시보드' },
  { id: 'robots', label: '로봇 관리', icon: circleX, title: '로봇 세부 관리 및 기구 정보' },
  { id: 'history', label: '기록 및 로그', icon: logs, title: '명령 수행 이력 및 가상 안전 감사 로그' },
  { id: 'diagnostics', label: '실시간 진단', icon: chartLine, title: '미들웨어 및 서비스 자가 점검 시스템' },
  { id: 'settings', label: '관제 설정', icon: settings, title: '통합 관제 시스템 환경 설정' },
];
