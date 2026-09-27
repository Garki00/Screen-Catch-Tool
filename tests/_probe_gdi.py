"""探针：GDI 抓图的上下方向 + 显示器矩形与 WGC 索引的对应关系。

一次性排查用，结论已固化进 `app/core/fallback.py` 与 `tests/test_fallback.py`。

    .venv\\Scripts\\python.exe tests/_probe_gdi.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.config import AppConfig, CaptureTarget  # noqa: E402
from app.core.bus import TOPIC_FRAME_PREVIEW, EventBus  # noqa: E402
from app.core.capture import CaptureEngine  # noqa: E402
from app.core.fallback import monitor_rects  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402

TITLE = "SCT-TEST-WINDOW"
BLUE = np.array([255, 0, 0])
YELLOW = np.array([0, 255, 255])


def raw_grab(hwnd: int, flip: bool):
    import win32gui
    import win32ui

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    window_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(window_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    import ctypes
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 2)
    info = bitmap.GetInfo()
    array = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8)
    image = array.reshape(abs(info["bmHeight"]), info["bmWidth"], 4)[:, :, :3]
    if flip:
        image = image[::-1]
    win32gui.DeleteObject(bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, window_dc)
    return info, np.ascontiguousarray(image)


def blob(image, colour, tol=40):
    mask = np.all(np.abs(image.astype(np.int16) - colour.astype(np.int16)) <= tol, axis=-1)
    if mask.sum() < 200:
        return None
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())


def wgc_frame(target: CaptureTarget, seconds: float = 6.0):
    frames = []
    bus = EventBus()
    bus.subscribe(TOPIC_FRAME_PREVIEW, lambda image, width, height: frames.append(image))
    config = AppConfig()
    config.capture.target = target
    config.capture.preview_max_width = 4000
    config.capture.heartbeat_fallback = False
    engine = CaptureEngine(config, ScreenshotStore(Path(tempfile.mkdtemp()) / "s", max_screenshots=3), bus)
    engine.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not frames:
        time.sleep(0.1)
    engine.stop()
    return frames[0] if frames else None


def main() -> int:
    here = Path(__file__).resolve().parent
    proc = subprocess.Popen([sys.executable, str(here / "_magenta_window.py"), TITLE],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.5)
        import win32gui
        hwnd = win32gui.FindWindow(None, TITLE)

        print("== PrintWindow 方向探针 ==")
        for flip in (False, True):
            info, image = raw_grab(hwnd, flip)
            height = image.shape[0]
            print(f"  bmHeight={info['bmHeight']} bmWidth={info['bmWidth']} flipud={flip} "
                  f"尺寸={image.shape[1]}x{height} 蓝={blob(image, BLUE)} 黄={blob(image, YELLOW)}")

        print("\n== monitor_index 与矩形顺序 ==")
        rects = monitor_rects()
        print(f"  EnumDisplayMonitors 顺序：{[(i + 1, r) for i, r in enumerate(rects)]}")
        for index in range(1, len(rects) + 1):
            frame = wgc_frame(CaptureTarget(type="monitor", monitor_index=index))
            if frame is None:
                print(f"  WGC monitor #{index}: 抓不到")
                continue
            print(f"  WGC monitor #{index}: {frame.shape[1]}x{frame.shape[0]} 平均亮度 {frame.mean():.1f}")
        for i, rect in enumerate(rects, start=1):
            import win32gui as _w
            import win32ui as _u

            left, top, right, bottom = rect
            width, height = right - left, bottom - top
            screen_dc = _w.CreateDC("DISPLAY", None, None)
            mfc = _u.CreateDCFromHandle(screen_dc)
            save = mfc.CreateCompatibleDC()
            bmp = _u.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc, width, height)
            save.SelectObject(bmp)
            import win32con
            ok = save.BitBlt((0, 0), (width, height), mfc, (left, top), win32con.SRCCOPY)
            info = bmp.GetInfo()
            arr = np.frombuffer(bmp.GetBitmapBits(True), dtype=np.uint8).reshape(abs(info["bmHeight"]), info["bmWidth"], 4)[:, :, :3]
            print(f"  GDI rect #{i} {rect}: BitBlt={ok} 尺寸={arr.shape[1]}x{arr.shape[0]} 平均亮度 {arr.mean():.1f}")
            _w.DeleteObject(bmp.GetHandle())
            save.DeleteDC()
            mfc.DeleteDC()
            _w.DeleteDC(screen_dc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
