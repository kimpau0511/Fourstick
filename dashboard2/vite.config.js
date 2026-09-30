import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// forstick2 백엔드. 화면은 장면 영상(/v1/scene*)과 시연 API(/v1/sim-demo*)를 자기
// 주소로 부르고 개발 서버가 넘긴다 — 백엔드에 CORS를 열지 않아도 되고, 화면 코드에
// 주소를 두지 않는다.
// 백엔드에는 아직 인증이 없다. 제어 API(명령·확인·정지)를 넘기므로 개발 서버는
// 이 PC(localhost)에만 연다. LAN에 다시 열거나 외부 데모를 하려면 인증이 먼저다.
// 작업 셀 PC의 https는 자체 서명 인증서라 secure: false로 둔다.
const backend = process.env.FORSTICK2_BACKEND_URL || 'https://192.168.0.175:8443';

export default defineConfig({
  plugins: [react()],
  server: {
    host: 'localhost',
    port: 5175,
    proxy: {
      '/v1/scene': { target: backend, changeOrigin: true, secure: false, ws: true },
      '/v1/sim-demo': { target: backend, changeOrigin: true, secure: false },
    },
  },
});
