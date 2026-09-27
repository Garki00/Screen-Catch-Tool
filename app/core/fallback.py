"""静止画面的兜底抓取。

`windows-capture`（Windows Graphics Capture）**只在画面变化时出帧**：屏幕/窗口完全
静止时，捕获回调可以几十秒都不触发。对「每 N 秒存一张」的需求来说这不够，所以这里
提供按需抓一帧的兜底实现（用 pywin32 的 GDI）：

- 窗口：``PrintWindow`` + ``PW_RENDERFULLCONTENT``（能拿到大部分 GPU 合成内容）；
- 显示器：桌面 DC 的 ``BitBlt``，按显示器在虚拟屏里的矩形取。

两个函数都返回 BGR ``uint8`` 数组（与 WGC 那条通路的通道顺序一致），失败返回 ``None``。
本模块只依赖 pywin32 / numpy，不依赖 Qt，因此无界面模式也能用。
"""

from __future__ import annotations

import ctypes
import logging
from typing import Any

LOG = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 0x00000002

_dpi_ready = False


def _ensure_dpi_aware() -> None:
    """让进程按物理像素工作——否则 125% 缩放屏上拿到的矩形和图像尺寸都会缩水。"""
    global _dpi_ready
    if _dpi_ready:
        return
    _dpi_ready = True
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            LOG.debug("设置 DPI 感知失败（不影响 100% 缩放屏）")


def _to_bgr_topdown(image: Any) -> Any:
    """BGRA → BGR。

    实测（tests/_probe_gdi.py）：``GetBitmapBits(True)`` 返回的行序已经是自上而下，
    ``bmHeight`` 即使为正也不需要 ``flipud``，翻了反而上下颠倒。
    """
    import numpy as np

    return np.ascontiguousarray(image[:, :, :3])


def monitor_rects() -> list[tuple[int, int, int, int]]:
    """按系统枚举顺序返回各显示器的虚拟屏矩形（物理像素）。"""
    _ensure_dpi_aware()
    import ctypes.wintypes

    rects: list[tuple[int, int, int, int]] = []

    def callback(hmonitor, hdc, lprect, data) -> int:  # noqa: ANN001
        rect = lprect.contents
        rects.append((rect.left, rect.top, rect.right, rect.bottom))
        return 1

    proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.RECT),
        ctypes.c_double,
    )(callback)
    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, proc, 0):
        LOG.warning("EnumDisplayMonitors 失败")
    return rects


def grab_monitor(rect: tuple[int, int, int, int]) -> Any | None:
    """按显示器矩形抓一帧（virtual-screen 坐标）。"""
    _ensure_dpi_aware()
    try:
        import numpy as np
        import win32con
        import win32gui
        import win32ui
    except ImportError:
        LOG.warning("未安装 pywin32 / numpy，兜底抓取不可用")
        return None

    left, top, right, bottom = rect
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    screen_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        # 必须用 DISPLAY 这个 GDI 屏幕 DC；GetWindowDC(GetDesktopWindow()) 会 BitBlt 失败
        screen_dc = win32gui.CreateDC("DISPLAY", None, None)
        mfc_dc = win32ui.CreateDCFromHandle(screen_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        # 注意：pywin32 的 BitBlt 成功时返回 None（不是 True），不能拿返回值判断成败
        save_dc.BitBlt((0, 0), (width, height), mfc_dc, (left, top), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        buffer = bitmap.GetBitmapBits(True)
        image = np.frombuffer(buffer, dtype=np.uint8).reshape(abs(info["bmHeight"]), info["bmWidth"], 4)
        return _to_bgr_topdown(image)
    except Exception:  # noqa: BLE001
        LOG.exception("兜底抓取显示器失败：%s", rect)
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if screen_dc is not None:
                win32gui.DeleteDC(screen_dc)
        except Exception:  # noqa: BLE001
            LOG.debug("释放 GDI 资源时出错", exc_info=True)


def find_hwnd_by_title(title: str) -> int | None:
    """按标题找窗口句柄：先精确匹配，再退化成子串匹配（与捕获库的 window_name 语义一致）。

    配置里只写窗口标题时，兜底抓取需要自己把标题换成 hwnd。
    """
    try:
        import win32gui
    except ImportError:
        return None

    hwnd = win32gui.FindWindow(None, title)
    if hwnd:
        return int(hwnd)

    needle = title.strip().lower()
    matches: list[int] = []

    def collect(handle: int, _param) -> bool:  # noqa: ANN001
        if not win32gui.IsWindowVisible(handle):
            return True
        text = win32gui.GetWindowText(handle)
        if text and needle in text.lower():
            matches.append(int(handle))
        return True

    win32gui.EnumWindows(collect, None)
    return matches[0] if matches else None


def grab_window(hwnd: int) -> Any | None:
    """抓取指定窗口（含标题栏边框），与 WGC 的窗口捕获范围一致。"""
    _ensure_dpi_aware()
    try:
        import numpy as np
        import win32gui
        import win32ui
    except ImportError:
        LOG.warning("未安装 pywin32 / numpy，兜底抓取不可用")
        return None

    if not hwnd or not win32gui.IsWindow(hwnd):
        return None

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    window_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        window_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(window_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)
        flags = PW_RENDERFULLCONTENT
        if not ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags):
            LOG.debug("PrintWindow(PW_RENDERFULLCONTENT) 返回失败，重试普通模式 hwnd=%s", hwnd)
            if not ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0):
                LOG.warning("PrintWindow 失败：hwnd=%s", hwnd)
                return None
        info = bitmap.GetInfo()
        buffer = bitmap.GetBitmapBits(True)
        image = np.frombuffer(buffer, dtype=np.uint8).reshape(abs(info["bmHeight"]), info["bmWidth"], 4)
        return _to_bgr_topdown(image)
    except Exception:  # noqa: BLE001
        LOG.exception("兜底抓取窗口失败：hwnd=%s", hwnd)
        return None
    finally:
        try:
            if bitmap is not None:
                win32gui.DeleteObject(bitmap.GetHandle())
            if save_dc is not None:
                save_dc.DeleteDC()
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
            if window_dc is not None:
                win32gui.ReleaseDC(hwnd, window_dc)
        except Exception:  # noqa: BLE001
            LOG.debug("释放 GDI 资源时出错", exc_info=True)
