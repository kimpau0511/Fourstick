# 로그인 화면 주소

기존 `FORSTICK2_PUBLIC_ORIGIN`은 기본 화면 주소와 Origin 없는 요청의 Google 코드 교환 주소다.
추가 주소는 `FORSTICK2_ADDITIONAL_PUBLIC_ORIGINS`에 쉼표로 구분해 등록한다. 기본값은 비어 있다.

```sh
FORSTICK2_ADDITIONAL_PUBLIC_ORIGINS=http://localhost:5175
```

이 PC에서는 `config/local.env`에 설정한다. 실행 중인 백엔드는 재시작해야 설정과 코드가 반영된다.
기존 Cloudflare 주소를 `FORSTICK2_PUBLIC_ORIGIN`에 그대로 두면 두 주소를 함께 사용할 수 있다.
Google OAuth 클라이언트의 승인된 JavaScript 원본에도 각 화면 주소를 등록해야 한다.

로그인 요청과 WebSocket의 Origin은 등록된 주소와 일치해야 한다. 다른 포트·접미 도메인은 허용하지 않는다.
Google 팝업 코드 교환의 `redirect_uri`는 허용된 요청 Origin을 사용한다. 요청마다 설정 사본을 만들어
서로 다른 주소에서 동시에 로그인해도 교환 주소가 섞이지 않는다.
쿠키는 HTTPS 주소에서 Secure를 유지하고 HTTP localhost에서는 Secure 없이 발급·삭제한다.
HttpOnly·SameSite=Lax·등록 계정 검증·로그인 요구 설정은 유지한다.

검증: `.venv/bin/python -m unittest discover -s tests/integration -p test_auth_api.py -q`.
Google 교환은 테스트에서 모의 응답으로 대체한다.
