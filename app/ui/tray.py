"""系统托盘（P6）。

- 关闭主窗口只是**隐藏**，捕获与 web 服务继续跑（计划要求「关闭窗口后不中断捕获」），
  真正的退出在托盘菜单里。
- 图标自己画：彩色表示捕获中，灰色表示已停止——不依赖任何外部图片资源，打包后也在。
- 左键单击/双击托盘图标 = 唤回主界面；右键 = 菜单。

注意：Windows 上 ``QSystemTrayIcon.isSystemTrayAvailable()`` 为 False 的场合（比如
远程会话、某些精简系统）不该让程序退出，所以调用方一律要用返回值判断，失败就退回
「关闭即退出」的行为。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

LOG = logging.getLogger(__name__)

ACTIVE = "#2ee6a8"
IDLE = "#8b949e"
DARK = "#0f1724"


def make_icon(active: bool, size: int = 64) -> QIcon:
    """画一个「摄像机 + 录制点」图标：捕获中为绿色，停止为灰色。"""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    color = QColor(ACTIVE if active else IDLE)

    # 机身
    body = QPen(color, max(3, size // 16))
    painter.setPen(body)
    painter.setBrush(QColor(DARK))
    painter.drawRoundedRect(size * 0.1, size * 0.28, size * 0.8, size * 0.46,
                            size * 0.12, size * 0.12)
    # 顶部取景器
    painter.drawLine(int(size * 0.32), int(size * 0.28), int(size * 0.44), int(size * 0.16))
    painter.drawLine(int(size * 0.44), int(size * 0.16), int(size * 0.62), int(size * 0.16))
    painter.drawLine(int(size * 0.62), int(size * 0.16), int(size * 0.68), int(size * 0.28))
    # 镜头
    painter.setBrush(color if active else Qt.BrushStyle.NoBrush)
    painter.drawEllipse(int(size * 0.34), int(size * 0.36), int(size * 0.32), int(size * 0.32))
    # 录制红点（只在捕获中）
    if active:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ff5d6c"))
        painter.drawEllipse(int(size * 0.72), int(size * 0.12), int(size * 0.16), int(size * 0.16))
    painter.end()
    return QIcon(pixmap)


class TrayIcon(QSystemTrayIcon):
    """托盘图标 + 菜单，动作全部转交给主窗口。"""

    def __init__(self, window: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.window = window
        self._running = False
        self._web_text = "web 端：未启动"

        self._icons = {True: make_icon(True), False: make_icon(False)}
        self.setIcon(self._icons[False])
        self.setToolTip("屏幕捕获工具 · 已停止")

        self.menu = QMenu()
        self.act_show = QAction("显示主界面", self.menu)
        self.act_show.triggered.connect(self._show_window)
        self.act_toggle = QAction("开始捕获", self.menu)
        self.act_toggle.triggered.connect(self._toggle_capture)
        self.act_web = QAction("打开 web 界面", self.menu)
        self.act_web.triggered.connect(self._open_web)
        self.act_web_info = QAction("web 端：未启动", self.menu)
        self.act_web_info.setEnabled(False)
        self.act_quit = QAction("退出", self.menu)
        self.act_quit.triggered.connect(self._quit)

        self.menu.addAction(self.act_show)
        self.menu.addAction(self.act_toggle)
        self.menu.addSeparator()
        self.menu.addAction(self.act_web_info)
        self.menu.addAction(self.act_web)
        self.menu.addSeparator()
        self.menu.addAction(self.act_quit)
        self.setContextMenu(self.menu)

        self.activated.connect(self._on_activated)

    # ------------------------------------------------------------ 对外状态

    def set_capture_state(self, running: bool, interval: int | None = None) -> None:
        self._running = bool(running)
        self.setIcon(self._icons[self._running])
        self.act_toggle.setText("停止捕获" if self._running else "开始捕获")
        state = "捕获中" if self._running else "已停止"
        extra = f"（间隔 {interval} 秒）" if (self._running and interval) else ""
        self.setToolTip(f"屏幕捕获工具 · {state}{extra}")

    def set_web_state(self, running: bool, address: str | None = None) -> None:
        self.blockSignals(True)
        self._web_text = f"web 端：{address}" if (running and address) else \
            ("web 端：已启动" if running else "web 端：未启动")
        self.act_web_info.setText(self._web_text)
        self.act_web.setEnabled(bool(running))
        self.blockSignals(False)

    def notify(self, title: str, message: str, msecs: int = 3000) -> None:
        """气泡提示（只在托盘可用时调用）。"""
        try:
            self.showMessage(title, message, self._icons[self._running], msecs)
        except Exception:  # noqa: BLE001 - 某些系统不支持气泡
            LOG.debug("托盘气泡提示失败", exc_info=True)

    # ------------------------------------------------------------ 内部槽

    def _on_activated(self, reason: Any) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_window()

    def _show_window(self) -> None:
        self.window.show_front()

    def _toggle_capture(self) -> None:
        self.window.toggle_capture()

    def _open_web(self) -> None:
        self.window.open_web_page()

    def _quit(self) -> None:
        self.window.quit_app()


def tray_available() -> bool:
    """本机是否真的能用系统托盘。"""
    try:
        return bool(QSystemTrayIcon.isSystemTrayAvailable())
    except Exception:  # noqa: BLE001
        return False


def icon_size() -> QSize:  # pragma: no cover - 仅用于调试
    return QSize(64, 64)
