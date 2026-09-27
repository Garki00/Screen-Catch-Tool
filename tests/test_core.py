"""P0/P1 可重复验证：配置层、缓存层、事件总线。

不依赖屏幕与捕获库（除落盘用到的 OpenCV），可随时重跑：

    .venv\\Scripts\\python.exe tests\\test_core.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.config import (  # noqa: E402
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_PORT,
    AppConfig,
    ConfigError,
    clamp_interval,
    validate_port,
)
from app.core.bus import EventBus  # noqa: E402
from app.core.storage import ScreenshotStore  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(condition), detail))
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")


def make_image(height: int = 40, width: int = 60, seed: int = 0) -> np.ndarray:
    """造一张有内容的 BGR 图，避免全黑图被 JPEG 压成极小文件。"""
    x = np.linspace(0, 255, width, dtype=np.int32)
    y = np.linspace(0, 255, height, dtype=np.int32)
    grid = (np.add.outer(y, x) + (seed * 7 % 200)) % 256
    return np.stack([grid, (grid * 2) % 256, (grid * 3) % 256], axis=-1).astype(np.uint8)


# ---------------------------------------------------------------- 配置层

def test_config_defaults_and_roundtrip() -> None:
    print("\n[配置] 默认值 / 读写往返 / 容错")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config" / "settings.json"
        cfg = AppConfig.load(path)
        check("文件缺失时自动生成 settings.json", path.exists())
        check("默认间隔 5 秒", cfg.capture.interval_seconds == DEFAULT_INTERVAL_SECONDS)
        check("默认端口 9178", cfg.server.port == DEFAULT_PORT)
        check("默认缓存上限 20 张", cfg.cache.max_screenshots == 20)
        check("默认格式 jpeg", cfg.cache.format == "jpeg")
        check("默认目标为显示器", cfg.capture.target.type == "monitor")

        cfg.capture.interval_seconds = 3
        cfg.cache.quality = 90
        cfg.server.port = 12000
        cfg.capture.target = type(cfg.capture.target)(type="window", monitor_index=None, window_hwnd=4660)
        cfg.save(path)
        again = AppConfig.load(path)
        check("间隔可往返", again.capture.interval_seconds == 3, str(again.capture.interval_seconds))
        check("质量可往返", again.cache.quality == 90)
        check("端口可往返", again.server.port == 12000)
        check("窗口目标可往返", again.capture.target.window_hwnd == 4660,
              str(again.capture.target.window_hwnd))

        # 部分字段文件：其余字段保持默认
        path.write_text(json.dumps({"capture": {"interval_seconds": 7}}), encoding="utf-8")
        partial = AppConfig.load(path)
        check("部分配置：指定字段生效", partial.capture.interval_seconds == 7)
        check("部分配置：其余字段回默认", partial.cache.max_screenshots == 20)

        # 损坏文件：备份 + 重建
        path.write_text("{ 这不是 json", encoding="utf-8")
        broken = AppConfig.load(path)
        check("损坏文件回退默认配置", broken.capture.interval_seconds == DEFAULT_INTERVAL_SECONDS)
        check("损坏文件已备份为 .bak", path.with_suffix(".json.bak").exists())

        # 类型不符：字符串布尔
        path.write_text(json.dumps({"capture": {"cursor_capture": "false"}}), encoding="utf-8")
        coerced = AppConfig.load(path)
        check("字符串布尔被正确解析", coerced.capture.cursor_capture is False)


def test_config_validation() -> None:
    print("\n[配置] 间隔与端口校验")
    check("间隔 0 -> 2", clamp_interval(0) == 2, str(clamp_interval(0)))
    check("间隔 1 -> 2", clamp_interval(1) == 2)
    check("间隔 5 -> 5", clamp_interval(5) == 5)
    check("间隔 11 -> 10", clamp_interval(11) == 10)
    check("间隔 3.7 -> 4", clamp_interval(3.7) == 4)
    check("间隔 'abc' -> 默认 5", clamp_interval("abc") == DEFAULT_INTERVAL_SECONDS)

    check("端口 1024 合法", validate_port(1024) == 1024)
    check("端口 9178 合法", validate_port(9178) == 9178)
    check("端口 65535 合法", validate_port(65535) == 65535)
    for bad in (1023, 65536, 0, -1, "abc"):
        try:
            validate_port(bad)
            check(f"端口 {bad!r} 应被拒绝", False)
        except ConfigError:
            check(f"端口 {bad!r} 被拒绝", True)


# ---------------------------------------------------------------- 缓存层

def test_storage_limit_and_pin() -> None:
    print("\n[缓存] 20 张上限 / 钉住预览 / 文件名与磁盘内容")
    with tempfile.TemporaryDirectory() as tmp:
        store = ScreenshotStore(Path(tmp) / "screenshots", max_screenshots=3, quality=80)
        check("目录被自动创建", store.directory.is_dir())

        base = datetime.now() - timedelta(seconds=10)
        names = []
        for i in range(5):
            shot = store.save(make_image(seed=i), width=60, height=40,
                              name=store.build_name(base + timedelta(seconds=i), 60, 40))
            names.append(shot.name)
            store.enforce_limit()

        check("上限生效后只留 3 张", store.count() == 3, f"实际 {store.count()}")
        remaining = {s.name for s in store.list()}
        check("保留的是最新的 3 张", remaining == set(names[-3:]), str(sorted(remaining)))
        check("最旧的两张已被删除", not any((store.directory / n).exists() for n in names[:2]))
        check("列表按最新在前排序", [s.name for s in store.list()] == names[-3:][::-1])
        check("latest() 指向最后保存的一张", store.latest().name == names[-1])

        latest = store.latest()
        check("落盘文件是合法 JPEG",
              latest.path.read_bytes()[:3] == b"\xff\xd8\xff",
              latest.path.read_bytes()[:3].hex())
        check("文件非空", latest.size_bytes > 0, f"{latest.size_bytes} bytes")
        check("文件名带尺寸信息", latest.width == 60 and latest.height == 40,
              f"{latest.width}x{latest.height}")

        import cv2
        decoded = cv2.imread(str(latest.path))
        check("OpenCV 能正常解码（即图片查看器可打开）",
              decoded is not None and decoded.shape[:2] == (40, 60),
              str(None if decoded is None else decoded.shape))

        # 钉住最旧的一张：它既不被删除，也不占用 20 张的额度
        pinned_name = min(s.name for s in store.list())
        store.pin(pinned_name)
        for i in range(5, 9):
            store.save(make_image(seed=i), width=60, height=40,
                       name=store.build_name(base + timedelta(seconds=i), 60, 40))
            store.enforce_limit()
        names_after = {s.name for s in store.list()}
        check("钉住的图片未被删除", pinned_name in names_after)
        check("非钉住图片仍不超过上限",
              len(names_after - {pinned_name}) <= store.max_screenshots,
              f"非钉住 {len(names_after - {pinned_name})} 张")
        check("总数 = 上限 + 钉住的 1 张", store.count() == store.max_screenshots + 1,
              f"实际 {store.count()}")

        store.unpin(pinned_name)
        store.enforce_limit()
        check("取消钉住后回到上限内", store.count() == store.max_screenshots,
              f"实际 {store.count()}")

        store.pin(store.latest().name)
        removed = store.clear(keep_pinned=True)
        check("清空时保留钉住的图片", store.count() == 1 and removed == store.max_screenshots - 1,
              f"被删 {removed} 张，剩余 {store.count()} 张")


def test_storage_names_unique() -> None:
    print("\n[缓存] 文件名唯一性与解析")
    with tempfile.TemporaryDirectory() as tmp:
        store = ScreenshotStore(Path(tmp) / "shots", max_screenshots=50)
        same_ms = datetime.now()
        generated = {store.build_name(same_ms, 100, 50) for _ in range(3)}
        check("同一毫秒内生成的占位名不重复", len(generated) == 3, str(sorted(generated)))
        name = store.build_name(same_ms, 800, 600)
        shot = store.save(make_image(10, 10), width=800, height=600, name=name)
        check("尺寸写进文件名", "800x600" in shot.name, shot.name)
        parsed = store.list()[0]
        check("从文件名解析尺寸", parsed.width == 800 and parsed.height == 600)


# ---------------------------------------------------------------- 事件总线

def test_bus() -> None:
    print("\n[总线] 订阅/分发/异常隔离")
    bus = EventBus()
    seen: list = []

    def good(**payload):  # noqa: ANN003
        seen.append(payload)

    def bad(**payload):  # noqa: ANN003
        raise RuntimeError("订阅者故意报错")

    unsubscribe = bus.subscribe("shot.saved", good)
    bus.subscribe("shot.saved", bad)
    delivered = bus.publish("shot.saved", shot="x")
    check("订阅者收到事件", seen == [{"shot": "x"}])
    check("异常订阅者不影响分发", delivered == 1, f"投递 {delivered} 个")
    unsubscribe()
    bus.publish("shot.saved", shot="y")
    check("取消订阅后不再收到", seen == [{"shot": "x"}])


def main() -> int:
    tests = [
        test_config_defaults_and_roundtrip,
        test_config_validation,
        test_storage_limit_and_pin,
        test_storage_names_unique,
        test_bus,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  异常：{test.__name__}")
            traceback.print_exc()
    total = len(RESULTS)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== 断言 {passed}/{total} 通过，测试函数异常 {failed} 个 ==")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  未通过：{name} {detail}")
    return 0 if passed == total and failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
