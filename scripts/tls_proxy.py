#!/usr/bin/env python3
"""웹 UI 앞에 TLS만 얹는 중계.

브라우저는 **localhost가 아닌 평문 HTTP 주소에서 마이크를 주지 않는다**(보안
컨텍스트가 아니다). 그래서 같은 네트워크의 다른 기기나 PC의 사설 IP로 들어올
때 STT를 쓰려면 HTTPS가 필요하다.

서버 자체를 HTTPS로 바꾸지 않는다. 평문 8094는 그대로 두고 여기서 TLS만
벗겨 넘긴다 — `http://localhost:8094` 접속과 서버 구성이 바뀌지 않는다.

TCP를 그대로 잇기 때문에 WebSocket(`/v1/events`, `/v1/stt`, `/v1/scene/stream`)도
따로 다룰 것이 없다. HTTP를 해석하지 않으므로 헤더를 고치지도 않는다.

인증서는 자체 서명이다. 브라우저는 한 번 경고를 띄우고, 사용자가 넘기면 그
출처를 **보안 컨텍스트로 취급한다** — 마이크가 열린다.
"""

from __future__ import annotations

import argparse
import asyncio
import ssl
import sys
from pathlib import Path


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """한 방향으로 흘린다. 끊김은 정상 종료로 본다."""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — 닫기 실패로 다른 방향을 막지 않는다
            pass


async def _handle(client_reader, client_writer, *, host: str, port: int) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        target_reader, target_writer = await asyncio.open_connection(host, port)
    except OSError as exc:
        print(f"[tls] 뒤쪽 서버에 붙지 못했다 {host}:{port} — {exc}", file=sys.stderr)
        client_writer.close()
        return
    await asyncio.gather(
        _pipe(client_reader, target_writer),
        _pipe(target_reader, client_writer),
    )
    if peer:
        print(f"[tls] 닫힘 {peer[0]}:{peer[1]}", flush=True)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=8443)
    parser.add_argument("--target-host", default="127.0.0.1")
    parser.add_argument("--target-port", type=int, default=8094)
    parser.add_argument("--cert", required=True, type=Path)
    parser.add_argument("--key", required=True, type=Path)
    args = parser.parse_args()

    for path in (args.cert, args.key):
        if not path.is_file():
            print(f"[tls] 인증서 파일이 없다: {path}", file=sys.stderr)
            return 2

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(args.cert), keyfile=str(args.key))

    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, host=args.target_host, port=args.target_port),
        args.listen_host, args.listen_port, ssl=context,
    )
    print(f"[tls] https://{args.listen_host}:{args.listen_port}"
          f"  →  http://{args.target_host}:{args.target_port}", flush=True)
    async with server:
        await server.serve_forever()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
