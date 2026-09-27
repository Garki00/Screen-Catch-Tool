"""截图缓存：磁盘即真相。

规则来自计划：

- 最多保留 20 张截图 + 1 张「正在预览」的历史图片；
- 超出上限自动删除最旧的图片；
- 格式 jpeg，路径 ``./cache/screenshots/``。

「正在预览」的那张通过 ``pin()`` 钉住，删除时跳过。桌面端、web 端、下载按钮共用
同一份文件列表，所以这里只做纯粹的目录管理，不做任何内存缓存。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..paths import screenshots_dir

LOG = logging.getLogger(__name__)

# screenshot_20260921_154512_123_1920x1080.jpeg
NAME_RE = re.compile(
    r"^screenshot_(?P<date>\d{8})_(?P<time>\d{6})_(?P<ms>\d{3})(?:_(?P<w>\d+)x(?P<h>\d+))?\.(?P<ext>jpe?g)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Shot:
    """一张已落盘的截图。"""

    name: str
    path: Path
    size_bytes: int
    mtime: float
    width: int | None = None
    height: int | None = None

    @property
    def created_at(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    @property
    def is_readable(self) -> bool:
        return self.path.is_file() and self.size_bytes > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sizeBytes": self.size_bytes,
            "createdAt": self.created_at.isoformat(timespec="seconds"),
            "width": self.width,
            "height": self.height,
        }


class ScreenshotStore:
    """``cache/screenshots/`` 的管理者（线程安全）。"""

    def __init__(
        self,
        directory: Path | None = None,
        max_screenshots: int = 20,
        image_format: str = "jpeg",
        quality: int = 85,
    ) -> None:
        self.directory = Path(directory) if directory else screenshots_dir()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_screenshots = max(1, int(max_screenshots))
        self.image_format = "jpeg" if str(image_format).lower() not in {"jpg", "jpeg"} else "jpeg"
        self.quality = max(1, min(100, int(quality)))
        self._lock = threading.RLock()
        self._pinned: set[str] = set()
        self._reserved: set[str] = set()

    # ---------- 写入 ----------

    def build_name(self, when: datetime | None = None, width: int | None = None, height: int | None = None) -> str:
        """生成唯一文件名。

        同名判定同时看磁盘和本进程已预留（尚未写盘）的名字：同一毫秒内连续调用不会
        拿到重复的名字。
        """
        when = when or datetime.now()
        stem = when.strftime("screenshot_%Y%m%d_%H%M%S_")
        ms = when.microsecond // 1000
        with self._lock:
            for offset in range(1000):
                suffix = f"_{width}x{height}" if width and height else ""
                candidate = f"{stem}{(ms + offset) % 1000:03d}{suffix}.jpeg"
                if candidate in self._reserved or (self.directory / candidate).exists():
                    continue
                self._reserved.add(candidate)
                return candidate
        raise RuntimeError("同一秒内生成的截图超过 1000 张，文件名无法保证唯一")

    def save(self, image_bgr: Any, width: int | None = None, height: int | None = None,
             name: str | None = None) -> Shot:
        """把 BGR 图像写成 jpeg 并返回 Shot。

        ``image_bgr`` 必须是已经脱离捕获缓冲区的连续数组——``windows-capture`` 给的是
        零拷贝视图，编码必须发生在帧回调内部（见 ``capture.py``）。
        """
        import cv2  # 局部导入：只有真正落盘时才需要 OpenCV

        with self._lock:
            name = name or self.build_name(width=width, height=height)
            self._reserved.discard(name)
            path = self.directory / name
            params = [cv2.IMWRITE_JPEG_QUALITY, self.quality]
            ok = cv2.imwrite(str(path), image_bgr, params)
            if not ok or not path.exists() or path.stat().st_size == 0:
                raise OSError(f"写入截图失败：{path}")
            stat = path.stat()
            return Shot(name=name, path=path, size_bytes=stat.st_size,
                        mtime=stat.st_mtime, width=width, height=height)

    # ---------- 读取 ----------

    def list(self) -> list[Shot]:
        """按时间倒序返回所有截图（最新在前）。"""
        with self._lock:
            shots = [self._to_shot(p) for p in self.directory.glob("screenshot_*.jp*g")]
        return sorted((s for s in shots if s), key=lambda s: s.name, reverse=True)

    def latest(self) -> Shot | None:
        shots = self.list()
        return shots[0] if shots else None

    def count(self) -> int:
        return len(self.list())

    def _to_shot(self, path: Path) -> Shot | None:
        match = NAME_RE.match(path.name)
        try:
            stat = path.stat()
        except OSError:
            return None
        width = int(match.group("w")) if match and match.group("w") else None
        height = int(match.group("h")) if match and match.group("h") else None
        return Shot(name=path.name, path=path, size_bytes=stat.st_size,
                    mtime=stat.st_mtime, width=width, height=height)

    # ---------- 删除策略 ----------

    def pin(self, name: str) -> None:
        """钉住一张图（web 端正在预览），删除时跳过。"""
        with self._lock:
            self._pinned.add(name)

    def unpin(self, name: str) -> None:
        with self._lock:
            self._pinned.discard(name)

    @property
    def pinned(self) -> set[str]:
        with self._lock:
            return set(self._pinned)

    def enforce_limit(self) -> list[str]:
        """删除超出上限的最旧截图，返回被删除的文件名。

        保留规则：非钉住的截图最多 ``max_screenshots`` 张；钉住的图片不计入上限、
        也不被删除（对应计划里的「20 张 + 1 张正在预览的历史图片」）。
        """
        with self._lock:
            shots = self.list()  # 最新在前
            pinned = set(self._pinned)
            deleted: list[str] = []
            kept = 0
            for shot in shots:
                if shot.name in pinned:
                    continue
                kept += 1
                if kept > self.max_screenshots:
                    try:
                        shot.path.unlink()
                        deleted.append(shot.name)
                    except OSError as exc:
                        LOG.warning("删除旧截图 %s 失败：%s", shot.name, exc)
            if deleted:
                LOG.info("缓存上限 %d 张，已删除 %d 张最旧截图：%s",
                         self.max_screenshots, len(deleted), ", ".join(deleted))
            return deleted

    def clear(self, keep_pinned: bool = False) -> int:
        """清空截图目录，返回删除数量。"""
        with self._lock:
            pinned = set(self._pinned) if keep_pinned else set()
            removed = 0
            for shot in self.list():
                if shot.name in pinned:
                    continue
                try:
                    shot.path.unlink()
                    removed += 1
                except OSError as exc:
                    LOG.warning("删除 %s 失败：%s", shot.name, exc)
            return removed

    def stats(self) -> dict[str, Any]:
        shots = self.list()
        total = sum(s.size_bytes for s in shots)
        return {
            "directory": str(self.directory),
            "count": len(shots),
            "pinned": sorted(self.pinned),
            "maxScreenshots": self.max_screenshots,
            "totalBytes": total,
            "latest": shots[0].as_dict() if shots else None,
        }
