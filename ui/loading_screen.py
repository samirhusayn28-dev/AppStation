"""Transparent loading overlay for AppStation.

Two flavours share the same look:

* :class:`LoadingScreen` - a frameless, fully transparent top-level window
  shown while the app starts up.
* :class:`LoadingOverlay` - a transparent child widget laid over the main
  window, used while the emulator boots or during any long action.

Both draw **no background at all**: just the word "AppStation", a spinner and
a short step text such as "Starting emulator...".  Nothing is painted behind
the text, so the overlay never looks like a dark panel.
"""

import time

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import QWidget


def _draw_spinner(painter: QPainter, center: QPointF, radius: float,
                  angle: float, color: QColor):
    """Draw a small rotating arc (the busy indicator)."""
    pen = QPen(color, 2.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    rect = QRectF(center.x() - radius, center.y() - radius,
                  radius * 2, radius * 2)
    painter.drawArc(rect, int(angle * 16), 260 * 16)


class LoadingScreen(QWidget):
    """Frameless, fully transparent splash shown while the app starts.

    The background is completely transparent - only the text and the spinner
    are painted - so it never looks like a dark box on the desktop.
    """

    finished = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.SplashScreen
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFixedSize(320, 120)

        self._angle = 0.0
        self._dots = ""
        self._step = ""
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def set_step(self, text: str):
        """Set the short step text shown under the name."""
        self._step = text or ""
        self.update()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self._dots = (self._dots + ".")[-3:]
        self.update()

    def stop(self):
        """Stop the animation (called just before the screen is closed)."""
        self._timer.stop()

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Nothing is filled: the background stays fully transparent.
        rect = self.rect()

        _draw_spinner(painter, QPointF(rect.width() / 2.0, 26.0), 9.0,
                      self._angle, QColor(120, 190, 255))

        painter.setPen(QColor(235, 235, 238))
        font = QFont(painter.font())
        font.setPointSize(15)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 42, rect.width(), 24),
                         Qt.AlignmentFlag.AlignCenter,
                         f"AppStation{self._dots}")

        painter.setPen(QColor(175, 175, 185))
        font.setPointSize(10)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(QRectF(0, 70, rect.width(), 20),
                         Qt.AlignmentFlag.AlignCenter, self._step)

        painter.end()


class LoadingOverlay(QWidget):
    """Transparent busy overlay laid over the main window.

    Shows the AppStation name, a spinner and the current step.  The
    background is never painted, so the window content stays visible.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        # Purely visual: clicks pass through, so the overlay can never make
        # the main window feel unresponsive while it is shown.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._angle = 0.0
        self._step = ""
        self._dots = ""
        self._busy = False
        self._message = ""
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._maybe_hide)

        self.hide()

    # -- state -----------------------------------------------------------
    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self._dots = (self._dots + ".")[-3:]
        self.update()

    def set_busy(self, busy: bool, step: str = ""):
        """Show (or hide) the busy spinner with the given step text."""
        self._busy = bool(busy)
        self._step = step or ""
        self._dots = ""
        if self._busy:
            self._message = ""
            self._hide_timer.stop()
            self._timer.start()
            self.show()
            self.raise_()
        else:
            self._timer.stop()
            self.hide()
        self.update()

    def set_step(self, step: str, value=None):
        """Update the step text (and the optional percentage)."""
        self._step = step or ""
        self._dots = ""
        self.update()

    def show_message(self, message: str, timeout_ms: int = 4000):
        """Show a short transient message (no spinner)."""
        self._message = message or ""
        self._busy = False
        self._timer.stop()
        self.show()
        self.raise_()
        self._hide_timer.start(max(500, int(timeout_ms)))
        self.update()

    def _maybe_hide(self):
        if not self._busy:
            self.hide()

    def hide(self):
        self._message = ""
        self._timer.stop()
        super().hide()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        # Fully transparent: no background is painted.

        if self._busy:
            _draw_spinner(painter, QPointF(rect.width() / 2.0, rect.height()
                                           / 2.0 - 34.0), 12.0,
                          self._angle, QColor(120, 190, 255))
            painter.setPen(QColor(235, 235, 238))
            font = QFont(painter.font())
            font.setPointSize(15)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(QRectF(0, rect.height() / 2.0 - 8.0,
                                    rect.width(), 26),
                             Qt.AlignmentFlag.AlignCenter,
                             f"AppStation{self._dots}")
            painter.setPen(QColor(190, 190, 200))
            font.setPointSize(10)
            font.setBold(False)
            painter.setFont(font)
            painter.drawText(QRectF(0, rect.height() / 2.0 + 20.0,
                                    rect.width(), 20),
                             Qt.AlignmentFlag.AlignCenter, self._step)
        elif self._message:
            painter.setPen(QColor(225, 225, 232))
            font = QFont(painter.font())
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             self._message)

        painter.end()
