# dashboard2 배포 형태 시험 (nginx)

vite 개발 서버 대신 **build 결과 + nginx**로 띄워, 실제 배포와 같은 형태(정적 파일 · API 프록시 · 웹소켓)를 이 PC에서 확인한다.
설정은 `nginx.conf` 하나다. 이 PC(127.0.0.1:8088)에서만 열린다 — 백엔드에 인증이 없으므로 LAN·외부 공개 전에 인증이 먼저다.

## 실행 (Windows PowerShell, `dashboard2/` 기준)

```powershell
npx vite build                                   # dist/ 생성(.env.local의 VITE_COMMAND_MODE가 build에 들어간다)
# scoop 설치 기준(다른 경로면 바꾼다). 버전 폴더의 실행 파일을 직접 찾는다 — 아래 '주의' 참고
$ng = Get-ChildItem "$env:USERPROFILE\scoop\apps\nginx\*\nginx.exe" | Where-Object { $_.FullName -notmatch '\\current\\' } | Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
& $ng -p deploy -c nginx.conf -t                  # 설정 검사
& $ng -p deploy -c nginx.conf                     # 실행(창을 닫지 않고 둔다) → http://127.0.0.1:8088
& $ng -p deploy -c nginx.conf -s reload           # 설정 다시 읽기
& $ng -p deploy -c nginx.conf -s stop             # 끄기
```

- `-p deploy`: 로그·임시 파일은 `deploy/logs`·`deploy/temp`에 생긴다(git 제외).
- 주의: 이 PC에서는 scoop의 `nginx` 바로가기와 `apps\nginx\current\` 경로가 실행되지 않는다. `scoop reset nginx`도
  Windows의 "untrusted mount point" 정책에 막혀 `current` 연결을 못 만든다(2026-10-06 확인). 그래서 버전 폴더의
  `nginx.exe`를 직접 부른다.
- 화면을 고치면 `npx vite build`만 다시 하면 된다(index.html은 캐시하지 않는다).

## 백엔드 주소

`nginx.conf`의 `proxy_pass`·`proxy_ssl_name`·`Host` 세 줄. 지금은 공개 주소 `https://foursticks.xos.kr`(외부 서버 → SSH 터널 → PC1 8092).

## 확인한 것 (2026-10-06, 공개 주소 백엔드)

- 정적 파일 200, 없는 경로는 index.html, `/assets/`는 장기 캐시
- `/health`·`/v1/config`·`/v1/history`·`/v1/sim-view/state` 200, `/v1/sim-view/stream` 웹소켓 101
- 화면: 헤더 수신 시각·로봇 상태·진단 8개 서비스, 일반 경로 계획 → 안전 관문 판정까지(실행 승인은 누르지 않음), 콘솔 오류 0
- 확인 못 한 것: 음성(마이크 없는 자동 브라우저), 실행 중 긴 응답(`/v1/execute`, 600초 제한으로 둠)
