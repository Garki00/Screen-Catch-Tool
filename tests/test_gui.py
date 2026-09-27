"""P2/P3 界面端到端自检：真实起窗口、真实捕获、真实鼠标事件。

会短暂在弹出的 Qt 窗口里操作，并起一个纯品红测试窗口当捕获对象：

    .venv\\Scripts\\python.exe tests\\test_gui.py

覆盖：主界面控件与范围（下边栏/设置栏/状态指示器）、窗口与显示器枚举、
「捕获范围」弹窗选择并切换捕获目标、实时预览收到画面、范围框拖拽 → 原始像素坐标、
间隔与端口热更新并落盘。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import MAX_INTERVAL_SECONDS, MIN_INTERVAL_SECONDS, AppConfig, CaptureTarget  # noqa: E402
from app.core.bus import EventBus  # noqa: E402
from app.core.capture import CaptureEngine  # noqa: E402
from app.core.jiggle import CursorJiggler  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402
from app.ui.engine_bridge import EngineBridge  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.range_dialog import SELECTED_BG, RangeDialog  # noqa: E402
from app.ui.window_list import list_monitors, list_windows  # noqa: E402

TITLE = "SCT-TEST-WINDOW"
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


def main() -> int:
    here = Path(__file__).resolve().parent
    window_proc = subprocess.Popen(
        [sys.executable, str(here / "_magenta_window.py"), TITLE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    jiggler = CursorJiggler()
    engine = None
    window = None
    try:
        time.sleep(2.5)  # 等测试窗口出来
        app = QApplication.instance() or QApplication(sys.argv[:1])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = AppConfig()
            config.capture.interval_seconds = 2
            config_path = tmp_path / "settings.json"
            config.save(config_path)
            store = ScreenshotStore(tmp_path / "shots", max_screenshots=20, quality=85)
            bus = EventBus()
            engine = CaptureEngine(config, store, bus)
            bridge = EngineBridge(bus)
            window = MainWindow(config, store, engine, bridge, config_path=config_path)
            window.resize(1120, 720)
            window.show()
            pump(app, 0.5)

            print("\n[界面] 标题 / 下边栏 / 设置栏 / 状态指示器")
            check("窗口标题为中文", window.windowTitle() == "屏幕捕获工具", window.windowTitle())
            check("下边栏有开始/停止按钮", window.btn_toggle.text() == "开始捕获", window.btn_toggle.text())
            check("下边栏有捕获范围按钮", window.btn_range.text() == "捕获范围…")
            check("下边栏有范围选择按钮（可勾选）", window.btn_select.isCheckable())
            check("设置面板初始收起", not window.settings_panel.isVisible())
            window.btn_settings.click()
            pump(app, 0.3)
            check("点击「设置」后展开", window.settings_panel.isVisible())
            check("捕获间隔范围 2-10 秒",
                  (window.spin_interval.minimum(), window.spin_interval.maximum())
                  == (MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS),
                  f"{window.spin_interval.minimum()}-{window.spin_interval.maximum()}")
            check("web 端口范围 1024-65535",
                  (window.spin_port.minimum(), window.spin_port.maximum()) == (1024, 65535),
                  f"{window.spin_port.minimum()}-{window.spin_port.maximum()}")
            window.spin_port.setValue(1023)
            check("非法端口被控件拒绝（自动夹到 1024）", window.spin_port.value() == 1024,
                  str(window.spin_port.value()))
            check("状态指示器初始为「已停止」", "已停止" in window.lbl_status_capture.text(),
                  window.lbl_status_capture.text())
            check("web 状态初始为「未启动」", "未启动" in window.lbl_status_web.text())
            check("预览初始无画面", not window.preview.has_image())

            print("\n[P3] 窗口与显示器枚举")
            own_title_windows = [w for w in list_windows() if TITLE in w.title]
            check("窗口列表包含测试窗口", len(own_title_windows) == 1,
                  f"{len(own_title_windows)} 个匹配")
            check("捕获范围弹窗可见（父窗口）", not window.btn_range.isHidden())
            check("窗口列表排除了本进程自己的窗口",
                  all(w.pid != os.getpid() for w in list_windows()))
            monitors = list_monitors()
            check("显示器列表非空且序号从 1 开始",
                  bool(monitors) and monitors[0].index == 1,
                  "; ".join(m.label() for m in monitors))
            check("显示器列表包含主显示器", any(m.primary for m in monitors))

            print("\n[P3] 「捕获范围」弹窗：四宫格分布")
            dialog = RangeDialog(config.capture.target, window)
            dialog.show()          # 布局没激活前子控件坐标全是 0,0
            pump(app, 0.4)
            check("左上 = 窗口选择栏", dialog.window_group.title().startswith("窗口选择栏"),
                  dialog.window_group.title())
            check("右上 = 屏幕选择栏", dialog.monitor_group.title().startswith("屏幕选择栏"),
                  dialog.monitor_group.title())
            check("左下 = 预览栏", dialog.preview_group.title().startswith("预览栏"))
            check("右下 = 说明 + 刷新列表 + 确定/取消",
                  dialog.btn_refresh.text() == "刷新列表"
                  and dialog.btn_ok.text() == "确定" and dialog.btn_cancel.text() == "取消"
                  and dialog.btn_refresh.parent() is dialog.action_group,
                  dialog.action_group.title())
            win_pos = dialog.window_group.mapTo(dialog, QPoint(0, 0))
            mon_pos = dialog.monitor_group.mapTo(dialog, QPoint(0, 0))
            pre_pos = dialog.preview_group.mapTo(dialog, QPoint(0, 0))
            act_pos = dialog.action_group.mapTo(dialog, QPoint(0, 0))
            check("窗口栏(左上) 在 屏幕栏(右上) 左边、同一排",
                  win_pos.x() < mon_pos.x() and win_pos.y() == mon_pos.y(),
                  f"窗口 {win_pos.x()},{win_pos.y()} / 屏幕 {mon_pos.x()},{mon_pos.y()}")
            check("预览栏(左下) 在 说明栏(右下) 左边、同一排，且都在上一排之下",
                  pre_pos.x() < act_pos.x() and pre_pos.y() == act_pos.y()
                  and pre_pos.y() > win_pos.y(),
                  f"预览 {pre_pos.x()},{pre_pos.y()} / 说明 {act_pos.x()},{act_pos.y()}")

            print("\n[P3] 单选：窗口与屏幕只能有一个高亮")
            rows = [(row, dialog.window_list.item(row)) for row in range(dialog.window_list.count())]
            target_row = next((row for row, item in rows if TITLE in item.text()), None)
            check("弹窗里能找到测试窗口", target_row is not None)
            dialog.window_list.setCurrentRow(target_row)
            target = dialog.selected_target()
            check("选中窗口后得到窗口目标", target is not None and target.type == "window",
                  target.describe() if target else "None")
            expected_hwnd = own_title_windows[0].hwnd
            check("目标 hwnd 与真实窗口一致",
                  target is not None and target.window_hwnd == expected_hwnd,
                  f"{target.window_hwnd if target else None} vs {expected_hwnd}")
            check("屏幕选择栏的选中被自动清掉（只允许一个高亮）",
                  not dialog.monitor_list.selectedItems() and dialog.monitor_list.currentRow() == -1,
                  f"选中 {len(dialog.monitor_list.selectedItems())} 项")
            check("只有窗口栏打了「已选中」标记",
                  "已选中" in dialog.window_group.title() and "已选中" not in dialog.monitor_group.title(),
                  f"{dialog.window_group.title()} / {dialog.monitor_group.title()}")
            check("选中项自带底色（列表失焦也看得出来）",
                  dialog.window_list.item(target_row).background().color().name()
                  == SELECTED_BG.name(),
                  dialog.window_list.item(target_row).background().color().name())
            pixmap = dialog.preview_label.pixmap()
            check("预览栏显示选中窗口的画面",
                  pixmap is not None and not pixmap.isNull() and pixmap.width() > 100,
                  f"{pixmap.width()}x{pixmap.height()}" if pixmap and not pixmap.isNull() else "无")
            check("预览栏说明写着当前选中的目标",
                  TITLE in dialog.preview_caption.text() and "原始" in dialog.preview_caption.text(),
                  dialog.preview_caption.text())

            monitor_row = 0
            dialog.monitor_list.setCurrentRow(monitor_row)
            monitor_target = dialog.selected_target()
            check("屏幕选择栏能选出显示器目标",
                  monitor_target is not None and monitor_target.type == "monitor"
                  and monitor_target.monitor_index == 1,
                  monitor_target.describe() if monitor_target else "None")
            check("窗口栏的选中被自动清掉（反方向也一样）",
                  not dialog.window_list.selectedItems() and dialog.window_list.currentRow() == -1,
                  f"选中 {len(dialog.window_list.selectedItems())} 项")
            check("后点的屏幕赢，不会被窗口抢走",
                  monitor_target is not None and monitor_target.type == "monitor")
            check("「已选中」标记转到屏幕栏",
                  "已选中" in dialog.monitor_group.title() and "已选中" not in dialog.window_group.title())
            check("预览栏跟着换成显示器画面",
                  "显示器 #1" in dialog.preview_caption.text(), dialog.preview_caption.text())

            print("\n[P3] 预览内容 = 真抓到的那个目标")
            import numpy as np

            grabbed = RangeDialog._grab(CaptureTarget(type="window", monitor_index=None,
                                                     window_hwnd=expected_hwnd,
                                                     window_name=TITLE))
            magenta = float(np.mean(np.all(np.abs(grabbed.astype(int)
                                                  - np.array([255, 0, 255])) <= 8, axis=-1)))
            check("预览抓的就是该窗口的画面（品红底色）", magenta > 0.4,
                  f"品红占比 {magenta:.1%}，{grabbed.shape[1]}x{grabbed.shape[0]}")

            print("\n[P3] 刷新列表保留选中")
            dialog.btn_refresh.click()
            pump(app, 0.3)
            kept = dialog.selected_target()
            check("刷新列表后仍保持选中的显示器",
                  kept is not None and kept.type == "monitor" and kept.monitor_index == 1,
                  kept.describe() if kept else "None")
            dialog.close()

            window.apply_target(target)
            pump(app, 0.3)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            check("切换目标已写入配置", saved["capture"]["target"]["window_hwnd"] == expected_hwnd,
                  str(saved["capture"]["target"]))
            check("状态栏显示当前捕获目标", "窗口" in window.lbl_status_target.text(),
                  window.lbl_status_target.text())

            print("\n[P2] 开始捕获 → 实时预览 + 落盘 + 状态联动")
            jiggler.start()
            window.btn_toggle.click()
            check("按钮切换为「停止捕获」", wait_until(app, lambda: window.btn_toggle.text() == "停止捕获", 5, "按钮文本"),
                  window.btn_toggle.text())
            got_preview = wait_until(app, lambda: window.preview.has_image(), 15, "预览第一帧")
            check("预览收到画面", got_preview,
                  f"原始尺寸 {window.preview.image_size()}" if got_preview else "")
            got_shot = wait_until(app, lambda: store.count() >= 2, 15, "落盘两张")
            check("捕获已落盘（≥2 张）", got_shot, f"实际 {store.count()} 张")
            check("状态指示器显示「捕获中」", "捕获中" in window.lbl_status_capture.text(),
                  window.lbl_status_capture.text())
            check("缓存计数写进状态栏", "上限 20" in window.lbl_status_cache.text(),
                  window.lbl_status_cache.text())

            print("\n[P3] 范围框拖拽 → 原始像素坐标")
            window.btn_select.setChecked(True)
            pump(app, 0.2)
            check("「范围选择」进入编辑态", window.preview.is_editable()
                  and window.btn_select.text() == "范围选择中…", window.btn_select.text())
            preview = window.preview
            source_w, source_h = preview.image_size()
            want = QRect(int(source_w * 0.2), int(source_h * 0.2),
                         int(source_w * 0.4), int(source_h * 0.4))
            view = preview.image_to_view(want)
            QTest.mousePress(preview, Qt.MouseButton.LeftButton, pos=view.topLeft())
            pump(app, 0.1)
            QTest.mouseMove(preview, QPoint(view.right(), view.bottom()))
            pump(app, 0.1)
            QTest.mouseRelease(preview, Qt.MouseButton.LeftButton, pos=QPoint(view.right(), view.bottom()))
            pump(app, 0.3)
            crop = config.capture.crop
            ok = abs(crop.x - want.x()) <= 4 and abs(crop.y - want.y()) <= 4 \
                and abs(crop.width - want.width()) <= 6 and abs(crop.height - want.height()) <= 6
            check("拖拽结果按原始像素写入配置", ok,
                  f"配置 {crop.x},{crop.y} {crop.width}x{crop.height} vs 期望 {want.x()},{want.y()} {want.width()}x{want.height()}")
            check("范围框启用", crop.enabled)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            check("范围框已落盘到 settings.json",
                  saved["capture"]["crop"]["enabled"] and saved["capture"]["crop"]["width"] == crop.width,
                  str(saved["capture"]["crop"]))
            check("预览控件持有同一个范围框",
                  preview.crop_rect() is not None and preview.crop_rect().width() == crop.width,
                  str(preview.crop_rect()))
            check("状态栏显示范围尺寸", "范围" in window.lbl_status_cache.text())

            print("\n[P2] 设置项热更新")
            window.spin_interval.setValue(3)
            pump(app, 0.3)
            check("间隔热更新到引擎", engine.interval_seconds == 3, str(engine.interval_seconds))
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            check("间隔已落盘", saved["capture"]["interval_seconds"] == 3,
                  str(saved["capture"]["interval_seconds"]))
            window.spin_port.setValue(12000)
            pump(app, 0.3)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            check("端口已落盘", saved["server"]["port"] == 12000, str(saved["server"]["port"]))
            window.spin_port.setValue(9178)
            pump(app, 0.2)

            print("\n[P2] web 状态指示器（P4 起由 web 服务驱动）")
            window.set_web_state(True, "http://127.0.0.1:9178")
            pump(app, 0.2)
            text = window.lbl_status_web.text()
            check("显示「已启动」并带地址", "已启动" in text and "127.0.0.1:9178" in text, text)
            window.set_web_state(False)
            pump(app, 0.2)
            check("回到「未启动」", "未启动" in window.lbl_status_web.text(), window.lbl_status_web.text())

            print("\n[P2] 停止捕获")
            window.btn_toggle.click()
            stopped = wait_until(app, lambda: not engine.is_running, 8, "引擎停止")
            check("引擎已停止", stopped)
            check("按钮回到「开始捕获」", window.btn_toggle.text() == "开始捕获", window.btn_toggle.text())
            check("状态指示器回到「已停止」", "已停止" in window.lbl_status_capture.text())

            print("\n[P3] 「捕获范围」按钮 → 弹窗 → 确定（带模态框的真实链路）")
            seen: dict = {}

            def drive_dialog() -> None:
                modal = app.activeModalWidget()
                if modal is None:
                    QTimer.singleShot(200, drive_dialog)
                    return
                seen["title"] = modal.windowTitle()
                seen["rows"] = modal.monitor_list.count()
                modal.monitor_list.setCurrentRow(0)
                seen["caption"] = modal.preview_caption.text()
                seen["windowSelected"] = bool(modal.window_list.selectedItems())
                modal.btn_ok.click()

            QTimer.singleShot(300, drive_dialog)
            window.btn_range.click()
            pump(app, 1.0)
            check("「捕获范围」按钮打开的确实是范围弹窗", seen.get("title") == "捕获范围", str(seen))
            check("弹窗里显示器栏有内容", seen.get("rows", 0) >= 1, f"{seen.get('rows')} 块")
            check("点显示器后窗口栏没有残留选中",
                  seen.get("windowSelected") is False, str(seen))
            check("预览栏跟着显示选中目标的画面",
                  "当前选中：显示器 #1" in str(seen.get("caption")), str(seen.get("caption")))
            check("确定后捕获目标切到显示器 #1",
                  window.config.capture.target.type == "monitor"
                  and window.config.capture.target.monitor_index == 1,
                  window.config.capture.target.describe())
            check("状态栏同步为显示器目标", "显示器" in window.lbl_status_target.text(),
                  window.lbl_status_target.text())
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            check("新目标已落盘到 settings.json", saved["capture"]["target"]["type"] == "monitor",
                  str(saved["capture"]["target"]))

            print("\n[P2] 关闭窗口不抛异常")
            window.close()
            pump(app, 0.3)
            check("窗口已关闭", not window.isVisible())
            window = None

            shots = store.count()
            print(f"\n（本次共落盘 {shots} 张，缓存目录：{store.directory}）")
    finally:
        jiggler.stop()
        if engine is not None:
            engine.stop()
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
