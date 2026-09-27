"""排查 socket.io 握手问题的探针（不是验收脚本）。

    SCT_SOCKETIO_DEBUG=1 .venv/Scripts/python.exe tests/_probe_ws.py
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402
from app.core.bus import EventBus  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402
from app.web.server import WebService  # noqa: E402

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")


def main() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    cfg = AppConfig()
    cfg.server.port = port
    store = ScreenshotStore(directory=ROOT / "cache" / "screenshots")
    service = WebService(cfg, store, EventBus(), port=port)
    print(f"启动：{service.start()} {service.last_error or ''}")

    import urllib.request

    # 先看握手端点原始返回，能直接看到服务端异常
    for path in (f"/socket.io/?EIO=4&transport=polling",):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=8) as response:
                print(f"[{path}] {response.status} {response.read()[:200]!r}")
        except Exception as exc:  # noqa: BLE001
            body = getattr(exc, "read", lambda: b"")()
            print(f"[{path}] 异常 {exc} body={body[:400]!r}")

    import socketio

    client = socketio.Client(reconnection=False, logger=True, engineio_logger=True)
    try:
        client.connect(f"http://127.0.0.1:{port}", transports=["polling"], wait_timeout=8)
        print("连接成功")
        time.sleep(1.5)
        client.disconnect()
    except Exception as exc:  # noqa: BLE001
        print(f"客户端连接失败：{type(exc).__name__}: {exc}")
    time.sleep(0.5)
    service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
