"""Custom widget that shows the embedded device screen (Stage 2a - view only).

The widget keeps the device's aspect ratio (updated from each decoded frame, so
rotation and preset changes are reflected) and scales the image to fit, centred.
It shows a short note overlay while the screencap fallback is active.
"""

import time

from PyQt6.QtCore import Qt, QRect, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QSizePolicy, QWidget


class ScreenView(QWidget):
    """Shows the embedded device screen, preserving the device aspect ratio."""

    # Elapsed milliseconds spent in the last paintEvent (stream stats).
    painted = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self._note = ""
        self._aspect = (9, 16)  # width, height (portrait default)
        self._message = "No screen yet"
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_frame(self, image: QImage):
        self._image = image
        if image is not None and not image.isNull() and image.height() > 0:
            self._aspect = (image.width(), image.height())
            self._message = ""
        self.update()

    def set_note(self, text: str):
        self._note = text or ""
        self.update()

    def set_device_size(self, width: int, height: int):
        if width > 0 and height > 0:
            self._aspect = (width, height)
            self.update()

    def clear(self):
        self._image = None
        self._note = ""
        self._message = "No screen yet"
        self.update()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        aspect_w, aspect_h = self._aspect
        if aspect_w <= 0:
            return width
        return int(width * aspect_h / aspect_w)

    def sizeHint(self) -> QSize:
        return QSize(360, 640)

    @staticmethod
    def _fit(rect: QRect, image_w: int, image_h: int) -> QRect:
        if image_w <= 0 or image_h <= 0:
            return rect
        scale = min(rect.width() / image_w, rect.height() / image_h)
        width = max(1, int(image_w * scale))
        height = max(1, int(image_h * scale))
        x = rect.x() + (rect.width() - width) // 2
        y = rect.y() + (rect.height() - height) // 2
        return QRect(x, y, width, height)

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        started = time.perf_counter()
        painter = QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, self.palette().window())

        if self._image is not None and not self._image.isNull():
            target = self._fit(rect, self._image.width(), self._image.height())
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(target, self._image)
        elif self._message:
            painter.setPen(self.palette().text().color())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._message)

        self._paint_note(painter, rect)
        painter.end()

        try:
            self.painted.emit((time.perf_counter() - started) * 1000.0)
        except Exception:
            pass

    def _paint_note(self, painter: QPainter, rect: QRect):
        if not self._note:
            return
        painter.save()
        font = QFont(painter.font())
        font.setPointSize(max(9, font.pointSize()))
        painter.setFont(font)

        padding = 6
        metrics = painter.fontMetrics()
        text_bounds = metrics.boundingRect(
            QRect(0, 0, max(80, rect.width() - 2 * padding - 16), 1000),
            Qt.TextFlag.TextWordWrap, self._note)
        box = QRect(rect.x() + 8, rect.y() + 8,
                    text_bounds.width() + 2 * padding,
                    text_bounds.height() + 2 * padding)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(0, 0, 0, 160))
        painter.drawRoundedRect(box, 4, 4)

        painter.setPen(QColor(255, 255, 255))
        painter.drawText(box.adjusted(padding, padding, -padding, -padding),
                         Qt.TextFlag.TextWordWrap, self._note)
        painter.restore()
