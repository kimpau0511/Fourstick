# forstick2 안내판

작업 전에 **CODE_RULES.md를 먼저 읽는다.** 규칙은 거기에만 있고, 이 파일은
어디에 뭐가 있는지만 적는다(CODE_RULES.md 15번).

| 알고 싶은 것 | 볼 곳 |
|---|---|
| 코딩 규칙 · AI 협업 방식 | `CODE_RULES.md` |
| 설계 원칙(데이터 접근 · 확인 안 됨은 통과 아님 · 실행 직전 재검증 · 시뮬≠실기) | `docs/설계원칙.md` |
| 시스템 개요 · 실행 방법 · 전제 환경 | `README.md` |
| 디렉터리 역할 · 의존 방향 | `CODE_RULES.md` 1번 |
| 실패 · 거부 사유 코드 | `core/reason_codes.py` |
| 수치 · 정책 값과 근거 | `core/policy.py` |
| 실행 허가 원칙 | `validation/execution_permit.py` |
| 정지(STOP) 계약 | `core/stop_contract.py` |
| 로봇 어댑터 추가 | `robots/base/robot_adapter.py`(인터페이스), 본보기 `robots/fr3_gazebo/__init__.py`. 패키지는 `ADAPTER_ENTRY_POINT`(진입 함수 **이름**을 담은 문자열)·`build_transfer_capability`·`build_view_source`를 제공한다. 진입 함수는 키워드 `robot_id, profile, workcell_config, workcell_poses, now, stop_velocity_rad_s, max_sample_gap_sec`를 받아 dict를 돌려주며 필수 키는 `factory`·`status`(선택 키는 `server/runtime.py:1169-1175`). 반환 타입 힌트 `tuple[...]`은 틀렸고 실제는 dict. 새 셀 JSON은 `config/workcell/`의 `workcell`·`poses`·`resource_catalog`·(이송용)`grasp`·`path_clearance`와 `config/profiles/`의 능력 프로필 능력 프로필 스키마는 `config/loader.py`의 `CapabilityProfileModel` |
| 로봇이 등록되는 곳 | `config/workcell/active.json`의 `adapter_module` → `server/runtime.py`가 동적 import 후 `registry.register()`(1168행). `robots/registry.py`에는 고정 목록이 없어 새 로봇 때문에 고치지 않는다. **환경변수 `FORSTICK2_WORKCELL_ROBOT=1`일 때만** 이 블록이 실행된다(`server/config.py`, 기본 꺼짐 → 꺼져 있으면 Fake 로봇으로 떨어짐, `server/runtime.py:1190-1201`). 켜도 실패하면 예외 없이 미등록되고 이유는 `workcell_status`의 `detail`·`reason_code=config.invalid`에만 남는다. 켜는 스크립트는 `scripts/run_web_workcell.sh`. G1 휴머노이드는 registry를 거치지 않고 `server/humanoid_service.py`로 붙는다 |
| 실기 전환 준비 매니페스트 | `config/hardware/active.json`(작업 셀 매니페스트와 별개). 판정은 `validation/hardware_readiness.py` |
| 작업 셀 설정(활성 셀 선택) | `config/workcell/active.json`, 로더 `config/loader.py` |
| 입력·출력 예시 | `examples/INDEX.json` |
| 평가·회귀용 고정 입력 | `fixtures/` |
| 환경변수 | `server/config.py` |
| 이송 스킬 계약(로봇 무관) | `core/transfer_skill.py` |
| 일반 경로(`/v1/plan`→`/v1/execute`) pick/place — 시뮬레이션 셀 전용 | 열기 판정·transfer 묶음·실행 `server/sim_pick_place.py`, 시뮬레이션 전용 Profile `core/sim_profile.py` + `config/profiles/simulation/`(근거 측정 `scripts/measure_sim_profile_evidence.py`), 관문 `validation/pick_place_gate.py`의 `evaluate_simulation`, E2E `scripts/verify_general_pick_place.py`. 실물 Profile·실물 관문은 그대로 |
| 시연 명령 API (`/v1/sim-demo/*`) | `server/routes/sim_demo.py` — `/command` 응답은 `command_response`의 `base`와 `answer()`에서 조립한다. **`/confirm`은 `confirm_response`(940행~)가 자기 `base`(952행)를 따로 만든다 — `command_response`의 `base`를 고쳐도 반영되지 않는다.** `/goals/<id>/confirm`은 `handle`의 goals 분기(135행~)가 따로 처리. 하위 모듈: 규칙 해석 `server/sim_demo_commands.py`, 모호 발화 분류 `sim_demo_intent.py`, 확인 대기 `sim_demo_confirm.py`, 목표 `sim_demo_goals.py`, 맥락 `sim_demo_context.py`, 배치 `sim_demo_arrangement.py`, 자리 `sim_demo_places.py`, 작업 실행 `sim_demo_jobs.py` |
| 공개 응답 필터 — **모든 응답이 지나며 실기 상태 키를 조용히 지운다** | `server/routes/common.py`의 `json_response` → `public_payload`, 제거 대상 `HIDDEN_HARDWARE_KEYS` — 이름이 **정확히 일치하는** 6개 키(`real_hardware`·`real_hardware_ready`·`real_hardware_verified`·`real_hardware_connected`·`real_hardware_ready_at`·`hardware_readiness`)만 지운다. 접두어로 지우지 않으므로 `real_hardware_claim` 같은 다른 이름은 그대로 나간다. 새 응답 필드 이름이 저 6개와 겹치면 사라진다. 의도된 동작(시뮬레이션 전용 서비스) |
| 같은 API를 쓰는 화면 | 신 `dashboard2/src/simCommand.js`(표시 `App.jsx`), 구 웹 `html/static/js/` — 호출 `backend-http.js`, 처리 `main.js`, 상태 `state.js`, 표시 `render.js`·`sim-demo.js`, 모의 `backend-sim.js` |
| 관제 대시보드 (React+Vite, `npm run dev`) | `dashboard2/` — 진입 `src/App.jsx`, 서버 값 수집·상태 판정 `src/server.js`(useServer), 시연 명령 `src/simCommand.js`, 화면 `src/pages/*`, 공통 `src/components/*`. 설계 `dashboard2/IMPLEMENTATION_SPEC.md`. 목업 없음 — 서버에 없는 값은 "데이터 없음/확인 안 됨" |
| 요구사항 QA(대시보드 화면·서버 API·백엔드 테스트 대응) | `dashboard2/e2e/` — `npm run qa`·`qa:report`, 백엔드 대응 `dashboard2/e2e/backend_map.py`. 추적표·결과는 저장소 밖 `문서/QA_*.md`(로컬) |
| 휴머노이드(G1) 실험 | `humanoid/g1/README.md` |
| 테스트 | `tests/` (`unit` · `integration` · `contract` · `model` · `web`). `/v1/sim-demo/command`를 호출하는 단위 테스트는 `test_sim_demo_{commands,confirm,goals,context,arrangement}.py`에 더해 `test_material_colors.py`·`test_transfer_stages.py`·`test_public_payload.py`(공개 필터 검증), 화면은 `tests/web/*.mjs` |
| 설계 문서 (docstring의 `md/...` 참조) | `md/` — 저장소에 없음(`.gitignore`), 로컬에만 있음 |
| 실행 계획 기록 | `md/exec-plans/` (CODE_RULES.md 17번) |
