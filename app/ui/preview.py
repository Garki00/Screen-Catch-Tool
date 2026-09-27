"""实时预览控件：等比显示画面，并在其上绘制/拖动范围选择框。

坐标规则（对应 P3 的坑）：

- 控件内部**一切都用原始像素坐标**（即捕获到的画面尺寸），只在绘制和鼠标事件里做
  视图 ↔ 图像的换算；
- 因此范围框在窗口缩放、预览图被缩放（``preview_max_width``）时都不会漂移。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

MASK_COLOR = QColor(0, 0, 0, 110)
BOX_COLOR = QColor(0, 220, 130)
BOX_COLOR_EDIT = QColor(255, 190, 60)
TEXT_COLOR = QColor(210, 215, 220)
BG_COLOR = QColor(24, 26, 30)


class PreviewWidget(QWidget):
    """显示一帧画面，并允许在其上编辑范围选择框。"""

    # 编辑完成（松开鼠标）时发出，参数是原始像素坐标
    cropEdited = Signal(int, int, int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._source_size = (0, 0)
        self._crop: QRect | None = None          # 图像坐标系（原始像素）
        self._editable = False
        self._drag_origin: QPoint | None = None  # 图像坐标
        self._drag_rect: QRect | None = None     # 图像坐标
        self._move_offset: QPoint | None = None
        self.setMinimumSize(420, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

    # ------------------------------------------------------------------ 数据

    def has_image(self) -> bool:
        return self._image is not None

    def image_size(self) -> tuple[int, int]:
        return self._source_size

    def crop_rect(self) -> QRect | None:
        return QRect(self._crop) if self._crop else None

    def set_crop(self, rect: QRect | tuple[int, int, int, int] | None) -> None:
        if rect is None:
            self._crop = None
        elif isinstance(rect, QRect):
            self._crop = QRect(rect) if not rect.isNull() and rect.width() > 0 else None
        else:
            x, y, w, h = rect
            self._crop = QRect(x, y, w, h) if w > 0 and h > 0 else None
        self.update()

    def set_editable(self, editable: bool) -> None:
        self._editable = bool(editable)
        if not self._editable:
            self._drag_origin = None
            self._drag_rect = None
            self._move_offset = None
        self.setCursor(Qt.CursorShape.CrossCursor if editable else Qt.CursorShape.ArrowCursor)
        self.update()

    def is_editable(self) -> bool:
        return self._editable

    def set_frame(self, image: QImage, source_width: int, source_height: int) -> None:
        self._image = image
        self._source_size = (int(source_width), int(source_height))
        self.update()

    # ------------------------------------------------------------ 几何换算

    def display_rect(self) -> QRect:
        """画面在控件里的实际绘制区域（等比缩放、居中）。"""
        if self._image is None or not self._source_size[0] or not self._source_size[1]:
            return QRect()
        area_w, area_h = max(1, self.width()), max(1, self.height())
        source_w, source_h = self._source_size
        scale = min(area_w / source_w, area_h / source_h)
        width = max(1, int(source_w * scale))
        height = max(1, int(source_h * scale))
        return QRect((area_w - width) // 2, (area_h - height) // 2, width, height)

    def scale_factor(self) -> float:
        rect = self.display_rect()
        if rect.isEmpty() or not self._source_size[0]:
            return 1.0
        return rect.width() / self._source_size[0]

    def view_to_image(self, point: QPoint) -> QPoint:
        rect = self.display_rect()
        scale = self.scale_factor()
        if rect.isEmpty() or scale <= 0:
            return QPoint(0, 0)
        x = int(round((point.x() - rect.x()) / scale))
        y = int(round((point.y() - rect.y()) / scale))
        return QPoint(*self._clamp_point(x, y))

    def image_to_view(self, rect: QRect) -> QRect:
        display = self.display_rect()
        scale = self.scale_factor()
        return QRect(
            display.x() + int(rect.x() * scale),
            display.y() + int(rect.y() * scale),
            max(1, int(rect.width() * scale)),
            max(1, int(rect.height() * scale)),
        )

    def _clamp_point(self, x: int, y: int) -> tuple[int, int]:
        width, height = self._source_size
        if width <= 0 or height <= 0:
            return 0, 0
        return max(0, min(width, x)), max(0, min(height, y))

    # ---------------------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt 命名
        painter = QPainter(self)
        painter.fillRect(self.rect(), BG_COLOR)

        if self._image is None:
            painter.setPen(QPen(TEXT_COLOR))
            painter.setFont(QFont("Microsoft YaHei", 11))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "等待捕获…\n点击「开始捕获」后此处显示实时画面")
            return

        display = self.display_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(display, self._image)

        crop = self._drag_rect if self._drag_rect is not None else self._crop
        if crop is None or crop.isNull() or crop.width() <= 0 or crop.height() <= 0:
            return

        view_rect = self.image_to_view(crop).intersected(display)
        # 框外压暗
        painter.fillRect(QRect(display.x(), display.y(), display.width(), view_rect.y() - display.y()), MASK_COLOR)
        painter.fillRect(QRect(display.x(), view_rect.bottom() + 1, display.width(),
                               display.bottom() - view_rect.bottom()), MASK_COLOR)
        painter.fillRect(QRect(display.x(), view_rect.y(), view_rect.x() - display.x(),
                               view_rect.height()), MASK_COLOR)
        painter.fillRect(QRect(view_rect.right() + 1, view_rect.y(),
                               display.right() - view_rect.right(), view_rect.height()), MASK_COLOR)

        pen = QPen(BOX_COLOR_EDIT if self._editable else BOX_COLOR, 2)
        pen.setStyle(Qt.PenStyle.DashLine if self._editable else Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.drawRect(view_rect)

        text = f"范围 {crop.width()}x{crop.height()} @ ({crop.x()}, {crop.y()})"
        painter.setPen(QPen(TEXT_COLOR))
        painter.setFont(QFont("Microsoft YaHei", 9))
        painter.drawText(QRect(view_rect.x(), max(display.y(), view_rect.y() - 22),
                               max(160, view_rect.width()), 20),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    # ------------------------------------------------------------ 鼠标交互

    def mousePressEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._editable or self._image is None or event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.view_to_image(event.position().toPoint())
        if self._crop and self._crop.contains(point):
            self._move_offset = QPoint(point.x() - self._crop.x(), point.y() - self._crop.y())
            self._drag_rect = QRect(self._crop)
        else:
            self._move_offset = None
            self._drag_origin = point
            self._drag_rect = QRect(point, point)
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._editable or self._drag_rect is None:
            return
        point = self.view_to_image(event.position().toPoint())
        if self._move_offset is not None:
            width, height = self._source_size
            x = max(0, min(width - self._drag_rect.width(), point.x() - self._move_offset.x()))
            y = max(0, min(height - self._drag_rect.height(), point.y() - self._move_offset.y()))
            self._drag_rect.moveTo(x, y)
        elif self._drag_origin is not None:
            self._drag_rect = QRect(self._drag_origin, point).normalized()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001, N802
        if not self._editable or event.button() != Qt.MouseButton.LeftButton or self._drag_rect is None:
            return
        rect = self._drag_rect.normalized().intersected(QRect(0, 0, *self._source_size))
        self._drag_origin = None
        self._move_offset = None
        self._drag_rect = None
        if rect.width() < 8 or rect.height() < 8:  # 误点：不产生范围
            self.update()
            event.accept()
            return
        self._crop = QRect(rect)
        self.update()
        self.cropEdited.emit(rect.x(), rect.y(), rect.width(), rect.height())
        event.accept()
