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

export default function Sidebar({ page, server }) {
  const badge = runningBadge(server);
  return <aside className="sidebar">
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
      <div className="profile rail-hide">
        {/* 사용자 계정은 인증 도입 뒤에 생긴다 — 이름·근무를 지어내지 않는다(D2). */}
        <div className="profile-user"><div><strong>작업자</strong><small>로그인 없음 · 인증 도입 전</small></div></div>
      </div>
    </div>
  </aside>;
}
