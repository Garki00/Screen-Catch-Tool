"""numpy 图像 → QImage 的转换。

`QImage` 不接管外部缓冲区的所有权，所以必须 ``.copy()``，否则 QImage 会指向被复用
或被回收的 numpy 内存（画面撕裂 / 崩溃）。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QImage


def bgr_to_qimage(image: Any) -> QImage | None:
    """把 BGR 的 numpy 数组转成 QImage（RGB888），返回的 QImage 独占内存。"""
    try:
        import numpy as np

        array = np.ascontiguousarray(image)
        if array.ndim != 3 or array.shape[2] < 3:
            return None
        rgb = np.ascontiguousarray(array[:, :, 2::-1])  # BGR -> RGB
        height, width = rgb.shape[:2]
        return QImage(rgb.data, width, height, 3 * width, QImage.Format.Format_RGB888).copy()
    except Exception:  # noqa: BLE001
        return None
