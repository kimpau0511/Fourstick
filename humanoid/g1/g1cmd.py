"""G1 연속 제어기에 명령 보내기 · 상태 읽기.

    humanoid/g1/run_nav.sh g1cmd.py goto conveyor_front
    humanoid/g1/run_nav.sh g1cmd.py velocity 0.3 0 0
    humanoid/g1/run_nav.sh g1cmd.py stop | save_start | return | shutdown | status
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid


class Client:
    def __init__(self):
        from gz.msgs.stringmsg_pb2 import StringMsg
        from gz.transport import Node
        self._StringMsg = StringMsg
        self.node = Node()
        self.pub = self.node.advertise("/g1/command", StringMsg)
        self.lock = threading.Lock()
        self.status = None
        self.status_wall = None
        self.node.subscribe(StringMsg, "/g1/status", self._on_status)
        deadline = time.time() + 5
        while time.time() < deadline and not self.pub.has_connections():
            time.sleep(0.05)

    def _on_status(self, msg):
        with self.lock:
            self.status = json.loads(msg.data)
            self.status_wall = time.time()

    def send(self, kind: str, **fields) -> dict:
        cmd = {"type": kind, "id": uuid.uuid4().hex[:8], "sent_wall": time.time(), **fields}
        msg = self._StringMsg()
        msg.data = json.dumps(cmd)
        self.pub.publish(msg)
        return cmd

    def latest(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if self.status is not None:
                    return self.status
            time.sleep(0.05)
        return None


def main() -> int:
    args = sys.argv[1:] or ["status"]
    c = Client()
    kind = args[0]
    if kind == "status":
        print(json.dumps(c.latest(), ensure_ascii=False, indent=1))
    elif kind == "goto":
        print(c.send("goto", target=args[1]))
    elif kind == "velocity":
        vx, vy, wz = (float(v) for v in (args[1:4] + ["0", "0", "0"])[:3])
        print(c.send("velocity", vx=vx, vy=vy, wz=wz))
    else:
        print(c.send(kind))
    time.sleep(0.3)
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
