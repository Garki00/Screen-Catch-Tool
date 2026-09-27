"""静止兜底抓取（GDI）的验证，以及 monitor_index 与系统枚举顺序的一致性。

三件事：

1. 兜底抓的显示器画面 vs WGC 抓的同一块屏画面 —— 逐像素比对，确认兜底没抓错屏、没上下翻转；
2. 兜底抓的窗口画面 —— 用不对称标记（左上蓝、底部黄）确认内容与方向都对；
3. 端到端：捕获一个**完全静止**的窗口（不抖光标），确认心跳仍按间隔产出截图。

    .venv\\Scripts\\python.exe tests\\test_fallback.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.config import AppConfig, CaptureTarget  # noqa: E402
from app.core.bus import TOPIC_FRAME_PREVIEW, EventBus  # noqa: E402
from app.core.capture import CaptureEngine  # noqa: E402
from app.core.fallback import grab_monitor, grab_window, monitor_rects  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402

TITLE = "SCT-TEST-WINDOW"
RESULTS: list[tuple[str, bool, str]] = []

BLUE = np.array([255, 0, 0])      # BGR
YELLOW = np.array([0, 255, 255])  # BGR
MAGENTA = np.array([255, 0, 255])  # BGR


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def blob_centroid(image: np.ndarray, colour: np.ndarray, tolerance: int = 40) -> tuple[float, float] | None:
    mask = np.all(np.abs(image.astype(np.int16) - colour.astype(np.int16)) <= tolerance, axis=-1)
    if mask.sum() < 200:
        return None
    ys, xs = np.nonzero(mask)
    return float(xs.mean()), float(ys.mean())


def wgc_preview(config: AppConfig, seconds: float = 6.0) -> np.ndarray | None:
    """用捕获内核抓一帧 WGC 画面（走预览推送，保持原始尺寸）。"""
    frames: list[tuple] = []
    bus = EventBus()
    bus.subscribe(TOPIC_FRAME_PREVIEW, lambda image, width, height: frames.append((image, width, height)))
    engine = CaptureEngine(config, ScreenshotStore(Path(tempfile.mkdtemp()) / "shots", max_screenshots=5), bus)
    engine.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not frames:
        time.sleep(0.1)
    engine.stop()
    return frames[0][0] if frames else None


def main() -> int:
    here = Path(__file__).resolve().parent
    window_proc = subprocess.Popen(
        [sys.executable, str(here / "_magenta_window.py"), TITLE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(2.5)
        import win32gui
        hwnd = win32gui.FindWindow(None, TITLE)
        check("找到测试窗口", bool(hwnd), f"hwnd={hwnd}")

        print("\n[兜底] 显示器矩形与 WGC 的一致性")
        rects = monitor_rects()
        check("枚举到显示器矩形", len(rects) >= 1,
              "; ".join(f"{i + 1}:{r}" for i, r in enumerate(rects)))

        grabbed_frames: dict[int, np.ndarray] = {}
        for index, rect in enumerate(rects, start=1):
            base = AppConfig()
            base.capture.target = CaptureTarget(type="monitor", monitor_index=index)
            base.capture.preview_max_width = 4000  # 预览不缩放，便于逐像素比对
            base.capture.heartbeat_fallback = False
            reference = wgc_preview(base)
            grabbed = grab_monitor(rect)
            if reference is None or grabbed is None:
                check(f"显示器 #{index} 两条通路都能抓到画面", False,
                      f"WGC={'有' if reference is not None else '无'} GDI={'有' if grabbed is not None else '无'}")
                continue
            check(f"显示器 #{index} 尺寸一致",
                  reference.shape == grabbed.shape,
                  f"WGC {reference.shape[1]}x{reference.shape[0]} vs GDI {grabbed.shape[1]}x{grabbed.shape[0]}")
            if reference.shape != grabbed.shape:
                continue
            diff = float(np.abs(reference.astype(np.int16) - grabbed.astype(np.int16)).mean())
            check(f"显示器 #{index} 兜底画面与 WGC 一致（平均像素差 < 12）", diff < 12, f"平均差 {diff:.2f}")
            check(f"显示器 #{index} 兜底画面非全黑", float(grabbed.mean()) > 5,
                  f"平均亮度 {grabbed.mean():.1f}")
            grabbed_frames[index] = grabbed

        if len(grabbed_frames) >= 2:
            a, b = grabbed_frames[1], grabbed_frames[2]
            between = float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())
            check("两块屏画面确实不同（索引确实指向不同显示器）", between > 5,
                  f"两屏平均差 {between:.2f}")

        print("\n[兜底] 窗口抓取的内容与方向")
        shot = grab_window(hwnd)
        check("窗口兜底抓取成功", shot is not None,
              f"{shot.shape[1]}x{shot.shape[0]}" if shot is not None else "None")
        if shot is not None:
            height, width = shot.shape[:2]
            ratio = float(np.mean(np.all(np.abs(shot.astype(int) - MAGENTA) <= 8, axis=-1)))
            blue = blob_centroid(shot, BLUE)
            yellow = blob_centroid(shot, YELLOW)
            check("底色是品红", ratio > 0.4, f"品红占比 {ratio:.1%}")
            check("左上角标记是蓝色且位于画面上半部", blue is not None and blue[1] < height * 0.45,
                  f"蓝色质心 {blue}（画面高 {height}）")
            check("底部标记是黄色且位于画面下半部", yellow is not None and yellow[1] > height * 0.55,
                  f"黄色质心 {yellow}（画面高 {height}）")
            check("蓝色标记在左侧", blue is not None and blue[0] < width * 0.5,
                  f"蓝色质心 x={blue[0] if blue else None}（画面宽 {width}）")

        print("\n[心跳] 完全静止的窗口也要按间隔产出截图")
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.capture.target = CaptureTarget(type="window", monitor_index=None, window_hwnd=hwnd)
            config.capture.interval_seconds = 2
            config.capture.heartbeat_fallback = True
            store = ScreenshotStore(Path(tmp) / "shots", max_screenshots=20)
            engine = CaptureEngine(config, store, EventBus())
            engine.start()
            time.sleep(9.0)   # 不抖光标：WGC 期间最多给一帧
            engine.stop()
            shots = store.list()
            check("静止窗口下仍产出多张截图（心跳生效）", len(shots) >= 3,
                  f"{len(shots)} 张，回调帧 {engine.stats.frames_seen}")
            if len(shots) >= 2:
                ordered = sorted(shots, key=lambda s: s.name)
                gaps = [b.mtime - a.mtime for a, b in zip(ordered, ordered[1:])]
                check("相邻截图间隔接近 2 秒",
                      all(1.5 <= g <= 3.5 for g in gaps),
                      "间隔 " + ", ".join(f"{g:.2f}s" for g in gaps))
                images = [cv2.imread(str(s.path)) for s in ordered[:2]]
                check("落盘文件可解码", all(i is not None for i in images))
    finally:
        window_proc.terminate()
        try:
            window_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            window_proc.kill()

    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== 断言 {passed}/{total} 通过 ==")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  未通过：{name} {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
