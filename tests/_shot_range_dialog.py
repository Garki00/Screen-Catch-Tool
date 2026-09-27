"""把「捕获范围」弹窗真实渲染成 PNG，用于人工/视觉检视（不进自动化断言）。

    .venv\\Scripts\\python.exe tests\\_shot_range_dialog.py cache\\uidemo

会拉起一个带花纹的测试窗口当捕获目标，分别渲染三种状态：
未选择 / 选中窗口（含预览）/ 选中显示器（含预览）。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.range_dialog import RangeDialog  # noqa: E402

TITLE = "SCT-RANGE-DEMO"


def pump(app: QApplication, seconds: float = 0.4) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "cache/uidemo")
    out_dir.mkdir(parents=True, exist_ok=True)

    here = Path(__file__).resolve().parent
    window_proc = subprocess.Popen(
        [sys.executable, str(here / "_magenta_window.py"), TITLE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    app = QApplication(sys.argv[:1])
    try:
        time.sleep(2.5)
        dialog = RangeDialog(None)
        dialog.resize(880, 620)
        dialog.show()
        pump(app, 0.6)

        shots: list[str] = []

        def snap(name: str) -> None:
            pump(app, 0.5)
            path = out_dir / f"{name}.png"
            dialog.grab().save(str(path))
            shots.append(f"{name}: {dialog.preview_caption.text()}")
            print(f"  {name}.png  ->  {dialog.preview_caption.text()}")

        snap("range-01-empty")

        for row in range(dialog.window_list.count()):
            if TITLE in dialog.window_list.item(row).text():
                dialog.window_list.setCurrentRow(row)
                break
        snap("range-02-window")

        dialog.monitor_list.setCurrentRow(0)
        snap("range-03-monitor")

        print("\n输出目录：", out_dir.resolve())
        for line in shots:
            print(" -", line)
        dialog.close()
        return 0
    finally:
        window_proc.terminate()
        try:
            window_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            window_proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
