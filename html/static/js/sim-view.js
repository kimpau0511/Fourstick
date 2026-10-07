// 작업 셀 3D 화면 (읽기 전용). Gazebo·MoveIt이 실행·관측을 맡고, 이 화면은
// **관측한 관절·그리퍼·자재 pose만** 그린다. 명령을 보내지 않는다.
//
// - 모델: /v1/sim-view/model (Gazebo에 올라간 조립 URDF의 시각 메시 + 셀 상자)
// - 상태: /v1/sim-view/stream (WebSocket, 새 관측마다)
// - 보간: 관측 두 개 사이만(sim-view-core.js). 외삽하지 않는다. 낡거나 끊기면
//   마지막 관측에 멈추고 그렇다고 적는다.
// - 기존 Gazebo 영상 카드(#card-scene)는 전환 가능한 대체 화면으로 둔다.

import * as THREE from '/static/vendor/three/build/three.module.js';
import { OrbitControls } from '/static/vendor/three/examples/jsm/controls/OrbitControls.js';
import URDFLoader from '/static/vendor/urdf-loader/URDFLoader.js';
import {
  ClockSkew, DISPLAY_DELAY_SEC, SampleBuffer, lerpJoints, lerpPoses, percentile, staleness,
} from './sim-view-core.js';

// 자재 형상 — 서버 셀 정보의 shape를 Gazebo world 생성기와 같은 규칙으로 그린다(ROS z-up, 중심 원점).
// dashboard2/src/materialGeometry.js와 같은 규칙이다.
function materialGeometry(item) {
  const [sx, sy, sz] = item.size_m;
  const side = Math.min(sx, sy);
  if (item.shape === 'cylinder') {
    const g = new THREE.CylinderGeometry(side / 2, side / 2, sz, 40);
    g.rotateX(Math.PI / 2);
    return g;
  }
  if (item.shape === 'triangle_prism') {
    const r = side / Math.sqrt(3);
    const shape = new THREE.Shape();
    [0, 120, 240].forEach((deg, i) => {
      const a = (deg * Math.PI) / 180;
      if (i === 0) shape.moveTo(r * Math.cos(a), r * Math.sin(a)); else shape.lineTo(r * Math.cos(a), r * Math.sin(a));
    });
    shape.closePath();
    const g = new THREE.ExtrudeGeometry(shape, { depth: sz, bevelEnabled: false });
    g.translate(0, 0, -sz / 2);
    return g;
  }
  return new THREE.BoxGeometry(sx, sy, sz);
}

const VIEW_KEY = 'forstick2.simView';
const TRACKED_LINKS = ['wrist3_Link', 'robotiq_85_left_finger_tip_link',
  'robotiq_85_right_finger_tip_link', 'robotiq_85_base_link'];

const card = document.getElementById('card-sim3d');
const sceneCard = document.getElementById('card-scene');

const state = {
  model: null, robot: null, materials: {}, root: null,
  joints: new SampleBuffer(), poses: new SampleBuffer(), robotPose: null,
  skew: new ClockSkew(), connected: false, serverStale: true, staleAfter: 0.5,
  lastJointSeq: null, lastPoseSeq: null, meshesLoaded: false,
  view: 'camera', rafId: null, displayed: null,
  metrics: { frames: [], messages: [], transport: [], display: [], fps: 0 },
};

function readView() {
  try { return localStorage.getItem(VIEW_KEY) || '3d'; } catch { return '3d'; }
}

function saveView(view) {
  try { localStorage.setItem(VIEW_KEY, view); } catch { /* 저장 못 해도 동작한다 */ }
}

