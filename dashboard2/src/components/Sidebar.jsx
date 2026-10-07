import { useEffect, useRef, useState } from 'react';
import { NAV } from '../nav.js';
import bot from '../assets/bot.svg';
import StatusWidget from './StatusWidget.jsx';

// 가동 = 상태가 "데이터 없음·긴급"이 아닌 로봇. 서버 로봇 상태(robotStatus)는 /health의 로봇 1대 것이라 다른 로봇은 확인 안 됨 → 가동으로 세지 않는다.
function runningBadge(server) {
  const ids = Object.keys(server.robots?.robots || {});
  if (!server.robots) return null;
  const mine = server.health?.robot?.robot_id;
  const running = ids.filter((id) => id === mine && !['NO_DATA', 'CRITICAL'].includes(server.robotStatus.level)).length;
  return `가동 ${running}/${ids.length}`;
}

// 구글 프로필 사진이 없거나 못 불러오면 이름 첫 글자.
function Avatar({ user }) {
  const [broken, setBroken] = useState(false);
  if (user.picture && !broken) return <img className="avatar" src={user.picture} alt="" referrerPolicy="no-referrer" onError={() => setBroken(true)} />;
  return <span className="avatar" aria-hidden="true">{(user.name || user.email || '?').slice(0, 1)}</span>;
}

export default function Sidebar({ page, server, user, onLogout, logoutFailed, inert }) {
  const badge = runningBadge(server);
  const [menuOpen, setMenuOpen] = useState(false);
  const profileRef = useRef(null);
  // 펼친 프로필은 바깥을 누르거나 Esc를 누르면 접는다. 페이지를 옮겨도 접는다(아래 menuPage).
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDown = (e) => { if (!profileRef.current?.contains(e.target)) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === 'Escape') setMenuOpen(false); };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('pointerdown', onDown); document.removeEventListener('keydown', onKey); };
  }, [menuOpen]);
  const [menuPage, setMenuPage] = useState(page);
  if (menuPage !== page) { setMenuPage(page); setMenuOpen(false); }
  return <aside className="sidebar" inert={inert}>
    <div>
      <div className="brand">
        <span className="brand-mark"><img src={bot} alt="" width="18" height="18" /></span>
        <div className="rail-hide"><strong>FORSTICK</strong><small>CONTROL PANEL</small></div>
      </div>
      <nav className="nav">
        {NAV.map((item) => <a key={item.id} href={`#/${item.id}`} aria-label={item.label} title={item.label} className={item.id === page ? 'nav-item active' : 'nav-item'} aria-current={item.id === page ? 'page' : undefined}>
          <span className={item.id === 'home' ? 'nav-icon inset' : 'nav-icon'}><img src={item.icon} alt="" /></span>
          <span className="rail-hide">{item.label}</span>
          {item.id === 'robots' && badge && <span className="nav-badge">{badge}</span>}
        </a>)}
      </nav>
    </div>
    <div className="side-bottom">
      <StatusWidget server={server} />
      {/* 계정은 서버(/v1/auth/me)가 준 값만 — 이름·근무를 지어내지 않는다(D2). */}
      {/* 프로필을 누르면 이 칸 안에서 펼쳐져 로그아웃이 나온다(떠 있는 창 없음, 2026-10-07 요청). */}
      {user && <div className={menuOpen ? 'profile open' : 'profile'} ref={profileRef}>
        {logoutFailed && <small className="profile-error" role="alert">로그아웃을 서버에서 확인하지 못했습니다. 다시 시도하세요.</small>}
        <button type="button" className="profile-btn" aria-expanded={menuOpen} aria-controls="profile-actions"
          aria-label={`${user.name || user.email} 계정`} onClick={() => setMenuOpen((v) => !v)}>
          <Avatar user={user} />
          <div className="rail-hide"><strong>{user.name || user.email}</strong><small>{user.email}</small></div>
        </button>
        {menuOpen && <div className="profile-actions" id="profile-actions">
          <button type="button" className="profile-logout" onClick={() => { setMenuOpen(false); onLogout(); }}>로그아웃</button>
        </div>}
      </div>}
    </div>
  </aside>;
}
