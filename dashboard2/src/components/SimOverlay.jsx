import { useCallback, useState } from 'react';
import SimView3D from '../SimView3D.jsx';
import { simLabel } from '../simLabel.js';
import dotSimDanger from '../assets/dot-sim-danger.svg';
import dotSimRunning from '../assets/dot-sim-running.svg';
import dotSimStopping from '../assets/dot-sim-stopping.svg';

function timeText(date) {
  return [date.getHours(), date.getMinutes(), date.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

const STAGES = ['명령·범위 확인', '로봇 상태 확인', '안전 검증'];

export default function SimOverlay({ state, at, onClose, onRerun }) {
  const [scene, setScene] = useState({ mode: '3d', status: 'connecting', fps: 0 });
  const onView = useCallback((view) => setScene(view), []);
  const time = at ? timeText(at) : '';
  const card = {
    running: {
      dot: dotSimRunning, title: '안전 검사 실행 중', titleTone: '', sub: simLabel(scene),
      section: '검사 세부 진행',
      stages: [{ mark: '1', tone: 'done' }, { mark: '2', tone: 'current' }, { mark: '3', tone: 'wait' }],
      lines: ['done', 'wait'],
      foot: `1/3 단계 완료 · 마지막 갱신 ${time}   ·   이 창이 열려 있어도 헤더의 즉시 정지는 언제든 즉시 실행됩니다.`,
    },
    stopping: {
      dot: dotSimStopping, title: '즉시 정지 처리됨', titleTone: 'danger', sub: '검사가 중단되었습니다 · 이전 검증 결과는 재사용되지 않습니다',
      section: '즉시 정지 처리 흐름',
      stages: [{ mark: '✓', tone: 'requested', label: '즉시 정지 요청됨' }, { mark: '✓', tone: 'confirmed', label: '정지 확인' }],
      lines: ['danger'],
      foot: `정지 확인 · ${time}   ·   재개하려면 명령을 다시 확인해야 합니다. 헤더의 즉시 정지는 계속 즉시 실행됩니다.`,
    },
    danger: {
      dot: dotSimDanger, title: '안전 검사 결과 — 실행 불가', titleTone: 'danger', sub: '검증 완료 · 위험 감지',
      section: '안전 검사 완료 · 위험 판정',
      stages: [{ mark: '✓', tone: 'done' }, { mark: '✓', tone: 'current' }, { mark: '!', tone: 'danger', label: '안전 검증 (위험)' }],
      lines: ['done', 'danger'],
      foot: '3/3 완료 · 위험 판정으로 실행이 차단되었습니다   ·   헤더의 즉시 정지는 언제든 즉시 실행됩니다.',
    },
  }[state];
  return <div className="scrim">
    <section className={`sim-card ${state}`} role="dialog" aria-label={card.title}>
      <div className="sim-head">
        <img src={card.dot} alt="" width="8" height="8" />
        <div><strong className={card.titleTone}>{card.title}</strong><small>{card.sub}</small></div>
        {state === 'running' && <button className="sim-cancel" onClick={onClose}><b>안전 검사 취소</b><small>검사만 중단 · 로봇 동작에는 영향 없음</small></button>}
      </div>
      <div className="sim-video">
        {state === 'running' && <SimView3D onView={onView} />}
        {state === 'stopping' && <p className="sim-message">로봇이 현재 위치에서 정지했습니다<br />(정지 확인됨 · 이 명령의 이전 검증 결과는 폐기되었습니다)</p>}
        {/* 위험 판정: 영상 칸은 시뮬레이션 화면 자리다 — 문구를 두지 않는다(피그마 결정 2026-10-02). */}
        {state === 'danger' && <SimView3D onView={onView} />}
      </div>
      <hr />
      <p className="sim-section">{card.section}</p>
      <div className="stages">
        {card.stages.map((s, i) => <div key={i} className="stage-wrap">
          {i > 0 && <i className={`stage-line ${card.lines[i - 1]}`} />}
          <div className="stage"><span className={`stage-mark ${s.tone}`}>{s.mark}</span><small className={s.tone === 'wait' ? 'muted' : s.tone === 'danger' ? 'danger' : ''}>{s.label || STAGES[i]}</small></div>
        </div>)}
      </div>
      <p className="sim-foot">{card.foot}</p>
      {/* 위험 판정에서는 판정을 덮어쓰는 승인 버튼을 두지 않는다(피그마 메모). */}
      {state === 'danger' && <div className="sim-actions"><button onClick={onClose}>명령 수정</button><button onClick={onRerun}>검사 재실행</button></div>}
    </section>
  </div>;
}
