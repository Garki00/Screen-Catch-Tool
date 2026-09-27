"""事件总线 → Qt 信号。

捕获回调跑在 `windows-capture` 自己的线程里，直接碰 Qt 控件会随机崩溃。总线订阅
发生在那个线程，但 `EngineBridge` 是 QObject，发出的信号会被 Qt 排队到主线程的槽，
这是本项目的跨线程铁律。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..core.bus import (
    TOPIC_CAPTURE_CLOSED,
    TOPIC_CAPTURE_ERROR,
    TOPIC_CAPTURE_STARTED,
    TOPIC_CAPTURE_STOPPED,
    TOPIC_FRAME_PREVIEW,
    TOPIC_SHOT_SAVED,
    EventBus,
)


class EngineBridge(QObject):
    """把总线事件转成 Qt 信号（这些信号可以从任意线程安全发出）。"""

    shotSaved = Signal(object)              # Shot
    previewFrame = Signal(object, int, int)  # numpy BGR(缩放后), 原始宽, 原始高
    captureStarted = Signal(int)            # 间隔秒数
    captureStopped = Signal(int)            # 累计落盘张数
    captureClosed = Signal(str)             # 目标描述
    errorOccurred = Signal(str)

    def __init__(self, bus: EventBus, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._unsubscribers = [
            bus.subscribe(TOPIC_SHOT_SAVED, self._on_shot),
            bus.subscribe(TOPIC_FRAME_PREVIEW, self._on_preview),
            bus.subscribe(TOPIC_CAPTURE_STARTED, self._on_started),
            bus.subscribe(TOPIC_CAPTURE_STOPPED, self._on_stopped),
            bus.subscribe(TOPIC_CAPTURE_CLOSED, self._on_closed),
            bus.subscribe(TOPIC_CAPTURE_ERROR, self._on_error),
        ]

    # 这些槽在捕获线程里执行，只做转发
    def _on_shot(self, shot) -> None:  # noqa: ANN001
        self.shotSaved.emit(shot)

    def _on_preview(self, image, width: int, height: int) -> None:  # noqa: ANN001
        self.previewFrame.emit(image, width, height)

    def _on_started(self, target=None, interval: int = 0) -> None:  # noqa: ANN001
        self.captureStarted.emit(int(interval))

    def _on_stopped(self, saved: int = 0) -> None:
        self.captureStopped.emit(int(saved))

    def _on_closed(self, target: str = "") -> None:
        self.captureClosed.emit(str(target))

    def _on_error(self, error: str = "") -> None:
        self.errorOccurred.emit(str(error))

    def detach(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
