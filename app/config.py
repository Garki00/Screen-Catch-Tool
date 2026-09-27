"""配置读写：``config/settings.json``。

设计要点：

- 每个字段都有默认值，文件缺失/字段缺失/类型不对都不会让程序起不来；
- 写入前做校验（间隔夹到 2-10 秒，端口必须落在 1024-65535）；
- 文件损坏时备份为 ``settings.json.bak`` 再重建默认配置，不静默丢数据。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from .paths import config_file

LOG = logging.getLogger(__name__)

# 计划约束：捕获间隔 2-10 秒，默认 5 秒
MIN_INTERVAL_SECONDS = 2
MAX_INTERVAL_SECONDS = 10
DEFAULT_INTERVAL_SECONDS = 5

# 计划约束：web 端口默认 9178，<1024 与 >65535 非法
MIN_PORT = 1024
MAX_PORT = 65535
DEFAULT_PORT = 9178

DEFAULT_MAX_SCREENSHOTS = 20  # 本地缓存最多 20 张截图
SUPPORTED_FORMATS = ("jpeg", "jpg")


class ConfigError(ValueError):
    """配置值非法。"""


def clamp_interval(seconds: Any) -> int:
    """把捕获间隔夹到 [2, 10] 秒，非法值回退到默认 5 秒。"""
    try:
        value = int(round(float(seconds)))
    except (TypeError, ValueError):
        LOG.warning("捕获间隔 %r 无法解析，回退默认 %d 秒", seconds, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    clamped = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, value))
    if clamped != value:
        LOG.warning("捕获间隔 %s 超出 [%d, %d]，已夹到 %d",
                    value, MIN_INTERVAL_SECONDS, MAX_INTERVAL_SECONDS, clamped)
    return clamped


def validate_port(port: Any) -> int:
    """校验 web 端口，返回合法端口，否则抛 ConfigError。"""
    try:
        value = int(port)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"端口必须是整数：{port!r}") from exc
    if not (MIN_PORT <= value <= MAX_PORT):
        raise ConfigError(f"端口 {value} 非法：必须在 {MIN_PORT}-{MAX_PORT} 之间")
    return value


@dataclass
class CaptureTarget:
    """捕获目标：显示器或窗口（二选一，捕获库内部会清掉 monitor_index）。"""

    type: Literal["monitor", "window"] = "monitor"
    monitor_index: int | None = 1  # 捕获库要求从 1 开始（1 = 主显示器，0 会报错）
    window_hwnd: int | None = None
    window_name: str | None = None

    def describe(self) -> str:
        if self.type == "window":
            if self.window_hwnd is not None:
                return f"窗口 hwnd={self.window_hwnd}"
            return f"窗口 标题包含 {self.window_name!r}"
        return f"显示器 #{self.monitor_index if self.monitor_index is not None else 1}"

    def is_valid(self) -> tuple[bool, str]:
        if self.type == "window":
            if self.window_hwnd is None and not self.window_name:
                return False, "窗口捕获需要 window_hwnd 或 window_name"
            return True, ""
        if self.monitor_index is None:
            return False, "显示器捕获需要 monitor_index"
        if self.monitor_index < 1:
            return False, f"monitor_index 从 1 开始（1 = 主显示器，收到了 {self.monitor_index}）"
        return True, ""


@dataclass
class CropRect:
    """web 端裁剪框：只影响推给 web 的图，落盘始终是原始全尺寸。"""

    enabled: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    def as_tuple(self) -> tuple[int, int, int, int]:
        """返回 ``(start_width, start_height, end_width, end_height)``，可直接给 ``Frame.crop``。"""
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass
class CaptureConfig:
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    cursor_capture: bool = True
    minimum_update_interval_ms: int = 100
    preview_enabled: bool = True
    preview_interval_ms: int = 400          # 预览刷新间隔（与落盘间隔解耦，避免 UI 卡）
    preview_max_width: int = 960            # 预览图缩放后的最大宽度（原始像素仍全尺寸落盘）
    heartbeat_fallback: bool = True         # 画面静止（WGC 不出帧）时用 GDI 兜底抓一帧
    target: CaptureTarget = field(default_factory=CaptureTarget)
    crop: CropRect = field(default_factory=CropRect)


@dataclass
class CacheConfig:
    max_screenshots: int = DEFAULT_MAX_SCREENSHOTS
    format: str = "jpeg"
    quality: int = 85
    directory: str = "cache/screenshots"


@dataclass
class ServerConfig:
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"  # 只监听本机；要让手机访问改成 0.0.0.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "logs/screencatch.log"


@dataclass
class AppConfig:
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # ---------- 序列化 ----------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        if not isinstance(raw, dict):
            raise ConfigError("配置根节点必须是对象")
        cfg = cls()
        cfg.capture = _build(CaptureConfig, raw.get("capture"))
        cfg.cache = _build(CacheConfig, raw.get("cache"))
        cfg.server = _build(ServerConfig, raw.get("server"))
        cfg.logging = _build(LoggingConfig, raw.get("logging"))
        cfg.normalize()
        return cfg

    def normalize(self) -> None:
        """夹取 / 校验所有受限字段，使内存中的配置始终合法。"""
        self.capture.interval_seconds = clamp_interval(self.capture.interval_seconds)
        self.capture.minimum_update_interval_ms = max(0, int(self.capture.minimum_update_interval_ms))
        self.capture.cursor_capture = _as_bool(self.capture.cursor_capture, True)
        self.capture.preview_enabled = _as_bool(self.capture.preview_enabled, True)
        self.capture.heartbeat_fallback = _as_bool(self.capture.heartbeat_fallback, True)
        self.capture.preview_interval_ms = max(100, min(5000, _as_int(self.capture.preview_interval_ms, 400)))
        self.capture.preview_max_width = max(160, min(3840, _as_int(self.capture.preview_max_width, 960)))
        self.capture.crop.enabled = _as_bool(self.capture.crop.enabled, False)
        for name in ("x", "y", "width", "height"):
            setattr(self.capture.crop, name, max(0, _as_int(getattr(self.capture.crop, name))))
        target = self.capture.target
        if str(target.type).lower() not in {"monitor", "window"}:
            LOG.warning("未知捕获目标类型 %r，回退 monitor", target.type)
            target.type = "monitor"
        if target.type == "monitor":
            index = _as_int(target.monitor_index, 1)
            if index < 1:  # 捕获库要求 monitor_index >= 1
                LOG.warning("monitor_index %s 非法（从 1 开始），回退主显示器 #1", target.monitor_index)
                index = 1
            target.monitor_index = index
            target.window_hwnd = None
            target.window_name = None
        elif target.window_hwnd is not None:
            target.window_hwnd = _as_int(target.window_hwnd, 0) or None
        self.cache.max_screenshots = max(1, _as_int(self.cache.max_screenshots, DEFAULT_MAX_SCREENSHOTS))
        self.cache.quality = max(1, min(100, _as_int(self.cache.quality, 85)))
        if str(self.cache.format).lower() not in SUPPORTED_FORMATS:
            LOG.warning("不支持的图片格式 %r，回退 jpeg", self.cache.format)
            self.cache.format = "jpeg"
        try:
            self.server.port = validate_port(self.server.port)
        except ConfigError as exc:
            LOG.warning("%s，回退默认端口 %d", exc, DEFAULT_PORT)
            self.server.port = DEFAULT_PORT
        self.logging.level = str(self.logging.level).upper()

    # ---------- 磁盘 IO ----------

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """读取配置；文件不存在时写入默认配置。"""
        path = Path(path) if path else config_file()
        if not path.exists():
            cfg = cls()
            cfg.normalize()
            cfg.save(path)
            LOG.info("已生成默认配置：%s", path)
            return cfg
        try:
            text = path.read_text(encoding="utf-8")
            cfg = cls.from_dict(json.loads(text))
            if text != _dump(cfg):
                path.write_text(_dump(cfg), encoding="utf-8")
                LOG.info("配置已规范化并回写：%s", path)
        except (OSError, ValueError, ConfigError) as exc:
            backup = path.with_suffix(path.suffix + ".bak")
            LOG.error("配置文件解析失败（%s），已备份到 %s 并重建默认配置", exc, backup)
            try:
                path.replace(backup)
            except OSError:
                LOG.warning("备份 %s 失败，继续重建", path)
            cfg = cls()
            cfg.normalize()
            cfg.save(path)
        return cfg

    def save(self, path: Path | None = None) -> Path:
        """写回配置，返回写入路径。"""
        path = Path(path) if path else config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.normalize()
        path.write_text(_dump(self), encoding="utf-8")
        return path


def _dump(cfg: "AppConfig") -> str:
    """统一的 JSON 序列化格式（ensure_ascii=False，缩进 2）。"""
    return json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False) + "\n"


_NESTED_FIELDS: dict[str, type] = {"target": CaptureTarget, "crop": CropRect}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        LOG.warning("配置值 %r 不是整数，回退 %d", value, default)
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off", ""}:
            return False
    LOG.warning("配置值 %r 不是布尔值，回退 %s", value, default)
    return default


def _build(cls: type, raw: Any):
    """按数据类字段构造子配置：未知键丢弃、缺失键用默认值、嵌套字段递归。"""
    if not isinstance(raw, dict):
        return cls()
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        nested = _NESTED_FIELDS.get(f.name)
        kwargs[f.name] = _build(nested, raw[f.name]) if nested else raw[f.name]
    return cls(**kwargs)
