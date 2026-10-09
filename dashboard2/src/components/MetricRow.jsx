export default function MetricRow({ title, items, badgeSize }) {
  return <section>
    <h2>{title}</h2>
    {/* 지표는 낮은 카드 하나에 칸으로 나눈다(2026-10-08 — 숫자 한 줄마다 큰 카드를 두면 공간만 차지했다) */}
    <div className="card metric-row">
      {items.map((m) => <div key={m.label} className="metric">
        <span className="metric-label">{m.label}</span>
        <div className="metric-value"><strong>{m.value}{m.unit && <small>{m.unit}</small>}</strong>{m.badge && <span className={`tag ${m.tone} ${badgeSize || ''}`} title={m.badge}>{m.badge}</span>}</div>
      </div>)}
    </div>
  </section>;
}
