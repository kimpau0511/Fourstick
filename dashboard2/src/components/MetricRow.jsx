export default function MetricRow({ title, items, badgeSize }) {
  return <section>
    <h2>{title}</h2>
    <div className="row3">
      {items.map((m) => <div key={m.label} className="card metric">
        <span className="metric-label">{m.label}</span>
        <div className="metric-value"><strong>{m.value}{m.unit && <small>{m.unit}</small>}</strong>{m.badge && <span className={`tag ${m.tone} ${badgeSize || ''}`} title={m.badge}>{m.badge}</span>}</div>
      </div>)}
    </div>
  </section>;
}