function renderShell() {
  card.innerHTML = `
    <div class="card-header"><h3 class="card-title">작업 셀 화면</h3>
      <div class="btn-row" role="tablist" aria-label="화면 전환">
        <button class="btn sm" data-view="3d" role="tab">3D (관측)</button>
        <button class="btn sm" data-view="camera" role="tab">Gazebo 영상</button>
      </div></div>
    <div class="card-body" id="sim3d-body">
      <div class="scene-frame sim3d-frame" id="sim3d-frame">
        <canvas id="sim3d-canvas"></canvas>
        <div class="scene-badges"><span class="badge-overlay" id="sim3d-status">연결 중…</span></div>
        <div class="sim3d-stale" id="sim3d-stale" hidden></div>
      </div>
      <p class="hint"><span class="hint-mark">⬡</span>
        Gazebo가 관측한 관절·그리퍼·자재 위치만 그립니다(읽기 전용). 관측 사이만 보간하고,
        관측이 끊기거나 오래되면 마지막 상태로 멈춥니다. <span id="sim3d-metrics"></span></p>
    </div>`;
  card.querySelectorAll('button[data-view]').forEach((button) => {
    button.addEventListener('click', () => setView(button.dataset.view));
  });
}

function setView(view) {
  state.view = view;
  saveView(view);
  card.querySelectorAll('button[data-view]').forEach((button) => {
    const on = button.dataset.view === view;
    button.classList.toggle('on', on);
    button.classList.toggle('off', !on);
    button.setAttribute('aria-selected', String(on));
  });
  const body = document.getElementById('sim3d-body');
  if (body) body.hidden = view !== '3d';
  if (sceneCard) sceneCard.hidden = view === '3d';
  if (view === '3d' && state.renderer && state.rafId === null) {
    state.rafId = requestAnimationFrame(frame);
  }
}

function color(rgba) {
  return new THREE.Color(rgba[0], rgba[1], rgba[2]);
}

function buildScene(model) {
  const canvas = document.getElementById('sim3d-canvas');
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf2f2f0);
  const camera = new THREE.PerspectiveCamera(45, 16 / 9, 0.05, 20);
  // ROS(z-up) 좌표 (1.9, -1.6, 1.6)에서 (0.4, -0.2, 0.7)을 본다 → three(y-up)로 바꿔 둔다.
  camera.position.set(1.9, 1.6, 1.6);
  const controls = new OrbitControls(camera, canvas);
  controls.target.set(0.4, 0.7, 0.2);
  controls.update();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8a80, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(2, 4, 3);
  scene.add(sun);

  // ROS 월드(z-up)를 한 묶음으로 두고 x축으로 −90° 돌려 three(y-up)에 맞춘다.
  const root = new THREE.Group();
  root.rotation.x = -Math.PI / 2;
  scene.add(root);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(4, 4),
    new THREE.MeshStandardMaterial({ color: 0xe4e4df }));
  root.add(floor);
  for (const box of model.cell.fixed) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(...box.size_m),
      new THREE.MeshStandardMaterial({ color: color(box.color_rgba) }));
    mesh.position.set(...box.center_xyz_m);
    root.add(mesh);
  }
  for (const item of model.cell.materials) {
    const mesh = new THREE.Mesh(materialGeometry(item),
      new THREE.MeshStandardMaterial({ color: color(item.color_rgba) }));
    mesh.position.set(...item.home_xyz_m);
    mesh.visible = false;            // 관측이 오기 전에는 그리지 않는다
    root.add(mesh);
    state.materials[item.model] = mesh;
  }

  const manager = new THREE.LoadingManager();
  manager.onLoad = () => { state.meshesLoaded = true; };
  const loader = new URDFLoader(manager);
  const robot = loader.parse(model.urdf, '');
  robot.visible = false;             // 첫 관절 관측 전에는 그리지 않는다
  root.add(robot);

  Object.assign(state, { renderer, scene, camera, controls, root, robot });
  const frameEl = document.getElementById('sim3d-frame');
  const resize = () => {
    const width = frameEl.clientWidth || 640;
    const height = Math.min(Math.round(width * 9 / 16), Math.round(window.innerHeight * 0.58));
    renderer.setSize(width, height, false);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  };
  resize();
  if (typeof ResizeObserver === 'function') new ResizeObserver(resize).observe(frameEl);
}

