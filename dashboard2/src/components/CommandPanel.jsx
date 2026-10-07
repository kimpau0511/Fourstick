import { useState } from 'react';
import { RESULT_LABELS } from '../simCommand.js';
import { commandLock, robotNameOf } from '../server.js';
import { useWakeVoice } from '../wakeVoice.js';
import RepeatPanel from './RepeatPanel.jsx';
import Spinner from './Spinner.jsx';
import VoiceControl from './VoiceControl.jsx';
import './command.css';
import checkSquare from '../assets/check-square-2.svg';
import dotPanel from '../assets/dot-panel.svg';
import shieldCheck from '../assets/shield-check.svg';

// 서버 판정 → [톤, 표시]. 판정 코드는 서버 계약(server/routes/sim_demo.py) 그대로다.
const DECISIONS = {
  CONFIRM: ['info', '확인 필요'],
  CONFIRM_GOAL: ['info', '확인 필요 · 여러 단계'],
  RUN: ['ok', '실행'],
  STOP: ['danger', '정지 요청'],
  ASK: ['warn', '되묻기 · 실행 안 함'],
  BLOCK: ['danger', '차단 · 실행 안 함'],
  PASS_THROUGH: ['warn', '시연 명령 아님'],
  ENVIRONMENT: ['info', '환경 변경'],
  NOOP: ['info', '할 일 없음'],
  CANCELLED: ['warn', '취소됨'],
};

const stepText = (p) => `${p.material_label || p.material} · ${p.from_label || ''} → ${p.to_label || ''}`;

// 체크리스트 3단계(상태매트릭스 §11). 판정 도착 = 1·2 완료, 승인 후 실행 = 3 진행, 종료 = 3 완료.
const STAGES = ['동작 준비', '안전 규칙 확인', '가상 동작 확인'];
const MARK = { done: '✓', active: '●', fail: '✕', idle: '○' };
const STAGE_TEXT = { done: '완료', active: '진행 중', fail: '실패', idle: '대기' };

// 자재 버튼은 서버 상태 기록(/v1/sim-demo)만 따른다. 문장은 입력창과 같은 일반 계획 요청(sim.send)으로 보낸다
// — 승인 전에는 실행하지 않는다. 복귀 버튼은 자재가 원래 자리 밖에 있다고 기록됐을 때만 보이고,
// 기록된 위치가 확정(컨베이어 칸·팔레트)이 아니면 잠그고 이유를 보인다.
const PLACED = ['held_on_target', 'on_pallet'];
function materialSkills(server) {
  const sd = server && server.simDemo;
  const materials = (sd && sd.materials) || [];
  const stale = server && server.simDemoFailing ? '작업 상태 조회가 실패해 자재 버튼을 잠갔습니다 — 연결을 확인해 주세요' : null;
  const cell = sd && sd.running_job ? '다른 작업이 실행 중이라 자재 버튼을 잠갔습니다'
    : sd && sd.recovery_required ? '작업 셀 복구가 필요해 자재 버튼을 잠갔습니다' : null;
  const pallet = (id) => (materials.find((m) => m.support_model === id) || {}).support_korean || id;
  return materials.flatMap((m) => {
    const r = m.record;
    const home = m.support_korean || '원래 자리';
    const out = [];
    if (m.actions && m.actions.transfer) {
      out.push({ key: `${m.model}-t`, label: `${m.korean} → 컨베이어`, sentence: `${m.korean}를 컨베이어로 옮겨줘`,
        title: `현재: 원래 자리(${home}) → 컨베이어`, blocked: stale || cell });
    }
    if (r) {
      const here = r.state === 'held_on_target' ? `컨베이어${m.slot_label ? ` ${m.slot_label}` : ''}`
        : r.state === 'on_pallet' ? pallet(r.pallet) : null;
      out.push({ key: `${m.model}-r`, label: `${m.korean} 원래 자리로`, sentence: `${m.korean}를 원래 자리로 돌려놔`,
        title: `현재: ${here || '위치 확인 필요'} → 원래: ${home}`,
        blocked: stale || cell || (PLACED.includes(r.state) ? null : `${m.korean}의 위치를 확인할 수 없어(${r.state}) 복귀 버튼을 잠갔습니다 — 복구 후 사용할 수 있습니다`) });
    }
    return out;
  });
}

