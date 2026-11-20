"""Main window V2 for AppStation - rebuilt layout with wired controls.

Layout: one top toolbar row plus the Logcat/Controls tab widget, which takes
all remaining space.  The Android screen lives in its own frameless preview
window.  Long-running work (APK parse, install, emulator start/stop, adb
actions) runs on worker threads so the UI never blocks.
"""

import time
import os
import webbrowser
from pathlib import Path
from typing import Optional, Callable, Tuple

from PyQt6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                             QLayout, QLabel, QPushButton, QComboBox,
                             QTabWidget, QFileDialog,
                             QMessageBox, QGridLayout,
                             QSizePolicy, QSpinBox, QCheckBox, QToolButton,
                             QMenu, QApplication, QWidgetAction)
from PyQt6.QtCore import (Qt, QThread, QTimer, QObject, pyqtSignal, pyqtSlot,
                          QUrl, QSize, QRect)
from PyQt6.QtGui import (QDragEnterEvent, QDropEvent, QPainter, QColor,
                         QDesktopServices, QGuiApplication, QFont)
from PyQt6.QtCore import QPoint

from .loading_screen import LoadingOverlay
from .logcat_panel import LogcatPanel
from .screen_view import ScreenView
from .android_versions_dialog import AndroidVersionsDialog
from core.platform_utils import PlatformUtils
from core.sdk_manager import SDKManager, ANDROID_VERSION_NAMES
from core.hardware_profile import HardwareProfiler, PerformanceMode
from core.emulator import EmulatorManager
from core.adb import ADB, detect_package_and_activity
from core.scrcpy import ScrcpyManager
from core.apk_parser import get_apk_info
from core.file_watcher import create_file_watcher
from core.screen_stream import ScreenStream
from core.settings import settings


# (action key, glyph, tooltip) for the floating control bar under the preview.
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


# Dark styling for the Controls panel: white bold text on a near-black card.
# Scoped to the panel's object name so the rest of the window keeps its theme.
_CONTROLS_STYLE = """
QWidget#controlsPanel {
    background: #1e1e22;
}
QWidget#controlsPanel QLabel {
    color: #ffffff;
    font-weight: 700;
}
QWidget#controlsPanel QPushButton {
    color: #ffffff;
    font-weight: 700;
    background: #2f2f36;
    border: 1px solid #4a4a54;
    border-radius: 6px;
    padding: 4px 10px;
}
QWidget#controlsPanel QPushButton:hover {
    background: #3c3c46;
    border-color: #6a6a78;
}
QWidget#controlsPanel QPushButton:pressed {
    background: #26262c;
}
QWidget#controlsPanel QPushButton:disabled {
    color: #8a8a94;
    background: #26262a;
    border-color: #3a3a42;
}
QWidget#controlsPanel QCheckBox {
    color: #ffffff;
    font-weight: 700;
    spacing: 6px;
}
QWidget#controlsPanel QComboBox,
QWidget#controlsPanel QSpinBox {
    color: #ffffff;
    background: #2b2b31;
    border: 1px solid #4a4a54;
    border-radius: 6px;
    padding: 2px 8px;
    min-height: 20px;
}
QWidget#controlsPanel QComboBox QAbstractItemView {
    color: #ffffff;
    background: #2b2b31;
    selection-background-color: #3f5f8f;
    selection-color: #ffffff;
}
QWidget#controlsPanel QComboBox::drop-down {
    border: none;
    width: 20px;
}
QWidget#controlsPanel QComboBox::down-arrow {
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #e8e8ee;
}
QWidget#controlsPanel QSpinBox::up-button,
QWidget#controlsPanel QSpinBox::down-button {
    background: transparent;
    border: none;
    width: 16px;
}
QWidget#controlsPanel QSpinBox::up-arrow {
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid #e8e8ee;
}
QWidget#controlsPanel QSpinBox::down-arrow {
    image: none;
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #e8e8ee;
}
"""


class _SignalBridge(QObject):
    """Marshals events from background threads onto the main (UI) thread."""

    apk_changed = pyqtSignal(object)


class _ScreenBridge(QObject):
    """Marshals embedded-screen events from the stream thread onto the UI thread.

    Frames are NOT pushed through the bridge: the stream keeps only the latest
    decoded frame in a single slot and the UI pulls it on a QTimer (~30 Hz).
    """

    note = pyqtSignal(str)
    error = pyqtSignal(str)
    state = pyqtSignal(str)
    status = pyqtSignal(str)


# ----------------------------------------------------------------------------
# Detachable preview pane
# ----------------------------------------------------------------------------
class PreviewPane(QWidget):
    """Embeddable device preview: the Android screen and nothing else.

    The pane owns nothing but its layout around the shared ``ScreenView``, so it
    can be reparented between the main window and a detached top-level window
    without losing the streamed frame.  There is no device frame, no control
    bar and no background: the window shows the screen only.
    """

    action = pyqtSignal(str)

    def __init__(self, screen_view, parent=None):
        super().__init__(parent)
        self.screen_view = screen_view
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.screen_view, 1)

    def set_controls_enabled(self, enabled: bool):
        """Kept for compatibility: the preview has no buttons of its own."""
        return


class AspectColumnLayout(QLayout):
    """One column: the Android screen and the nav bar directly beneath it.

    Both items always share the *same* width - the screen width - and the nav
    bar height is 9% of that width, clamped to 36..56 px.  The column is
    centred inside the layout rectangle, so the screen keeps the guest aspect
    ratio however the preview window is resized.
    """

    NAV_FRACTION = 0.09
    NAV_MIN = 36
    NAV_MAX = 56

    def __init__(self, parent=None, guest_w: int = 720, guest_h: int = 1600):
        super().__init__(parent)
        self._items = []
        self._guest_w = max(1, int(guest_w))
        self._guest_h = max(1, int(guest_h))

    # -- guest size ------------------------------------------------------
    def set_guest_size(self, guest_w: int, guest_h: int):
        """Follow the real guest resolution (it changes on rotation)."""
        w, h = max(1, int(guest_w)), max(1, int(guest_h))
        if (w, h) == (self._guest_w, self._guest_h):
            return
        self._guest_w, self._guest_h = w, h
        self.invalidate()

    def guest_aspect(self) -> float:
        return self._guest_w / float(self._guest_h)

    # -- QLayout plumbing ------------------------------------------------
    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return False

    def sizeHint(self):
        nav_h = self._clamp_nav(self.NAV_FRACTION * self._guest_w)
        return QSize(self._guest_w, self._guest_h + nav_h)

    def minimumSize(self):
        return QSize(160, 240)

    @classmethod
    def _clamp_nav(cls, value: float) -> int:
        return max(cls.NAV_MIN, min(cls.NAV_MAX, int(round(value))))

    def column_size(self, avail_w: int, avail_h: int):
        """Return ``(screen_w, screen_h, nav_h)`` that fit ``avail_w/h``."""
        aspect = self.guest_aspect()
        # First guess the nav height from the width the screen would like to
        # be, then refine it from the actual screen width.
        guess_w = max(1, min(avail_w, int(avail_h * aspect)))
        nav_h = self._clamp_nav(self.NAV_FRACTION * guess_w)
        screen_w = max(1, min(avail_w, int((avail_h - nav_h) * aspect)))
        nav_h = self._clamp_nav(self.NAV_FRACTION * screen_w)
        screen_w = max(1, min(avail_w, int((avail_h - nav_h) * aspect)))
        screen_h = max(1, int(round(screen_w / aspect)))
        return screen_w, screen_h, nav_h

    def setGeometry(self, rect):
        super().setGeometry(rect)
        if not self._items:
            return
        if len(self._items) == 1:
            self._items[0].setGeometry(rect)
            return
        screen_w, screen_h, nav_h = self.column_size(rect.width(),
                                                     rect.height())
        x = rect.x() + (rect.width() - screen_w) // 2
        y = rect.y() + (rect.height() - (screen_h + nav_h)) // 2
        self._items[0].setGeometry(QRect(x, y, screen_w, screen_h))
        self._items[1].setGeometry(QRect(x, y + screen_h, screen_w, nav_h))


