# 외부 라이브러리 (웹 3D 작업 셀 화면)

오프라인 LAN에서도 화면이 뜨도록 복사해 둔 것이다. 바꾼 것은 **import 경로뿐**이다 —
`'three'`, `'three/examples/jsm/…'` 같은 bare 경로를 상대 경로로 바꿨다(import map 없이
진입점이 우리 자산 두 개만 불러오게 하려고). 코드 본문은 수정하지 않는다.

| 경로 | 출처 | 라이선스 |
|---|---|---|
| `three/` | npm `three@0.170.0` (build/three.module.js, examples/jsm 로더·OrbitControls) | MIT (`three/LICENSE`) |
| `urdf-loader/` | npm `urdf-loader@0.12.6` (src/URDFLoader.js, src/URDFClasses.js) | Apache-2.0 (`urdf-loader/LICENSE`) |

`static/js/sim-view.js`가 `main.js`에서 동적 import로 불려 이 경로를 쓴다.
