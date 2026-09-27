"""核对 cache/screenshots/ 里的一批截图：格式、尺寸、间隔、数量上限、非黑屏。

注意：间隔断言只在「目录里就是同一次运行的产物」时成立，先清空再跑：

    rm -rf cache/screenshots
    .venv\\Scripts\\python.exe -m app --capture --seconds 14 --interval 3 --jiggle
    .venv\\Scripts\\python.exe tests\\verify_shots.py --interval 3 --max 20
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from app.core.storage import ScreenshotStore  # noqa: E402

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  -> ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "cache" / "screenshots"))
    parser.add_argument("--interval", type=float, default=2.0, help="设定间隔（秒）")
    parser.add_argument("--max", type=int, default=20, help="缓存上限")
    parser.add_argument("--tolerance", type=float, default=0.6, help="间隔允许的抖动（秒）")
    args = parser.parse_args()

    store = ScreenshotStore(Path(args.dir), max_screenshots=args.max)
    shots = store.list()
    print(f"目录：{store.directory}")
    print(f"数量：{len(shots)} 张（上限 {args.max}）\n")
    if not shots:
        print("FAIL  目录里没有截图")
        return 1

    check("数量不超过缓存上限", len(shots) <= args.max, f"{len(shots)} <= {args.max}")

    ordered = sorted(shots, key=lambda s: s.name)  # 旧 -> 新
    gaps = []
    for previous, current in zip(ordered, ordered[1:]):
        gaps.append((current.mtime - previous.mtime))

    too_close = [round(g, 2) for g in gaps if g < args.interval - args.tolerance]
    check(f"相邻截图间隔 >= {args.interval}s（容差 {args.tolerance}s）", not too_close,
          f"最小 {min(gaps):.2f}s 最大 {max(gaps):.2f}s" if gaps else "只有一张")

    shapes = set()
    for shot in shots:
        raw = shot.path.read_bytes()[:3]
        if raw != b"\xff\xd8\xff":
            FAILURES.append(f"{shot.name} 不是 JPEG")
        image = cv2.imread(str(shot.path))
        if image is None:
            FAILURES.append(f"{shot.name} 无法解码")
            continue
        shapes.add(image.shape)
        if shot.width and (image.shape[1], image.shape[0]) != (shot.width, shot.height):
            FAILURES.append(f"{shot.name} 实际尺寸与文件名不符")
    check("全部文件是合法且可解码的 JPEG", not [f for f in FAILURES if "JPEG" in f or "解码" in f],
          f"{len(shots)} 个文件")
    check("所有截图尺寸一致", len(shapes) == 1, str(shapes))
    check("尺寸与文件名一致", not [f for f in FAILURES if "尺寸" in f])

    # 防「捕获失败拿到全黑帧」：截图必须有真实的亮度分布
    newest = cv2.imread(str(shots[0].path))
    mean = float(newest.mean())
    std = float(newest.std())
    distinct = len(np.unique(newest.reshape(-1, 3)[::97], axis=0))
    check("画面不是全黑（捕获到了真实内容）", mean > 5 and std > 5,
          f"平均亮度 {mean:.1f}，标准差 {std:.1f}")  # noqa: E231
    check("颜色分布有内容（非纯色）", distinct > 50, f"抽样到的不同颜色 {distinct} 种")

    first = datetime.fromtimestamp(ordered[0].mtime).strftime("%H:%M:%S")
    last = datetime.fromtimestamp(ordered[-1].mtime).strftime("%H:%M:%S")
    span = ordered[-1].mtime - ordered[0].mtime
    print(f"\n时间跨度 {first} → {last}（{span:.1f}s），最新一张：{shots[0].name}"
          f"（{shots[0].size_bytes / 1024:.1f} KB）")

    if FAILURES:
        print(f"\n== {len(FAILURES)} 项未通过 ==")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("\n== 全部通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