class DetachedPreviewWindow(QWidget):
    """Frameless, translucent preview: the screen plus a slim nav bar.

    The screen sits only a few pixels inside the window, so the resize/move
    ring starts right at the visible screen border instead of floating far
    away in a wide transparent margin.  The margin is painted with an almost
    invisible alpha so macOS still delivers mouse events there (fully
    transparent pixels are click-through).  The outer few pixels resize the
    window, the rest of the margin moves it, Option/Alt+drag moves it from
    anywhere, and *every* press on the Android screen is forwarded by
    :class:`ScreenView` - there is no dead border.
    """

    close_requested = pyqtSignal()
    always_on_top_changed = pyqtSignal(bool)
    nav_action = pyqtSignal(str)

    # Thin transparent ring around the screen: outer RESIZE_EDGE px resize,
    # the remaining (MARGIN - RESIZE_EDGE) px move the window.
    MARGIN = 10
    RESIZE_EDGE = 5
    MIN_SIZE = QSize(220, 300)

    _NAV_BUTTONS = (
        ("back", "\u25c0", "Back"),
        ("home", "\u2302", "Home"),
        ("recents", "\u25a3", "Recents"),
        ("rotate", "\u27f3", "Rotate"),
        ("vol_up", "\U0001F50A", "Volume up"),
        ("vol_down", "\U0001F509", "Volume down"),
        ("power", "\u23fb", "Power"),
    )

    def __init__(self, pane, parent=None):
        super().__init__(parent)
        self._pane = pane

        self._always_on_top = bool(settings.always_on_top)
        flags = (Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setWindowModality(Qt.WindowModality.NonModal)

        self.setMouseTracking(True)
        self._pane.setMouseTracking(True)

        self._margin = self.MARGIN
        self._resize_edge_px = self.RESIZE_EDGE
        self._min_size = self.MIN_SIZE

        # Fallback drag/resize state, used when the platform does not
        # implement startSystemMove()/startSystemResize().
        self._manual_mode = None          # "move" | "resize" | None
        self._manual_edge = None
        self._manual_start_geom = None
        self._manual_start_pos = None

        # -- column (screen + nav bar) -------------------------------------
        self._column = QWidget(self)
        self._column.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._screen_container = QWidget(self._column)
        self._screen_container.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True)
        container_layout = QVBoxLayout(self._screen_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(pane)

        # -- nav bar: same width as the screen, rounded dark background ----
        self._nav_bar = QWidget(self._column)
        self._nav_bar.setObjectName("previewNavBar")
        self._nav_bar.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True)
        self._nav_bar.setStyleSheet(
            "QWidget#previewNavBar { background: rgba(28, 28, 32, 200);"
            " border-radius: 14px; }"
            "QPushButton { color: #e8e8ea; border: none;"
            " background: transparent; padding: 0px;"
            " border-radius: 7px; font-size: 13px; }"
            "QPushButton:hover { background: rgba(255, 255, 255, 40); }")
        nav_layout = QHBoxLayout(self._nav_bar)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(2)
        for key, glyph, tooltip in self._NAV_BUTTONS:
            btn = QPushButton(glyph)
            btn.setToolTip(tooltip)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Expanding)
            btn.clicked.connect(
                lambda _c=False, k=key: self.nav_action.emit(k))
            nav_layout.addWidget(btn, 1)

        self._col_layout = AspectColumnLayout(self._column)
        self._col_layout.addWidget(self._screen_container)
        self._col_layout.addWidget(self._nav_bar)

        # Follow the real guest resolution (it changes after rotation).
        try:
            self._pane.screen_view.screen_size_changed.connect(
                self._on_guest_size_changed)
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(self._margin, self._margin,
                                self._margin, self._margin)
        root.setSpacing(0)
        root.addWidget(self._column)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # ------------------------------------------------------------------
    # Guest geometry
    # ------------------------------------------------------------------
    def _on_guest_size_changed(self, width: int, height: int):
        try:
            self._col_layout.set_guest_size(width, height)
        except Exception:
            pass

    def guest_aspect(self) -> float:
        try:
            return self._col_layout.guest_aspect()
        except Exception:
            return 9.0 / 16.0

    # ------------------------------------------------------------------
    # Window sizing: the column (screen + nav bar) fills the window exactly
    # ------------------------------------------------------------------
    @classmethod
    def _nav_height(cls, screen_w: int) -> int:
        return AspectColumnLayout._clamp_nav(
            AspectColumnLayout.NAV_FRACTION * max(1, int(screen_w)))

    def window_size_for_screen_width(self, screen_w: int) -> QSize:
        """Window size whose column fills it exactly.

        The nav-bar height follows the screen width, and the thin margin is
        added on every side.  The screen border therefore sits exactly
        ``MARGIN`` px from the window border - no centring slack, no air.
        """
        screen_w = max(1, int(round(screen_w)))
        screen_h = max(1, int(round(screen_w / self.guest_aspect())))
        nav_h = self._nav_height(screen_w)
        return QSize(screen_w + 2 * self._margin,
                     screen_h + nav_h + 2 * self._margin)

    def screen_width_for_window_height(self, height: int) -> int:
        """Inverse of :meth:`window_size_for_screen_width` (for a height)."""
        avail_h = max(1, int(height) - 2 * self._margin)
        aspect = self.guest_aspect()
        guess_w = max(1, int(avail_h * aspect))
        nav_h = self._nav_height(guess_w)
        screen_w = max(1, int((avail_h - nav_h) * aspect))
        nav_h = self._nav_height(screen_w)
        return max(1, int((avail_h - nav_h) * aspect))

    def _screen_rect_in_window(self):
        col = self._column.geometry()
        sc = self._screen_container.geometry()
        return (col.x() + sc.x(), col.y() + sc.y(), sc.width(), sc.height())

    # ------------------------------------------------------------------
    # Region classification (plain geometry, testable without a window).
    # ------------------------------------------------------------------
    @staticmethod
    def classify_point(px, py, win_w, win_h, margin, screen_rect, edge_px=8):
        """Return the region a point belongs to.

        ``screen_rect`` is ``(sx, sy, sw, sh)`` in window coordinates.
        Returns ``screen``, ``move``, ``resize-<edge>`` or ``resize-<corner>``.
        """
        sx, sy, sw, sh = screen_rect
        near_left = px < edge_px
        near_right = px >= win_w - edge_px
        near_top = py < edge_px
        near_bottom = py >= win_h - edge_px
        if near_top and near_left:
            return "resize-top-left"
        if near_top and near_right:
            return "resize-top-right"
        if near_bottom and near_left:
            return "resize-bottom-left"
        if near_bottom and near_right:
            return "resize-bottom-right"
        if near_left or near_right or near_top or near_bottom:
            if near_left or near_right:
                return "resize-left" if near_left else "resize-right"
            return "resize-top" if near_top else "resize-bottom"
        if (sx <= px < sx + sw) and (sy <= py < sy + sh):
            return "screen"
        return "move"

    def _region_at(self, pos):
        return self.classify_point(
            int(pos.x()), int(pos.y()), self.width(), self.height(),
            self._margin, self._screen_rect_in_window(),
            self._resize_edge_px)

    @staticmethod
    def _region_to_edges(region):
        mapping = {
            "resize-top-left": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
            "resize-top-right": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
            "resize-bottom-left": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
            "resize-bottom-right": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
            "resize-left": Qt.Edge.LeftEdge,
            "resize-right": Qt.Edge.RightEdge,
            "resize-top": Qt.Edge.TopEdge,
            "resize-bottom": Qt.Edge.BottomEdge,
        }
        return mapping.get(region, Qt.Edge.TopEdge)

    @staticmethod
    def _cursor_for_region(region):
        if "left" in region and "top" in region:
            return Qt.CursorShape.SizeFDiagCursor
        if "right" in region and "top" in region:
            return Qt.CursorShape.SizeBDiagCursor
        if "left" in region and "bottom" in region:
            return Qt.CursorShape.SizeBDiagCursor
        if "right" in region and "bottom" in region:
            return Qt.CursorShape.SizeFDiagCursor
        if "left" in region or "right" in region:
            return Qt.CursorShape.SizeHorCursor
        if "top" in region or "bottom" in region:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    @staticmethod
    def _move_modifier(event) -> bool:
        try:
            mods = event.modifiers()
        except Exception:
            return False
        return bool(mods & (Qt.KeyboardModifier.AltModifier
                            | Qt.KeyboardModifier.MetaModifier))

    @staticmethod
    def _manual_geometry(edge, dx, dy, start, aspect, min_size,
                         margin=10, nav_fraction=0.09, nav_min=36,
                         nav_max=56):
        """Window geometry after dragging ``edge`` by (dx, dy).

        Pure geometry (no Qt calls) so it can be unit-tested.  The *screen*
        keeps the guest aspect ratio and the window is rebuilt as screen +
        nav bar + the thin margin, so the screen border always sits exactly
        ``margin`` px from the window border.
        """
        def nav_for(screen_w):
            return max(nav_min, min(
                nav_max, int(round(nav_fraction * max(1, screen_w)))))

        def screen_w_for_height(window_h):
            avail_h = max(1, window_h - 2 * margin)
            guess_w = max(1, int(avail_h * aspect))
            nav_h = nav_for(guess_w)
            sw = max(1, int((avail_h - nav_h) * aspect))
            nav_h = nav_for(sw)
            return max(1, int((avail_h - nav_h) * aspect))

        left, top = start.x(), start.y()
        width, height = start.width(), start.height()

        if "left" in edge:
            width = max(min_size.width(), width - dx)
        elif "right" in edge:
            width = max(min_size.width(), width + dx)
        if "top" in edge:
            height = max(min_size.height(), height - dy)
        elif "bottom" in edge:
            height = max(min_size.height(), height + dy)

        horizontal = "left" in edge or "right" in edge
        vertical = "top" in edge or "bottom" in edge
        if horizontal and not vertical:
            screen_w = width - 2 * margin
        elif vertical and not horizontal:
            screen_w = screen_w_for_height(height)
        elif abs(width - start.width()) >= abs(height - start.height()):
            screen_w = width - 2 * margin
        else:
            screen_w = screen_w_for_height(height)

        # Never go below the window minimum in either dimension.
        min_screen_w = max(1, min_size.width() - 2 * margin)
        min_screen_w_h = max(
            1, int((min_size.height() - 2 * margin)
                   / (1.0 / aspect + nav_fraction)))
        screen_w = max(1, int(screen_w), min_screen_w, min_screen_w_h)
        screen_h = max(1, int(round(screen_w / aspect)))
        width = screen_w + 2 * margin
        height = screen_h + nav_for(screen_w) + 2 * margin

        # Keep the edge the user is not dragging anchored.
        if "left" in edge:
            left = start.x() + start.width() - width
        if "top" in edge:
            top = start.y() + start.height() - height
        return QRect(left, top, width, height)

    # ------------------------------------------------------------------
    # Mouse handling
    # ------------------------------------------------------------------
    def paintEvent(self, event):  # noqa: N802
        # Fill the transparent margin with an almost invisible colour:
        # fully transparent pixels are click-through on macOS.
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.end()
        super().paintEvent(event)

    def _begin_system_move(self) -> bool:
        handle = self.windowHandle()
        if handle is None:
            return False
        try:
            return bool(handle.startSystemMove())
        except Exception:
            return False

    def _begin_manual(self, mode, edge, event):
        self._manual_mode = mode
        self._manual_edge = edge
        self._manual_start_geom = self.geometry()
        self._manual_start_pos = event.globalPosition().toPoint()
        if mode == "move":
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        region = self._region_at(event.position())
        if self._move_modifier(event):
            if not self._begin_system_move():
                self._begin_manual("move", None, event)
            event.accept()
            return
        if region.startswith("resize-"):
            # Always use our own aspect-locked resize.  The native
            # startSystemResize() lets the window take any shape, which leaves
            # the screen floating in empty space inside a too-wide window.
            self._begin_manual("resize", region, event)
            event.accept()
            return
        if region == "move":
            if not self._begin_system_move():
                self._begin_manual("move", None, event)
            event.accept()
            return
        # On the screen: the child ScreenView forwards the touch.
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._manual_mode is not None and self._manual_start_geom is not None:
            delta = event.globalPosition().toPoint() - self._manual_start_pos
            if self._manual_mode == "move":
                geom = self._manual_start_geom
                self.move(geom.x() + delta.x(), geom.y() + delta.y())
            else:
                self.setGeometry(self._manual_geometry(
                    self._manual_edge, delta.x(), delta.y(),
                    self._manual_start_geom, self.guest_aspect(),
                    self._min_size, self._margin,
                    AspectColumnLayout.NAV_FRACTION,
                    AspectColumnLayout.NAV_MIN,
                    AspectColumnLayout.NAV_MAX))
            event.accept()
            return

        region = self._region_at(event.position())
        if region.startswith("resize-"):
            self.setCursor(self._cursor_for_region(region))
        elif region == "move":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._manual_mode = None
        self._manual_edge = None
        self._manual_start_geom = None
        self._manual_start_pos = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.unsetCursor()
        super().leaveEvent(event)

    # ------------------------------------------------------------------
    # Context menu: Close, Always on top.
    # ------------------------------------------------------------------
    def _show_context_menu(self, pos):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: rgba(40,40,40,230); color: white; "
            "border: 1px solid rgba(255,255,255,60); border-radius: 6px; "
            "padding: 4px; }"
            "QMenu::item { padding: 6px 20px; border-radius: 4px; }"
            "QMenu::item:selected { background: rgba(100,100,100,160); }"
        )
        close_act = menu.addAction("Close")
        close_act.triggered.connect(self.close_requested.emit)

        menu.addSeparator()

        aot_act = menu.addAction(
            "Always on top" if not self._always_on_top else "Always on top (on)"
        )
        aot_act.setCheckable(True)
        aot_act.setChecked(self._always_on_top)
        aot_act.triggered.connect(
            lambda _c: self.always_on_top_changed.emit(not self._always_on_top)
        )
        menu.exec(self.mapToGlobal(pos))

    def set_always_on_top(self, checked: bool):
        self._always_on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()


