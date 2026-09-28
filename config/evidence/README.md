# 실행 근거 스냅숏

`reports/`(Git 제외, 측정 산출물)에 있던 파일 중 **FR3 작업 셀을 띄우는 데 필요한 것만** 복사해 둔 것이다.
내용은 원본 그대로다(출처 기록 포함 — 파일 안의 `/home/asd/...` 경로는 측정 당시 기록이며 실행에 쓰지 않는다).

| 파일 | 원본 | 쓰는 곳 |
|---|---|---|
| `moveit_self_collision_review.json` | `reports/moveit/self_collision_review.json` (2026-09-16) | `run_moveit_workcell.sh` → `prune_self_collision_srdf.py`(SRDF를 다시 만들 때) |
| `gripper_self_collision_review.json` | `reports/gripper/self_collision_review.json` (2026-09-16) | 같음 |
| `loop_closure_pairs.json` | `reports/workcell/loop_closure_pairs.json` (2026-09-16) | `inject_loop_closure_srdf.py`(그리퍼 닫힌 고리 쌍) |
| `fr3_2f85_workcell.srdf` | 개발 PC `/tmp/forstick2_workcell/workcell/fr3_2f85_workcell.srdf` (2026-09-21 생성, 비활성 쌍 46) | 처음 실행하는 PC의 MoveIt SRDF(`collisions_updater`는 무작위 표본이라 PC마다 달라질 수 있다) |

측정을 다시 했으면 `FORSTICK2_EVIDENCE_DIR`로 새 근거 폴더를 가리키고 `FORSTICK2_MOVEIT_REGEN_SRDF=1`로 SRDF를 다시 만든다.
