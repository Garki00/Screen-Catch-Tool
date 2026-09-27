"""窗口捕获端到端验证：捕获一个纯品红测试窗口，并检查落盘图片确实是那个窗口。

做法：起一个纯品红（#FF00FF）的 tkinter 窗口 → 用窗口标题作为捕获目标跑捕获内核 →
读取最新落盘的 jpeg，检查其像素是否基本全是品红。桌面捕获不会得到纯品红画面，
因此这条断言足以区分「捕获了指定窗口」和「捕获了整个屏幕」。

    .venv\\Scripts\\python.exe tests\\test_window_capture.py [秒数]
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
from app.core.bus import EventBus  # noqa: E402
from app.core.capture import CaptureEngine, CaptureError  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402

TITLE = "SCT-TEST-WINDOW"
MAGENTA = np.array([255, 0, 255])  # BGR


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    here = Path(__file__).resolve().parent

    window = subprocess.Popen(
        [sys.executable, str(here / "_magenta_window.py"), TITLE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        time.sleep(3.0)  # 等窗口真正显示出来
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig()
            config.capture.interval_seconds = 2
            config.capture.target = CaptureTarget(type="window", monitor_index=None, window_name=TITLE)
            config.cache.max_screenshots = 20
            store = ScreenshotStore(Path(tmp) / "shots", max_screenshots=20, quality=85)
            engine = CaptureEngine(config, store, EventBus())

            print(f"捕获窗口：{TITLE}，运行 {seconds:.0f} 秒…")
            try:
                engine.start()
            except CaptureError as exc:
                print(f"FAIL  启动捕获失败：{exc}")
                return 1

            time.sleep(seconds)
            engine.stop()
            snap = engine.snapshot()
            shots = store.list()
            print(f"  回调帧={snap['frames_seen']} 落盘={snap['shots_saved']} 目录内={len(shots)} 张")

            if not shots:
                failures.append("没有产生任何截图")
            elif len(shots) < 3:
                failures.append(f"静止窗口下截图过少（{len(shots)} 张），静止兜底没生效")
            if snap["frames_seen"] < 1:
                failures.append("捕获回调一次都没触发")
            if shots:
                image = cv2.imread(str(shots[0].path))
                if image is None:
                    failures.append("落盘文件无法解码")
                else:
                    ratio = float(np.mean(np.all(np.abs(image.astype(int) - MAGENTA) <= 8, axis=-1)))
                    print(f"  最新图尺寸={image.shape[1]}x{image.shape[0]} 品红像素占比={ratio:.3%}")
                    if ratio < 0.5:
                        failures.append(f"画面不是测试窗口（品红占比仅 {ratio:.1%}）")
                    if image.shape[0] < 100 or image.shape[1] < 100:
                        failures.append(f"窗口捕获尺寸异常：{image.shape}")
    finally:
        window.terminate()
        try:
            window.wait(timeout=5)
        except subprocess.TimeoutExpired:
            window.kill()

    if failures:
        for item in failures:
            print(f"FAIL  {item}")
        return 1
    print("PASS  窗口捕获端到端验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
