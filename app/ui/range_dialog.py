"""捕获范围选择弹窗：窗口 / 屏幕二选一，四宫格布局。

面板分布（QGridLayout 2x2）：

    ┌────────────────┬────────────────┐
    │ 窗口选择栏      │ 屏幕选择栏      │
    ├────────────────┼────────────────┤
    │ 预览栏          │ 说明 / 刷新 /   │
    │（选中目标的画面）│ 确定 取消       │
    └────────────────┴────────────────┘

「窗口」与「屏幕」是**互斥**的：选中一个列表里的项，另一个列表的选择会被清掉，
并且选中项自己带底色与粗体——即使列表失去焦点也看得出来选的是哪一个（Qt 默认在失焦时
把选中色画灰，容易让人以为两边都选中了）。
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from ..config import CaptureTarget
from .qt_image import bgr_to_qimage
from .window_list import MonitorInfo, WindowInfo, list_monitors, list_windows

LOG = logging.getLogger(__name__)

ROLE_HWND = Qt.ItemDataRole.UserRole
ROLE_MONITOR = Qt.ItemDataRole.UserRole + 1

SELECTED_BG = QColor("#1f6feb")
SELECTED_FG = QColor("#ffffff")
PREVIEW_MIN = (320, 200)


class RangeDialog(QDialog):
    """选择「捕获哪个窗口 / 哪块显示器」，两者只能选一个。"""

    def __init__(self, current: CaptureTarget | None = None, parent=None) -> None:  # noqa: ANN001
        super().__init__(parent)
        self.setWindowTitle("捕获范围")
        self.resize(880, 620)

        self._target: CaptureTarget | None = None
        self._selected: CaptureTarget | None = None      # 唯一真相：当前选中的目标
        self._preview_image: QImage | None = None
        self.monitor_infos: list[MonitorInfo] = []
        self.window_infos: list[WindowInfo] = []

        # ---------------- 左上：窗口选择栏 ----------------
        self.window_list = QListWidget()
        self.window_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.window_list.setToolTip("选择一个窗口作为捕获目标（用 hwnd 定位，标题只用于展示）")
        self.window_group = QGroupBox("窗口选择栏")
        window_layout = QVBoxLayout(self.window_group)
        window_layout.addWidget(self.window_list)

        # ---------------- 右上：屏幕选择栏 ----------------
        self.monitor_list = QListWidget()
        self.monitor_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.monitor_list.setToolTip("选择一块显示器作为捕获目标（序号从 1 开始，与系统枚举一致）")
        self.monitor_group = QGroupBox("屏幕选择栏")
        monitor_layout = QVBoxLayout(self.monitor_group)
        monitor_layout.addWidget(self.monitor_list)

        # ---------------- 左下：预览栏 ----------------
        self.preview_group = QGroupBox("预览栏")
        self.preview_label = QLabel("在上方选择窗口或显示器，这里显示选中目标的画面")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(*PREVIEW_MIN)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.preview_label.setStyleSheet(
            "border: 1px solid #30363d; border-radius: 6px; color: #8b949e;")
        self.preview_caption = QLabel("未选择")
        self.preview_caption.setWordWrap(True)
        preview_layout = QVBoxLayout(self.preview_group)
        preview_layout.addWidget(self.preview_label, 1)
        preview_layout.addWidget(self.preview_caption)

        # ---------------- 右下：说明 + 刷新列表 + 确定/取消 ----------------
        self.action_group = QGroupBox("说明 / 操作")
        self.hint = QLabel(
            "· 窗口与屏幕只能选一个，选中一个会取消另一个的选择。\n"
            "· 窗口用 hwnd 定位，标题只用来显示；列表已排除不可见、无标题与本程序自身的窗口。\n"
            "· 显示器序号从 1 开始，顺序与系统枚举一致。\n"
            "· 左下预览是选中目标的一张即时快照（选完才会开始连续捕获）。\n"
            "· 双击列表项等于「选中并确定」。")
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        self.btn_refresh = QPushButton("刷新列表")
        self.btn_refresh.setToolTip("重新枚举窗口与显示器（新开的窗口不会自动出现）")
        self.btn_refresh.clicked.connect(self.reload)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.btn_ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.btn_cancel = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.btn_ok.setText("确定")
        self.btn_cancel.setText("取消")
        self.btn_ok.setDefault(True)
        self.buttons.accepted.connect(self._accept_selection)
        self.buttons.rejected.connect(self.reject)

        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.btn_refresh)
        refresh_row.addStretch(1)

        action_layout = QVBoxLayout(self.action_group)
        action_layout.addWidget(self.hint, 1)
        action_layout.addLayout(refresh_row)
        action_layout.addWidget(self.buttons)

        # ---------------- 四宫格 ----------------
        grid = QGridLayout(self)
        grid.setSpacing(10)
        grid.addWidget(self.window_group, 0, 0)
        grid.addWidget(self.monitor_group, 0, 1)
        grid.addWidget(self.preview_group, 1, 0)
        grid.addWidget(self.action_group, 1, 1)
        grid.setRowStretch(0, 3)
        grid.setRowStretch(1, 4)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # 单选：谁被点，就把另一边清掉。
        # 不用 currentItemChanged——弹窗第一次 show 时 Qt 会把「当前项」设成第一行，
        # 此时并没有真的选中（selectedItems() 为空），拿它当选择会造成「打开就默认选了第一个窗口」。
        for widget, handler in ((self.window_list, self._on_window_selection),
                                (self.monitor_list, self._on_monitor_selection)):
            widget.itemClicked.connect(lambda _item, fn=handler: fn())
            widget.itemSelectionChanged.connect(handler)
            widget.itemActivated.connect(lambda _item: self._accept_selection())
            widget.itemDoubleClicked.connect(lambda _item: self._accept_selection())

        self.reload()
        self._preselect(current)

    def showEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        super().showEvent(event)
        if self._selected is None:
            # Qt 在 show 之后会把「当前项」设成第一行（并未真的选中），
            # 排到事件循环下一轮再清，免得看起来像已经默认选好了
            QTimer.singleShot(0, self._clear_all_selection)

    def _clear_all_selection(self) -> None:
        self._clear_selection(self.window_list)
        self._clear_selection(self.monitor_list)

    # ------------------------------------------------------------------ 数据

    def reload(self) -> None:
        """重新枚举窗口与显示器；原来选中的目标若还在，保持选中。"""
        previous = self._selected

        self.window_list.blockSignals(True)
        self.monitor_list.blockSignals(True)
        self.window_list.clear()
        self.monitor_list.clear()
        self.window_infos = list_windows()
        for window in self.window_infos:
            item = QListWidgetItem(window.label())
            item.setData(ROLE_HWND, window.hwnd)
            item.setToolTip(f"hwnd={window.hwnd}  pid={window.pid}")
            self.window_list.addItem(item)

        self.monitor_infos = list_monitors()
        for monitor in self.monitor_infos:
            item = QListWidgetItem(monitor.label())
            item.setData(ROLE_MONITOR, monitor.index)
            item.setToolTip(f"物理 {monitor.width}x{monitor.height}，缩放 {monitor.device_pixel_ratio}x"
                            + ("，主显示器" if monitor.primary else ""))
            self.monitor_list.addItem(item)
        self.window_list.blockSignals(False)
        self.monitor_list.blockSignals(False)

        LOG.info("捕获范围弹窗：%d 个窗口，%d 块显示器",
                 len(self.window_infos), len(self.monitor_infos))

        if previous is not None and not self._reselect(previous):
            # 原目标已消失：取消选择并说明
            self._set_selected(None)
            self.hint.setText(f"原来选中的「{previous.describe()}」已不在列表里，请重新选择。")

    def _reselect(self, target: CaptureTarget) -> bool:
        """把列表里的选中状态恢复成 target，成功返回 True。"""
        if target.type == "window" and target.window_hwnd is not None:
            for row in range(self.window_list.count()):
                if self.window_list.item(row).data(ROLE_HWND) == target.window_hwnd:
                    self.window_list.setCurrentRow(row)
                    return True
        elif target.type == "monitor" and target.monitor_index is not None:
            for row in range(self.monitor_list.count()):
                if self.monitor_list.item(row).data(ROLE_MONITOR) == target.monitor_index:
                    self.monitor_list.setCurrentRow(row)
                    return True
        return False

    def _preselect(self, current: CaptureTarget | None) -> None:
        if current is None or current.type not in ("window", "monitor"):
            return
        if not self._reselect(current):
            if current.window_name:
                # 只配了标题（窗口可能还没开）：在窗口列表里找同名项
                for row in range(self.window_list.count()):
                    if current.window_name in self.window_list.item(row).text():
                        self.window_list.setCurrentRow(row)
                        return
            LOG.info("弹窗预选中：配置里的目标 %s 当前不在列表里", current.describe())

    # ------------------------------------------------------------------ 选择

    def _on_window_selection(self) -> None:
        items = self.window_list.selectedItems()
        if not items:
            return
        item = items[0]
        hwnd = int(item.data(ROLE_HWND))
        info = next((w for w in self.window_infos if w.hwnd == hwnd), None)
        title = info.title if info is not None else item.text()
        self._set_selected(CaptureTarget(type="window", monitor_index=None,
                                         window_hwnd=hwnd, window_name=title))
        self._clear_selection(self.monitor_list)
        self.hint.setText(f"已选中窗口：{title}（hwnd={hwnd}）—— 屏幕选择栏的选择已取消。")

    def _on_monitor_selection(self) -> None:
        items = self.monitor_list.selectedItems()
        if not items:
            return
        index = int(items[0].data(ROLE_MONITOR))
        self._set_selected(CaptureTarget(type="monitor", monitor_index=index))
        self._clear_selection(self.window_list)
        self.hint.setText(f"已选中显示器 #{index} —— 窗口选择栏的选择已取消。")

    def _clear_selection(self, widget: QListWidget) -> None:
        """清掉另一个列表的选中态（含我们画的底色/粗体）。"""
        widget.blockSignals(True)
        for row in range(widget.count()):
            self._paint(widget.item(row), False)
        widget.clearSelection()
        widget.setCurrentRow(-1)
        widget.blockSignals(False)

    def _set_selected(self, target: CaptureTarget | None) -> None:
        """唯一入口：设定当前选中的目标（另一边的清除由调用方负责）。"""
        same = (target is not None and self._selected is not None
                and target.type == self._selected.type
                and target.window_hwnd == self._selected.window_hwnd
                and target.monitor_index == self._selected.monitor_index)
        self._selected = target
        for widget, role in ((self.window_list, ROLE_HWND), (self.monitor_list, ROLE_MONITOR)):
            for row in range(widget.count()):
                item = widget.item(row)
                mine = (target is not None
                        and ((role == ROLE_HWND and target.type == "window"
                              and int(item.data(ROLE_HWND)) == target.window_hwnd)
                             or (role == ROLE_MONITOR and target.type == "monitor"
                                 and int(item.data(ROLE_MONITOR)) == target.monitor_index)))
                self._paint(item, bool(mine))
        self._mark_groups()
        if not same:  # 点击会同时触发 itemClicked 与 currentItemChanged，别重复抓图
            self._update_preview()

    @staticmethod
    def _paint(item: QListWidgetItem, selected: bool) -> None:
        """选中项给底色 + 粗体：列表失焦也看得出来（Qt 默认会把失焦的选中色画灰）。"""
        font: QFont = item.font()
        font.setBold(selected)
        item.setFont(font)
        if selected:
            item.setBackground(QBrush(SELECTED_BG))
            item.setForeground(QBrush(SELECTED_FG))
        else:
            item.setBackground(QBrush())
            item.setForeground(QBrush())

    def _mark_groups(self) -> None:
        which = None if self._selected is None else self._selected.type
        self.window_group.setTitle("窗口选择栏" + ("　✔ 已选中" if which == "window" else ""))
        self.monitor_group.setTitle("屏幕选择栏" + ("　✔ 已选中" if which == "monitor" else ""))

    # ------------------------------------------------------------------ 预览

    def _update_preview(self) -> None:
        """抓一张选中目标的静态快照放进预览栏。"""
        target = self._selected
        if target is None:
            self._preview_image = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("在上方选择窗口或显示器，这里显示选中目标的画面")
            self.preview_caption.setText("未选择")
            return

        image = self._grab(target)
        if image is None:
            self._preview_image = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("无法预览该目标\n（可能已最小化、被遮挡或权限不足）")
            self.preview_caption.setText(f"当前选中：{self._label_for(target)}（无预览）")
            return

        qimage = bgr_to_qimage(image)
        if qimage is None:
            self.preview_label.setText("预览转换失败")
            self.preview_caption.setText(f"当前选中：{self._label_for(target)}")
            return
        self._preview_image = qimage
        self.preview_caption.setText(
            f"当前选中：{self._label_for(target)}　·　原始 {qimage.width()}x{qimage.height()}")
        self._render_preview()

    def _label_for(self, target: CaptureTarget) -> str:
        """列表里的那行文字（标题 / 显示器名），拿来写预览说明。"""
        if target.type == "window":
            info = next((w for w in self.window_infos if w.hwnd == target.window_hwnd), None)
            if info is not None:
                return info.label()
        elif target.type == "monitor":
            info = next((m for m in self.monitor_infos if m.index == target.monitor_index), None)
            if info is not None:
                return info.label()
        return target.describe()

    @staticmethod
    def _grab(target: CaptureTarget) -> Any | None:
        """用 GDI 兜底通路抓一张：窗口走 PrintWindow，显示器走 BitBlt。"""
        try:
            from ..core.fallback import grab_monitor, grab_window, monitor_rects

            if target.type == "window" and target.window_hwnd:
                return grab_window(int(target.window_hwnd))
            if target.type == "monitor" and target.monitor_index:
                rects = monitor_rects()
                index = int(target.monitor_index) - 1
                if 0 <= index < len(rects):
                    return grab_monitor(rects[index])
        except Exception:  # noqa: BLE001 - 预览失败不该拦住选择
            LOG.exception("捕获范围预览抓图失败：%s", target.describe())
        return None

    def _render_preview(self) -> None:
        """按当前控件大小等比缩放显示（只用一张原图，缩放不损失信息）。"""
        if self._preview_image is None:
            return
        box = self.preview_label.size()
        pixmap = QPixmap.fromImage(self._preview_image).scaled(
            max(1, box.width() - 8), max(1, box.height() - 8),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview_label.setPixmap(pixmap)
        self.preview_label.setText("")

    def resizeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        super().resizeEvent(event)
        self._render_preview()

    # ------------------------------------------------------------------ 结果

    def selected_target(self) -> CaptureTarget | None:
        """当前选中的目标（没选返回 None）。窗口与屏幕互斥，这里只可能是其中一个。"""
        return self._selected

    def _accept_selection(self) -> None:
        if self._selected is None:
            self.hint.setText("请先在左上或右上的列表里选择一个窗口或显示器。")
            return
        self._target = self._selected
        self.accept()

    def choose_target(self, current: CaptureTarget | None = None) -> CaptureTarget | None:
        """弹出对话框并返回选择结果（取消返回 None）。主要给自检脚本用。"""
        self._preselect(current)
        if self.exec() == QDialog.DialogCode.Accepted:
            return self._target
        return None