// 음성 안내용 작업 요약(자재·출발지·목적지). 일반 경로는 계획 단계의 자원 id를 서버 카탈로그 이름으로 바꾼다.
const final = (word) => { const c = word.charCodeAt(word.length - 1) - 0xac00; return c >= 0 && c <= 11171 ? c % 28 : -1; };
const eul = (w) => `${w}${final(w) > 0 ? '을' : '를'}`;
const ro = (w) => `${w}${final(w) > 0 && final(w) !== 8 ? '으로' : '로'}`;
function speechSummary(pending, config) {
  if (!pending) return '';
  const steps = pending.planSteps || [];
  if (steps.length) {
    const cat = (config && config.catalogs) || {};
    const names = Object.fromEntries([...(cat.locations || []), ...(cat.objects || [])].map((r) => [r.resource_id, r.display_name]));
    const ids = (step) => Object.values((step && step.args) || {}).map(String);
    const pick = steps.find((x) => x.skill === 'pick');
    const place = steps.find((x) => x.skill === 'place');
    const mat = ids(pick).find((v) => v.startsWith('mat_')) || ids(place).find((v) => v.startsWith('mat_'));
    const from = ids(pick).find((v) => v.startsWith('loc_'));
    const to = ids(place).find((v) => v.startsWith('loc_'));
    if (mat && from && to) {
      const [m, a, b] = [names[mat] || mat, names[from] || from, names[to] || to];
      return `${eul(m)} ${a}에서 ${ro(b)} 옮깁니다.`;
    }
  }
  return `${(pending.summary || '').replace(/[.。]?$/, '.')}${pending.evidence && pending.evidence.slot_label ? ` 놓을 자리는 ${pending.evidence.slot_label}입니다.` : ''}`;
}
const pendingKeyOf = (pending) => (pending ? pending.plan_id || pending.token || pending.goal_id || null : null);

// 로봇 이름은 역할 우선(이송 가능하면 "이송 로봇"), 모델·id는 title로 보조.
const robotName = (id, profile) => ((profile.supported_skills || []).some((k) => k === 'pick' || k === 'place') ? '이송 로봇' : id);

