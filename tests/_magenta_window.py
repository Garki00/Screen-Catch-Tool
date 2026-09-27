"""给捕获测试用的标识窗口（独立进程，测完由测试脚本杀掉）。

内容刻意做成不对称，用来同时验证「抓到的确实是这个窗口」和「图像方向没上下翻转」：

- 底色品红 #FF00FF
- 左上角一块蓝色 #0000FF
- 底部一条黄色 #FFFF00

    .venv\\Scripts\\python.exe tests\\_magenta_window.py [标题]
"""

from __future__ import annotations

import sys
import tkinter as tk

TITLE = sys.argv[1] if len(sys.argv) > 1 else "SCT-TEST-WINDOW"

MAGENTA = "#FF00FF"
BLUE = "#0000FF"
YELLOW = "#FFFF00"

CLIENT_W, CLIENT_H = 480, 320
BLUE_BOX = (0, 0, 110, 70)            # 左上角
YELLOW_BOX = (0, CLIENT_H - 60, CLIENT_W, CLIENT_H)


def main() -> int:
    root = tk.Tk()
    root.title(TITLE)
    root.configure(bg=MAGENTA)
    root.geometry(f"{CLIENT_W}x{CLIENT_H}+120+120")
    root.attributes("-topmost", True)
    canvas = tk.Canvas(root, bg=MAGENTA, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_rectangle(*BLUE_BOX, fill=BLUE, outline=BLUE)
    canvas.create_rectangle(*YELLOW_BOX, fill=YELLOW, outline=YELLOW)
    root.after(200, lambda: root.attributes("-topmost", True))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
