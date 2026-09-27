"""P4 端到端验证：真实 web 服务 + 真实 HTTP 请求 + 真实 Socket.IO 客户端 + 真实裁剪。

     .venv\\Scripts\\python.exe tests\\test_web.py

会往 cache/screenshots 写几张测试图（240x320 纯色），并在 cache/web 生成裁剪视图；
不会启动捕获（用 store.save + 总线事件模拟新截图），所以跑得快且不依赖屏幕内容。
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from app.config import AppConfig, CropRect  # noqa: E402
from app.core.bus import TOPIC_SHOT_REMOVED, TOPIC_SHOT_SAVED, EventBus  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402
from app.web.server import VIEW_NAME, WebService  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    mark = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"  [{mark}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http(port: int, path: str, timeout: float = 8.0):
    """返回 (状态码, 头, 字节)。"""
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def http_json(port: int, path: str):
    status, _headers, body = http(port, path)
    try:
        return status, json.loads(body.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return status, None


def jpeg_size(data: bytes) -> tuple[int, int]:
    from PIL import Image
    import io

    with Image.open(io.BytesIO(data)) as image:
        return image.size


def main() -> int:
    cfg = AppConfig()
    port = free_port()
    cfg.server.port = port
    store = ScreenshotStore(directory=ROOT / "cache" / "screenshots",
                            max_screenshots=cfg.cache.max_screenshots,
                            quality=cfg.cache.quality)
    bus = EventBus()
    service = WebService(cfg, store, bus, port=port)

    print(f"启动 web 服务：127.0.0.1:{port}")
    started = service.start()
    check("服务启动成功", started, service.last_error or f"http://127.0.0.1:{port}")
    if not started:
        print(f"\n== 启动失败，无法继续：{service.last_error} ==")
        return 1
    check("is_running 与快照一致", service.is_running and service.snapshot()["running"])

    try:
        # ---------------- 页面与静态资源 ----------------
        print("\n[1] 页面与静态资源")
        status, _h, body = http(port, "/")
        html = body.decode("utf-8", errors="replace")
        check("GET / 返回 200", status == 200, f"状态 {status}")
        check("页面是中文界面", "屏幕捕获" in html and "历史截图" in html)
        check("页面引入了本地 socket.io 客户端", 'src="/static/js/socket.io.min.js"' in html)
        check("页面前端脚本存在", 'src="/static/js/app.js"' in html)
        for asset, kind in (("/static/js/socket.io.min.js", "js"), ("/static/js/app.js", "js"),
                            ("/static/css/app.css", "css")):
            status, headers, body = http(port, asset)
            check(f"静态资源 {asset} 可访问", status == 200 and len(body) > 500,
                  f"状态 {status}，{len(body)} 字节")
        check("socket.io 客户端版本为 4.x", "Socket.IO v4" in
              http(port, "/static/js/socket.io.min.js")[2].decode("utf-8", errors="replace")[:200])

        # ---------------- REST 接口 ----------------
        print("\n[2] REST 接口")
        status, data = http_json(port, "/api/status")
        check("GET /api/status 正常", status == 200 and data and data.get("success"))
        check("状态里带着端口与地址",
              data["server"]["port"] == port and str(port) in (data["server"]["url"] or ""),
              f"url={data['server']['url']}")
        check("状态里 capture.running 为 False（没起捕获）", data["capture"]["running"] is False)

        status, data = http_json(port, "/api/list")
        check("GET /api/list 正常", status == 200 and data and data.get("success"))
        check("缓存上限透出为 20", data["maxSize"] == 20, f"maxSize={data['maxSize']}")
        before_count = len(data["images"])

        status, data = http_json(port, "/api/config")
        check("GET /api/config 正常", status == 200 and data.get("success"))
        check("配置里含捕获间隔与端口",
              "intervalSeconds" in data["capture"] and data["server"]["port"] == port)

        # ---------------- 造两张真图，验证取图与下载 ----------------
        print("\n[3] 截图取用与下载")
        made = []
        for shade, mark in ((70, "第一张"), (150, "第二张")):
            image = np.full((240, 320, 3), shade, dtype=np.uint8)
            image[:, :40] = (mark == "第二张") and 255 or 0  # 左右两半可区分
            shot = store.save(image, width=320, height=240)
            made.append(shot)
        check("测试图写入磁盘", all(shot.path.is_file() for shot in made),
              f"{made[-1].name} {made[-1].size_bytes} 字节")

        status, data = http_json(port, "/api/latest")
        check("GET /api/latest 返回最新一张", status == 200 and data["image"]["name"] == made[-1].name,
              data["image"]["name"])
        latest_name = data["image"]["name"]

        status, headers, body = http(port, f"/cache/{latest_name}")
        check(f"GET /cache/<name> 返回图片", status == 200 and body[:2] == b"\xff\xd8",
              f"状态 {status}，{len(body)} 字节，content-type={headers.get('Content-Type')}")
        check("图片字节数与磁盘一致", len(body) == made[-1].size_bytes,
              f"{len(body)} vs {made[-1].size_bytes}")

        status, headers, body = http(port, "/api/download/latest")
        check("GET /api/download/latest 是附件下载",
              status == 200 and "attachment" in (headers.get("Content-Disposition") or ""),
              headers.get("Content-Disposition", ""))
        check("下载内容与磁盘一致", len(body) == made[-1].size_bytes)

        status, headers, body = http(port, "/api/download/" + made[0].name)
        check("GET /api/download/<name> 可下载指定历史图",
              status == 200 and len(body) == made[0].size_bytes)

        status, _h, _b = http(port, "/cache/" + urllib.parse.quote("不存在.jpeg"))
        check("取不存在的图返回 404", status == 404, f"状态 {status}")
        status, _h, _b = http(port, "/api/download/" + urllib.parse.quote("不存在.jpeg"))
        check("下载不存在的图返回 404", status == 404, f"状态 {status}")
        status, _h, body = http(port, "/cache/..%2F..%2Fconfig%2Fsettings.json")
        check("路径穿越拿不到配置文件", status == 404 and b"httpPort" not in body, f"状态 {status}")

        # ---------------- 裁剪只作用于 web ----------------
        print("\n[4] 范围框：只裁 web 输出，落盘原图")
        cfg.capture.crop = CropRect(enabled=True, x=10, y=20, width=100, height=60)
        size = service.refresh_view()
        check("refresh_view 生成裁剪视图", size == (100, 60), f"视图尺寸 {size}")
        status, _h, body = http(port, f"/web/{VIEW_NAME}")
        check("GET /web/latest.jpeg 是裁剪后的尺寸", status == 200 and jpeg_size(body) == (100, 60),
              f"状态 {status}，尺寸 {jpeg_size(body) if status == 200 else '-'}")
        status, _h, body = http(port, f"/web/view/{latest_name}")
        check("历史图 web 视图同样被裁剪", status == 200 and jpeg_size(body) == (100, 60),
              f"尺寸 {jpeg_size(body) if status == 200 else '-'}")
        status, _h, body = http(port, f"/cache/{latest_name}")
        check("落盘原图没有被裁（仍是 320x240）", jpeg_size(body) == (320, 240), f"尺寸 {jpeg_size(body)}")
        status, _h, body = http(port, f"/api/download/{latest_name}")
        check("下载的也是全尺寸原图", jpeg_size(body) == (320, 240))

        cfg.capture.crop = CropRect(enabled=True, x=300, y=0, width=400, height=400)  # 越界
        size = service.refresh_view()
        check("裁剪框越界时自动收缩不报错", size == (20, 240), f"视图尺寸 {size}")
        cfg.capture.crop = CropRect(enabled=False)
        service.refresh_view()
        status, _h, body = http(port, f"/web/view/{latest_name}")
        check("关掉范围框后 web 视图恢复原尺寸", jpeg_size(body) == (320, 240), f"尺寸 {jpeg_size(body)}")

        # ---------------- Socket.IO 推送 ----------------
        print("\n[5] Socket.IO 实时推送")
        try:
            import socketio
        except ImportError:
            check("python-socketio 客户端可用", False, "未安装，跳过推送测试")
            socketio = None

        if socketio is not None:
            client = socketio.Client(reconnection=False)
            received: dict[str, list] = {"init": [], "frame": [], "pinned": [], "status": []}
            lock = __import__("threading").Lock()

            for event in received:
                def make_handler(topic: str):
                    def handler(payload=None):  # noqa: ANN001, ANN202
                        with lock:
                            received[topic].append(payload)
                    return handler
                client.on(event, make_handler(event))

            client.connect(f"http://127.0.0.1:{port}", transports=["websocket", "polling"],
                           wait_timeout=10)
            check("Socket.IO 客户端连上了服务", client.connected)
            transport = client.transport()
            check("走的是真 WebSocket（不是降级成轮询）", transport == "websocket",
                  f"实际传输方式：{transport}")

            deadline = time.time() + 6
            while time.time() < deadline and not received["init"]:
                time.sleep(0.1)
            check("连上后立刻收到 init（含历史列表）", bool(received["init"]),
                  f"images={len((received['init'] or [{}])[0].get('images', []))}")

            new_image = np.full((240, 320, 3), 200, dtype=np.uint8)
            new_shot = store.save(new_image, width=320, height=240)
            bus.publish(TOPIC_SHOT_SAVED, shot=new_shot)
            deadline = time.time() + 6
            while time.time() < deadline and not received["frame"]:
                time.sleep(0.1)
            frames = received["frame"]
            check("新截图通过 WebSocket 推到了浏览器", bool(frames),
                  frames[0].get("name") if frames else "6 秒内没收到 frame 事件")
            if frames:
                payload = frames[0]
                check("推送内容带图片地址与尺寸",
                      payload["name"] == new_shot.name and payload["url"] == f"/cache/{new_shot.name}"
                      and payload["width"] == 320,
                      f"{payload['url']} {payload['width']}x{payload['height']}")
                check("推送内容带 web 视图地址（带防缓存 token）",
                      payload["webUrl"].startswith(f"/web/{VIEW_NAME}?v="), payload["webUrl"])
                check("推送内容带下载地址", payload["downloadUrl"] == f"/api/download/{new_shot.name}")

            client.emit("pin", {"name": new_shot.name})
            deadline = time.time() + 5
            while time.time() < deadline and not received["pinned"]:
                time.sleep(0.1)
            check("浏览器可以钉住某张（socket 事件）", new_shot.name in store.pinned,
                  f"pinned={sorted(store.pinned)}")
            check("钉住状态广播回所有客户端", bool(received["pinned"]),
                  str(received["pinned"][-1]) if received["pinned"] else "")

            client.emit("unpin", {"name": new_shot.name})
            deadline = time.time() + 5
            while time.time() < deadline and new_shot.name in store.pinned:
                time.sleep(0.1)
            check("浏览器可以取消钉住", new_shot.name not in store.pinned)

            status, data = http_json(port, f"/api/list")
            check("列表里带 isPinned 字段", "isPinned" in data["images"][0])

            # 钉住用 ack 回报真实结果（前端等确认再提示，避免「假成功」）
            ack = client.call("pin", {"name": new_shot.name}, timeout=6)
            check("钉住有 ack 回执", isinstance(ack, dict) and ack.get("success") is True, str(ack))
            missing = client.call("pin", {"name": "screenshot_不存在.jpeg"}, timeout=6)
            check("钉住不存在的图 ack 里带失败原因",
                  isinstance(missing, dict) and missing.get("success") is False
                  and "找不到" in str(missing.get("error")), str(missing))
            client.emit("unpin", {"name": new_shot.name})

            # 缓存轮转删除要推给浏览器（否则缩略图变破图）
            received.setdefault("removed", [])
            client.on("removed", lambda payload=None: received["removed"].append(payload))
            bus.publish(TOPIC_SHOT_REMOVED, names=[made[0].name])
            deadline = time.time() + 5
            while time.time() < deadline and not received["removed"]:
                time.sleep(0.1)
            check("缓存轮转删除推送到浏览器", bool(received["removed"]),
                  str(received["removed"][-1]) if received["removed"] else "没收到 removed 事件")
            if received["removed"]:
                check("删除事件里带被删文件名",
                      made[0].name in received["removed"][-1].get("names", []))
            client.disconnect()

        # ---------------- 前端页面结构（P5） ----------------
        print("\n[5b] 前端页面结构")
        _s, _h, css = http(port, "/static/css/app.css")
        css_text = css.decode("utf-8", errors="replace")
        check("CSS 有平板断点（700px，历史两列）", "@media (min-width: 700px)" in css_text
              and "repeat(2" in css_text)
        check("CSS 有电脑断点（1080px，左右分栏）", "@media (min-width: 1080px)" in css_text
              and "grid-template-columns: minmax(0, 1fr) 340px" in css_text)
        check("手机端历史条是横向滚动", "overflow-x: auto" in css_text)
        _s, _h, js = http(port, "/static/js/app.js")
        js_text = js.decode("utf-8", errors="replace")
        for event in ("'frame'", "'init'", "'removed'", "'pinned'", "'status'", "'updated'"):
            check(f"前端处理 {event} 事件", f"socket.on({event}" in js_text)
        check("前端有下载按钮逻辑（/api/download）", "/api/download/" in js_text)
        check("前端有「回到最新」的机制", "btnBackLive" in js_text and "followLive" in js_text)

        # ---------------- 端口热改 ----------------
        print("\n[6] 端口热改与占用")
        new_port = free_port()
        ok = service.restart(port=new_port)
        check("改端口后重启成功", ok and service.is_running, f"新端口 {new_port}")
        status, data = http_json(new_port, "/api/status")
        check("新端口能访问", status == 200 and data["success"])
        check("报告的端口是新的", data["server"]["port"] == new_port)
        try:
            http(port, "/api/status", timeout=2)
            old_alive = True
        except Exception:  # noqa: BLE001
            old_alive = False
        check("旧端口不再服务", not old_alive)

        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", 0))
            blocker.listen(1)
            busy_port = int(blocker.getsockname()[1])
            ok = service.restart(port=busy_port)
            check("端口被占用时启动失败且不崩溃", ok is False and not service.is_running,
                  f"last_error={service.last_error}")
            check("失败原因里写明端口", str(busy_port) in (service.last_error or ""),
                  service.last_error or "")
            check("失败后进程里没有半死不活的服务器线程",
                  not [t for t in __import__("threading").enumerate() if t.name == "web-server"])
            blocker.close()

        # ---------------- 停止 ----------------
        print("\n[7] 停止")
        service.start()
        check("重新启动成功", service.is_running, f"端口 {service.port}")
        was = service.stop()
        check("stop 返回「本来在跑」", was is True)
        check("停止后 is_running 为 False", not service.is_running)
        try:
            http(service.port, "/api/status", timeout=2)
            still_alive = True
        except Exception:  # noqa: BLE001
            still_alive = False
        check("停止后端口不再监听", not still_alive)
        check("总线钩子已摘掉", not service._unsubscribe)
    finally:
        service.stop()

    print(f"\n== 断言 {PASS}/{PASS + FAIL} 通过 ==")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
