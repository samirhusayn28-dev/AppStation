"""Floating device window: live screen plus a slim bar of device controls.

The window is a separate top-level QWidget that owns the shared ScreenView.
Its buttons emit a simple action string; the main window performs the adb call
on a worker thread and owns all status/error reporting, so nothing here blocks
the GUI thread.  The window's dimensions follow the ScreenView (which tracks the
streamed frame's aspect ratio, so rotation and presets are reflected) and its
position/size are remembered between runs through the shared settings object.

Layouts only - no setGeometry/move/setFixed*/setMaximum* on any child widget.
"""

from typing import Optional

from PyQt6.QtCore import QByteArray, QSize, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QToolButton, QVBoxLayout, QWidget

from .screen_view import ScreenView
from core.settings import settings


# (action key, glyph, tooltip)
_BUTTONS = (
    ("back", "\u25c0", "Back"),
    ("home", "\u2302", "Home"),
    ("recents", "\u25a3", "Recents"),
    ("rotate", "\u27f3", "Rotate"),
    ("vol_up", "\U0001F50A", "Volume up"),
    ("vol_down", "\U0001F509", "Volume down"),
    ("screenshot", "\U0001F4F7", "Screenshot"),
    ("power", "\u23fb", "Power"),
)


class DeviceWindow(QWidget):
    """Top-level window showing the device screen and control buttons."""

    # One of the action keys in _BUTTONS (e.g. "back", "home", "rotate").
    action = pyqtSignal(str)
    # Emitted when the user closes the window (X button / close()).
    closed = pyqtSignal()

    def __init__(self, screen_view=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ApkPreview - Device")

        # Sensible minimum size, applied to this top-level window only (its
        # children stay purely layout-managed).
        self.setMinimumSize(300, 520)

        self.screen_view = screen_view if screen_view is not None else ScreenView()
        self._buttons = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.screen_view, 1)

        bar = QHBoxLayout()
        bar.setSpacing(2)
        for key, glyph, tooltip in _BUTTONS:
            button = QToolButton()
            button.setText(glyph)
            button.setToolTip(tooltip)
            button.clicked.connect(
                lambda _checked=False, k=key: self.action.emit(k))
            bar.addWidget(button)
            self._buttons[key] = button
        bar.addStretch()
        layout.addLayout(bar)

        self._restore_geometry()

    # -- geometry persistence --------------------------------------------
    def _restore_geometry(self):
        """Restore the last saved position/size (best effort)."""
        try:
            stored = settings.screen_window_geometry
        except Exception:
            stored = None
        if not stored:
            return
        try:
            self.restoreGeometry(QByteArray.fromHex(str(stored).encode("ascii")))
        except Exception:
            pass

    def save_geometry(self):
        """Persist the current position/size (best effort)."""
        try:
            raw = bytes(self.saveGeometry())
            settings.screen_window_geometry = raw.hex()
        except Exception:
            pass

    # -- controls ---------------------------------------------------------
    def set_controls_enabled(self, enabled: bool):
        """Enable/disable the device buttons (disabled while stopped)."""
        for button in self._buttons.values():
            button.setEnabled(bool(enabled))

    # -- sizing -----------------------------------------------------------
    def sizeHint(self) -> QSize:  # noqa: N802 (Qt naming)
        hint = self.screen_view.sizeHint()
        return QSize(hint.width() + 8, hint.height() + 60)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 (Qt naming)
        # Never report smaller than the enforced minimum size (Qt's default
        # minimumSizeHint ignores setMinimumSize and would return a stub).
        base = self.minimumSize()
        layout_hint = super().minimumSizeHint()
        return QSize(max(base.width(), layout_hint.width()),
                     max(base.height(), layout_hint.height()))

    def closeEvent(self, event):  # noqa: N802 (Qt naming)
        self.save_geometry()
        try:
            self.closed.emit()
        except Exception:
            pass
        event.accept()
