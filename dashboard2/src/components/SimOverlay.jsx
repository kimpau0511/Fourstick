import { useCallback, useEffect, useRef, useState } from 'react';
import SimView3D from '../SimView3D.jsx';
import { commandLock } from '../server.js';
import { simLabel } from '../simLabel.js';
import dotSimDanger from '../assets/dot-sim-danger.svg';
import dotSimRunning from '../assets/dot-sim-running.svg';
import dotSimStopping from '../assets/dot-sim-stopping.svg';

const timeText = (ms) => new Date(ms).toTimeString().slice(0, 8);
const HEADER_STOP = '헤더의 즉시 정지는 언제든 즉시 실행됩니다.';

// 진행 단계: 서버 job.progress([{no,of,label,reached}]) 또는 goal.plan을 그대로. 없으면 null.
function stepsOf(cmd) {
  const { job, goal } = cmd;
  if (job && Array.isArray(job.progress) && job.progress.length) return job.progress.map((p) => ({ label: p.label, reached: !!p.reached }));
  if (goal && Array.isArray(goal.plan) && goal.plan.length) return goal.plan.map((p) => ({ label: `${p.material_label || p.material} · ${p.from_label || ''} → ${p.to_label || ''}`, reached: p.status === 'completed' }));
  return null;
}

