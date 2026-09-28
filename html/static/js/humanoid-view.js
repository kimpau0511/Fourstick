// G1 3D 화면 (읽기 전용). Gazebo가 관측한 골반 위치·방향과 다리 12관절만 그린다.
//
// - 모델: /v1/humanoid/view/model (g1_12dof.urdf 시각 메시 · 컨베이어 · 선언된 안전 지점)
// - 상태: /v1/humanoid/view/state (10 Hz로 읽음). 관측 두 개 사이만 보간하고 외삽하지 않는다.
//   낡거나 끊기면 마지막 관측에 멈추고 그렇다고 적는다.
// - 팔·허리는 URDF 고정 관절이다 — 화면에 "다리 12관절 제어 · 팔·허리 고정"을 적는다.
// - 시간은 두 가지를 따로 적는다: 시뮬레이션 시각(Gazebo), 관측 나이(벽시계).

import * as THREE from '/static/vendor/three/build/three.module.js';
import { OrbitControls } from '/static/vendor/three/examples/jsm/controls/OrbitControls.js';
import URDFLoader from '/static/vendor/urdf-loader/URDFLoader.js';
import {
  ClockSkew, DISPLAY_DELAY_SEC, SampleBuffer, lerpJoints, lerpPoses, staleness,
} from './sim-view-core.js';

const POLL_MS = 100;
// G1 시각 메시가 크다(STL 25 MB). 매 프레임 그리면 소프트웨어 GL에서 페이지 전체가 느려져
// 관측 읽기·명령 입력까지 밀린다(실측: 이동 중 3D 표시가 74% '정지 화면'). 그리는 횟수만 줄인다 —
// 관측 값·시각은 바꾸지 않는다.
const MAX_FPS = 15;