export default function CommandPanel({ now, sim, server, onOpenSimulation, onStop }) {
  const [text, setText] = useState('');
  const [recoverBusy, setRecoverBusy] = useState(false);
  const [recoverNote, setRecoverNote] = useState(null);
  const [picked, setPicked] = useState(null);
  const robots = Object.entries((server && server.robots && server.robots.robots) || {})
    .map(([id, profile]) => ({ id, name: robotName(id, profile), title: profile.profile_id || id }));
  // 1대면 기본 선택. 여러 대인데 고르지 않았으면 보내기를 잠근다.
  const target = robots.length === 1 ? robots[0] : robots.find((r) => r.id === picked) || null;
  // 연결·로봇 상태 잠금은 server.js의 commandLock(즉시 정지는 잠그지 않는다).
  // 반복 작업 중에는 다른 명령을 받지 않는다(서버도 셀 예약으로 실행을 거부한다) — 이유를 먼저 보인다.
  const repeatLock = server && server.repeat && server.repeat.run && server.repeat.run.active
    ? '반복 작업이 진행 중입니다 — 끝나거나 취소한 뒤 명령하세요' : null;
  const lockReason = commandLock(server) || repeatLock
    || (robots.length > 1 && !target ? '대상 로봇을 먼저 선택해 주세요' : null);
  // 텍스트·음성·스킬 버튼 모두 같은 일반 계획 요청(sim.send)을 사용한다.
  const skills = materialSkills(server);
  const skillNotes = [...new Set([...skills.map((k) => k.blocked),
    server && server.simDemoFailing && '작업 상태 조회가 실패해 자재 버튼을 잠갔습니다 — 연결을 확인해 주세요'].filter(Boolean))];
  const { result, pending, job, goal } = sim;
  const gripper = server && server.config && server.config.robot && server.config.robot.has_gripper;
  const [tone, label] = (result && DECISIONS[result.decision]) || ['info', result ? result.decision : ''];
  const remaining = sim.deadline ? Math.max(0, Math.ceil((sim.deadline - now.getTime()) / 1000)) : null;
  const expired = remaining === 0;
  // 조회가 끊겼으면(statusUnknown) 마지막 '실행 중'을 계속 보이지 않는다 — 오류 카드로 간다.
  const running = !sim.statusUnknown && ((job && job.status === 'running') || (goal && ['running', 'stopping'].includes(goal.status)));
  const report = job && job.report;
  // 일반 경로는 실행 최종 상태(job.general)를 서버 값으로 준다. 시연 경로는 작업 결과 코드.
  const [reportTone, resultLabel] = job && job.general ? [job.general.tone, job.general.label] : (report && RESULT_LABELS[report.status]) || ['warn', report ? report.status : ''];
  const resultTone = job && job.general ? job.general.tone : report ? reportTone : goal ? (goal.status === 'completed' ? 'ok' : goal.status === 'failed' ? 'danger' : 'warn') : 'warn';

  // 피그마 Light CommandPanel(23:2517)의 상태: 입력 → 해석·승인/되묻기/차단… → 실행 중 → 완료.
  // 입력 칸은 대기·되묻기에서만 보이고, 나머지는 결과 카드 하나가 그 자리를 쓴다.
  // 판정·문구는 서버 값이고, 여기서는 상태별 톤만 고른다.
  const phase = pending ? 'confirm' : running ? 'running' : sim.statusUnknown ? 'other' : (job || goal) ? 'done'
    : result && result.decision === 'ASK' ? 'ask' : (result || sim.error) ? 'other' : 'idle';
  const [statusTone, statusLabel] = {
    idle: ['muted', '명령 대기'],
    ask: ['warn', '추가 확인 필요'],
    confirm: ['warn', expired ? '확인 시간 만료' : `승인 대기 · ${remaining}초`],
    running: ['info', '실행 중'],
    done: [resultTone, '작업 종료'],
    other: [sim.error ? 'danger' : tone, sim.error ? '오류' : label || '오류'],
  }[phase];
  const steps = job ? (job.progress || []).map((p) => p.reached) : goal ? (goal.plan || []).map((p) => p.status === 'completed') : [];
  const percent = steps.length ? Math.round((steps.filter(Boolean).length / steps.length) * 100) : 0;
  const cardTone = { confirm: expired ? 'warn' : 'ok', running: 'info', done: resultTone, ask: 'warn', other: statusTone }[phase];

  // 호출어 음성 명령(2026-10-07): 마이크 버튼은 켜기/끄기. 호출어 뒤 명령은 텍스트와 같은 계획 요청(sim.send)으로
  // 보내고, 실행은 아래 확인 카드에서 사람이 '실행 승인'을 눌러야만 한다. 확인·실행 중에는 새 명령을 받지 않는다.
  const voice = useWakeVoice({
    config: server && server.config, health: server && server.health, name: robotNameOf(server),
    flow: {
      busy: sim.busy, phase, reason: sim.error || (result && (result.reason || result.summary)) || null,
      // 서버가 실행을 받아들였다 = 이송 작업(job_id)·목표가 생겼다. 승인 직후 응답 대기 중인 '실행 중' 표시는 아직 아니다.
      accepted: !!((job && (job.job_id || job.execution_id)) || goal),
      cancelled: !!(result && result.decision === 'CANCELLED'),
      // 서버가 승인·실행을 거부했다(확인 요청 실패·실행 허가 거부 등) — '시작했다'고 말하지 않고 사유를 안내한다.
      rejected: !!sim.error || !!(result && result.rejected),
      expired: phase === 'confirm' && expired,
      pendingKey: pendingKeyOf(pending), summary: speechSummary(pending, server && server.config),
    },
    onCommand: (utterance, stt) => {
      if (sim.busy || lockReason || phase === 'confirm' || phase === 'running') return;
      sim.send(utterance, target && target.id, stt);
    },
    onStop: () => onStop?.(),
    // 음성 승인·취소: 화면 버튼과 같은 sim.answer. 지금 떠 있는 그 카드에만, 만료·처리 중이면 하지 않는다.
    onAnswer: (action, key) => {
      if (!pending || pendingKeyOf(pending) !== key || expired || sim.busy || (action === 'confirm' && lockReason)) return false;
      sim.answer(action);
      return true;
    },
  });
  const voiceBusy = ['LISTENING', 'TRANSCRIBING', 'ANALYZING'].includes(voice.state) ? '음성 명령을 받는 중입니다 — 끝난 뒤 입력하세요' : null;
  // 긴급 정지 뒤 복구(시뮬레이션 보기와 같은 판정: 서버 기록에서 멈춘 자재·actions.resume/restore).
  const latched = !!(server && server.robots && server.robots.stop_diagnostics && server.robots.stop_diagnostics.stop_latch_active);
  const stoppedRow = ((server && server.simDemo && server.simDemo.materials) || []).find((m) => m.record && /unrestored|failed|stopped/.test(m.record.state || ''));
  const cellBusy = !!(server && server.simDemo && server.simDemo.running_job);
  const recovery = {
    latched, busy: recoverBusy || cellBusy || sim.busy, note: recoverNote,
    canResume: !latched && !cellBusy && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.resume),
    canRestore: !latched && !cellBusy && !(stoppedRow && stoppedRow.actions && stoppedRow.actions.resume) && !!(stoppedRow && stoppedRow.actions && stoppedRow.actions.restore),
    summary: latched ? '정지 래치가 걸려 있습니다 — 정지 해제 뒤 재개 또는 복구할 수 있습니다'
      : stoppedRow ? `${stoppedRow.korean || stoppedRow.model}이(가) 멈춘 자리에 있습니다 — 재개 또는 복구(원래 자리로)를 고르세요`
        : cellBusy ? '실행 중인 작업이 멈추기를 기다리는 중입니다' : '멈춘 작업이 없습니다 — 호출어 대기로 돌아갈 수 있습니다',
    release: async () => {
      setRecoverBusy(true); setRecoverNote(null);
      try {
        const res = await fetch('/v1/stop/release', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', cache: 'no-store' });
        const body = await res.json().catch(() => ({}));
        if (!body.released) setRecoverNote(body.detail || body.error || `정지 해제 실패 (${res.status})`);
      } catch (error) { setRecoverNote(`정지 해제 요청 실패: ${error.message}`); }
      await server.refresh?.();
      setRecoverBusy(false);
    },
    resume: () => stoppedRow && sim.resume?.(stoppedRow.model),
    restore: () => stoppedRow && sim.restore?.(stoppedRow.model),
  };

  function submit(event) {
    event.preventDefault();
    if (!sim.busy && !lockReason && !voiceBusy && text.trim()) {
      sim.send(text, target && target.id); setText('');
    }
  }

  // 해석 줄: 서버가 준 요약·사유만 보인다.
  let interpretation = [];
  if (sim.error) interpretation = [sim.error];
  else if (pending) interpretation = [pending.summary, pending.evidence && pending.evidence.slot_label].filter(Boolean);
  else if (result && result.decision === 'PASS_THROUGH') interpretation = ['이 화면은 시연 명령(자재 이송·복귀·정지·이어서)만 처리합니다.'];
  else if (result && result.decision === 'STOP') interpretation = [result.stop?.requested === true ? '전체 정지를 요청했습니다.' : result.stop?.detail || '정지할 작업이 없습니다'];
  else if (result) interpretation = [result.summary || (job && [job.action_label || job.action, job.slot_label].filter(Boolean).join(' · ')), result.reason].filter(Boolean);
  const stages = phase === 'done' ? ['done', 'done', resultTone === 'ok' ? 'done' : 'fail']
    : phase === 'running' ? ['done', 'done', 'active']
    : phase === 'confirm' ? ['done', 'done', 'idle']
    : sim.busy ? ['active', 'idle', 'idle'] : null;
  const [headline, ...details] = interpretation;

  const input = <form className="cmd-form" onSubmit={submit}>
    <div className="cmd-field">
      <textarea id="command-input" className="command-input" rows={phase === 'idle' ? 3 : 1} value={text}
        aria-label="자연어 명령" placeholder={phase === 'ask' ? '답을 입력해 다시 보내기' : '예: A 자재를 컨베이어로 옮겨줘'}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e); }} />
    </div>
    {(phase === 'idle' || phase === 'ask') && <button type="submit" className="btn-primary send" disabled={sim.busy || !!lockReason || !!voiceBusy || !text.trim()}>{sim.busy ? '보내는 중…' : phase === 'ask' ? '답변 보내기' : '보내기'}</button>}
    {lockReason && <small className="cmd-lock" role="status">{lockReason}</small>}
  </form>;

  const targetChips = <div className="cmd-chips" role="group" aria-label="대상 로봇">
    {robots.length === 0 && <small className="muted">대상 로봇 정보 없음</small>}
    {robots.map((r) => <button key={r.id} type="button" className="chip" title={r.title} aria-pressed={target && target.id === r.id}
      onClick={() => setPicked(r.id)}>{r.name}</button>)}
  </div>;
  const skillChips = (phase === 'idle' || phase === 'ask') && <div className="cmd-chips" role="group" aria-label="스킬 버튼">
    {skills.map((k) => <button key={k.key} type="button" className="chip" title={k.title} disabled={sim.busy || !!lockReason || !!k.blocked}
      onClick={() => sim.send(k.sentence, target && target.id)}>{k.label}</button>)}
    {skillNotes.map((note) => <small key={note} className="cmd-lock" role="status">{note}</small>)}
  </div>;

  const progress = job ? (job.progress || []).map((p) => ({ key: p.no, done: p.reached, text: `${p.no}/${p.of} ${p.label}` }))
    : goal ? (goal.plan || []).map((p) => ({ key: p.step, done: p.status === 'completed', text: `${stepText(p)} · ${p.status}` })) : [];

  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널{sim.mode === 'general' && <small className="muted"> · 일반 경로(계획·안전 검증)</small>}</span><small className={`state-label ${statusTone}`}>{statusLabel}</small></div>
    {stages && <ol className="cmd-stages" aria-label="진행 단계">
      {stages.map((st, i) => <li key={STAGES[i]} className={st}><span aria-hidden="true">{MARK[st]}</span>{STAGES[i]}<span className="sr-only"> {STAGE_TEXT[st]}</span></li>)}
    </ol>}
    <VoiceControl voice={voice} recovery={recovery} />
    {(phase === 'idle' || phase === 'ask') && targetChips}
    {skillChips}
    {/* 반복 작업: 예제(스킬) 버튼 아래·입력칸 위, 접고 펼침. 상태는 서버 값(server.repeat). */}
    <RepeatPanel server={server} lockReason={commandLock(server)} />
    {phase === 'idle' ? <div className="step step-input">{input}</div>
      : <div className={`step cmd-card ${cardTone}`}>
        {phase === 'running' || phase === 'done'
          ? <img src={shieldCheck} alt="" width="16" height="16" />
          : <span className={`tag ${cardTone}`}>{phase === 'confirm' && expired ? '확인 시간 만료' : sim.error ? '오류' : label}</span>}
        {headline && <b className="cmd-title">{headline}</b>}
        {details.map((line) => <small key={line} className="cmd-reason">{line}</small>)}
        {phase === 'confirm' && target && <small className="cmd-target">대상: {target.name}</small>}
        {/* 서버 값이 있는 것만 보인다 — 영향 구역은 서버가 주지 않아 넣지 않는다. */}
        {phase === 'confirm' && typeof gripper === 'boolean' && <small className="cmd-target">도구: {gripper ? '그리퍼' : '장착 도구 없음'}</small>}
        {phase === 'confirm' && Number.isFinite(pending.created_at) && <small className="cmd-target">판정 시각 {new Date(pending.created_at * 1000).toTimeString().slice(0, 8)}</small>}
        {pending && pending.kind === 'plan' && <ul className="plan" aria-label="계획 단계">
          {(pending.steps || []).map((line) => <li key={line}>{line}</li>)}
        </ul>}
        {pending && pending.latch && <small className="cmd-lock" role="status">{pending.latch}</small>}
        {pending && pending.kind === 'goal' && <ul className="plan">
          {(pending.plan || []).map((p) => <li key={p.step}><Spinner size={14} decorative />{stepText(p)}</li>)}
        </ul>}
        {pending && pending.kind === 'general' && <ol className="plan" aria-label="작업 계획">
          {pending.plan.steps.map((p) => <li key={p.no}>{p.description}</li>)}
        </ol>}
        {pending?.kind === 'general' && pending.validation?.detail && <small className="cmd-reason">안전 검증: {pending.validation.detail}</small>}
        {progress.length > 0 && <ul className="plan">
          {progress.map((p) => <li key={p.key} className={p.done ? 'done' : ''}>{p.done ? <img src={checkSquare} alt="" width="14" height="14" /> : <Spinner size={14} decorative />}{p.text}</li>)}
        </ul>}
        {running && <div className="run-bar" role="progressbar" aria-label="작업 진행률" aria-valuenow={percent} aria-valuemin="0" aria-valuemax="100"><i style={{ width: `${percent}%` }} /></div>}
        {running && <small className="info">{job && job.stage ? `Gazebo에서 실행 중 · ${job.stage}` : 'Gazebo에서 실행 중…'}</small>}
        {phase === 'done' && job && <b className={resultTone}>{resultLabel || `종료 코드 ${job.exit_code}`}</b>}
        {phase === 'done' && goal && !job && <small>목표 상태: {goal.status}</small>}
        {phase === 'ask' && input}
        {phase === 'confirm' && <div className="approve-row">
          <button className="approve" disabled={sim.busy || expired || !!lockReason} title={lockReason || undefined} onClick={() => sim.answer('confirm')}><span className="icon-play" aria-hidden="true" />{pending.kind === 'goal' ? '전체 실행 승인' : '실행 승인'}</button>
          {!expired && <button className="approve-cancel" disabled={sim.busy} onClick={() => sim.answer('cancel')}>취소</button>}
        </div>}
        {(phase === 'done' || phase === 'other' || (phase === 'confirm' && expired)) &&
          <button className="btn-secondary" onClick={sim.reset}>{phase === 'other' && result && result.decision === 'BLOCK' ? '명령 수정' : '새 명령 입력'}</button>}
      </div>}
    <button type="button" className="btn-secondary" onClick={onOpenSimulation}>시뮬레이션 보기</button>
    <div className="decide">
      {phase === 'confirm' && !expired && <p className="muted">{remaining}초 안에 승인하지 않으면 취소됩니다</p>}
      {phase === 'confirm' && !!lockReason && <p className="cmd-lock" role="status">{lockReason}</p>}
      {phase === 'confirm' && expired && <p>확인 시간이 지났습니다 — 명령을 다시 보내 주세요</p>}
      {phase === 'running' && <p className="muted">정지는 확인 없이 즉시 요청됩니다</p>}
      {phase === 'done' && <p className="muted">다음 명령을 입력해 주세요</p>}
      {sim.stopNote && <p className={sim.stopNote.tone} role="status">{sim.stopNote.text}</p>}
    </div>
  </section>;
}