# ----------------------------------------------------------------------------
# Workers (install worker copied verbatim from ui/main_window.py)
# ----------------------------------------------------------------------------
class APKInstallWorker(QThread):
    """Background worker for APK install and launch."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str, str, str)  # success, message, package, activity
    log = pyqtSignal(str)

    def __init__(self, apk_path: Path, emulator: EmulatorManager, adb: ADB,
                 scrcpy: ScrcpyManager, mode: PerformanceMode, scrcpy_available: bool = True,
                 no_window: bool = False, open_window: bool = False,
                 start_mirror: bool = True):
        super().__init__()
        self._apk_path = apk_path
        self._emulator = emulator
        self._adb = adb
        self._scrcpy = scrcpy
        self._mode = mode
        self._scrcpy_available = scrcpy_available
        self._no_window = no_window
        self._open_window = open_window
        self._start_mirror = start_mirror
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def request_cancel(self):
        """Alias used by the global Cancel button."""
        self.cancel()

    def run(self):
        try:
            # Step 1: Start emulator if not running
            if not self._emulator.is_running():
                self.progress.emit("Starting emulator...")
                self.log.emit("Starting emulator...")
                success, msg = self._emulator.start(self._mode, wait_for_boot=True, timeout=120, scrcpy_available=self._scrcpy_available, no_window=self._no_window, open_window=self._open_window)
                if not success:
                    self.finished.emit(False, f"Emulator start failed: {msg}", "", "")
                    return
                if self._cancelled:
                    return

            # Step 2: Wait for device to be fully ready
            self.progress.emit("Waiting for Android to finish starting...")
            self.log.emit("Waiting for Android to finish starting...")
            success, msg = self._adb.wait_for_device(180)
            if not success:
                self.finished.emit(False, f"Device not ready: {msg}", "", "")
                return
            if self._cancelled:
                return

            # Step 3: Install APK with retries
            self.progress.emit(f"Installing {self._apk_path.name}...")
            self.log.emit(f"Installing {self._apk_path.name}...")

            max_retries = 3
            retry_delay = 5
            retryable_errors = [
                "INSTALL_PARSE_FAILED",
                "Failed to load asset path",
                "device offline"
            ]

            for attempt in range(1, max_retries + 1):
                if self._cancelled:
                    return

                if attempt > 1:
                    self.progress.emit(f"Retrying install (attempt {attempt}/{max_retries})...")
                    self.log.emit(f"Retrying install (attempt {attempt}/{max_retries})...")
                    time.sleep(retry_delay)

                success, msg = self._adb.install(self._apk_path, reinstall=True)
                if success:
                    break

                # Check if error is retryable
                is_retryable = any(err in msg for err in retryable_errors)
                if not is_retryable or attempt == max_retries:
                    self.finished.emit(False, f"Install failed: {msg}", "", "")
                    return

            if not success:
                self.finished.emit(False, f"Install failed after {max_retries} attempts: {msg}", "", "")
                return

            if self._cancelled:
                return

            # Step 4: Parse package and activity
            self.progress.emit("Parsing APK...")
            self.log.emit("Parsing APK...")
            package, activity = detect_package_and_activity(self._apk_path)

            if not package:
                self.finished.emit(False, "Could not detect package name", "", "")
                return

            self.log.emit(f"Package: {package}")
            if activity:
                self.log.emit(f"Main activity: {activity}")

            if self._cancelled:
                return

            # Step 5: Launch app
            self.progress.emit("Launching app...")
            self.log.emit("Launching app...")
            if activity:
                success, msg = self._adb.start_activity(package, activity)
            else:
                # Try monkey to launch main activity
                success, msg = self._adb.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

            if not success:
                self.finished.emit(False, f"Launch failed: {msg}", package, activity or "")
                return

            # Step 6: Start scrcpy if available (native mode shows the
            # emulator's own window, so no external mirror is started there).
            if self._scrcpy_available and self._start_mirror:
                self.progress.emit("Starting screen mirror...")
                self.log.emit("Starting screen mirror...")
                low_spec = (self._mode == PerformanceMode.LOW_SPEC)
                self._scrcpy.start(low_spec=low_spec)
            elif not self._start_mirror:
                self.log.emit("Using emulator window (native display mode)")
            else:
                self.log.emit("scrcpy not available, using emulator window")

            self.finished.emit(True, "App launched successfully!", package, activity or "")

        except Exception as e:
            self.finished.emit(False, f"Error: {e}", "", "")


class APKParseWorker(QThread):
    """Parse an APK off the UI thread."""

    parsed = pyqtSignal(object, object)  # path, info dict

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def run(self):
        try:
            info = get_apk_info(self._path)
        except Exception as e:
            info = {"valid": False, "error": str(e), "package": None}
        self.parsed.emit(self._path, info)


class EmulatorWorker(QThread):
    """Run blocking emulator start/stop/restart off the UI thread."""

    result = pyqtSignal(str, bool, str)  # action, success, message
    # Real boot progress: (percent 0-100, stage text).
    progress = pyqtSignal(int, str)
    log = pyqtSignal(str)  # emulator stdout/stderr lines (incl. warnings)

    def __init__(self, action: str, emulator: EmulatorManager, scrcpy: ScrcpyManager,
                 mode: PerformanceMode, scrcpy_available: bool = True,
                 no_window: bool = False, open_window: bool = False):
        super().__init__()
        self._action = action
        self._emulator = emulator
        self._scrcpy = scrcpy
        self._mode = mode
        self._scrcpy_available = scrcpy_available
        self._no_window = no_window
        self._open_window = open_window
        self._cancelled = False

    def request_cancel(self):
        """Ask the running emulator action to stop."""
        self._cancelled = True
        try:
            self._emulator.stop()
        except Exception:
            pass

    def _emit_progress(self, percent: int, message: str):
        try:
            self.progress.emit(int(percent), str(message))
        except Exception:
            pass

    def _emit_log(self, message: str):
        try:
            self.log.emit(str(message))
        except Exception:
            pass

    def run(self):
        try:
            # Report real boot progress (percent + stage) to the UI.
            try:
                self._emulator.set_progress_callback(self._emit_progress)
            except Exception:
                pass
            try:
                self._emulator.set_output_callback(self._emit_log)
            except Exception:
                pass
            if self._action == "start":
                ok, msg = self._emulator.start(
                    self._mode, wait_for_boot=True,
                    scrcpy_available=self._scrcpy_available,
                    no_window=self._no_window,
                    open_window=self._open_window)
            elif self._action == "stop":
                self._scrcpy.stop()
                ok, msg = self._emulator.stop()
            elif self._action == "restart":
                self._scrcpy.stop()
                self._emulator.stop()
                ok, msg = self._emulator.start(
                    self._mode, wait_for_boot=True,
                    scrcpy_available=self._scrcpy_available,
                    no_window=self._no_window,
                    open_window=self._open_window)
            else:
                ok, msg = False, f"Unknown emulator action: {self._action}"
        except Exception as e:
            ok, msg = False, f"Error: {e}"
        finally:
            try:
                self._emulator.set_progress_callback(None)
            except Exception:
                pass
            try:
                self._emulator.set_output_callback(None)
            except Exception:
                pass
        if self._cancelled and ok:
            ok, msg = False, "Cancelled"
        self.result.emit(self._action, ok, msg)


class AdbActionWorker(QThread):
    """Run a short blocking adb callable off the UI thread."""

    done = pyqtSignal(bool, str)

    def __init__(self, fn: Callable[[], Tuple[bool, str]]):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            ok, msg = self._fn()
        except Exception as e:
            ok, msg = False, f"Error: {e}"
        self.done.emit(ok, msg)


class ProfileWorker(QThread):
    """Detect the hardware performance mode off the GUI thread.

    The profiler shells out (``sysctl`` on macOS) to check virtualisation
    support, which takes tens of milliseconds.  Doing that on the GUI thread
    delays the first paint, so it runs here instead.
    """

    detected = pyqtSignal(object)

    def __init__(self, profiler: HardwareProfiler):
        super().__init__()
        self._profiler = profiler

    def run(self):
        try:
            mode = self._profiler.get_mode()
        except Exception:
            mode = PerformanceMode.NORMAL
        self.detected.emit(mode)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindowV2(QMainWindow):
    """Main application window (rebuilt layout)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("AppStation")
        self.setMinimumSize(640, 480)

        # Core components
        self._profiler = HardwareProfiler()
        self._mode = PerformanceMode.NORMAL
        self._emulator = EmulatorManager()
        self._adb = ADB()
        self._scrcpy = ScrcpyManager()
        self._sdk = SDKManager()

        # Embedded screen: the raw Android stream is shown unframed, filling
        # the left pane's preview area.  Frames are pulled from the stream's
        # latest-frame slot by _frame_timer (~30 Hz).
        self._screen = ScreenStream(self._adb)
        self._screen_bridge = _ScreenBridge(self)
        self._screen_bridge.note.connect(self._on_screen_note)
        self._screen_bridge.error.connect(self._on_screen_error)
        self._screen_bridge.state.connect(self._on_screen_state)
        self._screen_bridge.status.connect(self._on_screen_status)
        self._screen.set_callbacks(
            on_note=lambda msg: self._screen_bridge.note.emit(msg),
            on_error=lambda msg: self._screen_bridge.error.emit(msg),
            on_state=lambda state: self._screen_bridge.state.emit(state),
            on_status=lambda msg: self._screen_bridge.status.emit(msg))

        self.screen_view = ScreenView()
        self.screen_view.painted.connect(self._on_screen_painted)
        self.screen_view.touch.connect(self._on_screen_touch)
        self.screen_view.drag.connect(self._on_screen_drag)
        self.screen_view.scroll.connect(self._on_screen_scroll)
        self.screen_view.app_drawer.connect(self._on_screen_drawer)
        self.screen_view.key_pressed.connect(self._on_screen_key)
        self.screen_view.screen_size_changed.connect(
            self._on_screen_size_changed)
        self.preview_pane = PreviewPane(self.screen_view)
        self.preview_pane.action.connect(self._on_device_action)
        self.preview_pane.set_controls_enabled(self._emulator.is_running())

        # Always detached: the preview lives in its own window.
        self._preview_detached = True
        self.detached_window = None
        # True while a running emulator wants the preview visible.  The window
        # is only shown once a real frame exists, so nothing floats on screen
        # before the emulator is started.
        self._preview_wanted = False

        self._last_frame_seq = -1
        # Input stays disabled until Android has really booted.  The state is
        # cached: probing adb on every frame/tap would add ~50-100 ms to each
        # interaction (and stall the GUI thread), so we only re-check while
        # still waiting for the boot to finish.
        self._device_ready = False
        self._boot_checked_at = 0.0
        # UI paint timer: pulls the stream's latest frame ~30 times a second.
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._on_frame_tick)

        # Trackpad scrolling is one continuous touch gesture.  Wheel deltas
        # are fed into a pending target and played back a few pixels per frame;
        # the finger lifts only after input stops.  This never drops events and
        # has no synthetic fling, so scrolling stays smooth and proportional.
        self._scroll_active = False
        self._scroll_fx = 0.0
        self._scroll_fy = 0.0
        self._scroll_pending_x = 0.0
        self._scroll_pending_y = 0.0
        self._scroll_move_timer = QTimer(self)
        self._scroll_move_timer.setInterval(16)
        self._scroll_move_timer.timeout.connect(self._scroll_step)
        self._scroll_idle_timer = QTimer(self)
        self._scroll_idle_timer.setSingleShot(True)
        self._scroll_idle_timer.timeout.connect(self._end_scroll_gesture)

        # State
        self._current_apk: Optional[Path] = None
        self._current_package: Optional[str] = None
        self._current_activity: Optional[str] = None
        self._rotation_index: int = 0
        self._install_worker: Optional[APKInstallWorker] = None
        self._emu_worker: Optional[EmulatorWorker] = None
        self._parse_worker: Optional[APKParseWorker] = None
        self._adb_workers: list = []
        # Guards use a real adb probe (worker thread), so block re-entry while
        # one is in flight.
        self._device_checking = False
        # Background worker that detects the performance mode (runs a
        # subprocess, so it must never run on the GUI thread).
        self._profile_worker: Optional[ProfileWorker] = None
        # Buttons currently busy (keyed by the button object).
        self._busy_buttons: list = []
        # Enabled state of each busy button, restored when the action ends.
        self._busy_prev_enabled: dict = {}

        # File watcher (auto reinstall) + thread bridge + status timer
        self._file_watcher = create_file_watcher()
        self._bridge = _SignalBridge(self)
        self._bridge.apk_changed.connect(self._on_apk_changed)
        self._file_watcher.set_callback(self._on_file_changed)

        self._setup_ui()
        self._connect_signals()
        # The preview window is created on demand: it appears only after the
        # emulator is running and the first frame has arrived.
        # Detect the performance mode in the background: it shells out to
        # sysctl, which must never block the GUI thread.
        self._start_profile_detection()
        self._load_controls_settings()
        self._update_emulator_buttons(self._emulator.is_running())
        self._update_app_buttons(False)
        # Apply the persisted display mode (native by default: Screen disabled).
        self._apply_display_mode()
        # Adopt an existing apkpreview_default AVD so upgrades keep working,
        # then show the selected Android version in the Controls tab.
        self._migrate_legacy_android_version()
        self._refresh_selected_android_label()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):  # noqa: N802 (Qt naming)
        """Keep the transparent overlay covering the whole window."""
        try:
            if (obj is self.centralWidget()
                    and event.type() == event.Type.Resize):
                self._overlay.setGeometry(obj.rect())
        except Exception:
            pass
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        try:
            overflow = self._toolbar_required_width() > self.width()
            if hasattr(self, "btn_rotate"):
                self.btn_rotate.setVisible(not overflow)
            if hasattr(self, "btn_screenshot"):
                self.btn_screenshot.setVisible(not overflow)
            if hasattr(self, "btn_reset"):
                self.btn_reset.setVisible(not overflow)
            if hasattr(self, "btn_overflow"):
                self.btn_overflow.setVisible(overflow)
        except Exception:
            pass

    def _toolbar_required_width(self) -> int:
        """Width the toolbar needs with every button visible.

        Uses sizeHint (which does not depend on visibility), so toggling the
        overflow button can never oscillate.
        """
        row = getattr(self, "_toolbar_row", None)
        if row is None:
            return 0
        widgets = (self.btn_open, self.btn_rotate, self.btn_screenshot,
                   self.btn_reset, self.lbl_status, self.btn_emulator,
                   self.btn_dev, self.btn_overflow)
        spacing = max(0, row.spacing())
        return (sum(w.sizeHint().width() for w in widgets)
                + spacing * len(widgets) + 8)

    def _setup_ui(self):
        """Build the window layout from scratch."""
        central = QWidget()
        self.setCentralWidget(central)
        # APKs can be dropped anywhere on the window.
        self.setAcceptDrops(True)
        central.setAcceptDrops(True)

        root = QVBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # (a) Top toolbar row
        root.addLayout(self._build_toolbar())

        # (b) Logcat / Controls tabs fill the whole window
        root.addWidget(self._build_tabs(), 1)

        # (c) Transparent busy overlay (no background) laid over the window.
        self._overlay = LoadingOverlay(central)
        self._overlay.setGeometry(central.rect())
        central.installEventFilter(self)

    def _on_open_github(self):
        """Open the GitHub profile in the default browser."""
        url = "https://github.com/samirhusayn28-dev"
        try:
            if webbrowser.open(url):
                return
        except Exception:
            pass
        try:
            QDesktopServices.openUrl(QUrl(url))
        except Exception:
            pass

    def _on_open_gmail(self):
        """Open the mail client; fall back to copying the address."""
        address = "sameerhussainsoomro22@gmail.com"
        mailto = f"mailto:{address}"
        try:
            if webbrowser.open(mailto):
                return
        except Exception:
            pass
        try:
            if QDesktopServices.openUrl(QUrl(mailto)):
                return
        except Exception:
            pass
        # No mail client available: copy the address to the clipboard.
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(address)
            self._set_status(f"Copied {address} to the clipboard")
        else:
            self._set_status(address)

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.setContentsMargins(0, 0, 0, 0)
        self._toolbar_row = row

        def _compact(btn):
            """Buttons keep their natural width - they never stretch."""
            btn.setSizePolicy(QSizePolicy.Policy.Minimum,
                              QSizePolicy.Policy.Fixed)

        self.btn_open = QPushButton("Open APK")
        self.btn_open.setToolTip("Open an APK file")
        self.btn_open.clicked.connect(self._on_open_clicked)
        _compact(self.btn_open)
        row.addWidget(self.btn_open)

        self.btn_rotate = QPushButton("Rotate")
        self.btn_rotate.setToolTip("Rotate the device screen")
        self.btn_rotate.clicked.connect(self._on_rotate_cycle)
        _compact(self.btn_rotate)
        row.addWidget(self.btn_rotate)

        self.btn_screenshot = QPushButton("Screenshot")
        self.btn_screenshot.setToolTip("Save a screenshot of the device")
        self.btn_screenshot.clicked.connect(self._on_screenshot)
        _compact(self.btn_screenshot)
        row.addWidget(self.btn_screenshot)

        self.btn_reset = QPushButton("Reset size")
        self.btn_reset.setToolTip("Reset the device screen size and density")
        self.btn_reset.clicked.connect(self._on_reset_screen)
        _compact(self.btn_reset)
        row.addWidget(self.btn_reset)

        # Spare space stays empty instead of widening every button.
        row.addStretch(1)

        # Small, unobtrusive status line.
        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: palette(mid); font-size: 11px;")
        self.lbl_status.setMinimumWidth(60)
        self.lbl_status.setMaximumWidth(260)
        self.lbl_status.setSizePolicy(QSizePolicy.Policy.Preferred,
                                     QSizePolicy.Policy.Fixed)
        row.addWidget(self.lbl_status)

        self.btn_emulator = QPushButton("Start Emulator")
        self.btn_emulator.setToolTip("Start or stop the Android emulator")
        self.btn_emulator.clicked.connect(self._on_toggle_emulator)
        _compact(self.btn_emulator)
        row.addWidget(self.btn_emulator)

        # Code-style developer button: opens the developer card popup.
        self.btn_dev = QPushButton("</Dev>")
        self.btn_dev.setToolTip("About the developer")
        self.btn_dev.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dev.setFont(QFont("Menlo, Consolas, monospace", 10))
        self.btn_dev.setStyleSheet(
            "QPushButton {"
            "  background: rgba(0, 122, 204, 40);"
            "  color: #7fc4ff;"
            "  border: 1px solid rgba(0, 122, 204, 120);"
            "  border-radius: 6px;"
            "  padding: 4px 10px;"
            "}"
            "QPushButton:hover {"
            "  background: rgba(0, 122, 204, 90);"
            "  border-color: rgba(0, 122, 204, 220);"
            "}"
        )
        self.btn_dev.setSizePolicy(QSizePolicy.Policy.Fixed,
                                   QSizePolicy.Policy.Fixed)
        self.btn_dev.clicked.connect(self._show_developer_card)
        row.addWidget(self.btn_dev)

        # Overflow menu, shown only when the row genuinely does not fit.
        self.btn_overflow = QToolButton()
        self.btn_overflow.setText("...")
        self.btn_overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._overflow_menu = QMenu(self)
        self.btn_overflow.setMenu(self._overflow_menu)
        for label, handler in (("Rotate", self._on_rotate_cycle),
                                ("Screenshot", self._on_screenshot),
                                ("Reset size", self._on_reset_screen)):
            act = self._overflow_menu.addAction(label)
            act.triggered.connect(handler)
        self.btn_overflow.setVisible(False)
        self.btn_overflow.setSizePolicy(QSizePolicy.Policy.Fixed,
                                       QSizePolicy.Policy.Fixed)
        row.addWidget(self.btn_overflow)

        return row

    def _build_developer_menu(self) -> QMenu:
        """Build the developer card popup (kept separate so tests can use it)."""
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: rgba(28, 28, 32, 240); color: #e8e8ea; "
            "border: 1px solid rgba(255,255,255,50); border-radius: 10px; "
            "padding: 10px; }"
            "QMenu::item { padding: 4px 10px; border-radius: 5px; }"
            "QMenu::item:selected { background: rgba(0, 122, 204, 140); }"
            "QLabel { color: #e8e8ea; padding: 6px 8px; }"
            "QPushButton {"
            "  background: rgba(255,255,255,20); color: #e8e8ea;"
            "  border: 1px solid rgba(255,255,255,50); border-radius: 6px;"
            "  padding: 5px 16px;"
            "}"
            "QPushButton:hover { background: rgba(0, 122, 204, 140); }"
        )

        name = QLabel("Designed & Developed By Samir Husayn")
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setFont(QFont("", 10))
        self._add_menu_widget(menu, name)

        menu.addSeparator()

        self.btn_github = QPushButton("GitHub")
        self.btn_github.setToolTip("Open github.com/samirhusayn28-dev")
        self.btn_github.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_github.clicked.connect(self._on_open_github)
        self._add_menu_widget(menu, self.btn_github)

        self.btn_gmail = QPushButton("Gmail")
        self.btn_gmail.setToolTip(
            "sameerhussainsoomro22@gmail.com\n"
            "Opens your mail client (or copies the address)")
        self.btn_gmail.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gmail.clicked.connect(self._on_open_gmail)
        self._add_menu_widget(menu, self.btn_gmail)
        return menu

    def _show_developer_card(self):
        """Show the developer card as a popup menu under the </Dev> button."""
        menu = self._build_developer_menu()
        menu.exec(self.btn_dev.mapToGlobal(
            QPoint(0, self.btn_dev.height() + 4)))

    @staticmethod
    def _add_menu_widget(menu: QMenu, widget: QWidget):
        """Put a real widget into a QMenu using a QWidgetAction."""
        action = QWidgetAction(menu)
        action.setDefaultWidget(widget)
        menu.addAction(action)
        return action

    def _open_detached_preview(self, show: bool = True):
        """Create the detached preview window (shown unless ``show`` is False)."""
        if self.detached_window is not None:
            return
        try:
            self.detached_window = DetachedPreviewWindow(
                self.preview_pane)
            self.detached_window.close_requested.connect(
                self._close_detached_preview)
            self.detached_window.always_on_top_changed.connect(
                self._on_always_on_top_changed)
            self.detached_window.nav_action.connect(self._on_device_action)
            # Default size: at most ~75% of the available screen height; the
            # window is then sized to the screen + nav bar + thin margin.
            available_h = 800
            try:
                screen = QApplication.primaryScreen()
                if screen is not None:
                    available_h = screen.availableGeometry().height()
            except Exception:
                pass
            target_h = min(round(available_h * 0.75), 900)
            target_w = max(
                240,
                self.detached_window.screen_width_for_window_height(target_h))
            restored = False
            saved = settings.get_screen_window_geometry()
            if saved:
                try:
                    restored = self.detached_window.restoreGeometry(saved)
                except Exception:
                    restored = False
            if not restored:
                self.detached_window.resize(
                    self.detached_window.window_size_for_screen_width(
                        target_w))
            if show:
                self.detached_window.show()
                self.detached_window.raise_()
                self.detached_window.activateWindow()
            self.preview_pane.show()
        except Exception as exc:
            self._set_status(f"Preview window error: {exc}")
            raise

    def _close_detached_preview(self):
        """Close the detached preview window."""
        self._preview_wanted = False
        if self.detached_window is not None:
            try:
                settings.set_screen_window_geometry(
                    self.detached_window.saveGeometry())
            except Exception:
                pass
            self.detached_window.close()
            self.detached_window = None

    def _on_always_on_top_changed(self, checked: bool):
        """Persist the always-on-top preference."""
        settings.always_on_top = bool(checked)
        try:
            self.chk_always_on_top.setChecked(bool(checked))
        except Exception:
            pass
        if self.detached_window is not None:
            self.detached_window.set_always_on_top(bool(checked))

    @pyqtSlot(bool)
    def _on_always_on_top_toggled(self, checked: bool):
        """Apply the always-on-top toggle from the Controls tab."""
        self._on_always_on_top_changed(bool(checked))
        self._set_status(
            "Always on top enabled" if checked else "Always on top disabled")

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setObjectName("mainTabs")
        tabs.setMinimumWidth(200)

        # The Logcat tab hosts a text editor, and creating *any* text editor
        # makes Qt build its font-alias table on macOS (~0.35 s).  The panel is
        # therefore built the first time the user actually opens the tab, which
        # keeps the app's startup fast.
        self.logcat = None
        self._logcat_placeholder = QLabel(
            "The logcat viewer is built the first time this tab is opened.")
        self._logcat_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._logcat_placeholder.setWordWrap(True)
        self._logcat_placeholder.setStyleSheet(
            "color: palette(mid); font-size: 11px;")
        tabs.addTab(self._logcat_placeholder, "Logcat")
        tabs.currentChanged.connect(self._on_tab_changed)

        self.tab_controls = self._build_controls_tab()
        tabs.addTab(self.tab_controls, "Controls")

        # Land on Controls: the Logcat panel is only built on demand.
        tabs.setCurrentIndex(1)
        self.main_tabs = tabs
        return tabs

    @pyqtSlot(int)
    def _logcat_visible(self) -> bool:
        """True only while the Logcat tab is the visible tab."""
        tabs = getattr(self, "main_tabs", None)
        return (self.logcat is not None and tabs is not None
                and tabs.currentIndex() == 0)

    def _on_tab_changed(self, index: int):
        """Build the Logcat panel on first use and stream only while visible."""
        if index == 0:
            self._ensure_logcat_panel()
            # Resume streaming when the tab becomes visible again.
            if self.logcat is not None and self._emulator.is_running():
                worker = getattr(self.logcat, "_worker", None)
                if worker is None or not worker.isRunning():
                    try:
                        self.logcat.start(self._current_package or "")
                    except Exception:
                        pass
            return
        # Logcat hidden: stop the adb logcat process instead of consuming CPU.
        if self.logcat is not None:
            try:
                self.logcat.stop()
            except Exception:
                pass

    def _ensure_logcat_panel(self):
        """Create the Logcat panel on demand (see _build_tabs)."""
        if self.logcat is not None:
            return
        tabs = getattr(self, "main_tabs", None)
        if tabs is None:
            return
        try:
            self.logcat = LogcatPanel()
        except Exception:
            self.logcat = None
            return
        try:
            current = tabs.currentIndex()
            tabs.removeTab(0)
            tabs.insertTab(0, self.logcat, "Logcat")
            tabs.setCurrentIndex(current)
        except Exception:
            pass

    def _build_controls_tab(self) -> QWidget:
        """Compact dark Controls tab: one labelled row per button group.

        The page is a plain widget, not a scroll area: every control fits at
        the window's minimum size, so the panel never shows a scrollbar.
        """
        content = QWidget()
        content.setObjectName("controlsPanel")
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        content.setStyleSheet(_CONTROLS_STYLE)
        outer = QVBoxLayout(content)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(12)

        def heading(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                "color: #ffffff; font-size: 11px; font-weight: 700;")
            return lbl

        def action_button(text: str, attr: str = None) -> QPushButton:
            btn = QPushButton(text)
            btn.setSizePolicy(QSizePolicy.Policy.Minimum,
                              QSizePolicy.Policy.Fixed)
            btn.setMinimumWidth(88)
            if attr:
                setattr(self, attr, btn)
            return btn

        # -- actions ------------------------------------------------------
        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(6)
        actions.setVerticalSpacing(6)
        actions.setColumnMinimumWidth(0, 84)
        # Only an empty trailing column absorbs spare width; buttons keep
        # their natural size.
        actions.setColumnStretch(6, 1)

        def action_row(row: int, title: str, specs):
            actions.addWidget(
                heading(title), row, 0,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            for col, (text, attr) in enumerate(specs, start=1):
                actions.addWidget(action_button(text, attr), row, col)

        action_row(0, "Emulator", [("Start", "btn_emu_start"),
                                   ("Stop", "btn_emu_stop"),
                                   ("Restart", "btn_emu_restart")])
        self.btn_cancel = action_button("Cancel")
        self.btn_cancel.setToolTip("Cancel the running task")
        self.btn_cancel.clicked.connect(self._on_cancel_task)
        self.btn_cancel.setVisible(False)
        actions.addWidget(self.btn_cancel, 0, 4)

        action_row(1, "Snapshot", [("Save", "btn_snap_save"),
                                   ("Load", "btn_snap_load")])
        action_row(2, "App", [("Install", "btn_app_install"),
                              ("Reinstall", "btn_app_reinstall"),
                              ("Launch", "btn_app_launch")])
        action_row(3, "", [("Stop", "btn_app_stop"),
                           ("Uninstall", "btn_app_uninstall"),
                           ("Clear data", "btn_app_clear")])
        action_row(4, "Screen", [("0\u00b0", "btn_rot_0"),
                                 ("90\u00b0", "btn_rot_90"),
                                 ("180\u00b0", "btn_rot_180"),
                                 ("270\u00b0", "btn_rot_270"),
                                 ("Reset", "btn_device_reset")])
        outer.addLayout(actions)

        # -- settings -----------------------------------------------------
        outer.addWidget(heading("Settings"))
        settings_grid = QGridLayout()
        settings_grid.setContentsMargins(0, 0, 0, 0)
        settings_grid.setHorizontalSpacing(10)
        settings_grid.setVerticalSpacing(6)
        settings_grid.setColumnMinimumWidth(0, 84)
        settings_grid.setColumnMinimumWidth(2, 96)
        settings_grid.setColumnStretch(4, 1)

        def add_field(row: int, col: int, text: str, widget: QWidget):
            settings_grid.addWidget(QLabel(text), row, col)
            settings_grid.addWidget(widget, row, col + 1,
                                    Qt.AlignmentFlag.AlignLeft)

        self.combo_display = QComboBox()
        self.combo_display.addItem("Native window", "native")
        self.combo_display.addItem("Embedded (experimental)", "embedded")
        self.combo_display.setMinimumWidth(170)

        self.combo_quality = QComboBox()
        self.combo_quality.addItems(
            ["Low (2M)", "Medium (4M)", "High (8M)", "Max (16M)"])
        self.combo_quality.setCurrentIndex(2)
        self.combo_quality.setMinimumWidth(150)

        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["15 FPS", "24 FPS", "30 FPS", "60 FPS"])
        self.combo_fps.setCurrentText("30 FPS")
        self.combo_fps.setMinimumWidth(110)

        self.spin_idle = QSpinBox()
        self.spin_idle.setRange(0, 120)
        self.spin_idle.setValue(10)
        self.spin_idle.setSpecialValueText("Disabled")
        self.spin_idle.setMinimumWidth(90)

        self.chk_auto_reinstall = QCheckBox()
        self.chk_auto_reinstall.setSizePolicy(QSizePolicy.Policy.Fixed,
                                              QSizePolicy.Policy.Fixed)

        self.chk_always_on_top = QCheckBox("Keep preview on top")
        self.chk_always_on_top.setChecked(settings.always_on_top)
        self.chk_always_on_top.setToolTip(
            "Keep the frameless preview window above all other windows.")

        add_field(0, 0, "Display", self.combo_display)
        add_field(0, 2, "Quality", self.combo_quality)
        add_field(1, 0, "Max FPS", self.combo_fps)
        add_field(1, 2, "Idle timeout", self.spin_idle)
        add_field(2, 0, "Auto reinstall", self.chk_auto_reinstall)
        add_field(2, 2, "Always on top", self.chk_always_on_top)

        # Android version: selected label + chooser on the same row.
        self.lbl_selected_android = QLabel("Not selected")
        self.lbl_selected_android.setMinimumWidth(120)
        self.lbl_selected_android.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.btn_android_version = QPushButton("Change...")
        self.btn_android_version.setToolTip(
            "Select the Android system image (version)")
        self.btn_android_version.setSizePolicy(QSizePolicy.Policy.Fixed,
                                               QSizePolicy.Policy.Fixed)
        version_row = QWidget()
        version_layout = QHBoxLayout(version_row)
        version_layout.setContentsMargins(0, 0, 0, 0)
        version_layout.setSpacing(8)
        version_layout.addWidget(self.lbl_selected_android)
        version_layout.addWidget(self.btn_android_version)
        version_layout.addStretch(1)
        settings_grid.addWidget(QLabel("Android version"), 3, 0)
        settings_grid.addWidget(version_row, 3, 1, 1, 3)

        self.btn_rerun_setup = QPushButton("Re-run setup...")
        self.btn_rerun_setup.setToolTip(
            "Open the setup wizard again (SDK, licenses, components, device, "
            "Android version)")
        self.btn_rerun_setup.setSizePolicy(QSizePolicy.Policy.Minimum,
                                           QSizePolicy.Policy.Fixed)

        self.chk_low_power = QCheckBox("Low-power mode")
        self.chk_low_power.setChecked(settings.low_power_mode)
        self.chk_low_power.setToolTip("Battery saver for the emulator")

        add_field(4, 0, "Setup", self.btn_rerun_setup)
        add_field(4, 2, "Battery", self.chk_low_power)

        outer.addLayout(settings_grid)
        outer.addStretch(1)

        # The macOS style under-sizes button rows in QGridLayout (rows come
        # out a few px shorter than their widgets).  Pin each row to its
        # tallest widget so rows never overlap.
        def _pin_rows(grid: QGridLayout):
            for r in range(grid.rowCount()):
                row_h = 0
                for c in range(grid.columnCount()):
                    item = grid.itemAtPosition(r, c)
                    if item is None:
                        continue
                    widget = item.widget()
                    if widget is not None:
                        row_h = max(row_h, widget.sizeHint().height())
                if row_h:
                    grid.setRowMinimumHeight(r, row_h)

        _pin_rows(actions)
        _pin_rows(settings_grid)

        return content

    def _connect_signals(self):
        """Connect controls that are in scope for this step."""
        # Emulator
        self.btn_emu_start.clicked.connect(self._on_start_emulator)
        self.btn_emu_stop.clicked.connect(self._on_stop_emulator)
        self.btn_emu_restart.clicked.connect(self._on_restart_emulator)

        # App
        self.btn_app_install.clicked.connect(self._on_install_apk)
        self.btn_app_reinstall.clicked.connect(self._on_reinstall_apk)
        self.btn_app_launch.clicked.connect(self._on_launch_app)
        self.btn_app_stop.clicked.connect(self._on_stop_app)
        self.btn_app_uninstall.clicked.connect(self._on_uninstall_app)
        self.btn_app_clear.clicked.connect(self._on_clear_data)

        # Rotate
        self.btn_rotate.clicked.connect(self._on_rotate_cycle)
        self.btn_rot_0.clicked.connect(lambda: self._on_rotate(0))
        self.btn_rot_90.clicked.connect(lambda: self._on_rotate(1))
        self.btn_rot_180.clicked.connect(lambda: self._on_rotate(2))
        self.btn_rot_270.clicked.connect(lambda: self._on_rotate(3))

        # Reset screen + screenshot
        self.btn_reset.clicked.connect(self._on_reset_screen)
        self.btn_device_reset.clicked.connect(self._on_reset_screen)
        self.btn_screenshot.clicked.connect(self._on_screenshot)

        # Snapshots
        self.btn_snap_save.clicked.connect(self._on_save_snapshot)
        self.btn_snap_load.clicked.connect(self._on_load_snapshot)

        # Settings - save immediately on every change
        self.combo_display.currentIndexChanged.connect(self._on_display_mode_changed)
        self.chk_auto_reinstall.toggled.connect(self._on_settings_changed)
        self.spin_idle.valueChanged.connect(self._on_settings_changed)
        self.combo_quality.currentIndexChanged.connect(self._on_settings_changed)
        self.combo_fps.currentIndexChanged.connect(self._on_settings_changed)
        self.chk_always_on_top.toggled.connect(self._on_always_on_top_toggled)

        # Android version chooser
        self.btn_android_version.clicked.connect(self._on_open_android_versions)
        self.btn_rerun_setup.clicked.connect(self._on_rerun_setup)

        # Performance mode + low-power (save immediately)
        self.chk_low_power.toggled.connect(self._on_low_power_changed)

        # NOTE: the status-row timer is wired in __init__; nothing else is left.

    @pyqtSlot(int)
    def _on_fold_screen_changed(self, index: int):
        """Kept for compatibility: there is no foldable any more."""
        return

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _start_profile_detection(self):
        """Detect the performance mode in a background thread."""
        if self._profile_worker is not None:
            return
        worker = ProfileWorker(self._profiler)
        worker.detected.connect(self._on_profile_detected)
        self._profile_worker = worker
        worker.start()

    @pyqtSlot(object)
    def _on_profile_detected(self, mode):
        """Apply the performance mode detected in the background."""
        self._profile_worker = None
        # Respect explicitly-set overrides from the UI.
        if settings.performance_mode != "auto":
            mode = PerformanceMode.from_setting(settings.performance_mode) or mode
        if settings.low_power_mode:
            mode = PerformanceMode.LOW_SPEC
        self._mode = mode

    def _profile_mode(self):
        """Apply the performance mode (re-detects on demand)."""
        try:
            mode = self._profiler.get_mode()
        except Exception:
            mode = PerformanceMode.NORMAL
        # Respect explicitly-set overrides from the UI.
        if settings.performance_mode != "auto":
            mode = PerformanceMode.from_setting(settings.performance_mode) or mode
        if settings.low_power_mode:
            mode = PerformanceMode.LOW_SPEC
        self._mode = mode

    def _on_performance_changed(self, index: int):
        """User changed the performance mode combo."""
        values = ["auto", "low", "normal", "high"]
        choice = values[index] if 0 <= index < len(values) else "auto"
        settings.performance_mode = choice
        # Update the profiler and UI immediately.
        try:
            mode = PerformanceMode.from_setting(choice) or self._profiler.get_mode()
        except Exception:
            mode = PerformanceMode.NORMAL
        self._mode = mode
        self.lbl_mode.setText(f"Mode: {mode.label}")
        self._set_status(f"Performance mode set to {self._mode.label}")

    def _on_low_power_changed(self, checked: bool):
        """User toggled the low-power mode checkbox."""
        settings.low_power_mode = checked
        # Immediately re-apply the mode (may switch to LOW_SPEC).
        self._profile_mode()
        self._set_status(f"Low-power mode {'enabled' if checked else 'disabled'}")

    def _set_status(self, message: str):
        """Show a short status/error message (never raise).

        Routine messages go to the small top-bar label so the transparent
        loader is not flashing over the window on every tap.  Real errors are
        also surfaced on the overlay so they cannot be missed.
        """
        if not message:
            return
        try:
            label = getattr(self, "lbl_status", None)
            if label is not None:
                metrics = label.fontMetrics()
                label.setText(metrics.elidedText(
                    message, Qt.TextElideMode.ElideRight, 330))
                label.setToolTip(message)
        except Exception:
            pass
        lowered = message.lower()
        if any(word in lowered for word in
               ("error", "failed", "✗", "could not", "not available",
                "cancelled", "timed out")):
            try:
                self._overlay.show_message(message, 3000)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Progress feedback (global status bar)
    # ------------------------------------------------------------------
    def set_progress(self, message: str, value: Optional[int] = None):
        """Show the current step on the transparent loader overlay.

        ``value`` of ``None`` means the progress is indeterminate (a spinner);
        an integer 0-100 is appended to the step text.
        """
        text = (message or "").strip()
        if value is not None and value > 0:
            text = f"{text} {int(value)}%" if text else f"{int(value)}%"
        try:
            self._overlay.set_step(text, value)
            self._overlay.set_busy(True, text)
        except Exception:
            pass

    def clear_progress(self, message: str = ""):
        """Hide the loader overlay."""
        try:
            self._overlay.set_busy(False)
            if message:
                self._overlay.show_message(message, 2500)
        except Exception:
            pass

    def set_busy(self, button, busy: bool):
        """Disable a button and show a busy state while its action runs.

        The button's previous enabled state is remembered and restored when
        the action finishes, so a button that was disabled for another reason
        (e.g. "Install" with no APK loaded) stays disabled.
        """
        if button is None:
            return
        try:
            if busy:
                if button not in self._busy_buttons:
                    self._busy_buttons.append(button)
                    self._busy_prev_enabled[button] = button.isEnabled()
                button.setEnabled(False)
                button.setProperty("busy", True)
            else:
                if button in self._busy_buttons:
                    self._busy_buttons.remove(button)
                    previous = self._busy_prev_enabled.pop(button, None)
                    if previous is not None:
                        button.setEnabled(previous)
                button.setProperty("busy", False)
                button.style().unpolish(button)
                button.style().polish(button)
        except Exception:
            pass

    def _clear_busy_buttons(self):
        """Re-enable every button that was marked busy."""
        for button in list(self._busy_buttons):
            try:
                previous = self._busy_prev_enabled.pop(button, None)
                button.setEnabled(True if previous is None else previous)
                button.setProperty("busy", False)
                button.style().unpolish(button)
                button.style().polish(button)
            except Exception:
                pass
        self._busy_buttons = []
        self._busy_prev_enabled = {}

    @pyqtSlot()
    def _on_cancel_task(self):
        """Cancel the running long task (emulator start / install)."""
        cancelled = False
        worker = self._emu_worker
        if worker is not None and worker.isRunning():
            try:
                worker.request_cancel()
                cancelled = True
            except Exception:
                pass
        worker = self._install_worker
        if worker is not None and worker.isRunning():
            try:
                worker.request_cancel()
                cancelled = True
            except Exception:
                pass
        if cancelled:
            self.set_progress("Cancelling...")
            self._set_status("Cancelling...")
        else:
            self.clear_progress()
            self._set_status("Nothing to cancel")

    def _update_emulator_buttons(self, running: bool):
        self.btn_emu_start.setEnabled(not running)
        self.btn_emu_stop.setEnabled(running)
        self.btn_emu_restart.setEnabled(running)
        # Snapshot buttons stay clickable so their handler can report
        # "Start the emulator first" in the status row.
        self.btn_emulator.setText("Stop Emulator" if running else "Start Emulator")
        self.preview_pane.set_controls_enabled(running)

    def _update_app_buttons(self, installed: bool):
        """Enable app buttons: install/reinstall/launch need a loaded APK;
        stop/uninstall/clear need an installed app."""
        apk_loaded = self._current_apk is not None
        self.btn_app_install.setEnabled(apk_loaded)
        self.btn_app_reinstall.setEnabled(apk_loaded)
        self.btn_app_launch.setEnabled(apk_loaded)
        self.btn_app_stop.setEnabled(installed)
        self.btn_app_uninstall.setEnabled(installed)
        self.btn_app_clear.setEnabled(installed)

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].toLocalFile().lower().endswith('.apk'):
                event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1:
                path = Path(urls[0].toLocalFile())
                if path.suffix.lower() == '.apk':
                    self._on_apk_dropped(path)
                    event.acceptProposedAction()

    def closeEvent(self, event):
        self._cleanup()
        event.accept()

    def _cleanup(self):
        # Stop only the things this window owns. We deliberately do NOT kill the
        # user's global adb server or sweep unrelated child processes here.
        try:
            self._frame_timer.stop()
        except Exception:
            pass
        try:
            self._file_watcher.stop()
        except Exception:
            pass
        workers = [self._emu_worker, self._install_worker, self._parse_worker]
        workers.extend(self._adb_workers)
        for worker in workers:
            try:
                if worker is not None and worker.isRunning():
                    worker.wait(2000)
            except Exception:
                pass
        try:
            if self.logcat is not None:
                self.logcat.stop()
        except Exception:
            pass
        # Close the detached preview window first so its closeEvent re-attaches
        # the pane cleanly before the main window tears down.
        try:
            if self.detached_window is not None:
                self.detached_window.close()
                self.detached_window = None
        except Exception:
            pass
        try:
            self._screen.stop()
        except Exception:
            pass
        try:
            self._screen.kill_remote_server()
        except Exception:
            pass
        try:
            self._scrcpy.stop()
        except Exception:
            pass
        try:
            self._emulator.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # APK loading (parse on a worker thread)
    # ------------------------------------------------------------------
    @pyqtSlot(Path)
    def _on_apk_dropped(self, path: Path):
        self._load_apk(path)

    @pyqtSlot()
    def _on_open_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open APK", "", "Android APK (*.apk)")
        if path:
            self._load_apk(Path(path))

    def _load_apk(self, apk_path: Path):
        apk_path = Path(apk_path)
        if not apk_path.exists():
            self._set_status("Error: file not found")
            return
        self._set_status(f"Loading {apk_path.name}...")
        self._parse_worker = APKParseWorker(apk_path)
        self._parse_worker.parsed.connect(self._on_apk_parsed)
        self._parse_worker.start()

    @pyqtSlot(object, object)
    def _on_apk_parsed(self, apk_path: Path, info: dict):
        if not info or not info.get("valid"):
            error = (info or {}).get("error", "Unknown error")
            self._set_status(f"Error: not a valid APK ({error})")
            return

        package = info.get("package")
        activity = info.get("main_activity")

        self._current_apk = apk_path
        self._current_package = None
        self._current_activity = activity
        self._update_apk_card(apk_path, package)
        self._update_app_buttons(False)
        try:
            settings.last_apk_path = apk_path
        except Exception:
            pass
        # Watch the loaded APK for changes (reinstall if auto-reinstall is on)
        try:
            self._file_watcher.set_auto_reinstall(
                self.chk_auto_reinstall.isChecked())
            self._file_watcher.watch(apk_path)
        except Exception:
            pass
        self._set_status(f"Loaded {apk_path.name}")

    # ------------------------------------------------------------------
    # Loaded-APK bookkeeping
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _update_apk_card(self, path: Path, package: Optional[str]):
        """Remember the loaded APK (there is no APK card any more)."""
        self._loaded_apk_info = (path.name, package or "unknown package")

    # ------------------------------------------------------------------
    # App handlers
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_install_apk(self):
        self._run_install()

    @pyqtSlot()
    def _on_reinstall_apk(self):
        self._run_install()

    def _run_install(self):
        if not self._current_apk or not self._current_apk.exists():
            self._set_status("Error: no APK loaded")
            return
        if not settings.has_selected_version:
            self._set_status(
                "Select an Android version first (Controls > Android version)")
            return
        if self._install_worker is not None and self._install_worker.isRunning():
            self._set_status("Install already in progress...")
            return

        scrcpy_available = self._scrcpy.is_available()
        self._set_status(f"Installing {self._current_apk.name}...")

        # Native mode starts the emulator with its own window and does not open
        # a mirror; embedded mode is headless and renders the device in-app.
        native = self._is_native_display()
        self._install_worker = APKInstallWorker(
            self._current_apk, self._emulator, self._adb, self._scrcpy,
            self._mode, scrcpy_available=scrcpy_available,
            no_window=not native, open_window=native, start_mirror=not native)
        self._install_worker.progress.connect(self._on_install_progress)
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.log.connect(self._on_install_log)
        self._install_worker.start()
        # Busy state: the install buttons are disabled until it finishes.
        self.set_busy(self.btn_app_install, True)
        self.set_busy(self.btn_app_reinstall, True)
        self.set_progress(f"Installing {self._current_apk.name}...", None)
        self.btn_cancel.setVisible(True)

    @pyqtSlot(str)
    def _on_install_log(self, message: str):
        """Forward an install-worker log line to the Logcat panel."""
        if self.logcat is None:
            return
        try:
            self.logcat._append_log(message, "verbose")
        except Exception:
            pass

    @pyqtSlot(str)
    def _on_install_progress(self, message: str):
        """Show the current install step in the global progress bar."""
        self.set_progress(message or "Working...", None)
        self._set_status(message)

    @pyqtSlot(bool, str, str, str)
    def _on_install_finished(self, success: bool, message: str, package: str, activity: str):
        self.set_busy(self.btn_app_install, False)
        self.set_busy(self.btn_app_reinstall, False)
        self.btn_cancel.setVisible(False)
        if success:
            self.clear_progress("App installed")
            self._set_status("✓ " + (message or "Installed"))
            self._current_package = package
            if activity:
                self._current_activity = activity
            self._adb.serial = "emulator-5554"
            self._update_app_buttons(True)
            try:
                if self._logcat_visible():
                    self.logcat.start(package)
            except Exception:
                pass
            # The install worker starts + boots the emulator, so it is ready now.
            self._device_ready = True
            self._auto_start_screen()
        else:
            self.clear_progress("Install failed")
            self._set_status(f"✗ {message}")
            # keep the loaded APK so the user can retry; only reflect not-installed
            self._update_app_buttons(False)

    @pyqtSlot()
    def _on_launch_app(self):
        if not self._current_package:
            self._set_status("Error: app not installed")
            return
        activity = self._current_activity
        package = self._current_package
        if activity:
            fn = lambda: self._adb.start_activity(package, activity)
        else:
            fn = lambda: self._adb.shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        self._set_status(f"Launching {package}...")
        self._run_adb(fn)

    @pyqtSlot()
    def _on_stop_app(self):
        if not self._current_package:
            return
        package = self._current_package
        self._set_status(f"Stopping {package}...")
        self._run_adb(lambda: self._adb.force_stop(package))

    @pyqtSlot()
    def _on_uninstall_app(self):
        if not self._current_package:
            return
        package = self._current_package
        reply = QMessageBox.question(
            self, "Uninstall", f"Uninstall {package}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"Uninstalling {package}...")

        def do_uninstall():
            ok, msg = self._adb.uninstall(package)
            return ok, msg

        self._run_adb(do_uninstall, on_done=self._on_uninstall_done)

    @pyqtSlot(bool, str)
    def _on_uninstall_done(self, success: bool, message: str):
        self._set_status(message)
        if success:
            self._current_package = None
            self._update_app_buttons(False)

    @pyqtSlot()
    def _on_clear_data(self):
        if not self._current_package:
            return
        package = self._current_package
        self._set_status(f"Clearing data for {package}...")
        self._run_adb(lambda: self._adb.clear_data(package))

    def _run_adb(self, fn: Callable[[], Tuple[bool, str]], on_done=None):
        """Run an adb callable on a worker thread; keep a strong reference."""
        worker = AdbActionWorker(fn)
        worker.done.connect(on_done or self._on_adb_action_done)
        self._adb_workers.append(worker)

        def _drop():
            if worker in self._adb_workers:
                self._adb_workers.remove(worker)

        worker.finished.connect(_drop)
        worker.start()

    @pyqtSlot(bool, str)
    def _on_adb_action_done(self, success: bool, message: str):
        """Report the result of a short adb action (success tick / error)."""
        if success:
            self.clear_progress("Done")
            self._set_status("✓ " + (message or "Done"))
        else:
            self.clear_progress("Failed")
            self._set_status(f"✗ {message}")

    # ------------------------------------------------------------------
    # Rotate / reset / screenshot
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_rotate_cycle(self):
        """Toolbar Rotate: cycle 0 -> 90 -> 180 -> 270."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._rotation_index = (self._rotation_index + 1) % 4
        self._on_rotate(self._rotation_index)

    @pyqtSlot(int)
    def _on_rotate(self, rotation: int):
        """Set a rotation directly (0, 1, 2, 3 == 0, 90, 180, 270)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._rotation_index = rotation
        self._run_adb(
            lambda: self._adb.set_rotation(rotation),
            on_done=lambda ok, msg: self._set_status(
                f"Rotation set to {rotation * 90}\u00b0" if ok else msg))

    @pyqtSlot()
    def _on_reset_screen(self):
        """Restore default size, density and rotation (wm resets + rotation 0)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return

        self._rotation_index = 0
        self._set_status("Resetting screen to default...")

        def do_reset():
            ok_size, msg_size = self._adb.reset_screen_size()
            ok_density, msg_density = self._adb.reset_density()
            ok_rot, msg_rot = self._adb.set_rotation(0)
            if not (ok_size and ok_density and ok_rot):
                return False, f"Reset failed: {msg_size}; {msg_density}; {msg_rot}"
            return True, ("Screen reset to default (size, density, rotation, "
                          "")

        self._run_adb(do_reset)

    @pyqtSlot()
    def _on_screenshot(self):
        """Save a screenshot to ~/Pictures/AppStation (copied from old window)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return

        pictures_dir = Path.home() / "Pictures" / "AppStation"
        try:
            pictures_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._set_status(f"Screenshot failed: {e}")
            return

        from datetime import datetime
        # Mac local time (NOT the device clock) for the filename.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = pictures_dir / f"screenshot_{timestamp}.png"
        remote_path = f"/sdcard/screenshot_{timestamp}.png"

        def do_screenshot():
            ok, msg = self._adb.screenshot(remote_path)
            if not ok:
                return False, f"Screenshot failed: {msg}"
            ok, msg = self._adb.pull(remote_path, local_path)
            if not ok:
                return False, f"Pull failed: {msg}"
            self._adb.shell(f"rm {remote_path}")
            return True, f"Screenshot saved to {local_path}"

        self._set_status("Taking screenshot...")
        self._run_adb(do_screenshot)

    # ------------------------------------------------------------------
    # Embedded screen (Stage 2a - view only; no touch / keyboard)
    # ------------------------------------------------------------------
    # adb key events for the floating device window's control bar.
    # Numeric Android keycodes (never routed through Qt Key enums, which do
    # not exist for every hardware key and would crash the app).
    _DEVICE_KEYCODES = {
        "back": "4",        # KEYCODE_BACK
        "home": "3",        # KEYCODE_HOME
        "recents": "187",   # KEYCODE_APP_SWITCH
        "vol_up": "24",     # KEYCODE_VOLUME_UP
        "vol_down": "25",   # KEYCODE_VOLUME_DOWN
        "power": "26",      # KEYCODE_POWER
    }

    def _screen_status(self, message: str):
        """Echo a screen-stream stage to stdout and the status row."""
        if not message:
            return
        try:
            print(f"[screen] {message}", flush=True)
        except Exception:
            pass
        self._set_status(message)

    def _show_preview(self):
        """Show the preview window once there is a real screen to show.

        The window is created hidden so frames can be delivered; it only
        becomes visible when the first frame arrives.  Before the emulator is
        started there is no floating bar of navigation buttons.
        """
        self.preview_pane.set_controls_enabled(self._emulator.is_running())
        self._preview_wanted = True
        if self.detached_window is None:
            try:
                self._open_detached_preview(show=False)
            except Exception:
                return
        if self.detached_window is None:
            return
        if not self.screen_view.has_frame():
            return
        self.detached_window.show()
        self.detached_window.raise_()
        self.detached_window.activateWindow()

    def _start_screen_stream(self):
        # Called by the embedded-mode auto-start once a device is ready.
        if not self._emulator.is_running():
            self._screen_status("Start the emulator first")
            return

        # Show a loading message on the detached preview while scrcpy connects
        # (or falls back to adb screencap ~3 fps when scrcpy is unavailable).
        self.screen_view.clear()
        self.screen_view.set_message("Waiting for Android...")
        self._show_preview()
        self._last_frame_seq = -1
        self._frame_timer.start()

        # Enable input only after boot completes; the touch/drag/key handlers
        # also check boot_completed individually as a secondary guard.
        self.screen_view.set_input_enabled(self._boot_completed())

        max_fps = max(1, min(int(settings.scrcpy_max_fps), 30))
        max_size = max(0, int(settings.scrcpy_max_size))
        try:
            self._screen.start(
                self._adb.serial,
                max_size=max_size,
                max_fps=max_fps,
                bitrate=settings.scrcpy_bitrate)
        except Exception as e:  # noqa: BLE001
            self._screen_status(f"Error: {e}")
            self._frame_timer.stop()

    def _stop_screen_stream(self, quiet: bool = False):
        try:
            self._frame_timer.stop()
        except Exception:
            pass
        try:
            self._screen.stop()
        except Exception:
            pass
        # Make sure no scrcpy-server is left running on the device.
        try:
            self._screen.kill_remote_server()
        except Exception:
            pass
        if not quiet:
            self._screen_status("Screen stopped")

    def _auto_start_screen(self):
        """Start the embedded stream once the device is ready.

        The emulator is always started headless, so the live device is mirrored
        inside AppStation regardless of the saved display mode.
        """
        if not self._emulator.is_running():
            return
        if self._screen.is_running():
            self._show_preview()
            return
        self._start_screen_stream()

    @pyqtSlot(str)
    def _on_device_action(self, action: str):
        """Dispatch a device control button press."""
        try:
            if action == "rotate":
                self._on_rotate_cycle()
                return
            if action == "screenshot":
                self._on_screenshot()
                return
            code = self._DEVICE_KEYCODES.get(action)
            if not code:
                return
            if not self._emulator.is_running():
                self._set_status("Start the emulator first")
                return
            label = action.replace("_", " ").title()
            self._set_status(f"Sending {action.replace('_', ' ')}...")
            controller = self._input_controller()
            if controller is not None:
                fn = lambda: controller.key(int(code))
            else:
                fn = lambda: self._adb.input_keyevent(code)
            self._run_adb(
                fn,
                on_done=lambda ok, msg: self._set_status(
                    f"{label} sent" if ok else msg))
        except Exception as e:  # noqa: BLE001
            self._set_status(f"Error: {e}")

    @pyqtSlot(int, int)
    def _on_screen_size_changed(self, width: int, height: int):
        """Keep the preview window at the screen aspect (e.g. after rotate)."""
        window = self.detached_window
        if window is None or width <= 0 or height <= 0:
            return
        try:
            aspect = width / float(height)
            if aspect >= 1.0:
                # Landscape: keep a 360 px tall screen and widen it.
                screen_w = max(240, round(360 * aspect))
            else:
                # Portrait: keep the current screen width and fit the height.
                screen_w = max(240, window.width() - 2 * window._margin)
            size = window.window_size_for_screen_width(screen_w)
            if size != window.size():
                window.resize(size)
        except Exception:
            pass

    def _input_controller(self):
        """Return the low-latency scrcpy input controller, when streaming."""
        try:
            return self._screen.controller()
        except Exception:
            return None

    def _screen_dimensions(self) -> Tuple[float, float]:
        view = self.screen_view
        return (view._screen_w or 360, view._screen_h or 640)

    @pyqtSlot(float, float)
    def _on_screen_touch(self, x: float, y: float):
        """Forward a tap on the preview screen to the Android device."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        if not self._boot_completed():
            return
        controller = self._input_controller()
        if controller is not None:
            w, h = self._screen_dimensions()
            fn = lambda: controller.tap(x, y, w, h)
        else:
            fn = lambda: self._adb.input_touch(x, y)
        self._run_adb(
            fn,
            on_done=lambda ok, msg: (self._set_status(msg) if not ok else
                                    print(f"[screen] tap at ({int(x)},{int(y)}) sent",
                                          flush=True)))

    @pyqtSlot(float, float, float, float)
    def _on_screen_drag(self, x1: float, y1: float, x2: float, y2: float):
        """Forward a swipe gesture to the emulator via adb input swipe."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        if not self._boot_completed():
            self._set_status("Android still booting - wait for boot to complete")
            return
        controller = self._input_controller()
        if controller is not None:
            w, h = self._screen_dimensions()
            fn = lambda: controller.swipe(
                x1, y1, x2, y2, w, h, duration_ms=300)
        else:
            fn = lambda: self._adb.input_swipe(x1, y1, x2, y2)
        self._run_adb(
            fn,
            on_done=lambda ok, msg: self._set_status(
                f"Swipe ({int(x1)},{int(y1)})->({int(x2)},{int(y2)}) "
                f"{'sent' if ok else msg}"))

    @pyqtSlot(float, float, float, float)
    def _on_screen_scroll(self, x: float, y: float,
                          dx: float, dy: float):
        """Feed wheel deltas into one held, frame-paced touch gesture.

        scrcpy's mouse ACTION_SCROLL is ignored by normal Android lists, so we
        drag a virtual finger instead.  Deltas are queued and played back a few
        pixels per frame; no event is dropped and there is no synthetic fling.
        """
        if not self._emulator.is_running() or not self._boot_completed():
            return
        w, h = self._screen_dimensions()
        x = max(6.0, min(float(w) - 7.0, float(x)))
        y = max(6.0, min(float(h) - 7.0, float(y)))

        controller = self._input_controller()
        if controller is None:
            return

        if not self._scroll_active:
            self._scroll_fx, self._scroll_fy = x, y
            controller.touch_down(x, y, w, h)
            self._scroll_active = True

        # dy < 0 (scroll down) drags upward; dx > 0 (scroll right) drags left.
        # 0.5 keeps even a fast trackpad flick gentle.
        self._scroll_pending_x += -float(dx) * 0.5
        self._scroll_pending_y += float(dy) * 0.5

        # Restarting on every event means a long burst keeps the finger down
        # until playback catches up; a final idle timeout lifts it.
        self._scroll_idle_timer.start(120)
        if not self._scroll_move_timer.isActive():
            self._scroll_move_timer.start()

    def _scroll_step(self):
        """Advance the held finger a few pixels toward the pending target."""
        if not self._scroll_active:
            self._scroll_move_timer.stop()
            return
        controller = self._input_controller()
        if controller is None:
            self._scroll_move_timer.stop()
            return

        w, h = self._screen_dimensions()
        w, h = float(w), float(h)
        margin = 8.0
        max_step = 10.0
        dx = max(-max_step, min(max_step, self._scroll_pending_x))
        dy = max(-max_step, min(max_step, self._scroll_pending_y))
        self._scroll_pending_x -= dx
        self._scroll_pending_y -= dy

        nx = self._scroll_fx + dx
        ny = self._scroll_fy + dy

        # If the finger would run off the content while more travel is still
        # pending, restart at the opposite side.  This lets a long scroll keep
        # going without ever dropping the user's input.
        need_rebase = (
            (dx < -0.5 and nx < margin and self._scroll_pending_x < -0.5) or
            (dx > 0.5 and nx > w - margin and self._scroll_pending_x > 0.5) or
            (dy < -0.5 and ny < margin and self._scroll_pending_y < -0.5) or
            (dy > 0.5 and ny > h - margin and self._scroll_pending_y > 0.5)
        )
        if need_rebase:
            controller.touch_up(self._scroll_fx, self._scroll_fy, w, h)
            self._scroll_fx = w * (0.85 if dx < 0 else (0.15 if dx > 0 else 0.5))
            self._scroll_fy = h * (0.82 if dy < 0 else (0.18 if dy > 0 else 0.5))
            controller.touch_down(self._scroll_fx, self._scroll_fy, w, h)
            nx = self._scroll_fx + dx
            ny = self._scroll_fy + dy

        nx = max(margin, min(w - margin, nx))
        ny = max(margin, min(h - margin, ny))
        if (nx, ny) != (self._scroll_fx, self._scroll_fy):
            controller.touch_move(nx, ny, w, h)
        self._scroll_fx, self._scroll_fy = nx, ny

        if (abs(self._scroll_pending_x) > 0.5 or
                abs(self._scroll_pending_y) > 0.5):
            self._scroll_idle_timer.start(120)
        else:
            self._scroll_move_timer.stop()

    def _end_scroll_gesture(self):
        """Lift the virtual finger once scrolling has stopped."""
        if not self._scroll_active:
            return
        self._scroll_active = False
        self._scroll_move_timer.stop()
        self._scroll_idle_timer.stop()
        self._scroll_pending_x = 0.0
        self._scroll_pending_y = 0.0
        controller = self._input_controller()
        if controller is not None:
            w, h = self._screen_dimensions()
            controller.touch_up(self._scroll_fx, self._scroll_fy, w, h)

    @pyqtSlot()
    def _on_screen_drawer(self):
        """Open the Android app drawer with a bottom-to-top swipe."""
        if not self._emulator.is_running():
            return
        if not self._boot_completed():
            return
        view = self.screen_view
        w = view._screen_w or 360
        h = view._screen_h or 640
        x = w / 2.0
        y1 = h * 0.85
        y2 = h * 0.22
        controller = self._input_controller()
        if controller is not None:
            fn = lambda: controller.swipe(
                x, y1, x, y2, w, h, duration_ms=240)
        else:
            fn = lambda: self._adb.input_swipe(x, y1, x, y2, 240)
        self._run_adb(fn, on_done=lambda ok, msg: None)

    @pyqtSlot(int)
    def _on_screen_key(self, key: int):
        """Forward a key event to the emulator."""
        try:
            if not self._emulator.is_running():
                return
            if not self._boot_completed():
                return
            # Map common Qt key codes to Android keyevent codes
            keycode = self._qt_key_to_android(key)
            if keycode is not None:
                controller = self._input_controller()
                if controller is not None:
                    fn = lambda: controller.key(int(keycode))
                else:
                    fn = lambda: self._adb.input_keyevent(keycode)
                self._run_adb(fn, on_done=lambda ok, msg: None)
        except Exception as e:  # noqa: BLE001
            self._set_status(f"Error: {e}")

    def _qt_key_to_android(self, qt_key: int):
        """Map Qt key codes to Android keyevent codes."""
        # Numeric Android keycodes: accepted by both `adb shell input keyevent`
        # and the scrcpy control channel (ENTER=66, ESCAPE=111).
        mapping = {
            Qt.Key.Key_Enter: "66",
            Qt.Key.Key_Return: "66",
            Qt.Key.Key_Escape: "111",
        }
        return mapping.get(qt_key)

    def _boot_completed(self) -> bool:
        """Check if Android has finished booting (sys.boot_completed == 1).

        The result is cached once true and re-probed at most every 2 s while
        false.  This keeps taps/frames from spawning an adb process each time.
        """
        if self._device_ready:
            return True
        if not self._emulator.is_running():
            self._device_ready = False
            return False
        now = time.monotonic()
        if now - self._boot_checked_at < 2.0:
            return False
        self._boot_checked_at = now
        try:
            result = self._adb._run(
                ["shell", "getprop", "sys.boot_completed"], timeout=5)
            ok = (result.returncode == 0
                  and result.stdout.strip() == "1")
        except Exception:
            ok = False
        if ok:
            self._device_ready = True
        return ok

    @pyqtSlot()
    def _on_frame_tick(self):
        """Pull the latest decoded frame (~30 Hz) and paint it once."""
        try:
            image, seq = self._screen.get_latest_frame()
        except Exception:
            return
        if image is None or seq == self._last_frame_seq:
            return
        self._last_frame_seq = seq
        try:
            self.screen_view.set_frame(image)
        except Exception:
            pass
        # First frame: the preview window can appear now (screen + nav bar).
        if (self._preview_wanted and self.detached_window is not None
                and not self.detached_window.isVisible()):
            self.detached_window.show()
            self.detached_window.raise_()
            self.detached_window.activateWindow()
        # Enable input once the first frame arrives (boot likely complete).
        self.screen_view.set_input_enabled(self._boot_completed())

    @pyqtSlot(float)
    def _on_screen_painted(self, elapsed_ms: float):
        """Record paint time for the stream's 5-second stats line."""
        try:
            self._screen.note_painted(elapsed_ms)
        except Exception:
            pass

    @pyqtSlot(str)
    def _on_screen_status(self, message: str):
        # Stage message from the stream worker (core already printed it).
        # The 5 s diagnostics line is for the console/log only.
        if message and not message.startswith("stats "):
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_note(self, message: str):
        # Notes are shown on the loader overlay, never on top of the screen.
        if message:
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_error(self, message: str):
        if message:
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_state(self, state: str):
        if state == "streaming":
            self._set_status("Screen streaming")
        elif state == "fallback":
            self._set_status("Using the adb screencap fallback (the emulator "
                             "is not sending video frames)")
        elif state == "stopped":
            # Stream ended on its own; keep the last frame (or the device
            # frame) visible so the preview never goes blank.
            try:
                self._frame_timer.stop()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Snapshots  (copied from old window; blocking adb on a worker)
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_save_snapshot(self):
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._set_status("Saving snapshot...")
        self._run_adb(lambda: self._emulator.save_snapshot("user_snapshot"))

    @pyqtSlot()
    def _on_load_snapshot(self):
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._set_status("Loading snapshot...")
        self._run_adb(lambda: self._emulator.load_snapshot("user_snapshot"))

    # ------------------------------------------------------------------
    # Settings (load from core.settings, save immediately on change)
    # ------------------------------------------------------------------
    def _load_controls_settings(self):
        """Load persisted values into the Controls-tab widgets (no writes)."""
        for w in (self.combo_display, self.chk_auto_reinstall, self.spin_idle,
                  self.combo_quality, self.combo_fps):
            w.blockSignals(True)

        display_idx = self.combo_display.findData(settings.display_mode)
        if display_idx >= 0:
            self.combo_display.setCurrentIndex(display_idx)
        self.chk_auto_reinstall.setChecked(settings.auto_reinstall)
        self.spin_idle.setValue(settings.idle_timeout_minutes)

        bitrate_map = {"2M": 0, "4M": 1, "8M": 2, "16M": 3}
        self.combo_quality.setCurrentIndex(
            bitrate_map.get(settings.scrcpy_bitrate, 2))

        fps_map = {15: 0, 24: 1, 30: 2, 60: 3}
        self.combo_fps.setCurrentIndex(
            fps_map.get(settings.scrcpy_max_fps, 2))

        for w in (self.combo_display, self.chk_auto_reinstall, self.spin_idle,
                  self.combo_quality, self.combo_fps):
            w.blockSignals(False)

        self._file_watcher.set_auto_reinstall(
            self.chk_auto_reinstall.isChecked())

    @pyqtSlot()
    def _on_settings_changed(self):
        """Persist all four settings and apply to the live subsystems."""
        auto_reinstall = self.chk_auto_reinstall.isChecked()
        idle_timeout = self.spin_idle.value()
        bitrate = ["2M", "4M", "8M", "16M"][self.combo_quality.currentIndex()]
        fps_text = self.combo_fps.currentText()
        try:
            max_fps = int(fps_text.split()[0])
        except (ValueError, IndexError):
            max_fps = 30

        settings.auto_reinstall = auto_reinstall
        settings.idle_timeout_minutes = idle_timeout
        settings.scrcpy_bitrate = bitrate
        settings.scrcpy_max_fps = max_fps

        self._file_watcher.set_auto_reinstall(auto_reinstall)
        self._scrcpy.set_bitrate(bitrate)
        self._scrcpy.set_max_fps(max_fps)

        self._set_status("Settings saved")

    # ------------------------------------------------------------------
    # Display mode (native emulator window vs. embedded scrcpy view)
    # ------------------------------------------------------------------
    def _is_native_display(self) -> bool:
        """True when the device is shown in the emulator's own window."""
        try:
            return settings.display_mode != "embedded"
        except Exception:
            return True

    @pyqtSlot(int)
    def _on_display_mode_changed(self, _index: int):
        """Persist the display mode immediately and apply what we can live."""
        mode = self.combo_display.currentData() or "native"
        settings.display_mode = mode
        if self._emulator.is_running():
            self._set_status("Restart the emulator to apply")
        self._apply_display_mode()

    def _apply_display_mode(self):
        """Apply the current display mode to the embedded screen.

        The in-app preview stays visible in both modes so the selected
        device's frame/cutout can always be seen.  Native mode simply does not
        start the scrcpy mirror; the emulator's own window shows the live
        device.  A running emulator keeps its current windowing until it is
        restarted (start/restart read the mode fresh).
        """
        if self._is_native_display() and self._screen.is_running():
            self._stop_screen_stream(quiet=True)

    # ------------------------------------------------------------------
    # Android version (system image) selection
    # ------------------------------------------------------------------
    def _migrate_legacy_android_version(self):
        """Auto-select an existing ``apkpreview_default`` AVD on first run.

        Keeps a pre-upgrade install working (and listed as an installed
        version) without deleting or re-downloading anything.
        """
        try:
            if settings.has_selected_version:
                return
            legacy = self._sdk.find_legacy_avd()
            if legacy is None:
                return
            avd_name, version = legacy
            settings.select_android_version(
                version.api_level, version.variant, avd_name)
        except Exception:
            pass

    def _refresh_selected_android_label(self):
        """Show the selected version in the Controls tab."""
        try:
            api = settings.selected_api_level
            variant = settings.selected_variant
            if api > 0:
                name = ANDROID_VERSION_NAMES.get(api, f"Android {api}")
                self.lbl_selected_android.setText(
                    f"{name} (API {api}, {variant or 'default'})")
            else:
                self.lbl_selected_android.setText("Not selected")
        except Exception:
            pass

    @pyqtSlot()
    def _on_open_android_versions(self):
        """Open the Android versions dialog (list/download/remove/select)."""
        try:
            dialog = AndroidVersionsDialog(self._sdk, self,
                                           emulator=self._emulator)
        except Exception as e:
            self._set_status(f"Could not open Android versions: {e}")
            return
        dialog.version_selected.connect(self._on_android_version_selected)
        dialog.exec()
        # A version may have been removed without a selection change.
        self._refresh_selected_android_label()

    @pyqtSlot()
    def _on_rerun_setup(self):
        """Open the resumable setup wizard again."""
        from .setup_wizard import SetupWizard
        wizard = SetupWizard(self, force=True)
        if wizard.exec():
            self.refresh_after_setup()
            self._set_status("Setup completed")

    def refresh_after_setup(self):
        """Re-read the SDK/ADB paths and selected version after the wizard."""
        self._sdk = SDKManager()
        self._adb = ADB()
        self._migrate_legacy_android_version()
        self._refresh_selected_android_label()
        self._update_emulator_buttons(self._emulator.is_running())

    @pyqtSlot(int, str)
    def _on_android_version_selected(self, api_level: int, variant: str):
        """React to a version being selected in the dialog."""
        self._refresh_selected_android_label()
        name = ANDROID_VERSION_NAMES.get(api_level, f"Android {api_level}")
        if self._emulator.is_running():
            self._set_status("Stop the emulator to switch version")
        else:
            self._set_status(
                f"Android version set to {name} (API {api_level}, "
                f"{variant or 'default'})")

    # ------------------------------------------------------------------
    # File watcher -> auto reinstall (bridge onto the UI thread)
    # ------------------------------------------------------------------
    def _on_file_changed(self, path: Path):
        """Called from the watcher thread; forward to the UI thread."""
        try:
            self._bridge.apk_changed.emit(path)
        except Exception:
            pass

    @pyqtSlot(object)
    def _on_apk_changed(self, path: Path):
        self._set_status(f"APK changed: {Path(path).name}")
        if self.chk_auto_reinstall.isChecked():
            self._run_install()

    # ------------------------------------------------------------------
    # Emulator start/stop/restart
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_start_emulator(self):
        self._set_status("Starting emulator...")
        self._start_emulator_action("start")

    @pyqtSlot()
    def _on_stop_emulator(self):
        self._set_status("Stopping emulator...")
        self._hide_preview_now()
        self._start_emulator_action("stop")

    @pyqtSlot()
    def _on_restart_emulator(self):
        self._set_status("Restarting emulator...")
        self._hide_preview_now()
        self._start_emulator_action("restart")

    def _hide_preview_now(self):
        """Hide the preview as soon as the device starts going away."""
        self._preview_wanted = False
        if self.detached_window is not None:
            self.detached_window.hide()

    @pyqtSlot()
    def _on_toggle_emulator(self):
        if self._emulator.is_running():
            self._on_stop_emulator()
        else:
            self._on_start_emulator()

    def _start_emulator_action(self, action: str):
        if not settings.has_selected_version:
            self._set_status(
                "Select an Android version first (Controls > Android version)")
            return
        if self._emu_worker is not None and self._emu_worker.isRunning():
            self._set_status("Emulator action already in progress...")
            return
        scrcpy_available = self._scrcpy.is_available()
        # Always start the emulator headless so no separate emulator window ever
        # appears; the live device is shown unframed inside AppStation.
        self._emu_worker = EmulatorWorker(
            action, self._emulator, self._scrcpy, self._mode, scrcpy_available,
            no_window=True, open_window=False)
        self._emu_worker.result.connect(self._on_emulator_action_done)
        self._emu_worker.progress.connect(self._on_emulator_progress)
        self._emu_worker.log.connect(self._on_emulator_log)
        self._emu_worker.start()
        # Show loading feedback while the emulator boots (can take 30-60s).
        self.btn_emu_start.setEnabled(False)
        self.btn_emu_stop.setEnabled(False)
        self.btn_emu_restart.setEnabled(False)
        self.btn_emulator.setEnabled(False)
        if hasattr(self, "lbl_emu_loading"):
            self.lbl_emu_loading.setText("Starting emulator...")
            self.lbl_emu_loading.setVisible(True)
        self._set_status("Starting emulator...")
        if action == "stop":
            self.set_progress("Stopping emulator...", None)
        elif action == "restart":
            self.set_progress("Restarting emulator...", 0)
        else:
            self.set_progress("Starting emulator...", 0)
        self.btn_cancel.setVisible(True)

    @pyqtSlot(int, str)
    def _on_emulator_progress(self, percent: int, message: str):
        """Show the real boot progress (percent + stage) on the loader."""
        self.set_progress(message or "Working...", percent)

    @pyqtSlot(str)
    def _on_emulator_log(self, message: str):
        """Surface emulator warnings (acceleration/GPU) and log the rest."""
        if not message:
            return
        try:
            if "WARNING" in str(message).upper():
                self._set_status(str(message))
        except Exception:
            pass
        try:
            if self.logcat is not None:
                self.logcat._append_log(str(message), "verbose")
        except Exception:
            pass

    @pyqtSlot(str, bool, str)
    def _on_emulator_action_done(self, action: str, success: bool, message: str):
        if action in ("stop", "restart"):
            # Emulator stopping/restarting: close the device window and stream.
            self._device_ready = False
            self._boot_checked_at = 0.0
            self._preview_wanted = False
            if self.detached_window is not None:
                self.detached_window.hide()
            self._stop_screen_stream(quiet=True)
        running = self._emulator.is_running()
        self._update_emulator_buttons(running)
        # Hide loading feedback and re-enable buttons.
        self.btn_emu_start.setEnabled(not running)
        self.btn_emu_stop.setEnabled(running)
        self.btn_emu_restart.setEnabled(running)
        self.btn_emulator.setEnabled(True)
        self.btn_cancel.setVisible(False)
        if hasattr(self, "lbl_emu_loading"):
            self.lbl_emu_loading.setVisible(False)
        if success:
            self.clear_progress("Emulator ready")
            self._set_status("✓ " + (message or "Emulator ready"))
            if action in ("start", "restart"):
                # start()/restart() wait for sys.boot_completed == 1, so input
                # can be enabled immediately without another adb round-trip.
                self._device_ready = True
                self._set_status("✓ Emulator ready - starting preview stream...")
                try:
                    if self._logcat_visible():
                        self.logcat.start(self._current_package or "")
                except Exception:
                    pass
                # Emulator is ready: open the device window and start streaming.
                self._auto_start_screen()
        else:
            self.clear_progress("Failed")
            self._set_status(f"✗ Error: {message}")
