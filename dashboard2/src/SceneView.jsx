import { useEffect, useRef, useState } from 'react';
import Spinner from './components/Spinner.jsx';

/** 실시간 프레임이 없을 때 서버 스냅샷을 다시 받는 간격. 스냅샷 한 장은 서버에서
 *  `gz topic -n 1`로 약 0.5초가 든다(실측 2026-09-28) — 카메라 주기보다 느리게 둔다. */
const SNAPSHOT_INTERVAL_MS = 2000;
/** 서버가 스트림을 닫으면(프레임 없음·재시작) 이 간격으로 다시 붙는다. */
const RECONNECT_MS = 3000;

/** forstick2 백엔드의 Gazebo 장면 카메라(`/v1/scene/stream`)를 그린다.
 *
 *  백엔드 화면(html/static/js/main.js)과 같은 규칙이다: 받은 프레임만 그리고,
 *  실시간이 없으면 서버 스냅샷(/v1/scene.png)으로 대신하며 그 사실을 알린다.
 *  `onView`로 { status, detail, fps, snapshotAt }을 알린다 —
 *  status: 'connecting' | 'live' | 'snapshot' | 'stalled' | 'unavailable'. */
export default function SceneView({ onView }) {
  const canvasRef = useRef(null);
  const [view, setView] = useState({ status: 'connecting', detail: '', fps: 0, snapshotAt: null, drawn: false });

  useEffect(() => { onView?.(view); }, [view, onView]);

  useEffect(() => {
    let socket = null;
    let geometry = null;
    let frames = 0;
    let snapshotActive = false;
    let snapshotTimer = null;
    let reconnectTimer = null;
    let disposed = false;
    const update = (patch) => { if (!disposed) setView((prev) => ({ ...prev, ...patch })); };

    function drawRaw(buffer) {
      const canvas = canvasRef.current;
      if (!canvas || !geometry) return;
      const { width, height, channels } = geometry;
      const source = new Uint8Array(buffer);
      if (source.length !== width * height * channels) return;
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext('2d');
      const image = context.createImageData(width, height);
      if (channels === 4) {
        image.data.set(source);
      } else {
        for (let i = 0, j = 0; i < source.length; i += 3, j += 4) {
          image.data[j] = source[i];
          image.data[j + 1] = source[i + 1];
          image.data[j + 2] = source[i + 2];
          image.data[j + 3] = 255;
        }
      }
      context.putImageData(image, 0, 0);
      if (frames === 0) update({ drawn: true });
      frames += 1;
    }

    function stopSnapshots() {
      snapshotActive = false;
      if (snapshotTimer !== null) clearTimeout(snapshotTimer);
      snapshotTimer = null;
    }

    async function fetchSnapshot() {
      const response = await fetch('/v1/scene.png', { cache: 'no-store' });
      // 요청 중에 실시간이 살아났으면 버린다 — 늦은 스냅샷이 실시간을 덮지 않게.
      if (!snapshotActive) return;
      if (response.ok) {
        const bitmap = await createImageBitmap(await response.blob());
        const canvas = canvasRef.current;
        if (snapshotActive && canvas) {
          canvas.width = bitmap.width;
          canvas.height = bitmap.height;
          canvas.getContext('2d').drawImage(bitmap, 0, 0);
          update({ status: 'snapshot', snapshotAt: Date.now(), detail: '', drawn: true });
        }
        bitmap.close();
        return;
      }
      const info = await response.json().catch(() => ({}));
      if (!snapshotActive) return;
      if (info.reason_code === 'config.missing') {
        // 카메라가 구성되지 않았다 — 다시 불러도 나아지지 않는다.
        stopSnapshots();
        update({ status: 'unavailable', detail: info.detail || '장면 카메라가 구성되지 않았습니다' });
        return;
      }
      update({ status: 'stalled', detail: info.detail || `스냅샷을 받지 못했습니다 (HTTP ${response.status})` });
    }

    async function pollSnapshot() {
      snapshotTimer = null;
      if (!snapshotActive) return;
      // 보이지 않는 탭은 서버에 부담만 준다.
      if (document.visibilityState === 'visible') {
        try {
          await fetchSnapshot();
        } catch (error) {
          if (snapshotActive) update({ status: 'stalled', detail: error.message });
        }
      }
      if (snapshotActive) snapshotTimer = setTimeout(pollSnapshot, SNAPSHOT_INTERVAL_MS);
    }

    function startSnapshots() {
      if (snapshotActive) return;
      snapshotActive = true;
      update({ status: 'snapshot', snapshotAt: null, detail: '' });
      pollSnapshot();
    }

    function connect() {
      if (disposed) return;
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
      socket = new WebSocket(`${scheme}://${window.location.host}/v1/scene/stream`);
      socket.binaryType = 'arraybuffer';
      socket.onmessage = (event) => {
        if (typeof event.data !== 'string') {
          drawRaw(event.data);
          return;
        }
        let payload;
        try { payload = JSON.parse(event.data); } catch { return; }
        if (payload.type === 'scene_header') {
          stopSnapshots();
          geometry = { width: payload.width, height: payload.height, channels: payload.channels };
          update({ status: 'live', detail: '' });
        } else if (payload.type === 'scene_stalled') {
          update({ status: 'stalled', detail: payload.detail || '프레임이 끊겼습니다' });
        } else if (payload.type === 'scene_unavailable') {
          // 실시간만 없는 경우(브리지 장애)는 서버 단발 캡처로 스냅샷을 받을 수 있다.
          if (payload.reason_code === 'exec.unverifiable') startSnapshots();
          else update({ status: 'unavailable', detail: payload.detail || '장면을 쓸 수 없습니다' });
        }
      };
      socket.onclose = () => {
        socket = null;
        if (disposed) return;
        // 스냅샷 중에는 재접속이 3초마다 닫힌다. 그때마다 상태를 바꾸면 깜빡인다.
        if (!snapshotActive) update({ status: 'connecting' });
        reconnectTimer = setTimeout(connect, RECONNECT_MS);
      };
    }

    const fpsTimer = setInterval(() => {
      update({ fps: frames });
      frames = 0;
    }, 1000);
    connect();

    return () => {
      disposed = true;
      stopSnapshots();
      clearInterval(fpsTimer);
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (socket) socket.close();
    };
  }, []);

  const { status, detail, drawn } = view;
  // 끊김이어도 마지막으로 받은 그림은 남긴다 — 카드 제목 줄의 상태 문구로 구분한다.
  const hasImage = drawn && status !== 'unavailable' && status !== 'connecting';
  return <>
    <canvas ref={canvasRef} className="scene-canvas" width="480" height="360" style={{ visibility: hasImage ? 'visible' : 'hidden' }} />
    {!hasImage && <p className="scene-waiting">{status !== 'unavailable' && <><Spinner size={14} label={status === 'snapshot' ? '스냅샷 받는 중' : '연결 중'} />{' '}</>}{status === 'unavailable' ? detail : status === 'snapshot' ? '실시간 영상이 없어 서버 스냅샷을 받는 중…' : 'Gazebo 시뮬레이션에 연결하는 중…'}</p>}
  </>;
}
