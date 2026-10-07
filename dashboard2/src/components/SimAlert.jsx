// 오류 안내(2026-10-07): 오류·안전 차단·확인 필요일 때만(계산은 simPanels.js의 simAlertOf).
// - 시뮬레이션 보기: 시뮬레이션 아래(전체화면이면 화면 하단) — '안내 닫기'는 화면에서만 숨긴다(정지·잠금 해제·복구 없음).
// - 명령 패널(inline): 카드에 이미 보이는 문장(shown)은 되풀이하지 않고 원인·조치·상세 보기만 덧붙인다. 닫기 없음 —
//   카드가 남아 있는 동안(새 명령 전까지) 함께 남는다. 창이 닫혀도 사용자가 원인·조치를 볼 수 있게.
export default function SimAlert({ alert, onDismiss, compact, inline, shown = [] }) {
  const repeated = (text) => !!text && shown.some((line) => line && (line === text || line.includes(text)));
  const showCause = !repeated(alert.cause);
  const showDetail = alert.detail && alert.detail !== alert.cause;
  return <section className={`sim-alert ${alert.tone}${compact ? ' compact' : ''}${inline ? ' inline' : ''}`}
    role={inline ? 'note' : 'alert'} aria-label={inline ? '오류 원인과 조치' : '오류 안내'}>
    {!inline && <div className="sim-alert-head">
      <b>{alert.title}</b>
      <button type="button" className="sim-alert-close" onClick={onDismiss} aria-label="안내 닫기" title="안내 닫기(화면에서만 숨김)">✕</button>
    </div>}
    {showCause && <p><span>원인</span>{alert.cause}</p>}
    <p><span>조치</span>{alert.action}</p>
    <details>
      <summary>상세 보기</summary>
      <dl><dt>코드</dt><dd>{alert.code || '없음'}</dd><dt>내용</dt><dd>{showDetail ? alert.detail : alert.cause || '없음'}</dd></dl>
    </details>
  </section>;
}
