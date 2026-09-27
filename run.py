"""打包入口（PyInstaller 从这里开始分析依赖）。

开发时等价于 ``python -m app``；打包后等价于双击 ``ScreenCatchTool.exe``。
"""

from __future__ import annotations

import multiprocessing
import sys

from app.main import main

if __name__ == "__main__":
    multiprocessing.freeze_support()   # 万一将来用到 multiprocessing，避免子进程反复启动
    raise SystemExit(main())