// 시뮬레이션 창(⑧). 내용은 overlay(simOverlayState.js)와 cmd의 서버 값으로만 채운다.
export default function SimOverlay({ overlay, cmd, server, onClose }) {
  const [scene, setScene] = useState({ mode: '3d', status: 'connecting', fps: 0 });
  const onView = useCallback((view) => setScene(view), []);
  // 시뮬레이션 화면 전체화면. 브라우저 전체화면 API를 쓰고, Esc로도 나온다. 화면 크기가 바뀌면
  // SimView3D가 ResizeObserver로 다시 그린다. 전체화면에서는 헤더가 가려지므로 같은 정지(cmd.stop)를 안에 둔다.
  const videoRef = useRef(null);
  const [full, setFull] = useState(false);
  useEffect(() => {
    const node = videoRef.current;
    const sync = () => setFull(!!node && document.fullscreenElement === node);
    document.addEventListener('fullscreenchange', sync);
    return () => {
      document.removeEventListener('fullscreenchange', sync);
      // 창이 닫히면(작업 끝·창 닫기) 전체화면도 함께 끝낸다.
      if (node && document.fullscreenElement === node) document.exitFullscreen().catch(() => {});
    };
  }, []);
  const toggleFull = useCallback(() => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else videoRef.current?.requestFullscreen?.().catch(() => {});
  }, []);
  const { state } = overlay;
  const steps = stepsOf(cmd);
  const lock = commandLock(server);
  const done = steps ? steps.filter((s) => s.reached).length : 0;
  const stamp = cmd.updatedAt ? timeText(cmd.updatedAt) : null;
  const first = steps ? steps.findIndex((s) => !s.reached) : -1;
  // 일시정지·재개·복구는 일반 경로 명령(cmd.pause·cmd.resume·cmd.restore)에서. 창이 열려 있으면 늘 보이고,
  // 쓸 수 없을 때는 잠근 채 이유를 보인다(2026-10-07). 멈춘 자재는 서버 상태에서 찾는다 — 이 창의 일시정지든
  // 헤더의 즉시 정지든 실행기가 남긴 '정지·미복구' 기록이 기준이다. 재개 가능 여부는 서버 판정(actions.resume).
  const controls = typeof cmd.pause === 'function' && state !== 'danger';
  const materials = (server && server.simDemo && server.simDemo.materials) || [];
  const pausedModel = cmd.job && cmd.job.paused ? cmd.job.paused.material : null;
  const stoppedRow = materials.find((m) => m.model === pausedModel && m.record)
    || materials.find((m) => m.record && /unrestored|failed|stopped/.test(m.record.state || ''));
  const moving = state === 'running' || (server && server.simDemo && server.simDemo.running_job);
  const canPause = controls && state === 'running' && !(cmd.job && cmd.job.resumed);
  const pauseWhy = canPause ? null : moving ? '이 화면에서 시작한 작업만 일시정지할 수 있습니다' : '실행 중인 작업이 없습니다';
  const canResume = controls && !moving && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.resume) && !lock;
  const resumeWhy = canResume ? null
    : moving ? '작업이 실행 중입니다'
      : !stoppedRow ? '멈춘 작업이 없습니다'
        : lock || '이 지점에서는 이어서 할 수 없습니다 — 복구하면 원래 자리로 되돌립니다(서버 판정)';
  // 재개할 수 없을 때만 '복구'(원래 자리로) — 서버가 복구 가능(actions.restore)하다고 할 때.
  const canRestore = controls && !moving && !canResume && !lock
    && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.restore);
  const stoppedName = stoppedRow ? stoppedRow.korean || stoppedRow.model : null;

  let card;
  if (state === 'viewing') {
    card = {
      dot: dotSimRunning, title: '시뮬레이션 보기', titleTone: '', sub: simLabel(scene),
      section: '작업 셀 관측', foot: HEADER_STOP,
    };
  } else if (state === 'danger') {
    card = {
      // 서버 BLOCK은 안전 판정 말고도(입력 오류·서비스 꺼짐 등) 나온다 — 안전 검사를 했다고 단정하지 않고 서버 사유만 보인다.
      dot: dotSimDanger, title: '실행 불가 — 서버가 차단했습니다', titleTone: 'danger', sub: cmd.result.reason || '실행 차단',
      section: '서버 판정 결과',
      stages: [{ mark: '✓', tone: 'done', label: '명령 접수' }, { mark: '✕', tone: 'danger', label: '실행 차단' }],
      lines: ['danger'],
      foot: `서버가 이 명령을 차단했습니다   ·   ${HEADER_STOP}`,
    };
  } else if (state === 'running') {
    card = {
      dot: dotSimRunning, title: '가상 동작 확인 중', titleTone: '', sub: simLabel(scene),
      section: '동작 세부 진행',
      stages: steps && steps.map((s, i) => ({ mark: s.reached ? '✓' : String(i + 1), tone: s.reached ? 'done' : i === first ? 'current' : 'wait', label: s.label })),
      lines: steps && steps.slice(1).map((s) => (s.reached ? 'done' : 'wait')),
      foot: `${steps ? `${done}/${steps.length} 단계 완료${stamp ? ` · 마지막 갱신 ${stamp}` : ''}` : '진행 단계 정보 없음'}   ·   이 창이 열려 있어도 ${HEADER_STOP}`,
    };
  } else if (state === 'paused') {
    card = {
      dot: dotSimStopping, title: '일시정지됨', titleTone: '',
      sub: `${stoppedName || pausedModel} — 정지 지점에서 멈췄습니다. 재개하면 그 지점부터 이어서 옮깁니다`,
      section: '동작 세부 진행 · 일시정지',
      stages: steps && steps.map((s, i) => ({ mark: s.reached ? '✓' : String(i + 1), tone: s.reached ? 'done' : i === first ? 'current' : 'wait', label: s.label })),
      lines: steps && steps.slice(1).map((s) => (s.reached ? 'done' : 'wait')),
      foot: HEADER_STOP,
    };
  } else {
    card = {
      dot: dotSimStopping, titleTone: 'danger',
      title: overlay.confirmed ? '정지 확인됨' : '정지 요청됨 · 시뮬레이터 확인 대기',
      sub: overlay.confirmed ? `시뮬레이터가 정지를 확인했습니다 · ${overlay.resultLabel}` : '정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 결과가 표시됩니다',
      section: '즉시 정지 처리 흐름',
      stages: [{ mark: '✓', tone: 'requested', label: '즉시 정지 요청됨' },
        overlay.confirmed ? { mark: '✓', tone: 'confirmed', label: '정지 확인' } : { mark: '?', tone: 'wait', label: '정지 확인 대기' }],
      lines: [overlay.confirmed ? 'danger' : 'wait'],
      foot: `${overlay.confirmed && stamp ? `정지 확인 · ${stamp}   ·   ` : ''}${HEADER_STOP}`,
    };
  }
  return <div className="scrim">
    <section className={`sim-card ${state}`} role="dialog" aria-label={card.title}>
      <div className="sim-head">
        <img src={card.dot} alt="" width="8" height="8" />
        <div><strong className={card.titleTone}>{card.title}</strong><small>{card.sub}</small></div>
        <button type="button" className="sim-cancel" onClick={onClose}><b>창 닫기</b><small>창만 닫음 · 로봇 동작에는 영향 없음</small></button>
      </div>
      <div className="sim-video" ref={videoRef}>
        <SimView3D onView={onView} />
        <button type="button" className="sim-fullscreen" onClick={toggleFull} aria-pressed={full}>
          {full ? '전체화면 종료 (Esc)' : '⛶ 전체화면'}
        </button>
        {full && <div className="sim-fs-bar">
          <span className={card.titleTone}>{card.title}{state !== 'viewing' && steps ? ` · ${done}/${steps.length} 단계` : ''}</span>
          {controls && <button type="button" className="sim-fs-pause" disabled={!canPause || cmd.busy} title={pauseWhy || undefined} onClick={cmd.pause}>❚❚ 일시정지</button>}
          {controls && <button type="button" className="sim-fs-pause" disabled={!canResume || cmd.busy} title={resumeWhy || undefined} onClick={() => cmd.resume(stoppedRow && stoppedRow.model)}>▶ 재개</button>}
          <button type="button" className="sim-fs-stop" onClick={cmd.stop}>■ 즉시 정지</button>
        </div>}
      </div>
      <hr />
      <p className="sim-section">{card.section}</p>
      {card.stages ? <div className="stages">
        {card.stages.map((s, i) => <div key={i} className="stage-wrap">
          {i > 0 && <i className={`stage-line ${card.lines[i - 1]}`} />}
          <div className="stage"><span className={`stage-mark ${s.tone}`}>{s.mark}</span><small className={s.tone === 'wait' ? 'muted' : s.tone === 'danger' ? 'danger' : ''}>{s.label}</small></div>
        </div>)}
      </div> : state !== 'viewing' && <p className="sim-foot">진행 단계 정보 없음</p>}
      <p className="sim-foot">{card.foot}</p>
      {controls && <div className="sim-actions" role="group" aria-label="이 작업 제어">
        <button type="button" disabled={!canPause || cmd.busy} title={pauseWhy || undefined} onClick={cmd.pause}>❚❚ 일시정지</button>
        <button type="button" disabled={!canResume || cmd.busy} title={resumeWhy || undefined} onClick={() => cmd.resume(stoppedRow && stoppedRow.model)}>▶ 재개</button>
        {canRestore && <button type="button" disabled={cmd.busy} onClick={() => cmd.restore(stoppedRow && stoppedRow.model)}>↺ 복구(원래 자리로)</button>}
      </div>}
      {controls && (stoppedRow || moving) && <p className="sim-foot" role="status">{canPause ? '일시정지: 이 작업만 멈춥니다(전체 정지는 헤더)'
        : canResume ? `${stoppedName}: 정지 지점에서 이어서 옮길 수 있습니다`
          : canRestore ? `${stoppedName}: ${resumeWhy}` : resumeWhy}</p>}
      {cmd.pauseNote && <p className={`sim-foot ${cmd.pauseNote.tone}`} role="status">{cmd.pauseNote.text}</p>}
      {/* 위험 판정에서는 판정을 덮어쓰는 승인 버튼을 두지 않는다(피그마 메모). 다시 보내면 서버가 다시 판정한다. */}
      {state === 'danger' && <div className="sim-actions"><button type="button" onClick={cmd.reset}>명령 수정</button><button type="button" disabled={cmd.busy || !!lock} title={lock || undefined} onClick={() => cmd.send(cmd.sent)}>다시 보내기</button></div>}
    </section>
  </div>;
}
