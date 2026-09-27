"""Screen Capture Tool - 实时屏幕捕获工具。

分包结构（见 docs/项目流程.md）：

- ``app.config``        配置读写（``config/settings.json``）
- ``app.paths``         路径解析（开发态 / 打包后一致）
- ``app.logging_setup`` 日志
- ``app.core.capture``  捕获内核（Windows Graphics Capture）
- ``app.core.storage``  截图缓存（20 张 + 1 张钉住的预览图）
- ``app.core.bus``      进程内事件总线

当前进度：P0（工程骨架）、P1（捕获内核）已完成，尚无 UI 与 web 层。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
