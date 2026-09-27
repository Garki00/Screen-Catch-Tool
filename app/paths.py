"""路径解析。

计划中的路径（``./cache/screenshots/``、``config/settings.json``）都是相对路径。
开发态下相对**项目根目录**解析，打包后相对 exe 所在目录解析，这样无论从哪个
工作目录启动行为都一致。
"""

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    """项目根目录：打包后为 exe 所在目录，开发态为 ``app`` 包的上级目录。"""
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_file() -> Path:
    """配置文件：``config/settings.json``。"""
    return project_root() / "config" / "settings.json"


def cache_root() -> Path:
    """缓存根目录：``cache/``。"""
    return project_root() / "cache"


def web_cache_dir() -> Path:
    """web 端专用产物目录：``cache/web/``（裁剪后的最新视图等）。"""
    return cache_root() / "web"


def web_package_dir() -> Path:
    """web 层代码目录：``app/web/``。"""
    return Path(__file__).resolve().parent / "web"


def web_static_dir() -> Path:
    """静态资源：``app/web/static/``。"""
    return web_package_dir() / "static"


def web_templates_dir() -> Path:
    """模板：``app/web/templates/``。"""
    return web_package_dir() / "templates"


def screenshots_dir() -> Path:
    """截图目录：``cache/screenshots/``（计划指定的存储路径）。"""
    return cache_root() / "screenshots"


def logs_dir() -> Path:
    """日志目录：``logs/``。"""
    return project_root() / "logs"


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回它。"""
    path.mkdir(parents=True, exist_ok=True)
    return path
