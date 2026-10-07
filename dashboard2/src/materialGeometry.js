import * as THREE from 'three';

// 자재 형상 — 서버 셀 정보(cell.materials[].shape)를 Gazebo world 생성기와 **같은 규칙**으로 그린다
// (scripts/build_workcell_world.py material_geometry). ROS 좌표(z-up) 기준, 중심이 원점.
//   box: size_m 그대로 · cylinder: 지름 = 짧은 변, 축 z · triangle_prism: 정삼각형 한 변 = 짧은 변, 꼭짓점 +x, 축 z
export function materialGeometry(item) {
  const [sx, sy, sz] = item.size_m;
  const side = Math.min(sx, sy);
  if (item.shape === 'cylinder') {
    const g = new THREE.CylinderGeometry(side / 2, side / 2, sz, 40);
    g.rotateX(Math.PI / 2); // three 원통 축(y) → ROS z
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