function connect() {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${scheme}://${window.location.host}/v1/sim-view/stream`);
  socket.onopen = () => { state.connected = true; };
  socket.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type !== 'state') return;
    const recv = Date.now() / 1000;
    state.skew.add(recv, data.server_time);
    state.serverStale = Boolean(data.stale);
    state.staleAfter = data.stale_after_sec || state.staleAfter;
    const m = state.metrics;
    m.messages.push(recv);
    if (data.joints && data.joint_seq !== state.lastJointSeq) {
      state.lastJointSeq = data.joint_seq;
      state.joints.push(data.joint_time, data.joints);
      m.transport.push((recv - data.joint_time) * 1000);
    }
    if (data.materials && data.pose_seq !== state.lastPoseSeq) {
      state.lastPoseSeq = data.pose_seq;
      state.poses.push(data.pose_time, data.materials);
      state.robotPose = data.robot_pose;
    }
    if (m.transport.length > 600) m.transport.splice(0, m.transport.length - 600);
  };
  state.socket = socket;
  socket.onclose = () => {
    state.connected = false;
    setTimeout(connect, Math.max(2000, (state.blockUntil || 0) - Date.now()));
  };
  socket.onerror = () => { state.connected = false; };
}

function toRos(object) {
  const v = new THREE.Vector3();
  object.getWorldPosition(v);
  state.root.worldToLocal(v);
  return [v.x, v.y, v.z];
}

function frame(nowMs) {
  if (state.view !== '3d') { state.rafId = null; return; }
  // 카드가 숨겨졌으면(다른 로봇 선택) 그리지 않는다 — 다시 보일 때 setView가 다시 시작한다.
  if (card.hidden) { state.rafId = null; return; }
  state.rafId = requestAnimationFrame(frame);
  const m = state.metrics;
  m.frames.push(nowMs);
  while (m.frames.length && nowMs - m.frames[0] > 1000) m.frames.shift();
  m.fps = m.frames.length;

  const serverNow = Date.now() / 1000 - state.skew.skew;
  const lastJ = state.joints.last;
  const lastP = state.poses.last;
  const status = staleness({
    connected: state.connected, serverStale: state.serverStale,
    lastJointT: lastJ && lastJ.t, lastPoseT: lastP && lastP.t,
    serverNow, staleAfter: state.staleAfter,
  });
  // 낡았으면 **마지막 관측에 멈춘다** — 보간·외삽 없이.
  const target = status.stale ? Infinity : serverNow - DISPLAY_DELAY_SEC;
  let shownT = null;
  const bj = state.joints.bracket(target);
  if (bj && state.robot) {
    const values = lerpJoints(bj.a.value, bj.b.value, bj.f);
    for (const [name, value] of Object.entries(values)) state.robot.setJointValue(name, value);
    state.robot.visible = true;
    shownT = bj.a.t + (bj.b.t - bj.a.t) * bj.f;
    state.displayed = { joints: values };
  }
  const bp = state.poses.bracket(target);
  if (bp) {
    const poses = lerpPoses(bp.a.value, bp.b.value, bp.f);
    for (const [name, pose] of Object.entries(poses)) {
      const mesh = state.materials[name];
      if (!mesh) continue;
      mesh.position.set(pose[0], pose[1], pose[2]);
      mesh.quaternion.set(pose[3], pose[4], pose[5], pose[6]);
      mesh.visible = true;
    }
    if (state.robotPose && state.robot) {
      const r = state.robotPose;
      state.robot.position.set(r[0], r[1], r[2]);
      state.robot.quaternion.set(r[3], r[4], r[5], r[6]);
    }
    state.displayed = { ...(state.displayed || {}), poses, poseT: bp.a.t + (bp.b.t - bp.a.t) * bp.f };
  }
  if (shownT !== null && !status.stale) {
    m.display.push((serverNow - shownT) * 1000);
    if (m.display.length > 600) m.display.splice(0, m.display.length - 600);
  }
  if (state.displayed) state.displayed.jointT = shownT;
  state.status = status;
  updateOverlay(status);
  state.controls.update();
  state.renderer.render(state.scene, state.camera);
}

