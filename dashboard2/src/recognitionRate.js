import { useEffect, useState } from 'react';

// 음성 인식률(글자 기준) 표시값 — GET /v1/stt/recognition-rate(평가 결과 파일 읽기 전용).
// 서버가 현재 STT 모델·설정과 같은 평가 결과를 골라 준다: 실제 녹음 + 사람 확인 정답 최종 평가가 우선,
// 없으면 같은 설정의 합성 음성 최종 평가(잡음 없음, 설명 문구에 합성이라고 적힘). 둘 다 없으면 '미측정'.
// 화면은 숫자를 만들지 않는다: 모델 신뢰도를 인식률로 쓰지 않고, 현재 발화에 %를 붙이지 않는다.
export const RATE_NOTE = '글자 기준 평가 결과이며 현재 발화의 정확도를 뜻하지 않습니다.';
const UNMEASURED = { status: 'unmeasured', label: '음성 인식률 미측정', note: RATE_NOTE, reason: null };
const REFRESH_MS = 5 * 60 * 1000;

export function useRecognitionRate() {
  const [rate, setRate] = useState(UNMEASURED);
  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const r = await fetch('/v1/stt/recognition-rate');
        const p = await r.json();
        if (!alive) return;
        // 서버가 measured와 숫자를 함께 줄 때만 %를 보인다. 그 밖(오류·형식 이상)은 미측정.
        if (r.ok && p && p.status === 'measured' && typeof p.rate_percent === 'number' && p.label) setRate({ ...p, note: p.note || RATE_NOTE });
        else setRate({ ...UNMEASURED, reason: (p && p.reason) || null });
      } catch {
        if (alive) setRate({ ...UNMEASURED, reason: '평가 결과를 불러오지 못했습니다' });
      }
    }
    load();
    const t = setInterval(load, REFRESH_MS);
    return () => { alive = false; clearInterval(t); };
  }, []);
  return rate;
}
