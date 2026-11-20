"""Device screen widget for AppStation.

The widget shows **only** the Android screen: no device body, no bezel, no
rim, no cutout, no drop shadow and no on-screen overlay.  The streamed image
is scaled to the widget size while preserving the screen's aspect ratio, so
the preview can be freely resized (by dragging the window edges) and the
screen always fills it edge to edge.

Everything about input is handled here:

* a press/release pair is a tap,
* a press/drag/release is a swipe,
* the wheel scrolls,
* a press that is held becomes a long press,
* Ctrl+drag performs a pinch gesture,
* key presses are forwarded as text/keys.

All coordinates are mapped from widget (logical) pixels to the device's
physical screen pixels, taking the current scale factor and the device pixel
ratio (Retina / Windows scaling) into account.
"""

import time

from PyQt6.QtCore import (QPointF, QRect, QRectF, QSize, QTimer, Qt,
                          pyqtSignal)
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PyQt6.QtWidgets import QSizePolicy, QWidget


class ScreenView(QWidget):
    """Shows the Android screen, scaled to fit the widget."""

    # Elapsed milliseconds spent in the last paintEvent (stream stats).
    painted = pyqtSignal(float)
    # Emitted when the user taps on the screen: (screen_x, screen_y) in
    # physical device pixels.
    touch = pyqtSignal(float, float)
    # Emitted when the user drags on the screen: (start_x, start_y, end_x,
    # end_y) in physical device pixels.  Used for swipe gestures.
    drag = pyqtSignal(float, float, float, float)
    # Emitted when the user scrolls: (screen_x, screen_y, delta_x, delta_y).
    # The position is the point under the cursor, in screen pixels, so the
    # scroll goes to the widget the user is actually pointing at instead of
    # always landing in the middle of the Android screen.
    scroll = pyqtSignal(float, float, float, float)
    # Emitted when a two-finger trackpad swipe up is recognised.  Android's
    # app drawer is opened by swiping up from the bottom of the screen, which
    # a plain scroll event cannot express.
    app_drawer = pyqtSignal()
    # Emitted when a key is pressed: key_code.
    key_pressed = pyqtSignal(int)
    # Emitted when the device screen size changes (e.g. after a rotation):
    # (width, height) in physical pixels.
    screen_size_changed = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = None
        self._note = ""
        self._message = ""
        self._screen_w = 0
        self._screen_h = 0
        self._tap_pos = None  # (widget_x, widget_y) of the last tap
        self._tap_timer = QTimer(self)
        self._tap_timer.setSingleShot(True)
        self._tap_timer.timeout.connect(self._clear_tap)
        # Long press / drag tracking
        self._press_pos = None  # (widget_x, widget_y) of last press
        self._press_time = 0.0
        self._long_press_timer = QTimer(self)
        self._long_press_timer.setSingleShot(True)
        self._long_press_timer.timeout.connect(self._emit_long_press)
        self._drag_start = None  # (widget_x, widget_y) for swipe detection
        self._drag_current = None
        self._ctrl_pressed = False
        # True once the long-press fired, so the release does not also send a
        # tap (a long press and a tap are different gestures).
        self._long_press_fired = False
        # Trackpad two-finger scroll accumulation for the app-drawer gesture.
        self._wheel_accum = 0.0
        self._wheel_reset_timer = QTimer(self)
        self._wheel_reset_timer.setSingleShot(True)
        self._wheel_reset_timer.timeout.connect(self._reset_wheel_accum)
        self._drawer_last_at = 0.0
        # Ignore the tail of the same physical swipe after the drawer fires.
        self._wheel_suppress_until = 0.0
        # Input enabled only after boot_completed
        self._input_enabled = False
        # Rounded corners: the image is clipped with a QPainterPath and the
        # widget mask follows, so the corners are both invisible and not
        # clickable.  The radius is recomputed on every resize.
        self._corner_radius = 0
        # Needed for keyboard forwarding: without focus the preview never
        # receives key events (the window is frameless and has no chrome).
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------
    # Streamed image
    # ------------------------------------------------------------------
    def set_frame(self, image: QImage):
        """Show a new frame (the newest one always wins)."""
        self._image = image
        if image is not None and not image.isNull() and image.height() > 0:
            self._update_screen_size(image.width(), image.height())
        self.update()

    def has_frame(self) -> bool:
        """True once a streamed frame has been shown."""
        return self._image is not None and not self._image.isNull()

    def _update_screen_size(self, width: int, height: int):
        if width > 0 and height > 0 and (width, height) != (
                self._screen_w, self._screen_h):
            self._screen_w = int(width)
            self._screen_h = int(height)
            try:
                self.screen_size_changed.emit(int(width), int(height))
            except Exception:
                pass

    def set_note(self, text: str):
        self._note = text or ""
        self.update()

    def set_message(self, text: str):
        """Show a short message where the screen would be (no frame yet)."""
        self._message = text or ""
        self.update()

    def clear(self):
        self._image = None
        self._note = ""
        self._message = ""
        self.update()

    def set_screen_size(self, width: int, height: int):
        """Tell the widget the device screen size (for coordinate mapping)."""
        self._update_screen_size(width, height)
        self.update()

    def _clear_tap(self):
        self._tap_pos = None
        self.update()

    def _reset_wheel_accum(self):
        self._wheel_accum = 0.0

    # ------------------------------------------------------------------
    # Rounded corners
    # ------------------------------------------------------------------
    @staticmethod
    def rounded_radius(width: int) -> int:
        """Corner radius for a screen ``width`` px wide (14..26 px).

        A typical 400 px preview gets the requested ~20 px radius; very
        small previews are clamped to 14 px and large ones to 26 px.
        """
        return max(14, min(26, int(round(width * 0.05))))

    def _effective_radius(self) -> int:
        """Radius for the current widget width (works before show too)."""
        if self.width() <= 0:
            return 0
        return self.rounded_radius(self.width())

    def corner_radius(self) -> int:
        return self._effective_radius()

    def set_corner_radius(self, radius: int):
        self._corner_radius = max(0, int(radius))
        self.update()

    @staticmethod
    def _inside_rounded(x: float, y: float, target: QRectF,
                        radius: float) -> bool:
        """True when (x, y) is inside the rounded rect, not in a corner."""
        if radius <= 0:
            return True
        r = float(radius)
        if x < target.left() + r:
            cx = target.left() + r
        elif x > target.right() - r:
            cx = target.right() - r
        else:
            cx = None
        if y < target.top() + r:
            cy = target.top() + r
        elif y > target.bottom() - r:
            cy = target.bottom() - r
        else:
            cy = None
        if cx is None or cy is None:
            return True
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    # ------------------------------------------------------------------
    # Input enabling
    # ------------------------------------------------------------------
    def set_input_enabled(self, enabled: bool):
        """Enable/disable input forwarding to the emulator."""
        self._input_enabled = bool(enabled)
        if not enabled:
            self._tap_pos = None
            self._press_pos = None
            self._drag_start = None

    def is_input_enabled(self) -> bool:
        return self._input_enabled

    def _emit_long_press(self):
        """Emit a long-press touch at the press position."""
        if self._press_pos is not None and self._input_enabled:
            wx, wy = self._press_pos
            pt = self._widget_to_screen(wx, wy)
            if pt is not None:
                self._long_press_fired = True
                self.touch.emit(pt[0], pt[1])
                self._tap_pos = self._press_pos
                self._tap_timer.start(300)
                self.update()

    # ------------------------------------------------------------------
    # Coordinate mapping: widget -> screen pixels
    # ------------------------------------------------------------------
    def _fit_rect(self) -> QRectF:
        """The rect the screen image is drawn into (aspect preserved)."""
        rect = self.rect()
        if self._image is None or self._image.isNull():
            return QRectF(rect)
        iw, ih = self._image.width(), self._image.height()
        if iw <= 0 or ih <= 0:
            return QRectF(rect)
        scale = min(rect.width() / iw, rect.height() / ih)
        w = iw * scale
        h = ih * scale
        return QRectF((rect.width() - w) / 2.0, (rect.height() - h) / 2.0,
                      w, h)

    def _widget_to_screen(self, wx: float, wy: float):
        """Map a widget (logical) coordinate to screen (physical) pixels.

        Returns (screen_x, screen_y) or None when the point is outside the
        screen image (letterbox area).  Uses the current scale factor and the
        device pixel ratio so Retina / Windows scaling is handled.
        """
        if self._image is None or self._image.isNull():
            return None
        if not self._input_enabled:
            return None
        if self._screen_w <= 0 or self._screen_h <= 0:
            return None

        target = self._fit_rect()
        if target.width() <= 0 or target.height() <= 0:
            return None

        # Widget coordinates are logical; the target rect is in logical units
        # too, so the mapping is a plain ratio inside the target rect.
        if not (target.left() <= wx < target.right()):
            return None
        if not (target.top() <= wy < target.bottom()):
            return None
        # Ignore the rounded-off corners: they are not part of the screen.
        if not self._inside_rounded(wx, wy, target, self._effective_radius()):
            return None

        rel_x = (wx - target.left()) / target.width()
        rel_y = (wy - target.top()) / target.height()
        screen_x = rel_x * self._screen_w
        screen_y = rel_y * self._screen_h
        return (max(0.0, min(self._screen_w - 1.0, screen_x)),
                max(0.0, min(self._screen_h - 1.0, screen_y)))

    # ------------------------------------------------------------------
    # Mouse events: touch, drag, swipe, long press
    # ------------------------------------------------------------------
    def mousePressEvent(self, event):  # noqa: N802 (Qt naming)
        # Option/Alt+drag moves the preview window: leave that gesture to the
        # parent.  Every other press - including presses right at the screen
        # border - belongs to Android, so the whole screen stays touchable.
        if event.button() == Qt.MouseButton.LeftButton and (
                event.modifiers()
                & (Qt.KeyboardModifier.AltModifier
                   | Qt.KeyboardModifier.MetaModifier)):
            event.ignore()
            return

        if not self._input_enabled:
            super().mousePressEvent(event)
            return

        if event.button() == Qt.MouseButton.LeftButton:
            # Keyboard input goes to the preview: clicking it focuses it.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            wx = event.position().x()
            wy = event.position().y()
            self._press_pos = (wx, wy)
            self._press_time = time.perf_counter()
            self._drag_start = (wx, wy)
            self._drag_current = (wx, wy)
            self._long_press_fired = False
            self._ctrl_pressed = bool(
                event.modifiers() & Qt.KeyboardModifier.ControlModifier)

            # Long press timer: 600ms
            self._long_press_timer.start(600)

            pt = self._widget_to_screen(wx, wy)
            if pt is not None:
                # Don't emit touch yet - wait for release to distinguish a
                # tap from a drag start.  Show visual feedback immediately.
                self._tap_pos = (wx, wy)
                self._tap_timer.start(300)
                self.update()
                print(f"[screen] press widget=({wx:.0f},{wy:.0f}) "
                      f"-> screen=({pt[0]:.0f},{pt[1]:.0f})", flush=True)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 (Qt naming)
        if not self._input_enabled:
            super().mouseMoveEvent(event)
            return

        if (event.buttons() & Qt.MouseButton.LeftButton
                and self._drag_start is not None):
            wx = event.position().x()
            wy = event.position().y()
            self._drag_current = (wx, wy)

            # Ctrl+drag = pinch gesture
            if self._ctrl_pressed:
                pt = self._widget_to_screen(wx, wy)
                if pt is not None:
                    self.touch.emit(pt[0], pt[1])
                    self._tap_pos = (wx, wy)
                    self._tap_timer.start(100)
                    self.update()

            # Cancel long press if dragged too far
            if self._press_pos is not None:
                dx = wx - self._press_pos[0]
                dy = wy - self._press_pos[1]
                if dx * dx + dy * dy > (20 * self.devicePixelRatio()) ** 2:
                    self._long_press_timer.stop()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt naming)
        if not self._input_enabled:
            super().mouseReleaseEvent(event)
            return

        self._long_press_timer.stop()

        if event.button() == Qt.MouseButton.LeftButton:
            wx = event.position().x()
            wy = event.position().y()

            # A tap is a press/release without significant movement.
            if self._drag_start is not None and not self._long_press_fired:
                drag_dx = wx - self._drag_start[0]
                drag_dy = wy - self._drag_start[1]
                drag_dist_sq = drag_dx * drag_dx + drag_dy * drag_dy
                min_drag = (30 * self.devicePixelRatio()) ** 2

                if drag_dist_sq > min_drag:
                    # It's a swipe - emit the drag signal.
                    pt_start = self._widget_to_screen(
                        self._drag_start[0], self._drag_start[1])
                    pt_end = self._widget_to_screen(wx, wy)
                    if pt_start is not None and pt_end is not None:
                        self.drag.emit(
                            pt_start[0], pt_start[1],
                            pt_end[0], pt_end[1])
                        print(f"[screen] swipe "
                              f"({pt_start[0]:.0f},{pt_start[1]:.0f}) -> "
                              f"({pt_end[0]:.0f},{pt_end[1]:.0f})", flush=True)
                else:
                    pt = self._widget_to_screen(wx, wy)
                    if pt is not None:
                        self.touch.emit(pt[0], pt[1])
                        self._tap_pos = (wx, wy)
                        self._tap_timer.start(300)
                        self.update()
                        print(f"[screen] tap at "
                              f"({pt[0]:.0f},{pt[1]:.0f})", flush=True)

            self._press_pos = None
            self._drag_start = None
            self._drag_current = None
            self._long_press_fired = False

        super().mouseReleaseEvent(event)

    def wheelEvent(self, event):  # noqa: N802 (Qt naming)
        if not self._input_enabled:
            super().wheelEvent(event)
            return

        now = time.monotonic()
        if now < self._wheel_suppress_until:
            event.accept()
            return

        angle = event.angleDelta()
        pixel = event.pixelDelta()
        dx, dy = angle.x(), angle.y()

        # Two-finger trackpad swipe up == Android "open app drawer".  A slow
        # swipe arrives as many small wheel events, so accumulate them over a
        # short window; the cooldown keeps one gesture from firing repeatedly.
        # Trackpads report precise pixel deltas (and scroll phases); a plain
        # mouse wheel reports only angle deltas, so it stays a normal scroll.
        # Momentum events arrive *after* the fingers are lifted; they keep the
        # content scrolling but must not re-trigger the drawer (which would
        # immediately close the drawer the same swipe just opened).
        phase = event.phase()
        trackpad = (not pixel.isNull()
                    or phase != Qt.ScrollPhase.NoScrollPhase)
        if trackpad and phase != Qt.ScrollPhase.ScrollMomentum:
            # Normalise both delta kinds to roughly one unit per "pixel".
            step = pixel.y() if not pixel.isNull() else angle.y() * 0.33
            # Follow the *fingers*, not the scroll direction: with macOS
            # natural scrolling (the default) fingers-up is a negative delta,
            # with it disabled the sign is reversed.  Qt reports which way the
            # device is inverted on the event itself.
            if not event.inverted():
                step = -step
            if step < 0:
                self._wheel_accum += step
                self._wheel_reset_timer.start(350)
                if (self._wheel_accum <= -50
                        and now - self._drawer_last_at > 1.0):
                    self._wheel_accum = 0.0
                    self._drawer_last_at = now
                    self._wheel_suppress_until = now + 0.6
                    self._wheel_reset_timer.stop()
                    print("[screen] app drawer gesture", flush=True)
                    self.app_drawer.emit()
                    event.accept()
                    return
            else:
                # Scrolling the other way must not keep the burst alive.
                self._wheel_accum = 0.0

        if phase == Qt.ScrollPhase.ScrollEnd:
            # The fingers are gone: the next swipe starts from zero.
            self._wheel_accum = 0.0
            self._wheel_reset_timer.stop()

        # Send the scroll at the cursor, not at the centre of the screen: on
        # Android a scroll event is delivered to the view under the pointer.
        pt = self._widget_to_screen(event.position().x(),
                                    event.position().y())
        if pt is None:
            pt = (self._screen_w / 2.0, self._screen_h / 2.0)
        self.scroll.emit(pt[0], pt[1], dx, dy)
        super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 (Qt naming)
        if not self._input_enabled:
            super().keyPressEvent(event)
            return

        key = event.key()
        self.key_pressed.emit(key)
        print(f"[screen] key press: {key}", flush=True)
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Sizing
    # ------------------------------------------------------------------
    def hasHeightForWidth(self) -> bool:
        return False

    def sizeHint(self) -> QSize:
        return QSize(360, 640)

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------
    def _paint_frame(self, painter: QPainter):
        """Draw the newest frame, clipped to the rounded screen."""
        if self._image is None or self._image.isNull():
            return
        target = self._fit_rect()
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform, True)
        radius = self._effective_radius()
        if radius > 0:
            # Clip the frame itself so the corners are truly rounded.
            clip = QPainterPath()
            clip.addRoundedRect(target, radius, radius)
            painter.setClipPath(clip)
        painter.drawImage(target, self._image)

    def paintEvent(self, event):  # noqa: N802 (Qt naming)
        started = time.perf_counter()
        painter = QPainter(self)

        # Only the Android screen is painted - no background, no frame, no
        # message, no tap ripple.  When no frame has arrived yet the widget
        # stays fully transparent (the loader overlay in the main window is
        # what tells the user what is happening).
        self._paint_frame(painter)

        painter.end()

        try:
            self.painted.emit((time.perf_counter() - started) * 1000.0)
        except Exception:
            pass
