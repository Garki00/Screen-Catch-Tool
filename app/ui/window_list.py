"""窗口与显示器枚举（P3 的「窗口选择栏」「屏幕选择栏」的数据来源）。

`windows-capture` 2.0.1 不含任何枚举 API（其 `__init__.py` 里没有 `find_window` /
`window_names` / `Monitor` 之类的符号），所以这里自己枚举：

- 窗口：pywin32 的 ``win32gui.EnumWindows``，过滤不可见、无标题、非顶层、DWM 幽灵窗口
  以及本进程自己的窗口；
- 显示器：以 ``EnumDisplayMonitors`` 的顺序为准（实测它与 ``WindowsCapture`` 的
  ``monitor_index`` 顺序一致，见 tests/test_fallback.py），再用 Qt 的 ``QScreen`` 补上
  名称与缩放比。序号从 1 开始，直接就是 ``monitor_index``。
"""

from __future__ import annotations

import ctypes
import logging
import os
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)

DWMWA_CLOAKED = 14


@dataclass(frozen=True)
class WindowInfo:
    """一个可选的顶层窗口。"""

    hwnd: int
    title: str
    pid: int
    minimized: bool = False
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    def label(self) -> str:
        parts = [self.title]
        if self.minimized:
            parts.append("(已最小化)")
        left, top, right, bottom = self.rect
        if right > left and bottom > top:
            parts.append(f"{right - left}x{bottom - top}")
        return f"{parts[0]}  " + "  ".join(parts[1:]) if len(parts) > 1 else parts[0]

    def as_dict(self) -> dict[str, Any]:
        return {"hwnd": self.hwnd, "title": self.title, "pid": self.pid,
                "minimized": self.minimized, "rect": list(self.rect)}


@dataclass(frozen=True)
class MonitorInfo:
    """一块显示器。``index`` 从 1 开始，直接对应 `monitor_index`。"""

    index: int
    name: str
    width: int          # 物理像素宽（与捕获到的图片尺寸一致）
    height: int
    device_pixel_ratio: float
    primary: bool
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    def label(self) -> str:
        size = f"{self.width}x{self.height}"
        if abs(self.device_pixel_ratio - 1.0) > 1e-6:
            size += f" @{self.device_pixel_ratio:g}x 缩放"
        return f"显示器 #{self.index}  {self.name}  {size}" + ("  (主显示器)" if self.primary else "")


def _is_cloaked(hwnd: int) -> bool:
    """UWP 挂起等情况下窗口仍然「可见」但其实是 DWM 幽灵窗口。"""
    try:
        value = ctypes.c_int(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)
        )
        return result == 0 and value.value != 0
    except OSError:
        return False


def list_windows(exclude_pids: set[int] | None = None, include_minimized: bool = True) -> list[WindowInfo]:
    """列出可捕获的顶层窗口，按标题排序。"""
    try:
        import win32gui
        import win32process
    except ImportError:  # pragma: no cover - 依赖缺失时返回空表而不是崩
        LOG.warning("未安装 pywin32，窗口列表为空")
        return []

    exclude_pids = set(exclude_pids or ()) | {os.getpid()}
    windows: list[WindowInfo] = []

    def collect(hwnd: int, _param: Any) -> bool:
        if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001 - 个别窗口查不到 pid，跳过即可
            return True
        if pid in exclude_pids:
            return True
        if _is_cloaked(hwnd):
            return True
        minimized = bool(win32gui.IsIconic(hwnd))
        if minimized and not include_minimized:
            return True
        try:
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:  # noqa: BLE001
            rect = (0, 0, 0, 0)
        windows.append(WindowInfo(hwnd=hwnd, title=title, pid=pid,
                                  minimized=minimized, rect=tuple(rect)))
        return True

    win32gui.EnumWindows(collect, None)
    windows.sort(key=lambda w: (w.minimized, w.title.lower()))
    return windows


def find_window_by_title(title: str, exclude_pids: set[int] | None = None) -> WindowInfo | None:
    """按标题（子串、大小写不敏感）找第一个匹配的窗口。"""
    needle = title.strip().lower()
    for window in list_windows(exclude_pids):
        if needle in window.title.lower():
            return window
    return None


def list_monitors() -> list[MonitorInfo]:
    """列出显示器，序号从 1 开始，顺序与捕获库的 ``monitor_index`` 一致。

    注意：不能直接用 ``QGuiApplication.screens()`` 的顺序——实测本机上 Qt 把主显示器
    排在前面，而 ``EnumDisplayMonitors``（= 捕获库的顺序）把另一块屏排在前面。
    """
    try:
        from PySide6.QtGui import QGuiApplication

        from ..core.fallback import monitor_rects
    except ImportError:  # pragma: no cover
        LOG.warning("缺少 PySide6 / pywin32，显示器列表为空")
        return []

    rects = monitor_rects()
    if not rects:
        return []

    app = QGuiApplication.instance()
    screens = list(app.screens()) if app is not None else []
    primary = app.primaryScreen() if app is not None else None

    monitors: list[MonitorInfo] = []
    for index, rect in enumerate(rects, start=1):
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        screen = _best_screen_match(rect, screens)
        monitors.append(
            MonitorInfo(
                index=index,
                name=screen.name() if screen is not None else "显示器",
                width=width,
                height=height,
                device_pixel_ratio=float(screen.devicePixelRatio()) if screen is not None else 1.0,
                primary=bool(screen is not None and screen is primary),
                rect=rect,
            )
        )
    return monitors


def _best_screen_match(rect: tuple[int, int, int, int], screens: list) -> object | None:
    """把显示器矩形对上 Qt 的 QScreen：先比左上角，再比物理尺寸。"""
    if not screens:
        return None
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    best, best_score = None, None
    for screen in screens:
        geometry = screen.geometry()
        ratio = float(screen.devicePixelRatio())
        origin_score = abs(geometry.x() - rect[0]) + abs(geometry.y() - rect[1])
        size_score = abs(round(geometry.width() * ratio) - width) + abs(round(geometry.height() * ratio) - height)
        score = origin_score + size_score / 10
        if best_score is None or score < best_score:
            best, best_score = screen, score
    return best
