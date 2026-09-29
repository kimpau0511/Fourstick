import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// forstick2 백엔드. 대시보드는 장면 영상(/v1/scene*)만 자기 주소로 부르고 개발
// 서버가 넘긴다 — 백엔드에 CORS를 열지 않아도 되고, 화면 코드에 주소를 두지 않는다.
// 이 개발 서버는 LAN(0.0.0.0)에 열려 있고 백엔드에는 아직 인증이 없다. 그래서
// 실행·정지 같은 제어 API는 넘기지 않는다.
// 작업 셀 PC의 https는 자체 서명 인증서라 secure: false로 둔다.
const backend = process.env.FORSTICK2_BACKEND_URL || 'https://192.168.0.175:8443';

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: {
      '/v1/scene': { target: backend, changeOrigin: true, secure: false, ws: true },
    },
  },
});
