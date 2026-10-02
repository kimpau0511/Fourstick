import { useState } from 'react';
import { RESULT_LABELS } from '../simCommand.js';
import { useVoice } from '../voice.js';
import Spinner from './Spinner.jsx';
import './command.css';
import checkSquare from '../assets/check-square-2.svg';
import dotPanel from '../assets/dot-panel.svg';
import mic from '../assets/mic.svg';
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

// 로봇 이름은 역할 우선(이송 가능하면 "이송 로봇"), 모델·id는 title로 보조.
const robotName = (id, profile) => ((profile.supported_skills || []).some((k) => k === 'pick' || k === 'place') ? '이송 로봇' : id);

// 입력칸 안 마이크 버튼. 듣는 동안은 음성 파형(막대 3개), 연결·확정 중에는 스피너를 보인다.
function MicButton({ voice, locked }) {
  const { status, unavailable } = voice;
  const listening = status === 'listening';
  const busy = status === 'connecting' || status === 'finalizing';
  const name = unavailable ? '음성 입력(사용할 수 없음)' : listening ? '음성 인식 중 — 누르면 종료'
    : status === 'connecting' ? '음성 입력 연결 중 — 누르면 취소' : status === 'finalizing' ? '음성 입력 확정 중' : '음성 입력 시작';
  const onClick = listening ? voice.stop : status === 'connecting' ? voice.cancel : voice.start;
  return <button type="button" className="mic-btn" aria-label={name} aria-pressed={listening} title={unavailable || undefined}
    disabled={!!unavailable || status === 'finalizing' || (locked && !listening && !busy)} onClick={onClick}>
    {listening ? <span className="voice-bars" aria-hidden="true"><i /><i /><i /></span>
      : busy ? <Spinner size={16} label={name} /> : <img src={mic} alt="" width="16" height="16" />}
  </button>;
}

