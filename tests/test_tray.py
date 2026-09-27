"""P6 系统托盘端到端自检：真实托盘图标 + 真实关闭事件 + 真实 web 服务。

    .venv\\Scripts\\python.exe tests\\test_tray.py

覆盖：
- 托盘图标可用、菜单项齐全、图标随捕获状态切换
- 关闭主窗口 = 隐藏到托盘，捕获**不中断**（关掉之后仍在按间隔落盘）
- 托盘菜单「开始/停止捕获」与主界面按钮状态同步
- 托盘「打开 web 界面」用的是真实地址
- 退出路径：停捕获 → 停 web → 收托盘
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QSystemTrayIcon  # noqa: E402

from app.config import AppConfig, CaptureTarget  # noqa: E402
from app.core.bus import EventBus  # noqa: E402
from app.core.capture import CaptureEngine  # noqa: E402
from app.core.jiggle import CursorJiggler  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402
from app.ui.engine_bridge import EngineBridge  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.tray import make_icon, tray_available  # noqa: E402
from app.web.server import WebService  # noqa: E402

TITLE = "SCT-TRAY-TARGET"
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_until(app: QApplication, predicate, timeout: float, label: str) -> bool:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    print(f"  （等待超时：{label}）")
    return False


class FakeEvent:
    """给 closeEvent 用的最小事件对象，记录 accept/ignore。"""

    def __init__(self) -> None:
        self.accepted: bool | None = None

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.accepted = False


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    print(f"系统托盘可用：{tray_available()}")

    # ---------------- 图标本身 ----------------
    print("\n[1] 托盘图标")
    on_icon, off_icon = make_icon(True), make_icon(False)
    check("能生成捕获中/已停止两种图标",
          not on_icon.pixmap(64, 64).isNull() and not off_icon.pixmap(64, 64).isNull())
    on_img = on_icon.pixmap(64, 64).toImage()
    off_img = off_icon.pixmap(64, 64).toImage()
    check("两种图标视觉上不同（状态可辨认）",
          on_img.pixelColor(40, 40) != off_img.pixelColor(40, 40),
          f"绿={on_img.pixelColor(40, 40).name()} 灰={off_img.pixelColor(40, 40).name()}")

    # ---------------- 起一个用来捕获的窗口 ----------------
    magenta = subprocess.Popen([sys.executable, str(Path(__file__).parent / "_magenta_window.py"),
                                TITLE])
    time.sleep(1.2)

    tmp = Path(tempfile.mkdtemp(prefix="sct-tray-"))
    config_path = tmp / "settings.json"
    cfg = AppConfig()
    cfg.capture.interval_seconds = 2
    cfg.capture.target = CaptureTarget(type="window", monitor_index=None, window_name=TITLE)
    cfg.cache.directory = str(tmp / "shots")
    cfg.server.port = free_port()  # 用一个空闲端口，避免撞上真在跑的实例
    cfg.save(config_path)

    store = ScreenshotStore(directory=tmp / "shots", max_screenshots=cfg.cache.max_screenshots,
                            quality=cfg.cache.quality)
    bus = EventBus()
    engine = CaptureEngine(cfg, store, bus)
    bridge = EngineBridge(bus)
    web = WebService(cfg, store, bus, engine=engine, port=cfg.server.port)
    window = MainWindow(cfg, store, engine, bridge, config_path=config_path, web_service=web)
    window.show()
    pump(app, 0.4)

    try:
        # ---------------- 托盘与窗口的关系 ----------------
        print("\n[2] 托盘对象与菜单")
        check("主窗口建出了托盘", window.tray is not None)
        if window.tray is None:
            print("\n== 本机没有系统托盘，P6 无法在此环境验证 ==")
            return 1

        tray = window.tray
        check("托盘图标已显示", tray.isVisible())
        labels = [a.text() for a in tray.menu.actions() if not a.isSeparator()]
        check("菜单含显示/捕获/web/退出", {"显示主界面", "开始捕获", "退出"} <= set(labels),
              " / ".join(labels))
        check("托盘提示写的是已停止", "已停止" in tray.toolTip(), tray.toolTip())

        # ---------------- 关闭窗口不中断捕获 ----------------
        print("\n[3] 关窗口 → 进托盘，捕获不中断")
        engine.start()
        check("捕获已启动", engine.is_running)
        pump(app, 0.3)
        check("托盘图标跟着切到捕获中", "捕获中" in tray.toolTip(), tray.toolTip())
        check("菜单项变成「停止捕获」", tray.act_toggle.text() == "停止捕获")

        event = FakeEvent()
        window.closeEvent(event)
        pump(app, 0.3)
        check("关闭窗口被拦下（事件被 ignore）", event.accepted is False)
        check("主窗口只是隐藏了", not window.isVisible())
        check("捕获仍在跑", engine.is_running)
        check("托盘仍可见", tray.isVisible())

        before = store.count()
        ok = wait_until(app, lambda: store.count() > before, 12,
                        "关窗后仍继续落盘")
        check("关窗后确实还在按间隔落盘", ok and store.count() > before,
              f"{before} 张 → {store.count()} 张")

        # ---------------- 托盘菜单驱动主界面 ----------------
        print("\n[4] 托盘菜单驱动主界面")
        check("左键单击前窗口是隐藏的", not window.isVisible())
        tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
        pump(app, 0.4)
        check("托盘左键单击唤回主界面", window.isVisible())
        window.hide()
        pump(app, 0.2)
        tray.activated.emit(QSystemTrayIcon.ActivationReason.DoubleClick)
        pump(app, 0.4)
        check("托盘双击也唤回主界面", window.isVisible())

        tray.act_show.trigger()
        pump(app, 0.4)
        check("「显示主界面」把窗口唤回来了", window.isVisible())

        tray.act_toggle.trigger()
        pump(app, 0.4)
        check("托盘「停止捕获」真的停了", not engine.is_running)
        check("主界面按钮文本跟着变", window.btn_toggle.text() == "开始捕获",
              window.btn_toggle.text())
        check("托盘图标回到已停止", "已停止" in tray.toolTip(), tray.toolTip())

        # ---------------- web 服务状态 ----------------
        print("\n[5] web 服务与托盘联动")
        started = window.start_web_service()
        check("界面能拉起 web 服务", started, web.last_error or web.address())
        check("托盘菜单显示 web 地址",
              web.address() in tray.act_web_info.text(), tray.act_web_info.text())
        check("「打开 web 界面」变为可用", tray.act_web.isEnabled())
        check("状态指示器显示已启动", "已启动" in window.lbl_status_web.text(),
              window.lbl_status_web.text())
        real_port = web.port
        check("web 真的在监听", real_port > 0, f"端口 {real_port}")

        # 关窗也不该把 web 服务带走
        window.closeEvent(FakeEvent())
        pump(app, 0.3)
        check("关窗后 web 服务仍在跑", web.is_running)

        # 范围框 → web 视图（P3 与 P5 的接缝）
        from app import paths
        window.apply_crop(40, 30, 200, 120)
        pump(app, 1.0)
        check("改范围框后立刻重做 web 视图", web._view_size == (200, 120), f"视图 {web._view_size}")
        view = paths.web_cache_dir() / "latest.jpeg"
        if view.is_file():
            from PIL import Image
            with Image.open(view) as image:
                check("web 视图文件就是裁剪后的尺寸", image.size == (200, 120), f"{image.size}")
        else:
            check("web 视图文件已生成", False, str(view))
        window.clear_crop()
        pump(app, 1.0)
        check("取消范围框后视图恢复全尺寸", (web._view_size or (0, 0))[0] > 200,
              f"视图 {web._view_size}")

        # ---------------- 退出 ----------------
        print("\n[6] 退出路径")
        engine.start()
        pump(app, 0.3)
        check("退出前捕获在跑", engine.is_running)
        window.quit_app()
        pump(app, 0.8)
        check("退出请求已置位", window._quit_requested)
        check("退出后捕获已停", not engine.is_running)
        check("退出后 web 已停（端口释放）", not web.is_running)
        check("退出后托盘已收起", not tray.isVisible())
        event = FakeEvent()
        window.closeEvent(event)
        check("退出时关窗事件被放行", event.accepted is True)
    finally:
        try:
            engine.stop()
        except Exception:  # noqa: BLE001
            pass
        web.stop()
        bridge.detach()
        if window.tray is not None:
            window.tray.hide()
        window.close()
        magenta.terminate()
        try:
            magenta.wait(timeout=5)
        except Exception:  # noqa: BLE001
            magenta.kill()

    passed = sum(1 for _n, ok, _d in RESULTS if ok)
    print(f"\n== 断言 {passed}/{len(RESULTS)} 通过 ==")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} -> {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
