import { useEffect, useRef, useState } from 'react';
import { RESULT_LABELS } from '../simCommand.js';
import { commandLock } from '../server.js';
import { useVoice } from '../voice.js';
import { simAlertOf } from '../simPanels.js';
import RepeatPanel from './RepeatPanel.jsx';
import SimAlert from './SimAlert.jsx';
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

// 로봇 이름은 역할 우선(이송 가능하면 "이송 로봇"), 모델·id는 title로 보조.
const robotName = (id, profile) => ((profile.supported_skills || []).some((k) => k === 'pick' || k === 'place') ? '이송 로봇' : id);

// 음성 인식 신뢰도 — STT 엔진이 준 점수(세그먼트 avg_logprob의 exp를 길이 가중 평균, 0~1)를 그대로 %로.
// 보정된 정답 확률이 아니다. 중간 결과는 최근 window 구간 전사의 점수다. 없는 점수는 만들지 않는다:
// 말하는 중 점수가 없으면 '인식 중…', 끝난 뒤 값이 없거나 0~1 밖이면 '확인 불가'. 계획·안전 검증·실행 승인에 쓰지 않는다.
function validScore(v) {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 1;
}
function SttConfidence({ score }) {
  const ok = validScore(score.value);
  const label = ok ? `음성 인식 신뢰도 ${Math.round(score.value * 100)}%`
    : score.kind === 'partial' ? '인식 중…' : '음성 인식 신뢰도 확인 불가';
  return <span className={`cmd-rate${ok ? '' : ' unmeasured'}`} data-testid="stt-confidence" data-kind={score.kind} role="status">{label}</span>;
}

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