export default function CommandPanel({ now, sim, server }) {
  const [text, setText] = useState('');
  const [picked, setPicked] = useState(null);
  const robots = Object.entries((server && server.robots && server.robots.robots) || {})
    .map(([id, profile]) => ({ id, name: robotName(id, profile), title: profile.profile_id || id }));
  // 1대면 기본 선택. 여러 대인데 고르지 않았으면 보내기를 잠근다.
  const target = robots.length === 1 ? robots[0] : robots.find((r) => r.id === picked) || null;
  const connOk = !!server && server.conn.status === 'ok';
  const lockReason = !connOk ? '서버와 연결이 정상이 아니어서 명령을 보낼 수 없습니다'
    : robots.length > 1 && !target ? '대상 로봇을 먼저 선택해 주세요' : null;
  // 스킬 버튼: 서버가 지금 가능하다고 준 동작만. 서버가 받는 source는 text·stt_final뿐이라
  // 버튼으로 보낸 명령도 기록에는 text와 구분되지 않는다.
  const skills = ((server && server.simDemo && server.simDemo.materials) || []).flatMap((m) => [
    m.actions && m.actions.transfer && { key: `${m.model}-t`, label: `${m.korean} → 컨베이어`, sentence: `${m.korean}를 컨베이어로 옮겨줘` },
    m.actions && m.actions.return && { key: `${m.model}-r`, label: `${m.korean} 원래 자리로`, sentence: `${m.korean}를 원래 자리로 돌려놔` },
  ]).filter(Boolean);
  // 음성 입력. final만 명령으로 보낸다(대상 로봇 선택 유지) — partial·clarify는 보내지 않는다.
  const voice = useVoice({
    config: server && server.config, health: server && server.health,
    onFinal: (utterance, stt) => { if (!sim.busy && !lockReason) sim.send(utterance, target && target.id, stt); },
  });
  const { result, pending, job, goal } = sim;
  const [tone, label] = (result && DECISIONS[result.decision]) || ['info', result ? result.decision : ''];
  const remaining = sim.deadline ? Math.max(0, Math.ceil((sim.deadline - now.getTime()) / 1000)) : null;
  const expired = remaining === 0;
  const running = (job && job.status === 'running') || (goal && ['running', 'stopping'].includes(goal.status));
  const report = job && job.report;
  const [reportTone, resultLabel] = (report && RESULT_LABELS[report.status]) || ['warn', report ? report.status : ''];
  const resultTone = report ? reportTone : goal ? (goal.status === 'completed' ? 'ok' : goal.status === 'failed' ? 'danger' : 'warn') : 'warn';

  // 피그마 Light CommandPanel(23:2517)의 상태: 입력 → 해석·승인/되묻기/차단… → 실행 중 → 완료.
  // 입력 칸은 대기·되묻기에서만 보이고, 나머지는 결과 카드 하나가 그 자리를 쓴다.
  // 판정·문구는 서버 값이고, 여기서는 상태별 톤만 고른다.
  const phase = pending ? 'confirm' : running ? 'running' : (job || goal) ? 'done'
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

  function submit(event) {
    event.preventDefault();
    if (!sim.busy && !lockReason && text.trim()) { sim.send(text, target && target.id); setText(''); }
  }

  // 해석 줄: 서버가 준 요약·사유만 보인다.
  let interpretation = [];
  if (sim.error) interpretation = [sim.error];
  else if (pending) interpretation = [pending.summary, pending.evidence && pending.evidence.slot_label].filter(Boolean);
  else if (result && result.decision === 'PASS_THROUGH') interpretation = ['자재 이송·복귀·정지·이어서 명령이 아닙니다. 일반 계획 경로는 이 화면에 아직 연결되지 않았습니다.'];
  else if (result && result.decision === 'STOP') interpretation = ['시연 작업에 정지를 요청했습니다.'];
  else if (result) interpretation = [result.summary || (job && `${job.action_label || job.action} · ${job.slot_label || ''}`), result.reason].filter(Boolean);
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
      <MicButton voice={voice} locked={!!lockReason} />
    </div>
    {phase === 'idle' && <button type="submit" className="btn-primary send" disabled={sim.busy || !!lockReason || !text.trim()}>{sim.busy ? '보내는 중…' : '보내기'}</button>}
    {voice.partial && <small className="cmd-partial" aria-live="polite">{voice.partial}</small>}
    {voice.clarify && <small className="cmd-lock" role="status">{voice.clarify}</small>}
    {(voice.error || voice.unavailable) && <small className="cmd-lock" role="status">{voice.error || voice.unavailable}</small>}
    {lockReason && <small className="cmd-lock" role="status">{lockReason}</small>}
  </form>;

  const targetChips = <div className="cmd-chips" role="group" aria-label="대상 로봇">
    {robots.length === 0 && <small className="muted">대상 로봇 정보 없음</small>}
    {robots.map((r) => <button key={r.id} type="button" className="chip" title={r.title} aria-pressed={target && target.id === r.id}
      onClick={() => setPicked(r.id)}>{r.name}</button>)}
  </div>;
  const skillChips = (phase === 'idle' || phase === 'ask') && <div className="cmd-chips" role="group" aria-label="스킬 버튼">
    {skills.map((k) => <button key={k.key} type="button" className="chip" disabled={sim.busy || !!lockReason}
      onClick={() => sim.send(k.sentence, target && target.id)}>{k.label}</button>)}
    <button type="button" className="chip chip-stop" onClick={sim.stop}>즉시 정지</button>
  </div>;

  const progress = job ? (job.progress || []).map((p) => ({ key: p.no, done: p.reached, text: `${p.no}/${p.of} ${p.label}` }))
    : goal ? (goal.plan || []).map((p) => ({ key: p.step, done: p.status === 'completed', text: `${stepText(p)} · ${p.status}` })) : [];

  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널</span><small className={`state-label ${statusTone}`}>{statusLabel}</small></div>
    {stages && <ol className="cmd-stages" aria-label="진행 단계">
      {stages.map((st, i) => <li key={STAGES[i]} className={st}><span aria-hidden="true">{MARK[st]}</span>{STAGES[i]}<span className="sr-only"> {STAGE_TEXT[st]}</span></li>)}
    </ol>}
    {(phase === 'idle' || phase === 'ask') && targetChips}
    {skillChips}
    {phase === 'idle' ? <div className="step step-input">{input}</div>
      : <div className={`step cmd-card ${cardTone}`}>
        {phase === 'running' || phase === 'done'
          ? <img src={shieldCheck} alt="" width="16" height="16" />
          : <span className={`tag ${cardTone}`}>{phase === 'confirm' && expired ? '확인 시간 만료' : sim.error ? '오류' : label}</span>}
        {headline && <b className="cmd-title">{headline}</b>}
        {details.map((line) => <small key={line} className="cmd-reason">{line}</small>)}
        {phase === 'confirm' && target && <small className="cmd-target">대상: {target.name}</small>}
        {pending && pending.kind === 'goal' && <ul className="plan">
          {(pending.plan || []).map((p) => <li key={p.step}><Spinner size={14} decorative />{stepText(p)}</li>)}
        </ul>}
        {progress.length > 0 && <ul className="plan">
          {progress.map((p) => <li key={p.key} className={p.done ? 'done' : ''}>{p.done ? <img src={checkSquare} alt="" width="14" height="14" /> : <Spinner size={14} decorative />}{p.text}</li>)}
        </ul>}
        {running && <div className="run-bar" role="progressbar" aria-valuenow={percent} aria-valuemin="0" aria-valuemax="100"><i style={{ width: `${percent}%` }} /></div>}
        {running && <small className="info">Gazebo에서 실행 중…</small>}
        {phase === 'done' && job && <b className={resultTone}>{resultLabel || `종료 코드 ${job.exit_code}`}</b>}
        {phase === 'done' && goal && !job && <small>목표 상태: {goal.status}</small>}
        {phase === 'ask' && input}
        {phase === 'confirm' && <div className="approve-row">
          <button className="approve" disabled={sim.busy || expired || !connOk} title={connOk ? undefined : lockReason} onClick={() => sim.answer('confirm')}><span className="icon-play" aria-hidden="true" />{pending.kind === 'goal' ? '전체 실행 승인' : '실행 승인'}</button>
          {!expired && <button className="approve-cancel" disabled={sim.busy} onClick={() => sim.answer('cancel')}>취소</button>}
        </div>}
        {(phase === 'done' || phase === 'other' || (phase === 'confirm' && expired)) &&
          <button className="btn-secondary" onClick={sim.reset}>{phase === 'other' && result && result.decision === 'BLOCK' ? '명령 수정' : '새 명령 입력'}</button>}
      </div>}
    <div className="decide">
      {phase === 'confirm' && !expired && <p className="muted">{remaining}초 안에 승인하지 않으면 취소됩니다</p>}
      {phase === 'confirm' && !connOk && <p className="cmd-lock" role="status">{lockReason}</p>}
      {phase === 'confirm' && expired && <p>확인 시간이 지났습니다 — 명령을 다시 보내 주세요</p>}
      {phase === 'running' && <p className="muted">정지는 확인 없이 즉시 요청됩니다</p>}
      {phase === 'done' && <p className="muted">다음 명령을 입력해 주세요</p>}
      {sim.stopNote && <p className={sim.stopNote.tone} role="status">{sim.stopNote.text}</p>}
    </div>
  </section>;
}
