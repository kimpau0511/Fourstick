import { useCallback, useEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import URDFLoader from 'urdf-loader';
import {
  ClockSkew, DISPLAY_DELAY_SEC, SampleBuffer, lerpJoints, lerpPoses, staleness,
} from '../../html/static/js/sim-view-core.js';
import SceneView from './SceneView.jsx';
import Spinner from './components/Spinner.jsx';
import { materialGeometry } from './materialGeometry.js';

/** 서버가 스트림을 닫으면 이 간격으로 다시 붙는다(백엔드 화면 sim-view.js와 같은 값). */
const RECONNECT_MS = 2000;

function color(rgba) {
  return new THREE.Color(rgba[0], rgba[1], rgba[2]);
}

/** 작업 셀 3D 화면(읽기 전용). 백엔드 화면 html/static/js/sim-view.js를 옮긴 것이다.
 *
 *  Gazebo 카메라 영상(/v1/scene/stream, 실측 5.6장/초)보다 갱신이 잦아서(관측 스트림
 *  /v1/sim-view/stream 실측 29.6회/초, 2026-10-02) 시뮬레이션 창의 기본 화면으로 쓴다.
 *  관측 두 개 사이만 보간하고, 끊기거나 낡으면 마지막 관측에 멈추고 그렇다고 알린다.
 *  서버가 3D 모델을 줄 수 없으면 Gazebo 영상(SceneView)으로 대신한다.
 *  `onView`로 { mode: '3d', status, fps, stale, reason } 또는 SceneView의 값을 알린다.
 *  `onJoints`(선택): 관절 상태 패널용 — 마지막 **관측 원본**(보간 전, rad)과 신선도 판정을 초당 최대 10번 알린다.
 *  { limits, armJoints, joints, seq, stale, reason, connected }. 3D 모델을 못 받으면 { unavailable: true }. */
const JOINT_REPORT_MS = 100;
export default function SimView3D({ onView, onJoints }) {
  const frameRef = useRef(null);
  const canvasRef = useRef(null);
  const [mode, setMode] = useState('3d');
  const [observed, setObserved] = useState(false); // 첫 관측이 오면 연결 중 스피너를 지운다
  const onCameraView = useCallback((view) => onView?.({ mode: 'camera', ...view }), [onView]);
  const jointsRef = useRef(onJoints);
  useEffect(() => { jointsRef.current = onJoints; });

  useEffect(() => {
    if (mode !== '3d') return undefined;
    let disposed = false;
    let socket = null;
    let rafId = null;
    let reconnectTimer = null;
    let observer = null;
    let renderer = null;
    let lastSent = '';
    const s = {
      joints: new SampleBuffer(), poses: new SampleBuffer(), skew: new ClockSkew(),
      connected: false, serverStale: true, staleAfter: 0.5, lastJointSeq: null, lastPoseSeq: null,
      robotPose: null, materials: {}, frames: [], limits: null, armJoints: [], lastJointsReport: 0,
    };
    const report = (view) => {
      const key = JSON.stringify(view);
      if (key !== lastSent) { lastSent = key; onView?.({ mode: '3d', ...view }); }
    };
    report({ status: 'connecting', fps: 0, stale: true, reason: '' });

    function buildScene(model) {
      const canvas = canvasRef.current;
      renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
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
      root.add(new THREE.Mesh(new THREE.PlaneGeometry(4, 4),
        new THREE.MeshStandardMaterial({ color: 0xe4e4df })));
      for (const box of model.cell.fixed) {
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(...box.size_m),
          new THREE.MeshStandardMaterial({ color: color(box.color_rgba) }));
        mesh.position.set(...box.center_xyz_m);
        root.add(mesh);
      }
      for (const item of model.cell.materials) {
        const mesh = new THREE.Mesh(materialGeometry(item),
          new THREE.MeshStandardMaterial({ color: color(item.color_rgba) }));
        mesh.name = item.name || item.model;
        mesh.position.set(...item.home_xyz_m);
        mesh.visible = false;            // 관측이 오기 전에는 그리지 않는다
        root.add(mesh);
        s.materials[item.model] = mesh;
      }
      const robot = new URDFLoader().parse(model.urdf, '');
      robot.visible = false;             // 첫 관절 관측 전에는 그리지 않는다
      root.add(robot);

      const resize = () => {
        const el = frameRef.current;
        if (!el) return;
        const width = el.clientWidth || 640;
        const height = el.clientHeight || Math.round(width * 9 / 16);
        renderer.setSize(width, height, false);
        camera.aspect = width / height;
        camera.updateProjectionMatrix();
      };
      resize();
      observer = new ResizeObserver(resize);
      observer.observe(frameRef.current);
      return { scene, camera, controls, robot };
    }

    function connect() {
      if (disposed) return;
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
      socket = new WebSocket(`${scheme}://${window.location.host}/v1/sim-view/stream`);
      socket.onopen = () => { s.connected = true; };
      socket.onmessage = (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch { return; }
        if (data.type !== 'state') return;
        s.skew.add(Date.now() / 1000, data.server_time);
        s.serverStale = Boolean(data.stale);
        s.staleAfter = data.stale_after_sec || s.staleAfter;
        if (data.joints && data.joint_seq !== s.lastJointSeq) {
          s.lastJointSeq = data.joint_seq;
          s.joints.push(data.joint_time, data.joints);
        }
        if (data.materials && data.pose_seq !== s.lastPoseSeq) {
          s.lastPoseSeq = data.pose_seq;
          s.poses.push(data.pose_time, data.materials);
          s.robotPose = data.robot_pose;
        }
      };
      socket.onclose = () => {
        s.connected = false;
        socket = null;
        if (!disposed) reconnectTimer = setTimeout(connect, RECONNECT_MS);
      };
    }

    function loop(world, nowMs) {
      rafId = requestAnimationFrame((t) => loop(world, t));
      s.frames.push(nowMs);
      while (s.frames.length && nowMs - s.frames[0] > 1000) s.frames.shift();
      const serverNow = Date.now() / 1000 - s.skew.skew;
      const status = staleness({
        connected: s.connected, serverStale: s.serverStale,
        lastJointT: s.joints.last?.t, lastPoseT: s.poses.last?.t,
        serverNow, staleAfter: s.staleAfter,
      });
      // 낡았으면 **마지막 관측에 멈춘다** — 보간·외삽 없이.
      const target = status.stale ? Infinity : serverNow - DISPLAY_DELAY_SEC;
      const bj = s.joints.bracket(target);
      if (bj) {
        const values = lerpJoints(bj.a.value, bj.b.value, bj.f);
        for (const [name, value] of Object.entries(values)) world.robot.setJointValue(name, value);
        world.robot.visible = true;
      }
      const bp = s.poses.bracket(target);
      if (bp) {
        for (const [name, pose] of Object.entries(lerpPoses(bp.a.value, bp.b.value, bp.f))) {
          const mesh = s.materials[name];
          if (!mesh) continue;
          mesh.position.set(pose[0], pose[1], pose[2]);
          mesh.quaternion.set(pose[3], pose[4], pose[5], pose[6]);
          mesh.visible = true;
        }
        if (s.robotPose) {
          const r = s.robotPose;
          world.robot.position.set(r[0], r[1], r[2]);
          world.robot.quaternion.set(r[3], r[4], r[5], r[6]);
        }
      }
      // FPS를 5 단위로 반올림해 알린다 — 59·60·61로 흔들릴 때마다 상위 컴포넌트를 다시 그리지 않게.
      setObserved(true);
      report({ status: 'live', fps: Math.round(s.frames.length / 5) * 5, stale: status.stale, reason: status.reason });
      if (nowMs - s.lastJointsReport >= JOINT_REPORT_MS) {
        s.lastJointsReport = nowMs;
        const last = s.joints.last;
        jointsRef.current?.({ limits: s.limits, armJoints: s.armJoints, joints: last ? last.value : null,
          seq: s.lastJointSeq, stale: status.stale, reason: status.reason, connected: s.connected });
      }
      world.controls.update();
      renderer.render(world.scene, world.camera);
    }

    (async () => {
      let model;
      try {
        model = await (await fetch('/v1/sim-view/model', { cache: 'no-store' })).json();
      } catch (error) {
        model = { available: false, detail: String(error) };
      }
      if (disposed) return;
      if (!model?.available) { jointsRef.current?.({ unavailable: true }); setMode('camera'); return; }
      s.staleAfter = model.stale_after_sec || s.staleAfter;
      s.limits = model.joint_limits || null;
      s.armJoints = Array.isArray(model.arm_joints) ? model.arm_joints : [];
      const world = buildScene(model);
      connect();
      rafId = requestAnimationFrame((t) => loop(world, t));
    })();

    return () => {
      disposed = true;
      if (rafId !== null) cancelAnimationFrame(rafId);
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (socket) socket.close();
      observer?.disconnect();
      renderer?.dispose();
    };
  }, [mode, onView]);

  if (mode === 'camera') return <SceneView onView={onCameraView} />;
  return <div ref={frameRef} className="sim3d-frame">
    <canvas ref={canvasRef} className="scene-canvas" />
    {!observed && <div style={{ position: 'absolute', inset: 0, display: 'grid', placeItems: 'center', pointerEvents: 'none' }}><Spinner size={28} label="3D 화면 연결 중" /></div>}
  </div>;
}