export function createG1View({ frame, statusEl }) {
  const st = {
    joints: new SampleBuffer(), base: new SampleBuffer(), skew: new ClockSkew(),
    connected: false, serverStale: true, staleAfter: 0.6, robot: null, running: false,
    visible: false, lastJointSeq: null, lastBaseSeq: null, simTime: null, ages: null,
    lastSample: null, shown: null,
  };
  let renderer; let scene; let camera; let controls; let root;

  async function init() {
    let model;
    try {
      model = await (await fetch('/v1/humanoid/view/model', { cache: 'no-store' })).json();
    } catch (error) {
      model = { available: false, detail: String(error) };
    }
    if (!model.available) {
      statusEl.textContent = `3D를 쓸 수 없음 · ${model.detail || ''}`;
      return false;
    }
    const canvas = document.createElement('canvas');
    frame.prepend(canvas);
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
    renderer.setPixelRatio(1);
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0xf2f2f0);
    camera = new THREE.PerspectiveCamera(45, 16 / 9, 0.05, 40);
    camera.position.set(1.2, 3.2, 4.2);              // three(y-up) 좌표
    controls = new OrbitControls(camera, canvas);
    controls.addEventListener('change', () => { st.dirty = true; });
    controls.target.set(1.3, 0.6, 0);
    controls.update();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a80, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.3);
    sun.position.set(3, 6, 4);
    scene.add(sun);
    root = new THREE.Group();                         // ROS(z-up) → three(y-up)
    root.rotation.x = -Math.PI / 2;
    scene.add(root);
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(8, 6),
      new THREE.MeshStandardMaterial({ color: 0xe4e4df }));
    floor.position.set(1.5, 0, 0);
    root.add(floor);
    const c = model.conveyor;
    const conveyor = new THREE.Mesh(new THREE.BoxGeometry(...c.size_m),
      new THREE.MeshStandardMaterial({ color: 0x4d4d59 }));
    conveyor.position.set(...c.center_m);
    root.add(conveyor);
    // 선언된 안전 지점(원판 + 방향 화살표). 좌표는 site.json에서만 온다.
    for (const point of Object.values(model.safe_points)) {
      const disc = new THREE.Mesh(new THREE.CircleGeometry(0.1, 24),
        new THREE.MeshBasicMaterial({ color: 0x2f9e44, transparent: true, opacity: 0.6 }));
      disc.position.set(point.xy_m[0], point.xy_m[1], 0.002);
      root.add(disc);
      const arrow = new THREE.ArrowHelper(
        new THREE.Vector3(Math.cos(point.yaw_rad), Math.sin(point.yaw_rad), 0),
        new THREE.Vector3(point.xy_m[0], point.xy_m[1], 0.01), 0.25, 0x2f9e44);
      root.add(arrow);
    }
    const keepOut = new THREE.Mesh(new THREE.PlaneGeometry(0.01, c.size_m[1] + 0.6),
      new THREE.MeshBasicMaterial({ color: 0xd9480f }));
    keepOut.position.set(model.keep_out_x_m, c.center_m[1], 0.003);
    root.add(keepOut);
    const robot = new URDFLoader(new THREE.LoadingManager()).parse(model.urdf, '');
    robot.visible = false;                             // 첫 관측 전에는 그리지 않는다
    root.add(robot);
    st.robot = robot;
    const resize = () => {
      const width = frame.clientWidth || 640;
      const height = Math.round(width * 9 / 16);
      renderer.setSize(width, height, false);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
    };
    resize();
    if (typeof ResizeObserver === 'function') new ResizeObserver(resize).observe(frame);
    return true;
  }

  async function poll() {
    if (!st.running) return;
    try {
      const data = await (await fetch('/v1/humanoid/view/state', { cache: 'no-store' })).json();
      const recv = Date.now() / 1000;
      st.connected = true;
      st.skew.add(recv, data.server_time);
      st.serverStale = Boolean(data.stale);
      st.staleAfter = data.stale_after_sec || st.staleAfter;
      st.lastSample = data;
      if (data.joints && data.joint_seq !== st.lastJointSeq) {
        st.lastJointSeq = data.joint_seq;
        st.joints.push(data.joint_time, data.joints);
      }
      if (data.base_pose && data.base_seq !== st.lastBaseSeq) {
        st.lastBaseSeq = data.base_seq;
        st.base.push(data.base_time, { g1: data.base_pose });
        st.simTime = data.base_sim_time;
      }
    } catch {
      st.connected = false;
    }
    setTimeout(poll, POLL_MS);
  }

  let lastRender = 0;
  function draw(nowMs) {
    if (!st.running) return;
    requestAnimationFrame(draw);
    if (!st.visible || !renderer) return;
    if (nowMs - lastRender < 1000 / MAX_FPS) return;
    lastRender = nowMs;
    const serverNow = Date.now() / 1000 - st.skew.skew;
    const lastJ = st.joints.last;
    const lastB = st.base.last;
    const status = staleness({
      connected: st.connected, serverStale: st.serverStale,
      lastJointT: lastJ && lastJ.t, lastPoseT: lastB && lastB.t, serverNow, staleAfter: st.staleAfter,
    });
    const target = status.stale ? Infinity : serverNow - DISPLAY_DELAY_SEC;
    const bj = st.joints.bracket(target);
    if (bj && st.robot) {
      const values = lerpJoints(bj.a.value, bj.b.value, bj.f);
      for (const [name, value] of Object.entries(values)) st.robot.setJointValue(name, value);
    }
    const bb = st.base.bracket(target);
    if (bb && st.robot) {
      const pose = lerpPoses(bb.a.value, bb.b.value, bb.f).g1;
      st.robot.position.set(pose[0], pose[1], pose[2]);
      st.robot.quaternion.set(pose[3], pose[4], pose[5], pose[6]);
      st.robot.visible = Boolean(bj);
      st.shown = pose;
    }
    const s = st.lastSample || {};
    statusEl.textContent = status.stale
      ? `■ 정지 화면 · ${status.reason} — 실제 움직임이 아닙니다`
      : `● Gazebo 관측 · 시뮬레이션 ${st.simTime == null ? '—' : st.simTime.toFixed(2)} s · `
        + `관측 나이 ${s.base_age_wall_s == null ? '—' : Math.round(s.base_age_wall_s * 1000)} ms(벽시계)`;
    statusEl.classList.toggle('stop', status.stale);
    controls.update();
    // 보이는 자세가 바뀌었거나 카메라를 움직였을 때만 그린다.
    const key = st.shown ? st.shown.map((v) => v.toFixed(4)).join(',') + (bj ? bj.a.t + bj.f : '') : '';
    if (key !== st.lastKey || st.dirty) {
      const t0 = performance.now();
      renderer.render(scene, camera);
      st.renderMs = (st.renderMs || []).concat(performance.now() - t0).slice(-60);
      st.renders = (st.renders || 0) + 1;
      st.lastKey = key;
      st.dirty = false;
    }
  }

  return {
    async start() {
      if (st.running) return;
      const ok = await init();
      if (!ok) return;
      st.running = true;
      poll();
      requestAnimationFrame(draw);
    },
    setVisible(visible) { st.visible = visible; },
    snapshot() {
      return { shown: st.shown, simTime: st.simTime, stale: st.serverStale, renders: st.renders || 0,
        renderMs: st.renderMs || [],
        jointsShown: st.robot ? Object.keys(st.robot.joints || {}).length : 0 };
    },
  };
}
