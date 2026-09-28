// 3D 작업 셀 화면 순수 로직 — 관측 사이만 보간 · 외삽 없음 · 낡음 판정.
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  ClockSkew, SampleBuffer, lerpJoints, lerpPose, percentile, staleness,
} from '../../html/static/js/sim-view-core.js';

test('관측 두 개 사이만 보간한다', () => {
  const buf = new SampleBuffer();
  buf.push(10.0, { j1: 0 });
  buf.push(10.1, { j1: 1 });
  const mid = buf.bracket(10.05);
  assert.equal(mid.held, false);
  assert.ok(Math.abs(lerpJoints(mid.a.value, mid.b.value, mid.f).j1 - 0.5) < 1e-9);
});

test('마지막 관측보다 뒤는 외삽하지 않고 멈춘다', () => {
  const buf = new SampleBuffer();
  buf.push(10.0, { j1: 0 });
  buf.push(10.1, { j1: 1 });
  const later = buf.bracket(12.0);
  assert.equal(later.held, true);
  assert.equal(lerpJoints(later.a.value, later.b.value, later.f).j1, 1);
});

test('거꾸로 온 표본은 버리고 오래된 표본은 정리한다', () => {
  const buf = new SampleBuffer(1.0);
  assert.equal(buf.push(5, { j1: 0 }), true);
  assert.equal(buf.push(4, { j1: 9 }), false);
  for (let t = 5.1; t < 8; t += 0.1) buf.push(t, { j1: t });
  assert.ok(buf.items[1].t >= buf.last.t - 1.0 - 1e-9);
});

test('방향은 짧은 쪽으로 slerp한다', () => {
  const a = [0, 0, 0, 0, 0, 0, 1];
  const b = [2, 0, 0, 0, 0, -1, 0];   // z축 180°(부호 반대 표현)
  const half = lerpPose(a, b, 0.5);
  assert.deepEqual(half.slice(0, 3), [1, 0, 0]);
  assert.ok(Math.abs(Math.hypot(...half.slice(3)) - 1) < 1e-9);
});

test('끊김·관측 없음·오래된 관측·서버 stale은 모두 낡음이다', () => {
  const base = { connected: true, serverStale: false, lastJointT: 100, lastPoseT: 100,
    serverNow: 100.2, staleAfter: 0.5 };
  assert.equal(staleness(base).stale, false);
  assert.equal(staleness({ ...base, connected: false }).reason, '연결 끊김');
  assert.equal(staleness({ ...base, lastPoseT: null }).reason, '관측 없음');
  assert.match(staleness({ ...base, serverNow: 101 }).reason, /마지막 관측/);
  assert.equal(staleness({ ...base, serverStale: true }).stale, true);
});

test('시계 차는 최근 수신 지연의 최솟값이다', () => {
  const skew = new ClockSkew(3);
  skew.add(10.05, 10.0);
  skew.add(11.02, 11.0);
  assert.ok(Math.abs(skew.skew - 0.02) < 1e-9);
  assert.equal(percentile([], 50), null);
  assert.equal(percentile([1, 2, 3, 4, 5], 50), 3);
});
