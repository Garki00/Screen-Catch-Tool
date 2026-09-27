"""日志：控制台 + 文件。

托盘程序（P6）没有控制台，所有故障必须能在文件里查到，因此文件日志是默认行为。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .paths import logs_dir

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", log_file: str | None = "logs/screencatch.log") -> Path | None:
    """配置根日志器，返回日志文件路径（未启用文件日志则返回 None）。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))

    # 重复调用（测试里常见）不要叠加 handler
    for handler in list(root.handlers):
        if getattr(handler, "_sct_managed", False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    if sys.stdout is not None:
        # 打包成窗口程序（--noconsole）后 sys.stdout 是 None，此时不要建控制台 handler，
        # 否则每条日志都会触发一次「Logging error」，而文件日志仍然要正常写。
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(formatter)
        stream._sct_managed = True  # type: ignore[attr-defined]
        try:  # 控制台编码不是 UTF-8 时（GBK 终端）不要让日志把程序打死
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        root.addHandler(stream)

    path: Path | None = None
    if log_file:
        path = Path(log_file)
        if not path.is_absolute():
            path = logs_dir().parent / path
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler._sct_managed = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)

    return path
