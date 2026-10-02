// 시뮬레이션 창 제목 줄 문구. 컴포넌트 파일과 나눠 둔다 — 컴포넌트만 내보내야 Vite 수정 즉시 반영(Fast Refresh)이 동작한다.

function sceneLabel({ status, fps, snapshotAt }) {
  if (status === 'live') return `Gazebo 실시간 스트리밍 · ${fps} FPS`;
  if (status === 'snapshot') {
    const at = snapshotAt ? ` · ${new Date(snapshotAt).toTimeString().slice(0, 8)}` : '';
    return `Gazebo 서버 스냅샷 · 실시간 아님${at}`;
  }
  if (status === 'stalled') return 'Gazebo 프레임 끊김 · 마지막 화면 표시 중';
  if (status === 'unavailable') return 'Gazebo 장면 카메라를 쓸 수 없음';
  return 'Gazebo 시뮬레이션에 연결하는 중';
}

export function simLabel(view) {
  if (view.mode === 'camera') return sceneLabel(view);
  if (view.status === 'connecting') return '3D 작업 셀에 연결하는 중';
  if (view.stale) return `3D 정지 화면 · ${view.reason}`;
  return `3D 관측 실시간 · ${view.fps} FPS`;
}
