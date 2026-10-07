import { useState } from 'react';
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
      {user && <div className="profile">
        {menuOpen && <div className="profile-menu" id="profile-menu" role="menu" onKeyDown={(e) => { if (e.key === 'Escape') setMenuOpen(false); }}>
          <div className="who"><Avatar user={user} /><div><strong>{user.name || user.email}</strong><small>{user.email}</small></div></div>
          <hr />
          <button type="button" role="menuitem" onClick={() => { setMenuOpen(false); onLogout(); }}>로그아웃</button>
        </div>}
        {logoutFailed && <small className="profile-error" role="alert">로그아웃을 서버에서 확인하지 못했습니다. 다시 시도하세요.</small>}
        <button type="button" className="profile-btn" aria-haspopup="menu" aria-expanded={menuOpen} aria-controls="profile-menu"
          aria-label={`${user.name || user.email} 계정 메뉴`} onClick={() => setMenuOpen((v) => !v)}>
          <Avatar user={user} />
          <div className="rail-hide"><strong>{user.name || user.email}</strong><small>{user.email}</small></div>
        </button>
      </div>}
    </div>
  </aside>;
}