let lastOverlay = '';
function updateOverlay(status) {
  const badge = document.getElementById('sim3d-status');
  const stale = document.getElementById('sim3d-stale');
  const metrics = document.getElementById('sim3d-metrics');
  const m = state.metrics;
  const display = percentile(m.display.slice(-90), 50);
  const text = status.stale
    ? `■ 정지 화면 · ${status.reason}`
    : `● 관측 · ${m.fps} FPS · 표시 지연 ${display == null ? '—' : Math.round(display)} ms`;
  if (text !== lastOverlay && badge) {
    badge.textContent = text;
    badge.classList.toggle('stop', status.stale);
    if (stale) {
      stale.hidden = !status.stale;
      stale.textContent = status.stale
        ? `관측이 끊겼거나 오래됐습니다 (${status.reason}). 마지막 관측 상태로 멈춰 있습니다 — 실제 로봇 움직임이 아닙니다.`
        : '';
    }
    lastOverlay = text;
  }
  if (metrics && m.frames.length % 30 === 0) {
    const t50 = percentile(m.transport.slice(-90), 50);
    metrics.textContent = t50 == null ? '' : `(전송 지연 중앙값 ${t50.toFixed(1)} ms)`;
  }
}

function metricsSnapshot() {
  const m = state.metrics;
  const now = Date.now() / 1000;
  const recent = m.messages.filter((t) => now - t < 2);
  m.messages = recent;
  return {
    fps: m.fps, messagesPerSec: recent.length / 2,
    transportMs: { p50: percentile(m.transport, 50), p95: percentile(m.transport, 95),
      n: m.transport.length },
    displayMs: { p50: percentile(m.display, 50), p95: percentile(m.display, 95),
      n: m.display.length },
    skewSec: state.skew.skew, stale: state.status ? state.status.stale : true,
    reason: state.status ? state.status.reason : '', meshesLoaded: state.meshesLoaded,
    view: state.view,
  };
}

function snapshot() {
  const out = { wall: Date.now() / 1000, skew: state.skew.skew,
    jointT: state.displayed ? state.displayed.jointT : null,
    poseT: state.displayed ? state.displayed.poseT : null,
    stale: state.status ? state.status.stale : true, links: {}, materials: {} };
  if (!state.robot) return out;
  for (const name of TRACKED_LINKS) {
    const link = state.robot.links[name];
    if (link) out.links[name] = toRos(link);
  }
  for (const [name, mesh] of Object.entries(state.materials)) out.materials[name] = toRos(mesh);
  return out;
}

async function start() {
  if (!card) return;
  renderShell();
  let model = null;
  try {
    const response = await fetch('/v1/sim-view/model', { cache: 'no-store' });
    model = await response.json();
  } catch (error) {
    model = { available: false, detail: String(error) };
  }
  if (!model || !model.available) {
    // 3D를 쓸 수 없으면 Gazebo 영상으로 두고 이유를 적는다.
    setView('camera');
    const button = card.querySelector('button[data-view="3d"]');
    if (button) { button.disabled = true; button.title = model && model.detail || '3D 화면을 쓸 수 없습니다'; }
    return;
  }
  state.model = model;
  state.staleAfter = model.stale_after_sec || 0.5;
  buildScene(model);
  connect();
  setView(readView());
  window.__simView = {
    metrics: metricsSnapshot, snapshot, setView,
    // 검증용: 지표를 비운다(페이지 로딩 구간을 빼고 정상 상태만 재기 위해).
    resetMetrics() {
      Object.assign(state.metrics, { frames: [], messages: [], transport: [], display: [] });
    },
    // 검증용: 연결을 끊고 ms 동안 다시 붙지 않는다(끊김 표시 확인).
    debugDisconnect(ms) {
      state.blockUntil = Date.now() + ms;
      if (state.socket) state.socket.close();
    },
  };
}

start();
