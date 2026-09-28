# 프론트엔드용 시뮬레이션 API (FR3 작업 셀)

기본 주소 `http://127.0.0.1:8092`. **인증 없음**(localhost 개발용). 모든 응답은 시뮬레이션(`is_simulated: true`)이다.
아래 예시 값은 깨끗한 복제본 서버에서 실제로 받은 응답을 줄인 것이다(2026-09-28).

## 안전 경계(프론트가 바꾸지 않는다)

- 작업은 **확인 카드**를 거친다: 명령 → `CONFIRM` + `token` → 사용자가 누르면 `/v1/sim-demo/confirm` → 그때 작업 생성. 만료 60 s.
- `멈춰`·`/v1/sim-demo/stop`은 확인 없이 즉시 정지 요청이다.
- 일반 `/v1/plan`의 pick/place는 계속 막혀 있다(`422 capability.profile_incomplete`). 시연 이송은 이 API로만 한다.
- 충돌·도달 검사(MoveIt 경로 표본 검사)는 서버·작업 스크립트가 하고, 통과하지 못하면 작업이 시작되지 않는다.

## 흐름

| 단계 | 요청 | 응답 핵심 |
|---|---|---|
| 세션 | `POST /v1/sessions` `{}` | `session_id` |
| 상태 | `GET /v1/sim-demo` | `enabled`, `materials[]`(자재별 가능 동작 `actions`), `conveyor.slots[]`, `running_job`, `pending_confirmation`, `intent_available` |
| 명령 | `POST /v1/sim-demo/command` `{session_id, mode:"simulation_demo", source:"text"\|"stt_final", utterance}` | `decision`: `CONFIRM`·`CONFIRM_GOAL`·`ASK`·`BLOCK`·`STOP`·`NOOP`·`PASS_THROUGH`·`RUN` · `reason` · `confirmation{token, summary, action, ...}` |
| 확인 | `POST /v1/sim-demo/confirm` `{session_id, token, action:"confirm"\|"cancel"}` | `202 decision:"RUN"` + `job{job_id, action, material, slot}` / 취소는 작업 0건 |
| 목표 확인 | `POST /v1/sim-demo/goals/<goal_id>/confirm` `{action}` | `CONFIRM_GOAL`(여러 단계 배치)일 때 |
| 진행 | `GET /v1/sim-demo/jobs/<job_id>` | `status`(`running`→`finished`), `exit_code`, `progress`, `console_tail[]`, `report.status` |
| 정지 | `POST /v1/sim-demo/stop` `{}` 또는 명령 "멈춰" | `STOP` + `stop{requested, job_id}` |
| 3D 모델 | `GET /v1/sim-view/model` · `GET /v1/sim-view/mesh/<n>/<파일>` | 화면용 URDF·셀 상자·자재 |
| 3D 관측 | `GET /v1/sim-view/state` · `WS /v1/sim-view/stream`(최대 30 Hz) | `joints{}`, `materials{model:[x,y,z,qx,qy,qz,qw]}`, `stale` |

`decision`별 화면: `CONFIRM`/`CONFIRM_GOAL` → 카드(`confirmation.summary`, 해석 근거 `interpretation.evidence`) · `ASK`/`BLOCK` → `reason` 표시,
작업 없음 · `PASS_THROUGH` → 시연 명령이 아님(기존 `/v1/plan`으로 갈지 화면이 정한다) · `RUN` → 텍스트 정확 명령이 바로 시작됨(`job`).
작업 결과 `report.status`: `simulation_transfer_completed` · `returned_to_origin` · `simulation_transfer_stopped` · `simulation_transfer_incomplete`(실패 — `report.reason_codes`).

## 실제 연결 예시

```bash
S=$(curl -s -X POST localhost:8092/v1/sessions -d '{}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST localhost:8092/v1/sim-demo/command -H 'content-type: application/json' \
  -d "{\"session_id\":\"$S\",\"mode\":\"simulation_demo\",\"source\":\"text\",\"utterance\":\"주황 자재 컨베이어로 옮겨줘\"}"
# → {"decision":"CONFIRM","confirmation":{"token":"…","summary":"A자재를 컨베이어 1번 위치로 옮기겠습니다.",…},
#    "interpretation":{"evidence":["'주황 자재' = A자재(등록 색 주황색, 관측 verified)"]}, …}
curl -s -X POST localhost:8092/v1/sim-demo/confirm -H 'content-type: application/json' \
  -d "{\"session_id\":\"$S\",\"token\":\"<위 token>\",\"action\":\"confirm\"}"
# → 202 {"decision":"RUN","job":{"job_id":"simjob_…","action":"transfer","material":"material_a",…}}
curl -s localhost:8092/v1/sim-demo/jobs/simjob_…     # status finished · report.status simulation_transfer_completed
curl -s localhost:8092/v1/sim-view/state              # materials.material_a ≈ [0.250, -0.500, 0.750, …] (컨베이어 1번)
```

```js
const api = (p, body) => fetch(p, body === undefined ? {} : {
  method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }).then((r) => r.json());
const { session_id } = await api('/v1/sessions', {});
const res = await api('/v1/sim-demo/command', { session_id, mode: 'simulation_demo', source: 'text', utterance: '주황 자재 컨베이어로 옮겨줘' });
if (res.decision === 'CONFIRM' && window.confirm(res.confirmation.summary)) {
  const { job } = await api('/v1/sim-demo/confirm', { session_id, token: res.confirmation.token, action: 'confirm' });
  const timer = setInterval(async () => {
    const j = await api(`/v1/sim-demo/jobs/${job.job_id}`);
    if (j.status !== 'running') { clearInterval(timer); console.log(j.report?.status); }
  }, 2000);
}
const ws = new WebSocket(`ws://${location.host}/v1/sim-view/stream`);
ws.onmessage = (m) => { const s = JSON.parse(m.data); if (!s.stale) console.log(s.materials.material_a); };
// 정지: await api('/v1/sim-demo/stop', {})
```

참고 구현: `html/static/js/backend-http.js`(`simDemoCommand`·`simDemoConfirm`·`simDemoJob`), `html/static/js/sim-view*.js`.
