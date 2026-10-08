import { useEffect, useRef, useState } from 'react';
import './stepper.css';

// 가로 스테퍼(2026-10-08) — 원형 체크포인트 + 연결 바 + 아래 단계명. 표시 전용(단계를 눌러 이동·재실행하지 않는다).
// 상태는 simStepper.stepperOf가 서버 값으로만 만든다. 참고 디자인('Horizontal Stepper Timeline')의 형태만 따랐고 외부 코드·라이브러리는 쓰지 않았다.
const MARK = { done: '✓', failed: '✕', halted: '■', paused: '❚❚' };

export default function SimStepper({ model, compact = false, stamp = null }) {
  const [detail, setDetail] = useState(false);
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
  const tone = model.stale || model.link === 'delayed' ? 'warn' : { completed: 'ok', failed: 'danger', stopped: 'danger', stopping: 'danger', cancelled: 'warn', paused: 'warn', unknown: 'warn' }[model.phase] || 'info';
  return <div className={`stepper ${compact ? 'compact' : ''} ${model.phase} ${model.stale ? 'stale' : ''}`} aria-label="작업 진행">
    <div className="stepper-head">
      <p className="stepper-now" role="status"><span className="stepper-now-key">현재:</span> {model.current || '진행 정보 없음'}</p>
      <span className={`stepper-chip ${tone}`}>
        {model.stale ? '통신 확인 안 됨' : model.link === 'delayed' ? '갱신 지연' : model.phaseLabel}{steps ? (model.total ? ` · ${model.done}/${model.total} 단계 완료` : ` · ${model.done}단계 완료 · 남은 단계 확인 중`) : ''}
      </span>
    </div>
    {model.stale && <p className="stepper-note warn">진행 조회가 연속으로 실패했습니다 — 마지막 확인 상태{stamp ? `(${stamp})` : ''}를 그대로 보입니다. 완료로 처리하지 않습니다.</p>}
    {model.link === 'delayed' && <p className="stepper-note warn">최근 진행 조회 응답이 늦습니다(실패는 확인되지 않음) — 마지막 확인 상태{stamp ? `(${stamp})` : ''}를 보입니다.</p>}
    {model.link === 'ok' && model.stageSec != null && model.stageSec >= 6 && <p className="stepper-note">이 단계 {model.stageSec}초째 진행 중 · 서버 조회 정상</p>}
    {model.phase === 'completed' && !model.allConfirmed && <p className="stepper-note">실행은 완료로 끝났지만 일부 단계의 진행 기록을 받지 못했습니다.</p>}
    {steps ? <div className="stepper-scroll" ref={scroller}>
      <ol className="stepper-track">
        {steps.map((s, i) => <li key={`${s.index}-${s.no}`} data-step={i} className={`stepper-step ${s.status}`} aria-current={s.status === 'current' ? 'step' : undefined}>
          {i > 0 && <span className={`stepper-bar ${steps[i - 1].status === 'done' && s.status === 'done' ? 'filled' : ''}`} aria-hidden="true" />}
          <span className="stepper-dot" aria-hidden="true">{MARK[s.status] || (s.placeholder ? '…' : s.index)}</span>
          <span className={`stepper-label ${s.named ? '' : 'unnamed'}`}>{s.label}</span>
          <span className="sr-only">{{ done: '완료', current: '진행 중', paused: '일시정지', halted: '멈춘 지점', failed: '도달 실패', wait: '대기' }[s.status]}</span>
        </li>)}
      </ol>
    </div> : <p className="stepper-note">단계 목록을 기다리는 중 — 실행기가 사전 확인을 마치면 단계가 표시됩니다</p>}
    {!compact && steps && <div className="stepper-detail">
      <button type="button" className="stepper-toggle" aria-expanded={detail} onClick={() => setDetail((v) => !v)}>{detail ? '상세 닫기' : '상세 보기'}</button>
      {detail && <ol className="stepper-list">
        {steps.map((s) => <li key={`d-${s.index}-${s.no}`} className={s.status}>
          <span className="stepper-list-no">{s.index}</span>
          <span>{s.label}{s.prior ? ' · 앞 실행에서 완료' : ''}</span>
          <span className="stepper-list-state">{{ done: '완료(실행기 도달 확인)', current: '진행 중', paused: '일시정지', halted: '여기서 멈춤', failed: '도달 실패', wait: '대기' }[s.status]}</span>
        </li>)}
      </ol>}
    </div>}
  </div>;
}
