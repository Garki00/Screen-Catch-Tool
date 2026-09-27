"""桌面端主窗口（P2）。

布局对应计划：

- 中央：实时预览（可显示/编辑范围选择框）
- 下边栏：开始/停止捕获、「捕获范围」按钮、「范围选择」按钮
- 右侧边栏：最上「设置」按钮栏（点击展开：捕获间隔、web 端口）、状态指示器
  （捕获状态、web 端状态与地址、缓存占用、当前捕获目标）

跨线程：所有画面/状态都来自 `EngineBridge` 的 Qt 信号，槽在 UI 线程执行，控件只在
UI 线程被触碰。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRect, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    MAX_INTERVAL_SECONDS,
    MAX_PORT,
    MIN_INTERVAL_SECONDS,
    MIN_PORT,
    AppConfig,
    CaptureTarget,
    ConfigError,
    CropRect,
    clamp_interval,
    validate_port,
)
from ..core.bus import EventBus
from ..core.capture import CaptureEngine, CaptureError
from ..core.storage import ScreenshotStore
from .engine_bridge import EngineBridge
from .preview import PreviewWidget
from .qt_image import bgr_to_qimage
from .range_dialog import RangeDialog
from .tray import TrayIcon, tray_available

LOG = logging.getLogger(__name__)

GREEN = "#2ecc71"
GREY = "#8b949e"
RED = "#e74c3c"
AMBER = "#f1c40f"


class MainWindow(QMainWindow):
    """主界面。"""

    def __init__(
        self,
        config: AppConfig,
        store: ScreenshotStore,
        engine: CaptureEngine,
        bridge: EngineBridge,
        config_path: Path | None = None,
        parent: QWidget | None = None,
        web_service: Any = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self.store = store
        self.engine = engine
        self.bridge = bridge
        self.config_path = config_path
        self.web = web_service            # P4 的 WebService，可为 None（--no-web）

        self._web_running = False
        self._web_address: str | None = None
        self._quit_requested = False
        self._tray_notice_shown = False
        self.tray: TrayIcon | None = None      # 必须先置空：update_status 早期就会被调用

        self.setWindowTitle("屏幕捕获工具")
        self.resize(1120, 720)

        self._build_preview()
        self._build_control_bar()
        self._build_sidebar()

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(self.preview, 1)
        left.addLayout(self.control_bar)
        outer.addLayout(left, 1)
        outer.addWidget(self.sidebar)
        self.setCentralWidget(central)

        self._connect_bridge()
        self._sync_from_config()
        self._build_tray()

    # ------------------------------------------------------------------ 托盘

    def _build_tray(self) -> None:
        """建托盘（P6）。系统不支持托盘时保持 ``self.tray is None``，关闭即退出。"""
        if not tray_available():
            LOG.warning("系统托盘不可用，关闭窗口将直接退出程序")
            return
        try:
            self.tray = TrayIcon(self, self)
            self.tray.show()
        except Exception:  # noqa: BLE001 - 托盘不是核心功能，失败不该拦启动
            LOG.exception("创建托盘图标失败")
            self.tray = None
            return
        self.tray.set_capture_state(self.engine.is_running, self.config.capture.interval_seconds)
        self.tray.set_web_state(self._web_running, self._web_address)

    def show_front(self) -> None:
        """从托盘唤回主界面。"""
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def open_web_page(self) -> None:
        """用系统浏览器打开 web 界面。"""
        if not (self._web_running and self._web_address):
            self.set_hint("web 服务未启动，无法打开")
            return
        QDesktopServices.openUrl(QUrl(self._web_address))

    def quit_app(self) -> None:
        """真正的退出：停捕获 → 停 web → 收托盘 → 退事件循环。"""
        self._quit_requested = True
        try:
            if self.engine.is_running:
                self.engine.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("退出时停止捕获失败")
        self.stop_web_service()
        if self.tray is not None:
            self.tray.hide()
        LOG.info("程序退出")
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        """关闭窗口只是隐藏（不中断捕获）；没有托盘时才真的退出。"""
        if self._quit_requested or self.tray is None:
            self.stop_web_service()
            event.accept()
            return
        self.hide()
        event.ignore()
        if not self._tray_notice_shown:
            self._tray_notice_shown = True
            self.tray.notify("仍在后台捕获", "窗口已最小化到托盘，捕获没有中断。"
                                          "要退出请右键托盘图标选择「退出」。", 4000)

    # ------------------------------------------------------------------ 构建

    def _build_preview(self) -> None:
        self.preview = PreviewWidget()
        self.preview.cropEdited.connect(self._on_crop_changed)

    def _build_control_bar(self) -> None:
        self.btn_toggle = QPushButton("开始捕获")
        self.btn_toggle.setMinimumWidth(120)
        self.btn_toggle.clicked.connect(self.toggle_capture)

        self.btn_range = QPushButton("捕获范围…")
        self.btn_range.setToolTip("选择要捕获的窗口或显示器")
        self.btn_range.clicked.connect(self.open_range_dialog)

        self.btn_select = QPushButton("范围选择")
        self.btn_select.setCheckable(True)
        self.btn_select.setToolTip("开启后可在预览画面上拖出范围框（只影响推送给 web 端的内容）")
        self.btn_select.toggled.connect(self._on_select_toggled)

        self.lbl_hint = QLabel("就绪")
        self.lbl_hint.setStyleSheet(f"color: {GREY};")

        self.control_bar = QHBoxLayout()
        self.control_bar.setSpacing(8)
        self.control_bar.addWidget(self.btn_toggle)
        self.control_bar.addWidget(self.btn_range)
        self.control_bar.addWidget(self.btn_select)
        self.control_bar.addWidget(self.lbl_hint, 1)

    def _build_sidebar(self) -> None:
        self.sidebar = QFrame()
        self.sidebar.setFrameShape(QFrame.Shape.StyledPanel)
        self.sidebar.setFixedWidth(270)
        layout = QVBoxLayout(self.sidebar)
        layout.setSpacing(8)

        # 「设置」按钮栏
        self.btn_settings = QToolButton()
        self.btn_settings.setText("设置")
        self.btn_settings.setCheckable(True)
        self.btn_settings.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_settings.setArrowType(Qt.ArrowType.RightArrow)
        self.btn_settings.setSizePolicy(self.btn_settings.sizePolicy().horizontalPolicy(), self.btn_settings.sizePolicy().verticalPolicy())
        self.btn_settings.clicked.connect(self._on_settings_toggled)
        layout.addWidget(self.btn_settings)

        self.settings_panel = QGroupBox()
        self.settings_panel.setVisible(False)
        panel = QVBoxLayout(self.settings_panel)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("捕获间隔"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setToolTip(f"允许 {MIN_INTERVAL_SECONDS}-{MAX_INTERVAL_SECONDS} 秒")
        self.spin_interval.valueChanged.connect(self._on_interval_changed)
        interval_row.addWidget(self.spin_interval, 1)
        panel.addLayout(interval_row)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("web 端口"))
        self.spin_port = QSpinBox()
        self.spin_port.setRange(MIN_PORT, MAX_PORT)
        self.spin_port.setToolTip(f"允许 {MIN_PORT}-{MAX_PORT}；修改后若 web 服务在运行会重启它")
        self.spin_port.valueChanged.connect(self._on_port_changed)
        port_row.addWidget(self.spin_port, 1)
        panel.addLayout(port_row)

        self.lbl_port_hint = QLabel(f"端口范围 {MIN_PORT}-{MAX_PORT}")
        self.lbl_port_hint.setWordWrap(True)
        self.lbl_port_hint.setStyleSheet(f"color: {GREY};")
        panel.addWidget(self.lbl_port_hint)
        layout.addWidget(self.settings_panel)

        # 状态指示器
        status_group = QGroupBox("状态")
        status = QVBoxLayout(status_group)
        self.lbl_status_capture = QLabel()
        self.lbl_status_web = QLabel()
        self.lbl_status_target = QLabel()
        self.lbl_status_cache = QLabel()
        for label in (self.lbl_status_capture, self.lbl_status_web,
                      self.lbl_status_target, self.lbl_status_cache):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.RichText)
            status.addWidget(label)
        layout.addWidget(status_group)

        layout.addStretch(1)
        self.lbl_footer = QLabel("范围框只影响 web 端显示，落盘始终是全尺寸原图。")
        self.lbl_footer.setWordWrap(True)
        self.lbl_footer.setFont(QFont("Microsoft YaHei", 8))
        self.lbl_footer.setStyleSheet(f"color: {GREY};")
        layout.addWidget(self.lbl_footer)

    # ------------------------------------------------------------------ 桥接

    def _connect_bridge(self) -> None:
        self.bridge.previewFrame.connect(self._on_preview_frame)
        self.bridge.shotSaved.connect(self._on_shot_saved)
        self.bridge.captureStarted.connect(self._on_capture_started)
        self.bridge.captureStopped.connect(self._on_capture_stopped)
        self.bridge.captureClosed.connect(self._on_capture_closed)
        self.bridge.errorOccurred.connect(self._on_error)

    def _sync_from_config(self) -> None:
        cfg = self.config
        self.spin_interval.blockSignals(True)
        self.spin_interval.setValue(cfg.capture.interval_seconds)
        self.spin_interval.blockSignals(False)
        self.spin_port.blockSignals(True)
        self.spin_port.setValue(cfg.server.port)
        self.spin_port.blockSignals(False)
        crop = cfg.capture.crop
        self.preview.set_crop(QRect(crop.x, crop.y, crop.width, crop.height) if crop.enabled else None)
        self.update_status()

    # ------------------------------------------------------------------ 交互

    def toggle_capture(self) -> None:
        if self.engine.is_running:
            self.stop_capture()
        else:
            self.start_capture()

    def start_capture(self) -> bool:
        try:
            self.engine.start()
        except CaptureError as exc:
            self.set_hint(f"启动失败：{exc}")
            LOG.error("启动捕获失败：%s", exc)
            return False
        self.set_hint("捕获中…")
        self.update_status()
        return True

    def stop_capture(self) -> None:
        self.engine.stop()
        self.set_hint("已停止捕获")
        self.update_status()

    def open_range_dialog(self) -> CaptureTarget | None:
        dialog = RangeDialog(self.config.capture.target, self)
        target = dialog.choose_target(self.config.capture.target)
        if target is not None:
            self.apply_target(target)
        return target

    def apply_target(self, target: CaptureTarget) -> None:
        """切换捕获目标：写配置，运行中则重启捕获。"""
        self.config.capture.target = target
        self.config.save(self.config_path)
        if self.engine.is_running:
            self.engine.restart(target)
        self.set_hint(f"捕获目标：{target.describe()}")
        self.update_status()

    def apply_crop(self, x: int, y: int, width: int, height: int) -> None:
        """由范围框写入裁剪配置（原始像素坐标）。"""
        self.config.capture.crop = CropRect(enabled=True, x=int(x), y=int(y),
                                            width=int(width), height=int(height))
        self.config.save(self.config_path)
        self.preview.set_crop(QRect(int(x), int(y), int(width), int(height)))
        self._on_crop_changed(int(x), int(y), int(width), int(height))

    def clear_crop(self) -> None:
        self.config.capture.crop = CropRect(enabled=False)
        self.config.save(self.config_path)
        self.preview.set_crop(None)
        self._refresh_web_view()
        self.update_status()

    def set_web_state(self, running: bool, address: str | None = None) -> None:
        """web 服务状态（P4 启动服务后由它调用）。"""
        self._web_running = bool(running)
        self._web_address = address
        self.update_status()

    def set_hint(self, text: str) -> None:
        self.lbl_hint.setText(text)

    # ------------------------------------------------------------------ 槽

    def _on_settings_toggled(self, checked: bool) -> None:
        self.settings_panel.setVisible(checked)
        self.btn_settings.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _on_select_toggled(self, checked: bool) -> None:
        self.preview.set_editable(checked)
        self.btn_select.setText("范围选择中…" if checked else "范围选择")
        self.set_hint("在预览上拖动即可框选范围" if checked else "就绪")

    def _on_interval_changed(self, value: int) -> None:
        interval = clamp_interval(value)
        if interval != value:
            self.spin_interval.blockSignals(True)
            self.spin_interval.setValue(interval)
            self.spin_interval.blockSignals(False)
        self.config.capture.interval_seconds = interval
        self.engine.set_interval(interval)
        self.config.save(self.config_path)
        self.set_hint(f"捕获间隔：{interval} 秒")
        self.update_status()

    def _on_port_changed(self, value: int) -> None:
        try:
            port = validate_port(value)
        except ConfigError as exc:
            self.lbl_port_hint.setText(f"端口非法：{exc}")
            return
        self.config.server.port = port
        self.config.save(self.config_path)
        if self._web_running:
            self.restart_web_service()
        self.lbl_port_hint.setText(
            f"已保存端口 {port}" + ("（web 服务运行中，已请求重启）" if self._web_running else "")
        )
        self.set_hint(f"web 端口：{port}")
        self.update_status()

    def restart_web_service(self) -> None:
        """改端口/地址后重启 web 服务；失败把原因写到界面提示里。"""
        if self.web is None:
            LOG.info("未启用 web 服务，端口 %d 只写入配置", self.config.server.port)
            self.lbl_port_hint.setText(f"已保存端口 {self.config.server.port}（本次未启用 web 服务）")
            return
        ok = self.web.restart(port=self.config.server.port, host=self.config.server.host)
        self.set_web_state(bool(ok and self.web.is_running),
                           self.web.address() if ok and self.web.is_running else None)
        if ok and self.web.is_running:
            self.lbl_port_hint.setText(f"web 服务已在新端口 {self.web.port} 重启")
            self.set_hint(f"web 端口：{self.web.port}")
        else:
            reason = self.web.last_error or "未知原因"
            self.lbl_port_hint.setText(f"重启失败：{reason}")
            self.set_hint(f"web 服务启动失败：{reason}")

    def start_web_service(self) -> bool:
        """启动 web 服务（P4）。成功/失败都同步到状态指示器。"""
        if self.web is None:
            self.set_web_state(False, None)
            return False
        ok = self.web.start()
        running = bool(ok and self.web.is_running)
        self.set_web_state(running, self.web.address() if running else None)
        if running:
            self.set_hint(f"web 界面：{self.web.address()}")
        else:
            self.set_hint(f"web 服务启动失败：{self.web.last_error}")
        return running

    def stop_web_service(self) -> None:
        if self.web is None:
            return
        try:
            self.web.stop()
        except Exception:  # noqa: BLE001
            LOG.exception("停止 web 服务失败")
        self.set_web_state(False, None)

    def _refresh_web_view(self) -> None:
        """范围框变了：立刻重做 web 视图并推送（P5 的实时画面跟着裁）。"""
        if self.web is None or not self.web.is_running:
            return
        try:
            self.web.refresh_view()
        except Exception:  # noqa: BLE001
            LOG.exception("刷新 web 视图失败")

    def _on_crop_changed(self, x: int, y: int, width: int, height: int) -> None:
        self.config.capture.crop = CropRect(enabled=True, x=x, y=y, width=width, height=height)
        self.config.save(self.config_path)
        self.preview.set_crop(QRect(x, y, width, height))
        self._refresh_web_view()
        self.set_hint(f"范围：{width}x{height} @ ({x}, {y})")
        self.update_status()

    def _on_preview_frame(self, image, width: int, height: int) -> None:  # noqa: ANN001
        qimage = bgr_to_qimage(image)
        if qimage is None:
            return
        self.preview.set_frame(qimage, width, height)

    def _on_shot_saved(self, shot) -> None:  # noqa: ANN001
        self.update_status(cache_note=f"最新：{shot.name}")

    def _on_capture_started(self, interval: int) -> None:
        self.btn_toggle.setText("停止捕获")
        self.set_hint(f"捕获中（间隔 {interval} 秒）")
        self.update_status()

    def _on_capture_stopped(self, saved: int) -> None:
        self.btn_toggle.setText("开始捕获")
        self.set_hint(f"已停止（本次保存 {saved} 张）")
        self.update_status()

    def _on_capture_closed(self, target: str) -> None:
        self.btn_toggle.setText("开始捕获")
        self.set_hint(f"捕获对象已关闭：{target}")
        self.update_status()

    def _on_error(self, message: str) -> None:
        self.set_hint(f"错误：{message}")
        self.update_status(error=message)

    # ------------------------------------------------------------------ 状态

    def _crop_values(self) -> tuple[int, int, int, int]:
        crop = self.config.capture.crop
        return crop.x, crop.y, crop.width, crop.height

    def update_status(self, cache_note: str | None = None, error: str | None = None) -> None:
        running = self.engine.is_running
        dot = GREEN if running else GREY
        self.lbl_status_capture.setText(
            f"捕获状态：<b style='color:{dot}'>●</b> {'捕获中' if running else '已停止'}"
        )

        web_dot = GREEN if self._web_running else GREY
        web_text = "已启动" if self._web_running else "未启动"
        if self._web_running and self._web_address:
            web_text += f"（{self._web_address}）"
        self.lbl_status_web.setText(
            f"web 端：<b style='color:{web_dot}'>●</b> {web_text}"
        )

        self.lbl_status_target.setText(f"捕获目标：{self.config.capture.target.describe()}")

        crop = self.config.capture.crop
        crop_text = f"，范围 {crop.width}x{crop.height}" if crop.enabled and crop.width > 0 else ""
        count = self.store.count()
        note = f"<br><span style='color:{GREY}'>{cache_note}</span>" if cache_note else ""
        self.lbl_status_cache.setText(
            f"缓存：{count} 张 / 上限 {self.store.max_screenshots}"
            f"（+{len(self.store.pinned)} 张预览）{crop_text}{note}"
        )
        if self.tray is not None:
            self.tray.set_capture_state(running, self.config.capture.interval_seconds)
            self.tray.set_web_state(self._web_running, self._web_address)
        if error:
            self.lbl_hint.setStyleSheet(f"color: {RED};")
        elif self.lbl_hint.styleSheet():
            self.lbl_hint.setStyleSheet(f"color: {GREY};")
