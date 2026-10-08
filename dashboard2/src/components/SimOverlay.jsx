import { useCallback, useEffect, useRef, useState } from 'react';
import { repeatAction } from '../repeatApi.js';
import SimView3D from '../SimView3D.jsx';
import { commandLock } from '../server.js';
import { simLabel } from '../simLabel.js';
import { simAlertOf } from '../simPanels.js';
import { stepperOf } from '../simStepper.js';
import JointPanel from './JointPanel.jsx';
import SimAlert from './SimAlert.jsx';
import SimStepper from './SimStepper.jsx';
import dotSimDanger from '../assets/dot-sim-danger.svg';
import dotSimRunning from '../assets/dot-sim-running.svg';
import dotSimStopping from '../assets/dot-sim-stopping.svg';

const timeText = (ms) => new Date(ms).toTimeString().slice(0, 8);
const RUN_POLL_MS = 1500; // 반복 작업 회차의 진행 조회 주기(화면 갱신 속도일 뿐 판단 기준 아님)

// 반복 작업처럼 이 화면의 명령 흐름(cmd.job)이 아닌 실행: 서버가 알려 준 실행 중 작업(running_job)의 진행만 읽는다(GET).
// 작업 id가 바뀌면(다음 회차) 이전 값을 버린다.
function useServerRun(jobId) {
  const [state, setState] = useState({ id: null, run: null, updatedAt: null, comm: null, stageAt: null });
  useEffect(() => {
    if (!jobId) return undefined;
    let alive = true;
    const tick = async () => {
      let run = null;
      try {
        const res = await fetch(`/v1/sim-demo/jobs/${encodeURIComponent(jobId)}`);
        if (res.ok) run = await res.json();
      } catch { /* 실패는 아래에서 통신 실패로 센다 — 마지막 값은 그대로 둔다 */ }
      if (!alive) return;
      const at = Date.now();
      setState((s) => {
        const same = s.id === jobId;
        const prev = same && s.comm ? s.comm : {};
        if (!run) return { ...(same ? s : { id: jobId, run: null, updatedAt: null, stageAt: null }), id: jobId, comm: { okAt: prev.okAt || null, failAt: at, fails: (prev.fails || 0) + 1 } };
        const before = same && s.run && Array.isArray(s.run.progress) ? s.run.progress.length : -1;
        const count = Array.isArray(run.progress) ? run.progress.length : 0;
        return { id: jobId, run, updatedAt: at, comm: { okAt: at, failAt: prev.failAt || null, fails: 0 }, stageAt: before !== count || !same || !s.stageAt ? at : s.stageAt };
      });
    };
    tick();
    const timer = setInterval(tick, RUN_POLL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [jobId]);
  return state.id === jobId ? state : { id: jobId, run: null, updatedAt: null, comm: null, stageAt: null };
}

// 갱신 중단 판정용 시계. 진행 값을 바꾸지 않는다 — 마지막 갱신에서 얼마나 지났는지만 본다.
function useNow(active) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}
const HEADER_STOP = '헤더의 즉시 정지는 언제든 즉시 실행됩니다.';


// 시뮬레이션 창(⑧). 내용은 overlay(simOverlayState.js)와 cmd의 서버 값으로만 채운다.
export default function SimOverlay({ overlay, cmd, server, onClose }) {
  const [scene, setScene] = useState({ mode: '3d', status: 'connecting', fps: 0 });
  const onView = useCallback((view) => setScene(view), []);
  // 관절 상태: SimView3D가 관측 원본·신선도를 초당 최대 10번 알린다.
  const [joints, setJoints] = useState(null);
  const onJoints = useCallback((data) => setJoints(data), []);
  const alert = simAlertOf({ cmd, server });
  const [dismissed, setDismissed] = useState(null);
  const showAlert = alert && alert.key !== dismissed ? alert : null;
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
  // 가로 스테퍼(2026-10-08): 이 화면의 실행(cmd.job)이 있으면 그것, 없으면 서버가 알려 준 실행 중 작업(반복 회차 등).
  const serverRunId = !cmd.job && server && server.simDemo && server.simDemo.running_job ? server.simDemo.running_job.job_id : null;
  const serverRun = useServerRun(serverRunId);
  const names = Object.fromEntries(((server && server.simDemo && server.simDemo.materials) || []).map((m) => [m.model, m.korean || m.model]));
  const tracking = (cmd.job && cmd.job.status === 'running') || !!serverRunId;
  const now = useNow(tracking);
  const stepper = cmd.job ? stepperOf(cmd, { now, names })
    : serverRun.run ? stepperOf({ job: serverRun.run, updatedAt: serverRun.updatedAt, comm: serverRun.comm, stageAt: serverRun.stageAt }, { now, names })
      : null;
  const stepperStamp = (cmd.job ? cmd.updatedAt : serverRun.updatedAt) ? timeText(cmd.job ? cmd.updatedAt : serverRun.updatedAt) : null;
  const lock = commandLock(server);
  const stamp = cmd.updatedAt ? timeText(cmd.updatedAt) : null;
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
  // 재개·복구 확인 카드(2026-10-08 리뷰 2번): 서버가 만든 카드를 승인해야 시작한다. 카드가 떠 있는 동안 버튼을 잠근다.
  const recovery = cmd.recovery || null;
  const canResume = controls && !moving && !recovery && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.resume) && !lock;
  const resumeWhy = canResume ? null
    : moving ? '작업이 실행 중입니다'
      : !stoppedRow ? '멈춘 작업이 없습니다'
        : lock || '이 지점에서는 이어서 할 수 없습니다 — 복구하면 원래 자리로 되돌립니다(서버 판정)';
  // 재개할 수 없을 때만 '복구'(원래 자리로) — 서버가 복구 가능(actions.restore)하다고 할 때.
  const canRestore = controls && !moving && !canResume && !lock && !recovery
    && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.restore);
  const recoveryCard = recovery && typeof cmd.answerRecovery === 'function'
    ? <div className="sim-recovery" role="group" aria-label={`${recovery.action === 'resume' ? '재개' : '복구'} 승인`}>
      <p><b>{recovery.action === 'resume' ? '재개' : '복구'} 승인 대기</b> — {recovery.summary}
        {recovery.expiresAt ? <small>승인 기한 {timeText(recovery.expiresAt)}</small> : null}</p>
      <div className="sim-actions">
        <button type="button" className="approve" disabled={cmd.busy || !!lock} title={lock || undefined} onClick={() => cmd.answerRecovery(true)}>승인</button>
        <button type="button" disabled={cmd.busy} onClick={() => cmd.answerRecovery(false)}>취소</button>
      </div>
    </div> : null;
  const stoppedName = stoppedRow ? stoppedRow.korean || stoppedRow.model : null;
  // 반복 작업이 진행 중이면 일시정지·재개는 그 반복을 제어한다(서버가 판정 — 재개 가능 구간 제한 그대로).
  const repeatRun = server && server.repeat && server.repeat.run && server.repeat.run.active ? server.repeat.run : null;
  const [repeatNote, setRepeatNote] = useState(null);
  const [repeatBusy, setRepeatBusy] = useState(false);
  const onRepeat = useCallback(async (action) => {
    if (!repeatRun) return;
    setRepeatBusy(true); setRepeatNote(null);
    const out = await repeatAction(repeatRun.run_id, action);
    if (!out.ok) setRepeatNote(out.reason);
    await server.refresh?.();
    setRepeatBusy(false);
  }, [repeatRun, server]);

  let card;
  if (state === 'viewing') {
    card = {
      dot: dotSimRunning, title: '시뮬레이션 보기', titleTone: '', sub: simLabel(scene),
      section: stepper ? '동작 세부 진행' : '작업 셀 관측', foot: HEADER_STOP,
    };
  } else if (state === 'danger') {
    card = {
      // 서버 BLOCK은 안전 판정 말고도(입력 오류·서비스 꺼짐 등) 나온다 — 안전 검사를 했다고 단정하지 않고 서버 사유만 보인다.
      // 사유·조치는 아래 오류 안내 한 곳에서만 보인다(중복 표시 통합, 2026-10-07).
      dot: dotSimDanger, title: '실행 불가 — 서버가 차단했습니다', titleTone: 'danger', sub: '서버 판정 사유는 아래 안내에 있습니다',
      section: '서버 판정 결과',
      stages: [{ mark: '✓', tone: 'done', label: '명령 접수' }, { mark: '✕', tone: 'danger', label: '실행 차단' }],
      lines: ['danger'],
      foot: HEADER_STOP,
    };
  } else if (state === 'running') {
    card = {
      dot: dotSimRunning, title: '가상 동작 확인 중', titleTone: '', sub: simLabel(scene),
      section: '동작 세부 진행',
      foot: `${stamp ? `마지막 갱신 ${stamp}   ·   ` : ''}이 창이 열려 있어도 ${HEADER_STOP}`,
    };
  } else if (state === 'paused') {
    card = {
      dot: dotSimStopping, title: '일시정지됨', titleTone: '',
      sub: `${stoppedName || pausedModel} — 정지 지점에서 멈췄습니다. 재개하면 그 지점부터 이어서 옮깁니다`,
      section: '동작 세부 진행 · 일시정지',
      foot: HEADER_STOP,
    };
  } else {
    card = {
      dot: dotSimStopping, titleTone: 'danger',
      title: overlay.confirmed ? '정지 확인됨' : '정지 요청됨 · 시뮬레이터 확인 대기',
      sub: overlay.confirmed ? `시뮬레이터가 정지를 확인했습니다 · ${overlay.resultLabel}` : '정지를 요청했습니다 — 시뮬레이터가 정지를 확인하면 결과가 표시됩니다',
      section: '동작 세부 진행 · 즉시 정지',
      foot: `${overlay.confirmed && stamp ? `정지 확인 · ${stamp}   ·   ` : ''}${HEADER_STOP}`,
    };
  }
  // 창 바깥(어두운 덮개)을 눌러도 닫는다 — 창은 보기만 하는 곳이라 닫아도 로봇 동작에는 영향이 없다.
  // 덮개는 헤더 아래부터라 헤더의 즉시 정지를 누르는 것은 여기로 오지 않는다.
  return <div className="scrim" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
    <section className={`sim-card ${state}`} role="dialog" aria-label={card.title}>
      <div className="sim-head">
        <img src={card.dot} alt="" width="8" height="8" />
        <div><strong className={card.titleTone}>{card.title}</strong><small>{card.sub}</small></div>
        {/* 아이콘만 두고 이름은 마우스를 올리면 보인다(data-tip). 전체화면 중에는 이 줄이 안 보이므로 종료 버튼은 화면 안에 둔다. */}
        <div className="sim-head-actions">
          {!full && <button type="button" className="sim-icon-btn sim-fullscreen" onClick={toggleFull} aria-pressed={false} aria-label="전체화면" data-tip="전체화면">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M8 3H5a2 2 0 0 0-2 2v3M21 8V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3M16 21h3a2 2 0 0 0 2-2v-3" /></svg>
          </button>}
          <button type="button" className="sim-icon-btn sim-cancel" onClick={onClose} aria-label="창 닫기" data-tip="창 닫기">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg>
          </button>
        </div>
      </div>
      {/* 왼쪽 관절 상태 · 오른쪽 실시간 시뮬레이션(좁으면 관절 상태가 아래). 전체화면은 이 묶음 전체다. */}
      <div className="sim-stage" ref={videoRef}>
      <JointPanel data={joints} />
      <div className="sim-video">
        <SimView3D onView={onView} onJoints={onJoints} />
        {full && showAlert && <SimAlert alert={showAlert} compact onDismiss={() => setDismissed(showAlert.key)} />}
        {full && <button type="button" className="sim-fullscreen sim-fullscreen-exit" onClick={toggleFull} aria-pressed>전체화면 종료 (Esc)</button>}
        {full && <div className="sim-fs-bar">
          <span className={card.titleTone}>{card.title}{stepper && stepper.steps ? (stepper.total ? ` · ${stepper.done}/${stepper.total} 단계` : ` · ${stepper.done}단계 완료 · 남은 단계 확인 중`) : ''}</span>
          {repeatRun ? <>
            <button type="button" className="sim-fs-pause" disabled={repeatBusy || repeatRun.state !== 'running'} onClick={() => onRepeat('pause')}>❚❚ 일시정지</button>
            <button type="button" className="sim-fs-pause" disabled={repeatBusy || repeatRun.state !== 'paused' || !!lock} onClick={() => onRepeat('resume')}>▶ 재개</button>
          </> : <>
            {controls && <button type="button" className="sim-fs-pause" disabled={!canPause || cmd.busy} title={pauseWhy || undefined} onClick={cmd.pause}>❚❚ 일시정지</button>}
            {controls && !recovery && <button type="button" className="sim-fs-pause" disabled={!canResume || cmd.busy} title={resumeWhy || undefined} onClick={() => cmd.resume(stoppedRow && stoppedRow.model)}>▶ 재개</button>}
            {controls && recovery && <button type="button" className="sim-fs-pause" disabled={cmd.busy || !!lock} onClick={() => cmd.answerRecovery(true)}>{recovery.action === 'resume' ? '재개' : '복구'} 승인</button>}
            {controls && recovery && <button type="button" className="sim-fs-pause" disabled={cmd.busy} onClick={() => cmd.answerRecovery(false)}>취소</button>}
          </>}
          <button type="button" className="sim-fs-stop" onClick={cmd.stop}>■ 즉시 정지</button>
        </div>}
      </div>
      {full && stepper && <SimStepper model={stepper} stamp={stepperStamp} compact />}
      </div>
      {!full && showAlert && <SimAlert alert={showAlert} onDismiss={() => setDismissed(showAlert.key)} />}
      <p className="sim-section">{card.section}</p>
      {card.stages ? <div className="stages">
        {card.stages.map((s, i) => <div key={i} className="stage-wrap">
          {i > 0 && <i className={`stage-line ${card.lines[i - 1]}`} />}
          <div className="stage"><span className={`stage-mark ${s.tone}`}>{s.mark}</span><small className={s.tone === 'wait' ? 'muted' : s.tone === 'danger' ? 'danger' : ''}>{s.label}</small></div>
        </div>)}
      </div> : stepper ? <SimStepper model={stepper} stamp={stepperStamp} />
        : state !== 'viewing' && <p className="sim-foot">진행 단계 정보 없음</p>}
      <p className="sim-foot">{card.foot}</p>
      {repeatRun && <div className="sim-actions" role="group" aria-label="반복 작업 제어">
        <button type="button" disabled={repeatBusy || repeatRun.state !== 'running'} title={repeatRun.state !== 'running' ? '실행 중인 반복이 아닙니다' : undefined} onClick={() => onRepeat('pause')}>❚❚ 일시정지</button>
        <button type="button" disabled={repeatBusy || repeatRun.state !== 'paused' || !!lock} title={repeatRun.state !== 'paused' ? '일시정지된 반복이 아닙니다' : lock || undefined} onClick={() => onRepeat('resume')}>▶ 재개</button>
        {repeatRun.state === 'paused' && <button type="button" disabled={repeatBusy} onClick={() => onRepeat('cancel')}>반복 취소</button>}
      </div>}
      {repeatRun && <p className="sim-foot" role="status">반복 작업: {repeatRun.label}{repeatNote ? ` — ${repeatNote}` : ''}</p>}
      {!full && recoveryCard}
      {controls && !repeatRun && <div className="sim-actions" role="group" aria-label="이 작업 제어">
        <button type="button" disabled={!canPause || cmd.busy} title={pauseWhy || undefined} onClick={cmd.pause}>❚❚ 일시정지</button>
        <button type="button" disabled={!canResume || cmd.busy} title={resumeWhy || undefined} onClick={() => cmd.resume(stoppedRow && stoppedRow.model)}>▶ 재개</button>
        {canRestore && <button type="button" disabled={cmd.busy} onClick={() => cmd.restore(stoppedRow && stoppedRow.model)}>↺ 복구(원래 자리로)</button>}
      </div>}
      {controls && !repeatRun && (stoppedRow || moving) && <p className="sim-foot" role="status">{canPause ? '일시정지: 이 작업만 멈춥니다(전체 정지는 헤더)'
        : canResume ? `${stoppedName}: 정지 지점에서 이어서 옮길 수 있습니다`
          : canRestore ? `${stoppedName}: ${resumeWhy}` : resumeWhy}</p>}
      {cmd.pauseNote && cmd.pauseNote.tone !== 'danger' && <p className={`sim-foot ${cmd.pauseNote.tone}`} role="status">{cmd.pauseNote.text}</p>}
      {/* 위험 판정에서는 판정을 덮어쓰는 승인 버튼을 두지 않는다(피그마 메모). 다시 보내면 서버가 다시 판정한다. */}
      {state === 'danger' && <div className="sim-actions"><button type="button" onClick={cmd.reset}>명령 수정</button><button type="button" disabled={cmd.busy || !!lock} title={lock || undefined} onClick={() => cmd.send(cmd.sent)}>다시 보내기</button></div>}
    </section>
  </div>;
}
