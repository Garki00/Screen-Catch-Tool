"""程序装配与命令行入口。

- ``python -m app``             启动桌面端界面（P2/P3）
- ``python -m app --selfcheck`` 自检：打印版本/路径、生成默认配置（P0 验收）
- ``python -m app --capture``   无界面跑捕获内核，用于验证 P1（落盘/间隔/缓存上限）

托盘（P6）与 web 层（P4）接入后仍从这里装配。
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import importlib.util
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import AppConfig, CaptureTarget, ConfigError
from .core.bus import TOPIC_CAPTURE_ERROR, TOPIC_CAPTURE_CLOSED, TOPIC_SHOT_SAVED, EventBus
from .core.jiggle import CursorJiggler
from .core.storage import ScreenshotStore
from .logging_setup import setup_logging
from .paths import config_file, logs_dir, project_root, screenshots_dir

LOG = logging.getLogger("app.main")


@dataclass
class AppContext:
    """装配好的运行时对象，UI/web 层复用同一份。"""

    config: AppConfig
    store: ScreenshotStore
    bus: EventBus
    config_path: Path | None = None


def build(config_path: Path | None = None) -> AppContext:
    """读取配置并装配缓存与事件总线。"""
    config = AppConfig.load(config_path)
    setup_logging(config.logging.level, config.logging.file)
    store = ScreenshotStore(
        directory=screenshots_dir(),
        max_screenshots=config.cache.max_screenshots,
        image_format=config.cache.format,
        quality=config.cache.quality,
    )
    return AppContext(config=config, store=store, bus=EventBus(),
                      config_path=config_path or config_file())


def _capture_library_version() -> str | None:
    if importlib.util.find_spec("windows_capture") is None:
        return None
    try:
        return metadata.version("windows-capture")
    except metadata.PackageNotFoundError:
        return "未知版本"


def run_selfcheck(config_path: Path | None = None) -> int:
    """P0 验收：打印版本与关键路径，缺失的默认配置会被生成。"""
    ctx = build(config_path)
    cfg = ctx.config
    lib = _capture_library_version()
    cfg_path = config_path or config_file()

    print(f"Screen Capture Tool {__version__}")
    print(f"项目根目录 : {project_root()}")
    print(f"配置文件   : {cfg_path}  ({'已存在' if cfg_path.exists() else '缺失'})")
    print(f"截图目录   : {screenshots_dir()}")
    print(f"日志文件   : {logs_dir() / Path(cfg.logging.file).name}")
    print(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    print(f"捕获库     : {'windows-capture ' + lib if lib else '未安装（P1 需要）'}")
    print(f"捕获间隔   : {cfg.capture.interval_seconds} 秒（允许 2-10）")
    print(f"捕获目标   : {cfg.capture.target.describe()}")
    print(f"缓存上限   : {cfg.cache.max_screenshots} 张 + 1 张预览")
    print(f"缓存现状   : {ctx.store.count()} 张，{ctx.store.stats()['totalBytes'] / 1024:.1f} KB")
    print(f"web 端口   : {cfg.server.port}（界面启动时会在此端口拉起 web 服务）")
    print("状态       : P0-P7 全部就绪（骨架 / 捕获内核 / 桌面界面 / 范围选择 / web 服务 / "
          "web 前端 / 系统托盘 / 打包）")
    LOG.info("自检完成：配置=%s 截图目录=%s", cfg_path, screenshots_dir())
    return 0


def parse_target(text: str) -> CaptureTarget:
    """解析 ``--target``：``monitor:0`` / ``window:0x1234`` / ``window:标题``。"""
    kind, _, value = text.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if kind == "monitor":
        if not value:
            return CaptureTarget(type="monitor", monitor_index=1)
        try:
            index = int(value)
        except ValueError as exc:
            raise ConfigError(f"显示器索引非法：{value!r}") from exc
        if index < 1:
            raise ConfigError(f"显示器索引从 1 开始（1 = 主显示器），收到 {index}")
        return CaptureTarget(type="monitor", monitor_index=index)
    if kind == "window":
        if not value:
            raise ConfigError("window 目标需要 hwnd 或标题，例如 window:0x1A2B 或 window:记事本")
        try:
            hwnd = int(value, 0)
        except ValueError:
            return CaptureTarget(type="window", monitor_index=None, window_name=value)
        return CaptureTarget(type="window", monitor_index=None, window_hwnd=hwnd)
    raise ConfigError(f"无法识别的目标 {text!r}，应为 monitor:N 或 window:...（或 window:标题）")


def run_capture(
    seconds: float = 0.0,
    interval: int | None = None,
    target_text: str | None = None,
    jiggle: bool = False,
    config_path: Path | None = None,
) -> int:
    """无界面运行捕获内核，用于 P1 验收。"""
    from .core.capture import CaptureEngine, CaptureError

    ctx = build(config_path)
    if interval is not None:
        ctx.config.capture.interval_seconds = interval
        ctx.config.normalize()
    if target_text:
        ctx.config.capture.target = parse_target(target_text)

    engine = CaptureEngine(ctx.config, ctx.store, ctx.bus)

    def on_shot(shot) -> None:  # noqa: ANN001
        print(f"[截图] {shot.name}  {shot.width}x{shot.height}  {shot.size_bytes / 1024:.1f} KB")

    def on_error(error: str) -> None:
        print(f"[错误] {error}")

    def on_closed(target: str) -> None:
        print(f"[关闭] 捕获会话结束：{target}")

    ctx.bus.subscribe(TOPIC_SHOT_SAVED, on_shot)
    ctx.bus.subscribe(TOPIC_CAPTURE_ERROR, on_error)
    ctx.bus.subscribe(TOPIC_CAPTURE_CLOSED, on_closed)

    jiggler = CursorJiggler()
    if jiggle:
        print("[自检] 已启用光标抖动，用于驱动画面变化")
        if not jiggler.start():
            print("[自检] 光标抖动不可用（仅 Windows 支持）")

    print(f"[启动] 目标={ctx.config.capture.target.describe()} 间隔={ctx.config.capture.interval_seconds}s "
          f"缓存上限={ctx.store.max_screenshots} 目录={ctx.store.directory}")
    try:
        engine.start()
    except CaptureError as exc:
        print(f"[失败] {exc}")
        jiggler.stop()
        return 2

    deadline = time.monotonic() + seconds if seconds and seconds > 0 else None
    try:
        while deadline is None or time.monotonic() < deadline:
            if not engine.is_running:
                print("[提示] 捕获线程已结束")
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[中断] 收到 Ctrl-C，正在停止…")
    finally:
        jiggler.stop()
        engine.stop()

    snap = engine.snapshot()
    print(f"[统计] 回调帧={snap['frames_seen']} 落盘={snap['shots_saved']} "
          f"总量={snap['count']} 张 / {snap['totalBytes'] / 1024:.1f} KB 上限={snap['maxScreenshots']}")
    if snap["last_error"]:
        print(f"[错误] {snap['last_error']}")
    return 0 if snap["shots_saved"] > 0 else 1


def run_gui(config_path: Path | None = None, autostart: bool = False, jiggle: bool = False,
            web: bool = True, port: int | None = None) -> int:
    """启动桌面端界面（P2/P3）并拉起 web 服务（P4/P5）。"""
    from PySide6.QtWidgets import QApplication

    from .core.capture import CaptureEngine
    from .ui.engine_bridge import EngineBridge
    from .ui.main_window import MainWindow

    ctx = build(config_path)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Screen Capture Tool")
    # 关窗口只是隐藏到托盘，程序不退出（P6）
    app.setQuitOnLastWindowClosed(False)

    engine = CaptureEngine(ctx.config, ctx.store, ctx.bus)
    bridge = EngineBridge(ctx.bus)

    web_service = None
    if web:
        from .web.server import WebService

        if port is not None:
            ctx.config.server.port = port
            ctx.config.save(ctx.config_path)
        web_service = WebService(ctx.config, ctx.store, ctx.bus, engine=engine)

    window = MainWindow(ctx.config, ctx.store, engine, bridge, config_path=ctx.config_path,
                        web_service=web_service)
    window.show()

    if web_service is not None:
        window.start_web_service()

    jiggler = CursorJiggler()
    if jiggle:
        jiggler.start()
    if autostart:
        window.start_capture()

    LOG.info("界面已启动：目标=%s 间隔=%d 秒 端口=%d web=%s",
             ctx.config.capture.target.describe(), ctx.config.capture.interval_seconds,
             ctx.config.server.port, "开" if web_service else "关")
    try:
        return app.exec()
    finally:
        jiggler.stop()
        engine.stop()
        if web_service is not None:
            web_service.stop()
        bridge.detach()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="Screen Capture Tool（默认启动桌面端界面）",
    )
    parser.add_argument("--config", type=Path, default=None, help="配置文件路径（默认 config/settings.json）")
    parser.add_argument("--gui", action="store_true", help="启动桌面端界面（默认行为，可省略）")
    parser.add_argument("--selfcheck", action="store_true", help="只做自检：打印版本/路径并生成默认配置")
    parser.add_argument("--autostart", action="store_true", help="界面启动后立即开始捕获")
    parser.add_argument("--capture", action="store_true", help="无界面运行捕获内核（P1 验证）")
    parser.add_argument("--seconds", type=float, default=0.0, help="捕获运行时长，0 表示一直跑到 Ctrl-C")
    parser.add_argument("--interval", type=int, default=None, help="捕获间隔秒数（2-10）")
    parser.add_argument("--target", default=None, help="捕获目标：monitor:1 / window:0x1234 / window:标题")
    parser.add_argument("--jiggle", action="store_true", help="抖动光标以驱动画面变化（自检/演示用）")
    parser.add_argument("--no-web", action="store_true", help="界面模式下不启动 web 服务")
    parser.add_argument("--port", type=int, default=None, help="web 端口（1024-65535），启动时覆盖配置")
    args = parser.parse_args(argv)

    if args.capture:
        return run_capture(
            seconds=args.seconds,
            interval=args.interval,
            target_text=args.target,
            jiggle=args.jiggle,
            config_path=args.config,
        )
    if args.selfcheck:
        return run_selfcheck(args.config)
    return run_gui(config_path=args.config, autostart=args.autostart, jiggle=args.jiggle,
                   web=not args.no_web, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
