---
name: sdd
description: forstick2에서 새 기능·안전 동작·API·계층 계약 변경을 Spec-Driven Development 절차로 진행할 때 사용한다. 스펙 작성, 구현 단계 분해, 단계별 구현, 교차 리뷰, 환류.
---

이 저장소의 SDD 절차 원본은 `docs/SDD.md`다. 먼저 그 파일과 `AGENTS.md`를 읽고 그대로 따른다.
여기에 절차를 따로 적지 않는다 — 두 곳에 두면 어긋난다.

요약:
1. 사용자가 기능을 설명하면 1·2단계를 수행해 `specs/<kebab-case-이름>.md`(템플릿 `specs/_TEMPLATE.md`)를 쓰고
   `specs/README.md` 목록을 갱신한 뒤 승인을 요청한다. 코드는 쓰지 않는다.
2. 승인 뒤 구현은 한 번에 한 단계씩. 단계가 3개 이상이면 단계마다 새 Codex 세션(또는 `codex exec`)에서
   `AGENTS.md`와 스펙 파일만 보고 시작하고, 끝나면 상태 표를 갱신한다.
3. 애매하거나 잘 모르겠으면 항상 사람에게 질문한다.
