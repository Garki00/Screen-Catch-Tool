"""捕获内核：Windows Graphics Capture → 按设定间隔落盘。

线程模型（重要）：

- ``windows-capture`` 在**自己的线程**里回调帧，主线程只负责 start/stop；
- 帧缓冲是零拷贝视图，**编码必须发生在回调内部**，回调外持有缓冲区会拿到被复用的
  内存（半张图 / 撕裂）；
- 落盘按「间隔门」限流：回调可能以 10+ fps 到达（画面有变化就有帧），但只有距上次
  保存超过 ``interval_seconds`` 的帧才会被编码写盘；
- 画面完全静止时 WGC 不会产生新帧，因此间隔是「最短间隔」而非定时器——这是捕获库的
  固有行为（见 docs/项目流程.md 的 P1 说明）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from windows_capture import CaptureControl, WindowsCapture

from ..config import AppConfig, CaptureTarget, clamp_interval
from .bus import (
    TOPIC_CAPTURE_CLOSED,
    TOPIC_CAPTURE_ERROR,
    TOPIC_CAPTURE_STARTED,
    TOPIC_CAPTURE_STOPPED,
    TOPIC_FRAME_PREVIEW,
    TOPIC_SHOT_REMOVED,
    TOPIC_SHOT_SAVED,
    EventBus,
)
from .storage import ScreenshotStore, Shot

LOG = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """捕获无法启动/无法继续。"""


@dataclass
class CaptureStats:
    running: bool = False
    frames_seen: int = 0
    shots_saved: int = 0
    bytes_written: int = 0
    started_at: float | None = None
    last_error: str | None = None
    last_shot: dict[str, Any] | None = None
    target: str = ""
    interval_seconds: int = 0

    def as_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["uptimeSeconds"] = round(time.monotonic() - self.started_at, 1) if self.started_at else 0
        return data


@dataclass
class _Runtime:
    """当前捕获会话的运行时对象。"""

    capture: WindowsCapture
    control: CaptureControl | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)


class CaptureEngine:
    """把「捕获目标 + 间隔 + 缓存」串成一条可启停的流水线。"""

    def __init__(self, config: AppConfig, store: ScreenshotStore, bus: EventBus | None = None) -> None:
        self.config = config
        self.store = store
        self.bus = bus or EventBus()
        self.stats = CaptureStats()
        self._runtime: _Runtime | None = None
        self._lock = threading.RLock()
        self._save_lock = threading.RLock()
        self._last_save = 0.0
        self._last_preview = 0.0
        self._last_frame_at = 0.0
        self._fallback_rect: tuple[int, int, int, int] | None = None
        self._fallback_hwnd: int | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat: threading.Thread | None = None
        self._interval = clamp_interval(config.capture.interval_seconds)

    # ---------- 生命周期 ----------

    @property
    def is_running(self) -> bool:
        return self.stats.running

    @property
    def interval_seconds(self) -> int:
        return self._interval

    def set_interval(self, seconds: int) -> int:
        """运行时热更新捕获间隔（2-10 秒），返回生效值。"""
        with self._lock:
            self._interval = clamp_interval(seconds)
            self.config.capture.interval_seconds = self._interval
            self.stats.interval_seconds = self._interval
        LOG.info("捕获间隔已设为 %d 秒", self._interval)
        return self._interval

    def start(self) -> bool:
        """按当前配置启动捕获；已在运行则直接返回 True。"""
        with self._lock:
            if self.stats.running:
                LOG.debug("捕获已在运行，忽略重复启动")
                return True

            target = self.config.capture.target
            valid, reason = target.is_valid()
            if not valid:
                raise CaptureError(f"捕获目标非法：{reason}")

            self._interval = clamp_interval(self.config.capture.interval_seconds)
            kwargs: dict[str, Any] = {
                "cursor_capture": bool(self.config.capture.cursor_capture),
                "draw_border": False,
                "dirty_region": False,
                "minimum_update_interval": max(0, int(self.config.capture.minimum_update_interval_ms)),
            }
            if target.type == "window":
                if target.window_hwnd is not None:
                    kwargs["window_hwnd"] = int(target.window_hwnd)
                else:
                    kwargs["window_name"] = str(target.window_name)
            else:
                kwargs["monitor_index"] = int(target.monitor_index or 1)

            try:
                capture = WindowsCapture(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 原生库抛的是通用异常
                message = f"创建捕获会话失败（{target.describe()}）：{exc}"
                self.stats.last_error = message
                self.bus.publish(TOPIC_CAPTURE_ERROR, error=message)
                raise CaptureError(message) from exc

            # 2.0.1 的 @event 装饰器按函数名分发，直接赋值更直观且行为一致
            capture.frame_handler = self._on_frame
            capture.closed_handler = self._on_closed

            self._last_save = 0.0  # 第一帧立即落盘，不必等满一个间隔
            self._last_preview = 0.0
            self._last_frame_at = time.monotonic()
            self._fallback_rect = self._resolve_fallback_rect(target)
            self._fallback_hwnd = self._resolve_fallback_hwnd(target)
            try:
                control = capture.start_free_threaded()
            except Exception as exc:  # noqa: BLE001
                message = f"启动捕获线程失败：{exc}"
                self.stats.last_error = message
                self.bus.publish(TOPIC_CAPTURE_ERROR, error=message)
                raise CaptureError(message) from exc

            self._runtime = _Runtime(capture=capture, control=control)
            self.stats.running = True
            self.stats.started_at = time.monotonic()
            self.stats.target = target.describe()
            self.stats.interval_seconds = self._interval
            self.stats.last_error = None

        LOG.info("开始捕获：%s，间隔 %d 秒，缓存上限 %d 张",
                 target.describe(), self._interval, self.store.max_screenshots)
        self._start_heartbeat()
        self.bus.publish(TOPIC_CAPTURE_STARTED, target=target, interval=self._interval)
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """停止捕获，返回是否在超时前停干净。"""
        with self._lock:
            runtime = self._runtime
            self._runtime = None
            self.stats.running = False
            target_desc = self.stats.target
        if runtime is None:
            self._stop_heartbeat()
            return True
        self._stop_heartbeat()

        control = runtime.control
        if control is not None:
            try:
                control.stop()
            except Exception:  # noqa: BLE001
                LOG.exception("停止捕获线程时出错")
        stopped = self._wait_control(control, timeout)
        if not stopped:
            LOG.warning("等待捕获线程退出超时（%.1fs）", timeout)

        LOG.info("已停止捕获：%s（本次共保存 %d 张）", target_desc, self.stats.shots_saved)
        self.bus.publish(TOPIC_CAPTURE_STOPPED, saved=self.stats.shots_saved)
        return stopped

    def restart(self, target: CaptureTarget | None = None) -> bool:
        """停止后按新目标重启（P3 用：切换窗口/显示器）。"""
        if target is not None:
            self.config.capture.target = target
        self.stop()
        return self.start()

    def switch_target(self, target: CaptureTarget) -> bool:
        return self.restart(target)

    @staticmethod
    def _wait_control(control: CaptureControl | None, timeout: float) -> bool:
        """``CaptureControl.wait()`` 没有超时参数，放到看门线程里等。"""
        if control is None:
            return True
        done = threading.Event()

        def waiter() -> None:
            try:
                control.wait()
            except Exception:  # noqa: BLE001
                LOG.exception("等待捕获线程退出时出错")
            finally:
                done.set()

        threading.Thread(target=waiter, name="capture-wait", daemon=True).start()
        return done.wait(timeout)

    # ---------- 帧回调（捕获库线程） ----------

    def _on_frame(self, frame: Any, control: Any) -> None:
        self.stats.frames_seen += 1
        if not self.stats.running:
            return

        now = time.monotonic()
        self._last_frame_at = now

        # 预览门：与落盘间隔解耦，UI 刷新更勤但不会拖慢落盘
        if self.config.capture.preview_enabled:
            interval = max(0.1, self.config.capture.preview_interval_ms / 1000.0)
            if now - self._last_preview >= interval:
                self._last_preview = now
                self._publish_preview(frame)

        if now - self._last_save < self._interval:
            return
        # 真正的占位在 _save_image 里（_save_lock 内），这里只是省掉一次无用的深拷贝

        try:
            # convert_to_bgr() 返回切片视图，ascontiguousarray 深拷贝出独立内存
            import numpy as np

            image = np.ascontiguousarray(frame.convert_to_bgr().frame_buffer)
        except Exception:  # noqa: BLE001 - 单帧失败不能打断捕获线程
            LOG.exception("转换捕获帧失败")
            return
        self._save_image(image, source="wgc")

    def _save_image(self, image: Any, source: str = "wgc") -> None:
        """落盘一张（含缓存上限裁剪），并广播事件。捕获回调线程与心跳线程都会调它。

        「每个间隔最多一张」在这里用 ``_save_lock`` **原子占位**：两条路径（WGC 回调与
        静止心跳）可能同时越过各自的间隔门，靠这把锁保证不会同一瞬间存出两张。
        """
        with self._save_lock:
            now = time.monotonic()
            if now - self._last_save < self._interval:
                return
            self._last_save = now
            try:
                height, width = image.shape[:2]
                shot = self.store.save(image, width=width, height=height)
                removed = self.store.enforce_limit()
            except Exception as exc:  # noqa: BLE001 - 保存失败不能打断捕获
                message = f"保存截图失败：{exc}"
                self.stats.last_error = message
                LOG.exception("%s", message)
                self.bus.publish(TOPIC_CAPTURE_ERROR, error=message)
                return

            self.stats.shots_saved += 1
            self.stats.bytes_written += shot.size_bytes
            self.stats.last_shot = shot.as_dict()
        LOG.info("截图已保存：#%d %s（%dx%d，%.1f KB，%s）",
                 self.stats.shots_saved, shot.name, width, height,
                 shot.size_bytes / 1024, source)
        self.bus.publish(TOPIC_SHOT_SAVED, shot=shot)
        if removed:
            # 缓存轮转删掉的文件要让 web 端立刻从列表里摘掉，否则缩略图会 404
            self.bus.publish(TOPIC_SHOT_REMOVED, names=list(removed))

    def _publish_preview(self, frame: Any) -> None:
        """把捕获库里的一帧缩放后发给界面。"""
        try:
            image = frame.convert_to_bgr().frame_buffer  # numpy 视图，别存下来
        except Exception:  # noqa: BLE001
            LOG.exception("转换预览帧失败")
            return
        self._publish_preview_image(image)

    def _publish_preview_image(self, image: Any) -> None:
        """缩放到预览尺寸后广播（缩放会产生独立内存，可以安全跨线程传）。"""
        try:
            import cv2
            import numpy as np

            source_height, source_width = image.shape[:2]
            max_width = max(160, int(self.config.capture.preview_max_width))
            if source_width > max_width:
                scale = max_width / source_width
                target = (max_width, max(1, int(source_height * scale)))
                image = cv2.resize(image, target, interpolation=cv2.INTER_AREA)
            else:
                image = np.ascontiguousarray(image)
            self.bus.publish(TOPIC_FRAME_PREVIEW, image=image,
                             width=source_width, height=source_height)
        except Exception:  # noqa: BLE001 - 预览失败不影响落盘
            LOG.exception("推送预览帧失败")

    # ---------- 静止兜底（心跳） ----------

    def _start_heartbeat(self) -> None:
        if not self.config.capture.heartbeat_fallback:
            LOG.info("已禁用静止兜底（heartbeat_fallback=false）")
            return
        self._heartbeat_stop.clear()
        self._heartbeat = threading.Thread(target=self._heartbeat_loop,
                                           name="capture-heartbeat", daemon=True)
        self._heartbeat.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread, self._heartbeat = self._heartbeat, None
        if thread is not None:
            thread.join(timeout=3)

    def _resolve_fallback_rect(self, target: CaptureTarget) -> tuple[int, int, int, int] | None:
        """显示器目标的兜底抓取矩形（系统枚举顺序，monitor_index 从 1 开始）。"""
        if target.type != "monitor":
            return None
        try:
            from .fallback import monitor_rects

            rects = monitor_rects()
        except Exception:  # noqa: BLE001
            LOG.exception("枚举显示器矩形失败")
            return None
        index = int(target.monitor_index or 1) - 1
        if 0 <= index < len(rects):
            return rects[index]
        LOG.warning("显示器索引 %s 超出系统枚举范围（共 %d 块）",
                    target.monitor_index, len(rects))
        return None

    def _resolve_fallback_hwnd(self, target: CaptureTarget) -> int | None:
        """窗口目标的兜底抓取句柄：只配置了标题时在这里换成 hwnd。"""
        if target.type != "window":
            return None
        if target.window_hwnd is not None:
            return int(target.window_hwnd)
        if not target.window_name:
            return None
        try:
            from .fallback import find_hwnd_by_title

            hwnd = find_hwnd_by_title(str(target.window_name))
        except Exception:  # noqa: BLE001
            LOG.exception("按标题解析窗口句柄失败：%s", target.window_name)
            return None
        if hwnd:
            LOG.info("兜底抓取已按标题解析到窗口句柄：%s -> %s", target.window_name, hwnd)
        else:
            LOG.warning("兜底抓取找不到标题为 %r 的窗口", target.window_name)
        return hwnd

    def _grab_fallback(self) -> Any | None:
        """按当前目标用 GDI 抓一帧。"""
        from .fallback import grab_monitor, grab_window

        target = self.config.capture.target
        if target.type == "window":
            if self._fallback_hwnd is None:
                self._fallback_hwnd = self._resolve_fallback_hwnd(target)
            if self._fallback_hwnd is None:
                return None
            return grab_window(self._fallback_hwnd)
        if self._fallback_rect is None:
            self._fallback_rect = self._resolve_fallback_rect(target)
        if self._fallback_rect is None:
            return None
        return grab_monitor(self._fallback_rect)

    def _heartbeat_loop(self) -> None:
        """画面静止时按间隔补一张。

        Windows Graphics Capture 只在画面变化时出帧，完全静止的屏幕/窗口一帧都不产生；
        没有这条心跳，「每 N 秒一张」在静止画面上会彻底失效（缓存不轮转、web 历史不增长、
        预览不刷新）。

        判据只用「距上次落盘是否够一个间隔」——**不能**再看「距上次收到帧」：画面轻微活动时
        WGC 会以远高于落盘间隔的频率出帧，那样写会把心跳永远推迟，实测落盘间隔会漂到 4 秒。
        真正的「一个间隔一张」由 ``_save_image`` 里的锁内占位保证，两条路径不会重复落盘。
        """
        warned = False
        while not self._heartbeat_stop.wait(0.5):
            if not self.stats.running:
                return
            now = time.monotonic()
            interval = self._interval
            if now - self._last_save < interval:
                continue
            if now - self._last_frame_at < 0.3 and self._last_frame_at > self._last_save:
                # 画面正在动（0.3 秒内刚收到帧，且比上次落盘新）：交给捕获回调去存，
                # 省下一次多余的 GDI 抓取。上限只有 0.3 秒，不会把间隔拖长。
                continue
            image = self._grab_fallback()
            if image is None:
                self._last_save = now  # 抓不到就别疯狂重试
                if not warned:
                    warned = True
                    LOG.warning("静止兜底不可用（目标：%s）：静止画面将不再产生新截图",
                                self.config.capture.target.describe())
                continue
            self._publish_preview_image(image)
            self._save_image(image, source="heartbeat")

    def _on_closed(self) -> None:
        """捕获会话被关闭（目标窗口关闭、显示器变化等）。"""
        self.stats.running = False
        LOG.warning("捕获会话已关闭（目标：%s）", self.stats.target or "未知")
        self.bus.publish(TOPIC_CAPTURE_CLOSED, target=self.stats.target)

    # ---------- 状态 ----------

    def snapshot(self) -> dict[str, Any]:
        data = self.stats.as_dict()
        data.update(self.store.stats())
        return data
