import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// forstick2 백엔드. 화면은 장면 영상(/v1/scene*)만 자기 주소로 부르고 개발 서버가
// 넘긴다 — 백엔드에 CORS를 열지 않아도 되고, 화면 코드에 주소를 두지 않는다.
// 관제 조회와 전체 정지는 같은 백엔드에 전달한다. 전체 정지는 서버가 세션과
// 무관하게 모든 활성 실행에 적용하는 기존 안전 경계다.
// 작업 셀 PC의 https는 자체 서명 인증서라 secure: false로 둔다.
const backend = process.env.FORSTICK2_BACKEND_URL || 'https://192.168.0.175:8443';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5175,
    proxy: {
      '/v1/scene': { target: backend, changeOrigin: true, secure: false, ws: true },
      '/v1/dashboard': { target: backend, changeOrigin: true, secure: false },
      '/v1/stop': { target: backend, changeOrigin: true, secure: false },
    },
  },
});
