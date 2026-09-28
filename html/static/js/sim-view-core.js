// 3D 작업 셀 화면의 순수 로직 — 관측 버퍼 · 보간 · 낡음 판정 · 지표.
//
// **꾸미지 않는다.** 화면은 관측 두 개 **사이만** 보간한다. 마지막 관측보다 앞선
// 시각을 그려야 하면 마지막 관측에 멈춘다(외삽하지 않는다). 관측이 오래되거나
// 연결이 끊기면 `stale`로 알리고 움직이지 않는다.
//
// 시각은 모두 **서버 벽시계 초**다. 브라우저 시계와의 차(skew)는 수신 시각으로
// 추정한다(같은 호스트면 0에 가깝다).

/** 기본 표시 지연(초). 스트림 30 Hz의 약 3주기 — 두 관측 사이를 보간할 여유. */
export const DISPLAY_DELAY_SEC = 0.1;
/** 버퍼 길이(초). 표시 지연보다 충분히 길게. */
export const BUFFER_SEC = 2.0;

/** 시각 순으로 쌓는 관측 버퍼. 같은 시각·거꾸로 온 표본은 버린다. */
export class SampleBuffer {
  constructor(keepSec = BUFFER_SEC) {
    this.keepSec = keepSec;
    this.items = [];
  }

  push(t, value) {
    if (typeof t !== 'number' || !Number.isFinite(t)) return false;
    const last = this.items[this.items.length - 1];
    if (last && t <= last.t) return false;
    this.items.push({ t, value });
    const cutoff = t - this.keepSec;
    while (this.items.length > 2 && this.items[1].t < cutoff) this.items.shift();
    return true;
  }

  get last() {
    return this.items[this.items.length - 1] || null;
  }

  /** 시각 `t`를 감싸는 두 표본과 비율. 마지막보다 뒤면 마지막에 멈춘다(held). */
  bracket(t) {
    const items = this.items;
    if (!items.length) return null;
    if (t <= items[0].t) return { a: items[0], b: items[0], f: 0, held: false };
    const last = items[items.length - 1];
    if (t >= last.t) return { a: last, b: last, f: 0, held: true };
    for (let i = items.length - 1; i > 0; i -= 1) {
      if (items[i - 1].t <= t) {
        const a = items[i - 1];
        const b = items[i];
        return { a, b, f: (t - a.t) / (b.t - a.t), held: false };
      }
    }
    return { a: items[0], b: items[0], f: 0, held: false };
  }
}

/** 관절 값 사전 보간(두 표본 모두에 있는 이름만). */
export function lerpJoints(a, b, f) {
  const out = {};
  for (const name of Object.keys(a)) {
    out[name] = name in b ? a[name] + (b[name] - a[name]) * f : a[name];
  }
  return out;
}

/** pose [x,y,z,qx,qy,qz,qw] 보간: 위치는 선형, 방향은 slerp(짧은 쪽). */
export function lerpPose(a, b, f) {
  const p = [0, 1, 2].map((i) => a[i] + (b[i] - a[i]) * f);
  let [ax, ay, az, aw] = [a[3], a[4], a[5], a[6]];
  let [bx, by, bz, bw] = [b[3], b[4], b[5], b[6]];
  let dot = ax * bx + ay * by + az * bz + aw * bw;
  if (dot < 0) { bx = -bx; by = -by; bz = -bz; bw = -bw; dot = -dot; }
  let s0 = 1 - f;
  let s1 = f;
  if (dot < 0.9995) {
    const theta = Math.acos(Math.min(1, dot));
    const sin = Math.sin(theta);
    s0 = Math.sin((1 - f) * theta) / sin;
    s1 = Math.sin(f * theta) / sin;
  }
  const q = [ax * s0 + bx * s1, ay * s0 + by * s1, az * s0 + bz * s1, aw * s0 + bw * s1];
  const n = Math.hypot(...q) || 1;
  return [...p, ...q.map((v) => v / n)];
}

export function lerpPoses(a, b, f) {
  const out = {};
  for (const name of Object.keys(a)) {
    out[name] = name in b ? lerpPose(a[name], b[name], f) : a[name];
  }
  return out;
}

/**
 * 지금 그릴 것이 낡았는가. 이유를 함께 준다(화면에 그대로 적는다).
 * - 연결이 없다 · 서버가 stale이라 했다 · 마지막 관측이 `staleAfter`보다 오래됐다.
 */
export function staleness({ connected, serverStale, lastJointT, lastPoseT, serverNow,
  staleAfter }) {
  if (!connected) return { stale: true, reason: '연결 끊김' };
  if (lastJointT == null || lastPoseT == null) return { stale: true, reason: '관측 없음' };
  const age = serverNow - Math.min(lastJointT, lastPoseT);
  if (age > staleAfter) {
    return { stale: true, reason: `마지막 관측 ${age.toFixed(1)}초 전`, age };
  }
  if (serverStale) return { stale: true, reason: '서버가 관측이 낡았다고 알림', age };
  return { stale: false, reason: '', age };
}

/** 브라우저 시계 → 서버 시계 차 추정. 수신 지연의 **최솟값** 쪽(최근 창)을 쓴다. */
export class ClockSkew {
  constructor(windowSize = 60) {
    this.windowSize = windowSize;
    this.samples = [];
  }

  /** clientRecvSec − serverSendSec (= 전송 지연 + 시계 차). */
  add(clientRecvSec, serverSendSec) {
    this.samples.push(clientRecvSec - serverSendSec);
    if (this.samples.length > this.windowSize) this.samples.shift();
  }

  /** 서버 시각 ≈ 브라우저 시각 − skew. 표본이 없으면 0. */
  get skew() {
    return this.samples.length ? Math.min(...this.samples) : 0;
  }
}

/** 백분위(정렬 복사본). 빈 배열이면 null. */
export function percentile(values, p) {
  if (!values.length) return null;
  const sorted = [...values].sort((x, y) => x - y);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.round((p / 100) * (sorted.length - 1))));
  return sorted[index];
}
