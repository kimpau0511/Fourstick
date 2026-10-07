# 자재 형상 변형: 사각형 · 삼각형 · 원형 (보관, 2026-10-07)

현장 1의 자재 A/B/C(상자)를 **사각형(상자)·삼각형(삼각기둥)·원형(원기둥)** 으로 바꾼 설정이다. 2026-10-07에 만들고
격리 복제 셀에서 이송·복귀 왕복을 확인했지만(삼각형 3회 포함), 사용자 결정으로 **운영 설정은 되돌리고 이 파일만 보관**한다.

| 모델 | 이름 | 형상 | 예전 이름(별칭으로 유지) |
|---|---|---|---|
| material_a | 사각형 자재 | box | A자재 |
| material_b | 삼각형 자재 | triangle_prism | B자재 |
| material_c | 원형 자재 | cylinder | C자재 |

위치·외곽 크기(0.05×0.05×0.10 m)·질량·색은 원래와 같다 → 검증된 자세·잡기 높이를 그대로 쓴다.
잡기는 시뮬레이션 강제 부착이다(마찰 파지 아님). 그리퍼 닫힘 값은 50 mm 상자 기준이다.

## 적용하는 법
1. 이 폴더의 두 파일을 `config/workcell/`에 덮어쓴다(`fr3_2f85_workcell.json`, `fr3_2f85_workcell_resource_catalog.json`).
2. Gazebo를 다시 띄운다 — 실행 스크립트가 world(`config/gazebo/fr3_2f85_workcell.sdf`)와 삼각기둥 메시를 다시 만든다.
3. 웹 서버를 다시 띄운다(서버 자재 정보·3D 화면이 새 형상·이름을 쓴다).
4. 시험에서 자재 이름을 기대하는 문장(A자재 → 사각형 자재 등)을 맞춘다. 커밋 570fb63에 바꾼 예가 있다.

형상을 그리는 코드는 이미 들어 있다(기본은 상자): `scripts/build_workcell_world.py`(material_geometry),
`server/sim_view.py`(cell_boxes의 shape·name), `dashboard2/src/materialGeometry.js`·`html/static/js/sim-view.js`,
`server/sim_demo_commands.py`(korean_aliases·korean_shapes 별칭). 확인 시험: `tests/unit/test_material_shapes.py`.
