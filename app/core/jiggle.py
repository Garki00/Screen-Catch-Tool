"""光标抖动器。

Windows Graphics Capture 只在画面变化时出帧，完全静止的桌面会让捕获回调长时间不触发。
自检脚本与 `--jiggle` 用它把光标来回挪 1 像素，制造出持续的画面变化。
"""

from __future__ import annotations

import logging
import sys
import threading

LOG = logging.getLogger(__name__)


class CursorJiggler:
    """在独立线程里把光标来回挪 1 像素（仅 Windows；非 Windows 上 start() 返回 False）。"""

    def __init__(self, period: float = 0.3) -> None:
        self.period = period
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = sys.platform == "win32"

    def start(self) -> bool:
        if not self.available:
            return False
        self._thread = threading.Thread(target=self._run, name="cursor-jiggler", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        point = ctypes.wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            LOG.warning("读取光标位置失败，抖动无效")
            return
        x, y = point.x, point.y
        offset = 0
        while not self._stop.wait(self.period):
            offset = 1 - offset
            user32.SetCursorPos(x + offset, y + offset)
