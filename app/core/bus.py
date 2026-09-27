"""进程内事件总线。

捕获回调在自己的线程里跑，UI（P2 的 Qt 主线程）与 web（P4 的服务线程）都不能被
直接调用，只能订阅事件。总线本身只做「线程安全的同步分发 + 异常隔离」，订阅者
要跨线程更新控件时仍需自己用 Qt Signal 转一次。
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any, Callable

LOG = logging.getLogger(__name__)

Listener = Callable[..., None]


class EventBus:
    """极简发布/订阅：``bus.subscribe("shot.saved", fn)`` / ``bus.publish("shot.saved", shot=...)``。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Listener]] = defaultdict(list)

    def subscribe(self, topic: str, listener: Listener) -> Callable[[], None]:
        """订阅主题，返回取消订阅的函数。"""
        with self._lock:
            self._listeners[topic].append(listener)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._listeners[topic].remove(listener)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, topic: str, **payload: Any) -> int:
        """同步分发事件，返回投递成功的订阅者数量。

        单个订阅者抛异常不影响其它订阅者，也绝不让捕获线程挂掉——这是设计约束，
        因为捕获回调是最不能被打断的路径。
        """
        with self._lock:
            listeners = list(self._listeners.get(topic, ()))
        delivered = 0
        for listener in listeners:
            try:
                listener(**payload)
                delivered += 1
            except Exception:  # noqa: BLE001 - 订阅者的问题不能影响发布者
                LOG.exception("事件 %s 的订阅者 %r 抛异常，已忽略", topic, listener)
        return delivered

    def clear(self) -> None:
        with self._lock:
            self._listeners.clear()


# 事件主题常量，避免各处手写字符串拼错
TOPIC_SHOT_SAVED = "shot.saved"
TOPIC_SHOT_REMOVED = "shot.removed"
TOPIC_FRAME_PREVIEW = "frame.preview"
TOPIC_CAPTURE_STARTED = "capture.started"
TOPIC_CAPTURE_STOPPED = "capture.stopped"
TOPIC_CAPTURE_CLOSED = "capture.closed"
TOPIC_CAPTURE_ERROR = "capture.error"
