import { Fragment, useEffect, useRef, useState } from 'react';
import { DetailRow, ExpandButton } from '../components/ExpandRow.jsx';
import { useExpand } from '../components/useExpand.js';
import Spinner from '../components/Spinner.jsx';
import { useWidgetPrefs } from '../widgetPrefs.js';
import './records.css';

// 진단 = useServer 수집값 + 장면 카메라(/v1/scene, 이 페이지에서 직접 조회).
// 상태 이력·지속 시간은 "이 화면이 열린 뒤 본 것"뿐이다 — 서버 이력이 아니다.
const SCENE_POLL_MS = 10000; // 장면 카메라 상태 조회 주기(화면 갱신 속도일 뿐 판단 기준 아님)
const STATUS = {
  ok: ['정상', 'ok', '●'], warn: ['경고', 'warn', '▲'], fail: ['장애', 'danger', '■'], none: ['수신 없음', 'muted', '?'],
};
const WIDGET_ROWS = [['ros2', 'ROS 2'], ['planner', 'PLANNER'], ['latency', 'LATENCY']]; // StatusWidget 행 id
// 내보내기에 넣을 항목(⑲-5). payload의 키는 항목마다 정해져 있다.
const EXPORT_ITEMS = [
  ['connection', '연결 상태'], ['services', '서비스 상태'], ['state_changes', '상태 변화 이력'],
  ['server', '서버 상태(health·정지 진단)'], ['policies', '정책'], ['recent_jobs', '최근 작업'],
];
const ORDER = { fail: 0, warn: 1, none: 2, ok: 3 }; // 문제 있는 것 위로
const stamp = () => Date.now(); // 이벤트 시점 시각(렌더 순수성 규칙 때문에 렌더 밖 함수로 둔다)
const clock = (ms) => (ms == null ? '—' : new Date(ms).toTimeString().slice(0, 8));
const span = (ms) => {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}초` : s < 3600 ? `${Math.floor(s / 60)}분 ${s % 60}초` : `${Math.floor(s / 3600)}시간 ${Math.floor((s % 3600) / 60)}분`;
};

function useScene() {
  const [scene, setScene] = useState({ at: null, ok: null, detail: '' });
  useEffect(() => {
    let cancelled = false;
    let timer;
    const loop = async () => {
      let next;
      try {
        const res = await fetch('/v1/scene', { cache: 'no-store' });
        let body = {};
        try { body = await res.json(); } catch { /* JSON이 아닌 응답 */ }
        next = res.ok && body.available !== false
          ? { ok: true, detail: '' }
          : { ok: false, detail: body.detail || body.reason_code || `응답 ${res.status}` };
      } catch (error) {
        next = { ok: null, detail: `조회 실패: ${error.message}` }; // 서버에 닿지 못함 → 확인 안 됨
      }
      if (!cancelled) setScene({ at: Date.now(), ...next });
      if (!cancelled) timer = setTimeout(loop, SCENE_POLL_MS);
    };
    loop();
    return () => { cancelled = true; clearTimeout(timer); };
  }, []);
  return scene;
}

// 서버 값 → 서비스 목록. 값이 없으면 '수신 없음' — 정상으로 세지 않는다(설계원칙 4).
function buildServices(server, scene) {
  const { health, robots, simState, conn, config } = server;
  const live = conn?.status === 'ok' && health;
  const feature = (f, label) => {
    if (!live || !f) return { status: 'none', error: '값 없음' };
    return f.available ? { status: 'ok', error: '' } : { status: 'fail', error: f.detail || `${label}을(를) 사용할 수 없습니다` };
  };
  const ros = (() => {
    if (!live || !robots || server.robotsFailing) return { status: 'none', error: server.robotsFailing ? '로봇 정보 조회 실패' : '값 없음' };
    if (!health.robot?.configured) return { status: 'fail', error: '로봇이 설정되지 않았습니다' };
    if (!robots.stop_diagnostics?.available) return { status: 'fail', error: '정지 진단을 사용할 수 없습니다' };
    return { status: 'ok', error: '' };
  })();
  const obs = !simState ? { status: 'none', error: '3D 관측 값 없음' }
    : simState.stale ? { status: 'warn', error: '3D 관측이 오래되었습니다' } : { status: 'ok', error: '' };
  const cam = scene.at == null || scene.ok === null ? { status: 'none', error: scene.detail || '값 없음' }
    : scene.ok ? { status: 'ok', error: '' } : { status: 'fail', error: scene.detail };
  const checked = server.refreshedAt;
  // LATENCY: 사이드바 위젯과 같은 기준 — 서버 정지 정책의 취소 ACK 제한(초)보다 짧으면 정상.
  const ackLimitMs = config?.policies?.stop?.cancel_ack_timeout_sec * 1000;
  const latency = !live || typeof conn?.latencyMs !== 'number' || !Number.isFinite(ackLimitMs) ? { status: 'none', error: '값 없음' }
    : conn.latencyMs < ackLimitMs ? { status: 'ok', error: '' }
      : { status: 'warn', error: `응답 ${Math.round(conn.latencyMs)}ms — 기준 ${ackLimitMs}ms 이상` };
  return [
    { id: 'ros2', name: 'ROS 2 / 로봇', ...ros, checked, raw: { robot: health?.robot, stop_diagnostics: robots?.stop_diagnostics } },
    { id: 'planner', name: 'PLANNER (계획)', ...feature(health?.features?.planning, '계획 모델'), checked, raw: health?.features?.planning },
    { id: 'stt', name: 'STT (음성 인식)', ...feature(health?.features?.stt, '음성 인식'), checked, raw: health?.features?.stt },
    { id: 'db', name: 'DB', ...feature(health?.db, 'DB'), checked, raw: health?.db },
    { id: 'obs', name: '3D 관측', ...obs, checked, raw: simState && { stale: simState.stale, joint_age_sec: simState.joint_age_sec, pose_age_sec: simState.pose_age_sec } },
    { id: 'camera', name: '장면 카메라', ...cam, checked: scene.at, raw: scene },
    { id: 'latency', name: 'LATENCY (서버 응답)', ...latency, checked, raw: { latency_ms: conn?.latencyMs ?? null, limit_ms: Number.isFinite(ackLimitMs) ? ackLimitMs : null } },
  ];
}

export default function Diagnostics({ server }) {
  const scene = useScene();
  const { hidden, toggle: toggleWidget, showAll } = useWidgetPrefs();
  // 사이드바 상태 위젯에서 #/diagnostics?service=<id>로 오면 그 서비스 행으로 스크롤한다.
  useEffect(() => {
    const id = new URLSearchParams(window.location.hash.split('?')[1] || '').get('service');
    if (id) document.getElementById(`svc-${id}`)?.scrollIntoView({ block: 'center' });
  }, []);
  const services = buildServices(server, scene);
  // 정상인 서비스는 원시값도 sig에 넣는다 — 정상인 동안 바뀐 값도 '마지막 정상' 샘플로 갱신되게(상태가 바뀔 때만이 아니라).
  const sig = services.map((s) => `${s.id}:${s.status}${s.status === 'ok' ? `:${JSON.stringify(s.raw ?? null)}` : ''}`).join('|');
  const [now, setNow] = useState(() => Date.now());
  // track: 서비스별 현재 상태가 시작된 시각·마지막 정상 시각, 상태 변화 이력(이 화면이 본 것만).
  const [track, setTrack] = useState({ sig: '', prev: {}, since: {}, lastOk: {}, lastOkRaw: {}, history: [] });
  const [open, setOpen] = useState({}); // 원시 샘플 펼침(자동으로 펼치지 않는다)
  const [exportJobs, setExportJobs] = useState([]);
  const [include, setInclude] = useState(() => Object.fromEntries(EXPORT_ITEMS.map(([k]) => [k, true])));
  const expand = useExpand();
  const urls = useRef([]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    const created = urls.current;
    return () => { clearInterval(timer); created.forEach((u) => URL.revokeObjectURL(u)); };
  }, []);

  if (track.sig !== sig) { // 렌더 중 상태 갱신(React 공식 패턴) — 변화가 있을 때만
    const next = { sig, prev: { ...track.prev }, since: { ...track.since }, lastOk: { ...track.lastOk }, lastOkRaw: { ...track.lastOkRaw }, history: track.history };
    services.forEach((s) => {
      const before = track.prev[s.id];
      if (before !== s.status) {
        next.since[s.id] = now;
        if (before) {
          next.history = [{ key: `${s.id}-${now}`, at: now, name: s.name, from: before, to: s.status, error: s.error, raw: s.raw, rawBefore: track.rawPrev?.[s.id] }, ...next.history].slice(0, 100);
        }
      }
      if (s.status === 'ok') { next.lastOk[s.id] = now; next.lastOkRaw[s.id] = s.raw; } // 이 화면이 본 마지막 정상 원시값(서버 이력 아님)
      else if (before === 'ok') next.lastOk[s.id] = now; // 정상이던 마지막 순간
      next.prev[s.id] = s.status;
    });
    next.rawPrev = Object.fromEntries(services.map((s) => [s.id, s.raw]));
    setTrack(next);
  }

  const sorted = [...services].sort((a, b) => ORDER[a.status] - ORDER[b.status]);
  const problem = services.some((s) => s.status !== 'ok');

  // 서버 API가 아니라 브라우저가 지금 가진 값으로 JSON을 만든다(Blob). 대기→생성 중→완료/실패.
  const chosen = EXPORT_ITEMS.filter(([k]) => include[k]).map(([k]) => k);
  function exportData() {
    const id = stamp();
    const update = (patch) => setExportJobs((jobs) => jobs.map((j) => (j.id === id ? { ...j, ...patch } : j)));
    setExportJobs((jobs) => [{ id, at: id, state: '대기' }, ...jobs].slice(0, 10));
    setTimeout(() => {
      update({ state: '생성 중' });
      setTimeout(() => {
        try {
          const all = {
            connection: { connection: server.conn },
            services: { services: services.map(({ id, name, status, error, checked, raw }) => ({ id, name, status, error, checked, raw })) },
            state_changes: { state_changes: track.history },
            server: { health: server.health, stop_diagnostics: server.robots?.stop_diagnostics },
            policies: { policies: server.config?.policies },
            recent_jobs: { recent_jobs: server.simDemo?.recent_jobs },
          };
          const payload = {
            exported_at: new Date().toISOString(),
            note: '이 화면이 본 값의 사본입니다(서버 내보내기 아님)',
            included: chosen,
            ...Object.assign({}, ...chosen.map((k) => all[k])),
          };
          const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
          const url = URL.createObjectURL(blob);
          urls.current.push(url);
          update({ state: '완료', url, size: blob.size });
        } catch (error) {
          update({ state: '실패', error: error.message });
        }
      }, 0);
    }, 0);
  }

  const download = (job) => {
    const a = document.createElement('a');
    a.href = job.url;
    a.download = `diagnostics-${new Date(job.at).toISOString().replace(/[:.]/g, '-')}.json`;
    a.click();
  };

  const badge = (status) => { const [label, tone, icon] = STATUS[status]; return <b className={tone}>{icon} {label}</b>; };
  const toggle = (key) => setOpen((o) => ({ ...o, [key]: !o[key] }));

  return <>
    <div className="card rec-pad widget-prefs" role="group" aria-label="상태 위젯 표시">
      <b>상태 위젯 표시</b>
      {WIDGET_ROWS.map(([id, label]) => <button key={id} type="button" role="switch" aria-checked={!hidden.has(id)} className="widget-switch" onClick={() => toggleWidget(id)}>
        <i aria-hidden="true" />{label}
      </button>)}
      <button type="button" className="rec-btn" onClick={showAll} disabled={hidden.size === 0}>모두 보이기</button>
      <small className="muted">사이드바 System status 위젯에 보일 항목만 고릅니다. 아래 서비스 목록과 상태 판정은 그대로입니다.</small>
    </div>
    <div className="rec-head">
      <h2>서비스 상태{problem ? ' · 확인이 필요한 항목이 있습니다' : ''}</h2>
      <div className="rec-group">
        <div role="group" aria-label="내보낼 항목" className="rec-group">
          {EXPORT_ITEMS.map(([k, label]) => <label key={k} className="rec-check"><input type="checkbox" checked={include[k]} onChange={(e) => setInclude((v) => ({ ...v, [k]: e.target.checked }))} /> {label}</label>)}
        </div>
        <button type="button" className="rec-btn" onClick={exportData} disabled={chosen.length === 0}>진단자료 내보내기</button>
      </div>
    </div>
    {exportJobs.length > 0 && <div className="card">
      <table className="data-table" aria-label="진단자료 내보내기 작업">
        <thead><tr><th>요청 시각</th><th>상태</th><th>결과</th></tr></thead>
        <tbody>{exportJobs.map((j) => <tr key={j.id}>
          <td>{clock(j.at)}</td><td>{j.state === '생성 중' ? <><Spinner size={14} label="생성 중" /> 생성 중</> : j.state}</td>
          <td>{j.state === '완료' && <button type="button" className="rec-btn" onClick={() => download(j)}>JSON 다운로드 ({j.size} B)</button>}
            {j.state === '실패' && <span className="danger">{j.error}</span>}</td>
        </tr>)}</tbody>
      </table>
    </div>}
    <div className="card">
      <table className="data-table" aria-label="서비스 목록">
        <thead><tr><th>서비스</th><th>상태</th><th className="col-extra">최근 점검</th><th className="col-extra">마지막 정상</th><th className="col-extra">지속(이 화면 기준)</th><th>최근 오류</th></tr></thead>
        <tbody>{sorted.map((s) => {
          const lastOkText = s.status === 'ok' ? '지금' : track.lastOk[s.id] ? clock(track.lastOk[s.id]) : '이 화면이 본 적 없음';
          const sinceText = track.since[s.id] ? span(now - track.since[s.id]) : '—';
          return <Fragment key={s.id}><tr id={`svc-${s.id}`}>
          <td><ExpandButton open={expand.isOpen(s.id)} onToggle={() => expand.toggle(s.id)} />{s.name}</td>
          <td>{badge(s.status)}</td>
          <td className="col-extra">{clock(s.checked)}</td>
          <td className="col-extra">{lastOkText}</td>
          <td className="col-extra">{sinceText}</td>
          <td>{s.id === 'camera' && scene.at == null ? <><Spinner size={14} label="카메라 확인 중" /> 확인 중</> : s.error || '—'}
            {s.status !== 'ok' && <div>
              <button type="button" className="rec-btn" aria-expanded={!!open[s.id]} onClick={() => toggle(s.id)}>문제 전후 원시 샘플 보기</button>
              {open[s.id] && <pre className="rec-pre">{JSON.stringify({ '마지막 정상': track.lastOkRaw[s.id] ?? '이 화면이 본 적 없음', 현재: s.raw ?? null }, null, 2)}</pre>}
            </div>}
          </td>
        </tr>
        <DetailRow open={expand.isOpen(s.id)} span={3} items={[['최근 점검', clock(s.checked)], ['마지막 정상', lastOkText], ['지속(이 화면 기준)', sinceText]]} />
        </Fragment>;
        })}</tbody>
      </table>
    </div>
    <section>
      <h2>실패 이력 <small className="muted">이 화면이 열린 뒤 본 상태 변화</small></h2>
      <div className="card">
        {track.history.length === 0 ? <p className="rec-empty">관측한 상태 변화가 없습니다</p> : <table className="data-table" aria-label="상태 변화 이력">
          <thead><tr><th>시각</th><th>서비스</th><th>변화</th><th>사유</th></tr></thead>
          <tbody>{track.history.map((h) => <tr key={h.key}>
            <td>{clock(h.at)}</td><td>{h.name}</td><td>{STATUS[h.from][0]} → {STATUS[h.to][0]}</td>
            <td>{h.error || '—'}
              <div>
                <button type="button" className="rec-btn" aria-expanded={!!open[h.key]} onClick={() => toggle(h.key)}>문제 전후 원시 샘플 보기</button>
                {open[h.key] && <pre className="rec-pre">{JSON.stringify({ 이전: h.rawBefore ?? null, 이후: h.raw ?? null }, null, 2)}</pre>}
              </div>
            </td>
          </tr>)}</tbody>
        </table>}
      </div>
    </section>
  </>;
}
