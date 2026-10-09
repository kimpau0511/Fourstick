import { Fragment, useEffect, useRef } from 'react';
import './stepper.css';
import { stepperChip, stepperNotes } from '../simStepper.js';

// 가로 스테퍼(2026-10-08) — 원형 체크포인트 + 연결 바 + 아래 단계명. 표시 전용(단계를 눌러 이동·재실행하지 않는다).
// 상태는 simStepper.stepperOf가 서버 값으로만 만든다. 참고 디자인('Horizontal Stepper Timeline')의 형태만 따랐고 외부 코드·라이브러리는 쓰지 않았다.
const MARK = { done: '✓', failed: '✕', halted: '■', paused: '❚❚' };

// 단계 이름을 의미 단위로 나눈다(2026-10-09): 띄어쓰기로 나뉜 낱말과 괄호 묶음('(컨베이어)')이 각각 한 덩어리다.
// 덩어리 안에서는 줄을 바꾸지 않고(닫는 괄호만 따로 내려가지 않게) 덩어리 사이에서만 바꾼다. 글자는 줄이거나 자르지 않는다.
function labelParts(label) {
  const text = String(label);
  const out = [];
  for (const m of text.matchAll(/\([^)]*\)|[^\s(]+/g)) out.push({ text: m[0], spaced: m.index > 0 && /\s/.test(text[m.index - 1]) });
  return out;
}

export function StepLabel({ label }) {
  return labelParts(label).map((part, i) => <Fragment key={i}>
    {i > 0 && (part.spaced ? ' ' : <wbr />)}<span className="stepper-seg">{part.text}</span>
  </Fragment>);
}

const STATE_TEXT = { done: '완료(실행기 도달 확인)', current: '진행 중', paused: '일시정지', halted: '여기서 멈춤', failed: '도달 실패', wait: '대기' };

/** 단계 목록(상세 보기 팝업). */
export function StepperList({ steps }) {
  return <ol className="stepper-list">
    {steps.map((s) => <li key={`d-${s.index}-${s.no}`} className={s.status}>
      <span className="stepper-list-no">{s.index}</span>
      <span className="stepper-list-label"><StepLabel label={s.label} />{s.prior ? ' · 앞 실행에서 완료' : ''}</span>
      <span className="stepper-list-state">{STATE_TEXT[s.status]}</span>
    </li>)}
  </ol>;
}

// compact(전체화면): 머리 + 안내 + 단계(2026-10-08 그대로). 일반 화면(panel): 머리 한 줄(제목·현재 작업·상태) + 단계만 —
// 긴 안내·단계 목록은 SimOverlay의 상세 보기 팝업이 보인다(2026-10-09, 진행 칸 세로 스크롤 없앰).
export default function SimStepper({ model, compact = false, stamp = null, title = null }) {
  const scroller = useRef(null);
  const modelKey = model ? model.key : null;
  const focusIndex = model && model.steps ? model.steps.findIndex((s) => ['current', 'paused', 'halted', 'failed'].includes(s.status)) : -1;
  // 현재 단계가 보이게 가로 스크롤만 맞춘다(좁은 화면). 페이지 스크롤은 건드리지 않는다.
  // 폭이 바뀌면(창 크기·전체화면) 다시 맞춘다.
  useEffect(() => {
    const box = scroller.current;
    if (!box) return undefined;
    const center = () => {
      const node = focusIndex >= 0 ? box.querySelector(`[data-step="${focusIndex}"]`) : null;
      if (node) box.scrollLeft = Math.max(0, node.offsetLeft - box.clientWidth / 2 + node.offsetWidth / 2);
    };
    center();
    if (typeof ResizeObserver === 'undefined') return undefined;
    const watch = new ResizeObserver(center);
    watch.observe(box);
    return () => watch.disconnect();
  }, [focusIndex, modelKey]);
  if (!model) return null;
  const { steps } = model;
  const chip = stepperChip(model);
  const notes = compact ? stepperNotes(model, stamp) : [];
  return <div className={`stepper ${compact ? 'compact' : 'panel'} ${model.phase} ${model.stale ? 'stale' : ''}`} aria-label="작업 진행">
    <div className="stepper-head">
      <p className="stepper-now" role="status" title={compact ? undefined : `현재: ${model.current || '진행 정보 없음'}`}>
        {title && <span className="stepper-title">{title}</span>}
        <span className="stepper-now-key">현재:</span> <StepLabel label={model.current || '진행 정보 없음'} /></p>
      <span className={`stepper-chip ${chip.tone}`}>{chip.text}</span>
    </div>
    {/* 안내 줄과 단계 영역은 자리를 미리 잡아 둔다 — 안내 문구가 생기고 없어지거나 단계 목록이 도착해도 박스 높이·아래 UI가 움직이지 않는다. */}
    {compact && <div className="stepper-notes">
      {notes.map((n) => <p key={n.key} className={`stepper-note ${n.tone}`}>{n.text}</p>)}
    </div>}
    {steps ? <div className="stepper-scroll" ref={scroller}>
      <ol className="stepper-track">
        {steps.map((s, i) => <li key={`${s.index}-${s.no}`} data-step={i} className={`stepper-step ${s.status}`} aria-current={s.status === 'current' ? 'step' : undefined}>
          {i > 0 && <span className={`stepper-bar ${steps[i - 1].status === 'done' && s.status === 'done' ? 'filled' : ''}`} aria-hidden="true" />}
          <span className="stepper-dot" aria-hidden="true">{MARK[s.status] || (s.placeholder ? '…' : s.index)}</span>
          <span className={`stepper-label ${s.named ? '' : 'unnamed'}`}><StepLabel label={s.label} /></span>
          <span className="sr-only">{{ done: '완료', current: '진행 중', paused: '일시정지', halted: '멈춘 지점', failed: '도달 실패', wait: '대기' }[s.status]}</span>
        </li>)}
      </ol>
    </div> : <div className="stepper-scroll stepper-waiting"><p className="stepper-note">단계 목록을 기다리는 중 — 실행기가 사전 확인을 마치면 단계가 표시됩니다</p></div>}
  </div>;
}