export default function CommandPanel({ now, sim, server, onOpenSimulation }) {
  const [text, setText] = useState('');
  // 음성 확정 결과(2026-10-08): 바로 보내지 않고 입력칸에 넣는다. 사용자가 확인·수정한 뒤 보내기를 누른다.
  // { text: STT 확정 문장, rawText, confidence, requestId } — 보낼 때 고쳤는지(edited)를 함께 보낸다.
  const [sttDraft, setSttDraft] = useState(null);
  // 음성 인식 신뢰도(2026-10-08): 발화 인식이 끝나면 STT가 준 점수를 바로 보인다. 모델 추정치이지 정확도가 아니다.
  // { value: 0~1 또는 null, kind: 'final' | 'clarify' } — 새 녹음을 시작하면 지운다.
  const [sttScore, setSttScore] = useState(null);
  const sttDraftRef = useRef(null);
  useEffect(() => { sttDraftRef.current = sttDraft; }, [sttDraft]);
  // 음성 자동 보내기(2026-10-08): 서버가 정상 확정한 최종 STT만 같은 계획 요청(sim.send)으로 바로 보낸다. 실행 승인은 그대로 사람이 한다.
  // autoSent: 자동으로 보낸 원문(화면에 남겨 확인·수정할 수 있게). voiceNote: 보내지 않은 이유 등 안내.
  const [autoSent, setAutoSent] = useState(null);
  const [voiceNote, setVoiceNote] = useState(null);
  const sentUtterances = useRef(new Set()); // 이미 보낸 발화 id — 같은 최종 결과는 한 번만
  const sendLock = useRef(false);           // 자동·버튼·스킬 보내기 공통 — 요청이 끝날 때까지 다음 요청을 막는다
  const editedWhileRecording = useRef(false); // 녹음 중(또는 녹음 전부터) 사용자가 입력칸에 쓴 문장이 있다
  const textRef = useRef('');
  const live = useRef({});                   // 최종 결과가 도착한 순간의 화면 상태(렌더 사이 값 대신 최신 값)
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
  // 음성 입력. final(확정 전사)만 입력칸에 넣는다 — partial·clarify는 넣지 않는다. 자동으로 보내지 않는다:
  // 사용자가 문장을 확인·수정한 뒤 보내기를 눌러야 계획을 요청한다(잘못 알아들은 문장이 그대로 계획되지 않게).
  // 텍스트·음성(자동)·스킬 버튼이 함께 쓰는 보내기. 같은 계획 입구(sim.send) 하나이고, 요청이 끝날 때까지 다음 요청을 막는다.
  // 실패해도 다시 보내지 않는다(자동 재시도 없음) — 사용자가 직접 다시 보낸다.
  function dispatch(utterance, stt) {
    if (sendLock.current) return false;
    sendLock.current = true;
    Promise.resolve(sim.send(utterance, target && target.id, stt)).finally(() => { sendLock.current = false; });
    return true;
  }
  const voice = useVoice({
    config: server && server.config, health: server && server.health,
    // 서버가 정상 확정한 최종 결과(final)만 온다 — 중간 결과·되묻기(clarify)·오류·빈 문장은 여기로 오지 않는다.
    onFinal: (utterance, stt) => {
      const meta = { text: utterance, rawText: stt && stt.rawText, confidence: stt && stt.confidence, requestId: stt && stt.requestId,
        sessionId: stt && stt.sessionId, persisted: stt && stt.persisted, utteranceId: stt && stt.utteranceId };
      setSttScore({ value: stt ? stt.confidence : null, kind: 'final' });
      const id = meta.utteranceId;
      if (id && sentUtterances.current.has(id)) return; // 같은 발화의 최종 결과가 또 왔다 — 한 번만
      const L = live.current;
      if (editedWhileRecording.current) {
        // 사용자가 쓴 문장을 덮어쓰지도 보내지도 않는다 — 수동 보내기로 둔다.
        setVoiceNote({ tone: 'warn', text: `입력칸에 직접 쓴 문장이 있어 음성 결과를 넣지도 보내지도 않았습니다 — 인식 결과: “${utterance}”. 보낼 문장을 확인한 뒤 보내기를 누르세요` });
        return;
      }
      const waitWhy = L.busy || sendLock.current ? '계획 요청이 진행 중입니다'
        : L.phase === 'confirm' ? '실행 승인을 기다리는 계획이 있습니다'
          : L.phase === 'running' ? '작업이 실행 중입니다'
            : L.phase !== 'idle' && L.phase !== 'ask' ? '이전 명령 결과가 열려 있습니다 — 새 명령 입력을 누른 뒤 다시 말해 주세요'
              : document.visibilityState === 'hidden' ? '화면이 보이지 않습니다' : null;
      if (waitWhy) {
        // 대기열에 쌓지 않는다 — 버리고 이유만 보인다.
        setVoiceNote({ tone: 'warn', text: `${waitWhy} — 음성 결과를 보내지 않았습니다(인식 결과: “${utterance}”)` });
        return;
      }
      if (L.lockReason) {
        // 명령을 받을 수 없는 상태(연결·반복 작업 등): 보내지 않고 입력칸에 넣어 둔다 — 풀리면 사용자가 보내기를 누른다.
        setText(utterance); textRef.current = utterance;
        setSttDraft({ id: id || `utt_${Date.now().toString(36)}`, ...meta });
        setVoiceNote({ tone: 'warn', text: `${L.lockReason} — 자동으로 보내지 않고 입력칸에 넣었습니다` });
        return;
      }
      if (id) sentUtterances.current.add(id);
      if (!dispatch(utterance, { ...meta, edited: false })) {
        setVoiceNote({ tone: 'warn', text: `다른 요청이 진행 중이라 음성 결과를 보내지 않았습니다(인식 결과: “${utterance}”)` });
        return;
      }
      setText(''); textRef.current = ''; setSttDraft(null);
      setAutoSent(meta); setVoiceNote(null);
    },
    // 신뢰도가 기준 미만이라 다시 말해 달라고 할 때도 그 점수를 보인다(입력칸에도 넣지 않고 보내지도 않는다).
    onClarify: (info) => setSttScore({ value: info ? info.confidence : null, kind: 'clarify' }),
    onEmpty: () => setVoiceNote({ tone: 'warn', text: '인식된 문장이 비어 있어 보내지 않았습니다 — 다시 말해 주세요' }),
    // 새 녹음 시작: 이전 음성 결과(입력칸 문장·점수)를 지운다. 사용자가 직접 친 문장은 그대로 두고, 그 문장이 있으면 자동 보내기를 하지 않는다.
    onStart: () => {
      setSttScore(null); setVoiceNote(null);
      if (sttDraftRef.current) { setText(''); textRef.current = ''; setSttDraft(null); }
      editedWhileRecording.current = !sttDraftRef.current && !!textRef.current.trim();
    },
  });
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
  // 그린 직후 최신 화면 상태를 남긴다(최종 결과 콜백이 읽는다). 보내는 중 여부는 sendLock이 즉시 막는다.
  useEffect(() => { live.current = { phase, busy: !!sim.busy, lockReason }; });

  // 말하는 중: 중간 결과의 점수(없으면 '인식 중…'). 끝나면: 최종(또는 되묻기) 점수.
  const recording = ['connecting', 'listening', 'finalizing'].includes(voice.status);
  const scoreView = recording ? { value: voice.partialConfidence, kind: 'partial' } : sttScore;

  function submit(event) {
    event.preventDefault();
    if (!sim.busy && !sendLock.current && !lockReason && text.trim()) {
      if (voice.status !== 'idle' && voice.status !== 'error') voice.cancel(); // 텍스트가 이긴다 — 듣던 음성은 버린다
      const stt = sttDraft ? { ...sttDraft, edited: text.trim() !== (sttDraft.text || '').trim() } : null;
      if (!dispatch(text, stt)) return;
      setText(''); textRef.current = ''; setSttDraft(null); setSttScore(null); setAutoSent(null); setVoiceNote(null);
    }
  }
  // 새 명령: 이전 음성 결과(보낸 문장·점수·안내)를 남기지 않는다.
  function newCommand() { sim.reset(); setAutoSent(null); setSttScore(null); setVoiceNote(null); }
  // 잘못 인식한 문장 고치기: 승인 대기 계획이면 먼저 취소(서버에 reject)하고, 입력칸에 원문을 넣는다. 고친 문장은 새 계획이 되고
  // 이전 계획의 승인은 이어지지 않는다(계획마다 승인 id·해시가 따로다).
  async function editVoice() {
    const s = autoSent;
    if (!s) return;
    if (phase === 'confirm' && !(await sim.answer('cancel'))) return;
    sim.reset();
    setText(s.text); textRef.current = s.text;
    setSttDraft({ id: s.utteranceId || `utt_${Date.now().toString(36)}`, ...s });
    setAutoSent(null);
    setVoiceNote({ tone: 'info', text: phase === 'confirm' ? '이전 계획을 취소했습니다 — 문장을 고친 뒤 보내기를 누르세요' : '문장을 고친 뒤 보내기를 누르세요' });
    const box = document.getElementById('command-input');
    if (box) box.focus();
  }

  // 해석 줄: 서버가 준 요약·사유만 보인다.
  let interpretation = [];
  if (sim.error) interpretation = [sim.error];
  else if (pending) interpretation = [pending.summary, pending.evidence && pending.evidence.slot_label].filter(Boolean);
  else if (result && result.decision === 'PASS_THROUGH') interpretation = ['이 화면은 시연 명령(자재 이송·복귀·정지·이어서)만 처리합니다.'];
  else if (result && result.decision === 'STOP') interpretation = [result.stop?.requested === true ? '전체 정지를 요청했습니다.' : result.stop?.detail || '정지할 작업이 없습니다'];
  // 이송이 아닌 계획(홈 이동 등)은 작업 이름이 없다 — 'undefined'를 보이지 않는다.
  else if (result) interpretation = [result.summary || (job && (job.action_label || job.action)
    ? `${job.action_label || job.action}${job.slot_label ? ` · ${job.slot_label}` : ''}` : (job ? '계획 실행' : null)), result.reason].filter(Boolean);
  const stages = phase === 'done' ? ['done', 'done', resultTone === 'ok' ? 'done' : 'fail']
    : phase === 'running' ? ['done', 'done', 'active']
    : phase === 'confirm' ? ['done', 'done', 'idle']
    : sim.busy ? ['active', 'idle', 'idle'] : null;
  const [headline, ...details] = interpretation;
  // 오류 원인·조치(2026-10-07): 시뮬레이션 창이 닫혀도 카드에 남는다. 명령 흐름에서 나온 오류만(서버 상태 오류는 시뮬레이션 보기에서).
  const cmdAlert = (() => {
    const a = simAlertOf({ cmd: sim, server: null });
    return a && ['request', 'decision', 'job', 'control'].includes(a.source) && (phase === 'done' || phase === 'other' || phase === 'ask') ? a : null;
  })();

  const input = <form className="cmd-form" onSubmit={submit}>
    <div className="cmd-field">
      <textarea id="command-input" className="command-input" rows={phase === 'idle' ? 3 : 1} value={text}
        aria-label="자연어 명령" placeholder={phase === 'ask' ? '답을 입력해 다시 보내기' : '예: A 자재를 컨베이어로 옮겨줘'}
        onChange={(e) => {
          setText(e.target.value); textRef.current = e.target.value;
          if (!e.target.value.trim()) setSttDraft(null);
          if (recording) editedWhileRecording.current = true; // 녹음 중 직접 고쳤다 — 음성 결과로 덮어쓰거나 보내지 않는다
        }}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e); }} />
      <MicButton voice={voice} locked={!!lockReason} />
    </div>
    {/* 말하는 동안 STT 중간 결과(입력칸 바로 아래) */}
    {voice.partial && <small className="cmd-partial" aria-live="polite">{voice.partial}</small>}
    {sttDraft && text.trim() && <small className="cmd-stt-hint" role="status">음성 인식 결과입니다 — 문장을 확인·수정한 뒤 {phase === 'ask' ? '답변 보내기' : '보내기'}를 누르세요</small>}
    {voiceNote && <small className={`cmd-voice-note ${voiceNote.tone}`} role="status">{voiceNote.text}</small>}
    {voice.notice && <small className="cmd-voice-note warn" role="status">{voice.notice}</small>}
    {(phase === 'idle' || phase === 'ask') && <div className="cmd-actions">
      {/* 음성 인식 신뢰도만 보인다(2026-10-08). 전체·합성 평가 인식률과 정답 확정 점수는 이 영역에 없다
          (평가 데이터·조회 API·정답 확정 API는 평가용으로 그대로 둔다). */}
      {scoreView ? <SttConfidence score={scoreView} /> : <span />}
      <button type="submit" className="btn-primary send" disabled={sim.busy || !!lockReason || !text.trim()}>{sim.busy ? '보내는 중…' : phase === 'ask' ? '답변 보내기' : '보내기'}</button>
    </div>}
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
    {skills.map((k) => <button key={k.key} type="button" className="chip" title={k.title} disabled={sim.busy || !!lockReason || !!k.blocked}
      onClick={() => { if (dispatch(k.sentence, null)) { setAutoSent(null); setVoiceNote(null); } }}>{k.label}</button>)}
    {skillNotes.map((note) => <small key={note} className="cmd-lock" role="status">{note}</small>)}
  </div>;

  const progress = job ? (job.progress || []).map((p) => ({ key: p.no, done: p.reached, text: `${p.no}/${p.of} ${p.label}` }))
    : goal ? (goal.plan || []).map((p) => ({ key: p.step, done: p.status === 'completed', text: `${stepText(p)} · ${p.status}` })) : [];

  return <section className="command">
    <div className="command-head"><span><img src={dotPanel} alt="" width="8" height="8" />통합 작업 명령 패널{sim.mode === 'general' && <small className="muted"> · 일반 경로(계획·안전 검증)</small>}</span><small className={`state-label ${statusTone}`}>{statusLabel}</small></div>
    {stages && <ol className="cmd-stages" aria-label="진행 단계">
      {stages.map((st, i) => <li key={STAGES[i]} className={st}><span aria-hidden="true">{MARK[st]}</span>{STAGES[i]}<span className="sr-only"> {STAGE_TEXT[st]}</span></li>)}
    </ol>}
    {(phase === 'idle' || phase === 'ask') && targetChips}
    {skillChips}
    {/* 반복 작업: 예제(스킬) 버튼 아래·입력칸 위, 접고 펼침. 상태는 서버 값(server.repeat). */}
    <RepeatPanel server={server} lockReason={commandLock(server)} />
    {phase === 'idle' ? <div className="step step-input">{input}</div>
      : <div className={`step cmd-card ${cardTone}`}>
        {phase === 'running' || phase === 'done'
          ? <img src={shieldCheck} alt="" width="16" height="16" />
          : <span className={`tag ${cardTone}`}>{phase === 'confirm' && expired ? '확인 시간 만료' : sim.error ? '오류' : label}</span>}
        {/* 자동으로 보낸 원문과 그 음성 인식 신뢰도(입력칸이 사라진 뒤에도 확인할 수 있게) */}
        {autoSent && phase !== 'idle' && <div className="cmd-sent-row">
          <small className="cmd-sent" data-testid="voice-sent">음성으로 보낸 문장: “{autoSent.text}”</small>
          <SttConfidence score={{ value: autoSent.confidence, kind: 'final' }} />
        </div>}
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
        {cmdAlert && <SimAlert alert={cmdAlert} inline shown={[headline, ...details, resultLabel]} />}
        {/* 입력칸이 없는 단계(승인 대기·실행 중 등)에서 음성 결과를 보내지 않았으면 그 이유를 카드에 보인다 */}
        {phase !== 'ask' && voiceNote && <small className={`cmd-voice-note ${voiceNote.tone}`} role="status">{voiceNote.text}</small>}
        {phase !== 'ask' && voice.notice && <small className="cmd-voice-note warn" role="status">{voice.notice}</small>}
        {phase === 'ask' && input}
        {phase === 'confirm' && <div className="approve-row">
          <button className="approve" disabled={sim.busy || expired || !!lockReason} title={lockReason || undefined} onClick={() => sim.answer('confirm')}><span className="icon-play" aria-hidden="true" />{pending.kind === 'goal' ? '전체 실행 승인' : '실행 승인'}</button>
          {!expired && <button className="approve-cancel" disabled={sim.busy} onClick={() => sim.answer('cancel')}>취소</button>}
        </div>}
        {autoSent && ['confirm', 'other', 'ask'].includes(phase) && <button type="button" className="btn-secondary voice-edit" disabled={sim.busy}
          onClick={editVoice}>{phase === 'confirm' ? '계획 취소 후 문장 수정' : '문장 수정해 다시 보내기'}</button>}
        {(phase === 'done' || phase === 'other' || (phase === 'confirm' && expired)) &&
          <button className="btn-secondary" onClick={newCommand}>{phase === 'other' && result && result.decision === 'BLOCK' ? '명령 수정' : '새 명령 입력'}</button>}
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
