"""Web 服务：Flask + Flask-SocketIO（P4）。

设计要点：

- 用 ``werkzeug.serving.make_server`` 自己起线程，而不是 ``socketio.run()``：
  后者不返回服务器对象，端口一改就没法优雅关掉。自建服务器可以 ``shutdown()``，
  这是「改端口时重启 web 服务」的前提（用 ``socketio.run()`` 只能重启进程）。
- 捕获回调**绝不**在这里做事：总线事件只投进一个容量 1 的队列，由本服务自己的
  工作线程去裁剪 / 落 web 视图 / 推送，回调侧的开销只有一次 ``put_nowait``。
- 裁剪只作用于 web：落盘永远是全尺寸原图，web 视图单独写 ``cache/web/latest.jpeg``。
- 默认只监听 127.0.0.1（隐私默认值），要让手机访问就把 ``server.host`` 改成 ``0.0.0.0``。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from ..config import AppConfig, validate_port

LOG = logging.getLogger(__name__)

VIEW_NAME = "latest.jpeg"           # web 视图文件名（裁剪后的最新一张）
_STOP = object()
# 排查 socket.io 握手问题时打开：SCT_SOCKETIO_DEBUG=1
DEBUG_SOCKETIO = os.environ.get("SCT_SOCKETIO_DEBUG") == "1"


class WebServiceError(RuntimeError):
    """web 服务启动/重启失败。"""


class WebService:
    """把最新截图推给浏览器：REST 拿数据，Socket.IO 推实时事件。"""

    def __init__(self, config: AppConfig, store: Any, bus: Any, engine: Any = None,
                 host: str | None = None, port: int | None = None) -> None:
        self.config = config
        self.store = store
        self.bus = bus
        self.engine = engine
        self._host = host or config.server.host or "127.0.0.1"
        self._port = validate_port(port if port is not None else config.server.port)
        self._lock = threading.RLock()
        self._app: Any = None
        self._socketio: Any = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._unsubscribe: list[Any] = []
        self._queue: queue.Queue = queue.Queue(maxsize=1)
        self._worker: threading.Thread | None = None
        self._pinned_names: set[str] = set()
        self.last_error: str | None = None
        self._view_size: tuple[int, int] | None = None
        self._frames_pushed = 0
        self._view_token = 0

    # ------------------------------------------------------------------ 生命周期

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> int:
        return self._port

    @property
    def host(self) -> str:
        return self._host

    def address(self) -> str:
        """给界面显示用的地址（0.0.0.0 显示成 localhost，否则没法点）。"""
        host = "localhost" if self._host in ("0.0.0.0", "::") else self._host
        return f"http://{host}:{self._port}"

    @property
    def frames_pushed(self) -> int:
        return self._frames_pushed

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.is_running,
            "host": self._host,
            "port": self._port,
            "url": self.address() if self.is_running else None,
            "clients": self._client_count(),
            "framesPushed": self._frames_pushed,
            "viewSize": list(self._view_size) if self._view_size else None,
            "error": self.last_error,
        }

    def _client_count(self) -> int:
        try:
            return len(self._socketio.server.manager.rooms.get("/", {}).get(None, set()))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 统计失败不该影响状态展示
            return 0

    def start(self) -> bool:
        """启动服务，返回是否成功（失败时 ``last_error`` 有原因，不抛异常）。"""
        with self._lock:
            if self.is_running:
                return True
            self.last_error = None
            try:
                app, socketio = self._build_app()
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"web 应用装配失败：{exc}"
                LOG.exception("web 应用装配失败")
                return False

            try:
                from werkzeug.serving import make_server

                if self._port_busy():
                    self.last_error = f"端口 {self._port} 已被占用，请换一个端口"
                    LOG.error("web 服务启动失败：%s", self.last_error)
                    return False
                server = make_server(self._host, self._port, app, threaded=True)
            except OSError as exc:
                self.last_error = f"端口 {self._port} 无法监听：{exc}"
                LOG.error("web 服务启动失败：%s", self.last_error)
                return False
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"web 服务启动失败：{exc}"
                LOG.exception("web 服务启动失败")
                return False

            self._app, self._socketio, self._server = app, socketio, server
            self._port = int(getattr(server, "server_port", self._port) or self._port)
            self._install_hooks()
            self._worker = threading.Thread(target=self._worker_loop, name="web-worker", daemon=True)
            self._worker.start()
            self._thread = threading.Thread(target=self._serve, name="web-server", daemon=True)
            self._thread.start()
            LOG.info("web 服务已启动：%s（监听 %s:%d）", self.address(), self._host, self._port)
            return True

    def _port_busy(self) -> bool:
        """端口是否已被别人监听。

        必须显式探测：Windows 上 ``SO_REUSEADDR``（Python ``HTTPServer`` 默认开）允许
        两个进程绑同一端口，直接 ``make_server`` 不会报错，而是变成两个服务器抢连接。
        """
        import socket as _socket

        host = "127.0.0.1" if self._host in ("0.0.0.0", "::", "") else self._host
        try:
            with _socket.socket() as probe:
                probe.settimeout(0.5)
                return probe.connect_ex((host, self._port)) == 0
        except OSError:
            return False

    def _serve(self) -> None:
        try:
            self._server.serve_forever()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 线程里不能抛出去
            LOG.exception("web 服务线程退出")

    def stop(self, timeout: float = 5.0) -> bool:
        """停止服务；返回是否「本来在跑」。"""
        with self._lock:
            was_running = self.is_running
            server, self._server = self._server, None
            thread, self._thread = self._thread, None
            worker, self._worker = self._worker, None
            self._remove_hooks()
        if worker is not None:
            try:
                self._queue.put_nowait(_STOP)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(_STOP)
                except Exception:  # noqa: BLE001
                    pass
        if server is not None:
            try:
                server.shutdown()
            except Exception:  # noqa: BLE001
                LOG.exception("web 服务关闭异常")
        if thread is not None:
            thread.join(timeout)
        if worker is not None:
            worker.join(timeout)
        if was_running:
            LOG.info("web 服务已停止")
        return was_running

    def restart(self, port: int | None = None, host: str | None = None) -> bool:
        """改端口/地址并重启（界面改端口时调用）。"""
        self.stop()
        if port is not None:
            self._port = validate_port(port)
        if host:
            self._host = host
        return self.start()

    # ------------------------------------------------------------------ 总线

    def _install_hooks(self) -> None:
        from ..core.bus import (TOPIC_CAPTURE_CLOSED, TOPIC_CAPTURE_ERROR,
                                TOPIC_CAPTURE_STARTED, TOPIC_CAPTURE_STOPPED,
                                TOPIC_SHOT_REMOVED, TOPIC_SHOT_SAVED)
        self._pinned_names = set(self.store.pinned)
        self._unsubscribe = [
            self.bus.subscribe(TOPIC_SHOT_SAVED, self._on_shot_saved),
            self.bus.subscribe(TOPIC_SHOT_REMOVED, self._on_shot_removed),
            self.bus.subscribe(TOPIC_CAPTURE_STARTED, self._on_capture_started),
            self.bus.subscribe(TOPIC_CAPTURE_STOPPED, self._on_capture_stopped),
            self.bus.subscribe(TOPIC_CAPTURE_CLOSED, self._on_capture_closed),
            self.bus.subscribe(TOPIC_CAPTURE_ERROR, self._on_capture_error),
        ]

    def _remove_hooks(self) -> None:
        for unsubscribe in self._unsubscribe:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self._unsubscribe = []

    def _enqueue(self, shot: Any) -> None:
        """总线回调只做这一件事：把最新一张塞进队列（满了就丢掉旧的）。"""
        try:
            self._queue.put_nowait(shot)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()  # 丢掉尚未处理的那张，只保留最新
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(shot)
        except queue.Full:  # pragma: no cover - 竞态兜底
            pass

    def _on_shot_saved(self, shot: Any = None, **_: Any) -> None:
        if shot is not None:
            self._enqueue(shot)

    def _on_shot_removed(self, names: Any = None, **_: Any) -> None:
        """缓存轮转删了文件：立刻告诉浏览器把对应缩略图摘掉。"""
        removed = [str(name) for name in (names or [])]
        if not removed:
            return
        LOG.debug("缓存轮转删除：%s", removed)
        self._broadcast("removed", {"names": removed, "count": self.store.count()})

    def _on_capture_started(self, target: Any = None, interval: int = 0, **_: Any) -> None:
        self._push_status("started", interval=interval)

    def _on_capture_stopped(self, saved: int = 0, **_: Any) -> None:
        self._push_status("stopped", saved=saved)

    def _on_capture_closed(self, target: str = "", **_: Any) -> None:
        self._push_status("closed", target=target)

    def _on_capture_error(self, error: str = "", **_: Any) -> None:
        self._push_status("error", error=error)

    # ------------------------------------------------------------------ 工作线程

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            try:
                self._handle_shot(item)
            except Exception:  # noqa: BLE001 - 工作线程不能死
                LOG.exception("处理新截图失败：%s", getattr(item, "name", item))

    def _handle_shot(self, shot: Any) -> None:
        view_size = self.write_web_view(shot)
        self._frames_pushed += 1
        payload = self._frame_payload(shot, view_size)
        self._broadcast("frame", payload)

    def _frame_payload(self, shot: Any, view_size: tuple[int, int] | None) -> dict[str, Any]:
        crop = self.config.capture.crop
        return {
            "name": shot.name,
            "url": f"/cache/{shot.name}",
            "webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
            "downloadUrl": f"/api/download/{shot.name}",
            "createdAt": shot.created_at.isoformat(timespec="seconds"),
            "sizeKb": round(shot.size_bytes / 1024, 1),
            "width": shot.width,
            "height": shot.height,
            "isPinned": shot.name in self._pinned_names,
            "cropped": bool(crop.enabled and view_size),
            "viewWidth": view_size[0] if view_size else shot.width,
            "viewHeight": view_size[1] if view_size else shot.height,
        }

    def write_web_view(self, shot: Any) -> tuple[int, int] | None:
        """按裁剪配置生成 web 视图，返回 (宽, 高)；失败返回 None（调用方退回原图）。"""
        from .. import paths

        directory = paths.ensure_dir(paths.web_cache_dir())
        target = directory / VIEW_NAME
        crop = self.config.capture.crop
        source = Path(shot.path)
        try:
            if not crop.enabled or crop.width <= 0 or crop.height <= 0:
                # 裁剪关掉时也要有一份视图：直接复制原图，客户端地址不变
                _atomic_copy(source, target)
                size = (shot.width, shot.height)
            else:
                from PIL import Image

                with Image.open(source) as image:
                    left = max(0, min(int(crop.x), image.width - 1))
                    top = max(0, min(int(crop.y), image.height - 1))
                    right = min(image.width, left + int(crop.width))
                    bottom = min(image.height, top + int(crop.height))
                    if right - left < 1 or bottom - top < 1:
                        raise ValueError(f"裁剪框超出图像范围：{crop.as_tuple()} vs {image.size}")
                    size = (right - left, bottom - top)
                    box = image.crop((left, top, right, bottom))
                    if box.mode != "RGB":
                        box = box.convert("RGB")
                    tmp = target.with_suffix(".tmp")
                    box.save(tmp, format="JPEG", quality=int(self.config.cache.quality))
                os.replace(tmp, target)
        except Exception:  # noqa: BLE001 - 视图失败不该影响推送本身
            LOG.exception("生成 web 视图失败，退回原图：%s", source)
            return None
        self._view_size = size
        self._view_token = int(time.time() * 1000)
        return size

    def refresh_view(self) -> tuple[int, int] | None:
        """立刻按当前裁剪配置重做视图并通知客户端（改范围框时调用）。"""
        latest = self.store.latest()
        if latest is None:
            return None
        size = self.write_web_view(latest)
        self._broadcast("updated", {"webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
                                    "viewWidth": size[0] if size else latest.width,
                                    "viewHeight": size[1] if size else latest.height,
                                    "cropped": bool(self.config.capture.crop.enabled and size)})
        return size

    def cropped_bytes(self, shot: Any) -> bytes | None:
        """按裁剪配置现裁一份 JPEG 字节；未启用裁剪或裁剪越界时返回 None。"""
        crop = self.config.capture.crop
        if not crop.enabled or crop.width <= 0 or crop.height <= 0:
            return None
        try:
            import io

            from PIL import Image

            with Image.open(Path(shot.path)) as image:
                left = max(0, min(int(crop.x), image.width - 1))
                top = max(0, min(int(crop.y), image.height - 1))
                right = min(image.width, left + int(crop.width))
                bottom = min(image.height, top + int(crop.height))
                if right - left < 1 or bottom - top < 1:
                    return None
                box = image.crop((left, top, right, bottom))
                if box.mode != "RGB":
                    box = box.convert("RGB")
                buffer = io.BytesIO()
                box.save(buffer, format="JPEG", quality=int(self.config.cache.quality))
            return buffer.getvalue()
        except Exception:  # noqa: BLE001
            LOG.exception("按需裁剪失败：%s", shot.path)
            return None

    def _push_status(self, event: str, **extra: Any) -> None:
        payload = {"event": event, **extra, **self.status_payload()}
        self._broadcast("status", payload)

    def _broadcast(self, event: str, payload: dict[str, Any]) -> None:
        socketio = self._socketio
        if socketio is None:
            return
        try:
            socketio.emit(event, payload)
        except Exception:  # noqa: BLE001
            LOG.exception("推送 %s 事件失败", event)

    # ------------------------------------------------------------------ 数据

    def shot_json(self, shot: Any) -> dict[str, Any]:
        return {
            "name": shot.name,
            "url": f"/cache/{shot.name}",
            "viewUrl": f"/web/view/{shot.name}",
            "downloadUrl": f"/api/download/{shot.name}",
            "sizeBytes": shot.size_bytes,
            "sizeKb": round(shot.size_bytes / 1024, 1),
            "width": shot.width,
            "height": shot.height,
            "mtime": shot.mtime,
            "createdAt": shot.created_at.isoformat(timespec="seconds"),
            "isPinned": shot.name in self._pinned_names,
        }

    def list_json(self) -> dict[str, Any]:
        shots = self.store.list()
        return {
            "success": True,
            "images": [self.shot_json(shot) for shot in shots],
            "count": len(shots),
            "maxSize": int(self.store.max_screenshots),
            "pinned": sorted(self._pinned_names),
            "crop": self.crop_json(),
        }

    def crop_json(self) -> dict[str, Any]:
        crop = self.config.capture.crop
        return {"enabled": bool(crop.enabled), "x": crop.x, "y": crop.y,
                "width": crop.width, "height": crop.height}

    def status_payload(self) -> dict[str, Any]:
        engine = self.engine
        capture: dict[str, Any] = {"running": bool(engine.is_running) if engine else False}
        if engine is not None:
            capture.update(engine.snapshot())
        latest = self.store.latest()
        return {
            "capture": capture,
            "target": self.config.capture.target.describe(),
            "interval": self.config.capture.interval_seconds,
            "cache": {"count": self.store.count(), "max": int(self.store.max_screenshots),
                      "pinned": len(self._pinned_names)},
            "server": self.snapshot(),
            "crop": self.crop_json(),
            "latest": self.shot_json(latest) if latest else None,
            "webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
        }

    # ------------------------------------------------------------------ Flask

    def _build_app(self) -> tuple[Any, Any]:
        from flask import Flask, jsonify, request, send_file, send_from_directory
        from flask_socketio import SocketIO, emit

        from .. import paths

        app = Flask(__name__,
                    static_folder=str(paths.web_static_dir()),
                    template_folder=str(paths.web_templates_dir()))
        app.config["JSON_AS_ASCII"] = False
        app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
        socketio = SocketIO(app, async_mode="threading", cors_allowed_origins=None,
                            logger=DEBUG_SOCKETIO, engineio_logger=DEBUG_SOCKETIO)

        def _find(name: str) -> Any:
            for shot in self.store.list():
                if shot.name == name:
                    return shot
            return None

        @app.get("/")
        def index():  # noqa: ANN202
            from flask import render_template

            return render_template("index.html", version=self._version())

        @app.get("/favicon.ico")
        def favicon():  # noqa: ANN202
            from flask import Response

            return Response(status=204)

        @app.get("/api/status")
        def api_status():  # noqa: ANN202
            return jsonify({"success": True, **self.status_payload()})

        @app.get("/api/list")
        def api_list():  # noqa: ANN202
            return jsonify(self.list_json())

        @app.get("/api/latest")
        def api_latest():  # noqa: ANN202
            latest = self.store.latest()
            # 返回完整状态（含 capture/target/interval/cache），前端一次请求就能刷新整页，
            # 否则轮询刷新会把状态区冲成空值
            return jsonify({
                **self.status_payload(),
                "success": True,
                "image": self.shot_json(latest) if latest else None,
                "webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
                "viewSize": list(self._view_size) if self._view_size else None,
            })

        @app.get("/cache/<path:name>")
        def cache_file(name: str):  # noqa: ANN202
            shot = _find(name)
            if shot is None:
                return jsonify({"success": False, "error": "找不到这张截图"}), 404
            return send_file(shot.path, mimetype="image/jpeg", max_age=0)

        @app.get("/web/<path:name>")
        def web_view(name: str):  # noqa: ANN202
            if name != VIEW_NAME:
                return jsonify({"success": False, "error": "未知的 web 视图"}), 404
            directory = paths.web_cache_dir()
            if not (directory / VIEW_NAME).is_file():
                latest = self.store.latest()
                if latest is None:
                    return jsonify({"success": False, "error": "还没有截图"}), 404
                self.write_web_view(latest)
            response = send_from_directory(directory, VIEW_NAME, mimetype="image/jpeg", max_age=0)
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response

        @app.get("/web/view/<path:name>")
        def web_view_named(name: str):  # noqa: ANN202
            """历史图的 web 视图：按需裁剪（点开某张历史图时用）。"""
            from flask import Response

            shot = _find(name)
            if shot is None:
                return jsonify({"success": False, "error": "找不到这张截图"}), 404
            data = self.cropped_bytes(shot)
            if data is None:
                return send_file(shot.path, mimetype="image/jpeg", max_age=0)
            response = Response(data, mimetype="image/jpeg")
            response.headers["Cache-Control"] = "no-store, max-age=0"
            return response

        @app.get("/api/download/latest")
        def download_latest():  # noqa: ANN202
            latest = self.store.latest()
            if latest is None:
                return jsonify({"success": False, "error": "还没有截图可下载"}), 404
            return send_file(latest.path, mimetype="image/jpeg", as_attachment=True,
                             download_name=latest.name, max_age=0)

        @app.get("/api/download/<path:name>")
        def download_named(name: str):  # noqa: ANN202
            shot = _find(name)
            if shot is None:
                return jsonify({"success": False, "error": "找不到这张截图"}), 404
            return send_file(shot.path, mimetype="image/jpeg", as_attachment=True,
                             download_name=shot.name, max_age=0)

        @app.post("/api/pin/<path:name>")
        def api_pin(name: str):  # noqa: ANN202
            return self._set_pin(name, True)

        @app.post("/api/unpin/<path:name>")
        def api_unpin(name: str):  # noqa: ANN202
            return self._set_pin(name, False)

        @app.get("/api/config")
        def api_config():  # noqa: ANN202
            return jsonify({"success": True, "capture": {
                "intervalSeconds": self.config.capture.interval_seconds,
                "target": self.config.capture.target.describe(),
                "previewEnabled": bool(self.config.capture.preview_enabled),
            }, "server": {"port": self._port, "host": self._host}, "cache": {
                "maxScreenshots": int(self.config.cache.max_screenshots),
                "format": self.config.cache.format, "quality": int(self.config.cache.quality),
            }})

        # ---------------- Socket.IO ----------------

        @socketio.on("connect")
        def on_connect():  # noqa: ANN202
            LOG.info("web 客户端接入：%s", request.sid)
            emit("status", {"event": "hello", **self.status_payload()})
            latest = self.store.latest()
            emit("init", {
                "images": [self.shot_json(shot) for shot in self.store.list()],
                "latest": self.shot_json(latest) if latest else None,
                "webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
                "maxSize": int(self.store.max_screenshots),
                "crop": self.crop_json(),
                "status": {"event": "hello", **self.status_payload()},
            })

        @socketio.on("disconnect")
        def on_disconnect():  # noqa: ANN202
            LOG.info("web 客户端断开：%s", request.sid)

        @socketio.on("pin")
        def on_pin(data: Any = None):  # noqa: ANN202
            name = (data or {}).get("name") if isinstance(data, dict) else None
            if not name:
                return {"success": False, "error": "缺少 name"}
            return self.pin_state(str(name), True)      # 返回值就是 ack

        @socketio.on("unpin")
        def on_unpin(data: Any = None):  # noqa: ANN202
            name = (data or {}).get("name") if isinstance(data, dict) else None
            if not name:
                return {"success": False, "error": "缺少 name"}
            return self.pin_state(str(name), False)

        @socketio.on("refresh")
        def on_refresh():  # noqa: ANN202
            emit("init", {
                "images": [self.shot_json(shot) for shot in self.store.list()],
                "latest": self.shot_json(self.store.latest()) if self.store.latest() else None,
                "webUrl": f"/web/{VIEW_NAME}?v={self._view_token}",
                "maxSize": int(self.store.max_screenshots),
                "crop": self.crop_json(),
            })

        return app, socketio

    def _version(self) -> str:
        from .. import __version__

        return __version__

    def pin_state(self, name: str, pinned: bool) -> dict[str, Any]:
        """钉住/取消钉住某张；返回结果字典（REST 与 socket ack 共用）。"""
        if any(shot.name == name for shot in self.store.list()):
            if pinned:
                self.store.pin(name)
                self._pinned_names.add(name)
            else:
                self.store.unpin(name)
                self._pinned_names.discard(name)
            self._broadcast("pinned", {"name": name, "isPinned": pinned,
                                       "pinned": sorted(self._pinned_names)})
            return {"success": True, "name": name, "isPinned": pinned,
                    "pinned": sorted(self._pinned_names)}
        return {"success": False, "error": "找不到这张截图（可能已被缓存轮转删除）", "name": name}

    def _set_pin(self, name: str, pinned: bool) -> Any:
        from flask import jsonify

        result = self.pin_state(name, pinned)
        return jsonify(result) if result["success"] else (jsonify(result), 404)


def _atomic_copy(source: Path, target: Path) -> None:
    """复制原图到 web 视图（先写临时文件再替换，避免读到半截文件）。"""
    tmp = target.with_suffix(".tmp")
    with open(source, "rb") as src, open(tmp, "wb") as dst:
        while True:
            chunk = src.read(256 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp, target)
