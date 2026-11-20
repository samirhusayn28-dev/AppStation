"""Unit tests for the ScreenView coordinate mapping.

The preview shows the Android screen only, scaled to the widget size.  These
tests verify that a click at a known widget position maps to the correct
*device* pixel, that clicks outside the screen are ignored, and that the
mapping is independent of the device pixel ratio (Retina / Windows scaling).
No emulator is started.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtCore import QPointF, Qt  # noqa: E402
from PyQt6.QtGui import QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.screen_view import ScreenView  # noqa: E402


class TestScreenViewCoordinateMapping(unittest.TestCase):
    """Widget -> device-pixel mapping for the screen-only preview."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.view = ScreenView()
        # A 1080x1920 screen shown in a 360x640 widget: the image is scaled
        # by 360/1080 = 1/3 and letterboxed vertically.
        image = QImage(1080, 1920, QImage.Format.Format_RGB888)
        image.fill(0)
        self.view.set_frame(image)
        self.view.resize(360, 640)
        self.view.set_input_enabled(True)

    # -- the image is scaled to fit --------------------------------------
    def test_screen_is_scaled_into_the_widget(self):
        """The image keeps its aspect ratio and is centred."""
        target = self.view._fit_rect()
        self.assertAlmostEqual(target.width(), 360.0, delta=0.5)
        # 1080x1920 scaled by 1/3 -> 360x640, so it fills the widget exactly.
        self.assertAlmostEqual(target.height(), 640.0, delta=0.5)

    def test_letterboxed_image_keeps_aspect(self):
        """A narrower image is letterboxed, not stretched."""
        image = QImage(1080, 1920, QImage.Format.Format_RGB888)
        image.fill(0)
        self.view.set_frame(image)
        self.view.resize(800, 600)
        target = self.view._fit_rect()
        self.assertAlmostEqual(target.width(), 337.5, delta=1.0)
        self.assertAlmostEqual(target.height(), 600.0, delta=1.0)
        self.assertAlmostEqual(target.left(), (800 - 337.5) / 2.0, delta=1.0)

    # -- mapping ---------------------------------------------------------
    def test_center_maps_to_screen_center(self):
        """The widget centre is the screen centre."""
        pt = self.view._widget_to_screen(180.0, 320.0)
        self.assertIsNotNone(pt)
        self.assertAlmostEqual(pt[0], 540.0, delta=2)
        self.assertAlmostEqual(pt[1], 960.0, delta=2)

    def test_top_left_maps_near_origin(self):
        """A point just inside the rounded corner maps near screen (0, 0)."""
        radius = self.view.corner_radius()
        pt = self.view._widget_to_screen(float(radius + 1), float(radius + 1))
        self.assertIsNotNone(pt)
        self.assertLess(pt[0], 60.0)
        self.assertLess(pt[1], 60.0)

    def test_rounded_corner_is_not_clickable(self):
        """The clipped-off corner is not part of the screen."""
        self.assertIsNone(self.view._widget_to_screen(0.0, 0.0))
        self.assertIsNone(self.view._widget_to_screen(
            float(self.view.width() - 1), 0.0))

    def test_quarter_maps_to_quarter(self):
        """A quarter across and down is a quarter of the screen."""
        pt = self.view._widget_to_screen(90.0, 160.0)
        self.assertIsNotNone(pt)
        self.assertAlmostEqual(pt[0], 270.0, delta=2)
        self.assertAlmostEqual(pt[1], 480.0, delta=2)

    def test_letterbox_area_is_ignored(self):
        """A click in the letterbox (outside the image) maps to nothing."""
        self.view.resize(800, 600)
        pt = self.view._widget_to_screen(10.0, 300.0)
        self.assertIsNone(pt)

    def test_outside_the_widget_is_ignored(self):
        """Clicks outside the widget are ignored."""
        self.assertIsNone(self.view._widget_to_screen(-50.0, -50.0))
        self.assertIsNone(self.view._widget_to_screen(9999.0, 9999.0))

    def test_input_disabled_returns_none(self):
        """While input is disabled nothing is mapped."""
        self.view.set_input_enabled(False)
        self.assertIsNone(self.view._widget_to_screen(180.0, 320.0))
        self.view.set_input_enabled(True)
        self.assertIsNotNone(self.view._widget_to_screen(180.0, 320.0))

    def test_no_image_returns_none(self):
        """Without a frame there is nothing to map."""
        view = ScreenView()
        view.set_input_enabled(True)
        self.assertIsNone(view._widget_to_screen(10.0, 10.0))

    def test_mapped_values_stay_inside_the_screen(self):
        """Every mapped point is inside the device screen."""
        for wx in range(0, 360, 15):
            for wy in range(0, 640, 15):
                pt = self.view._widget_to_screen(float(wx), float(wy))
                if pt is not None:
                    self.assertGreaterEqual(pt[0], 0)
                    self.assertLess(pt[0], 1080)
                    self.assertGreaterEqual(pt[1], 0)
                    self.assertLess(pt[1], 1920)

    def test_mapping_ignores_the_device_pixel_ratio(self):
        """The mapping is in device pixels regardless of the display scale.

        The widget coordinates are logical; the device screen size is in
        physical pixels.  A Retina display changes the logical size of the
        widget, but the *relative* position on the screen must not change.
        """
        pt1 = self.view._widget_to_screen(180.0, 320.0)
        self.assertIsNotNone(pt1)
        ratio = self.view.devicePixelRatio()
        self.assertGreater(ratio, 0)
        # The same relative position (50 %, 50 %) on a twice as large widget
        # must still land on the same device pixel.
        self.view.resize(720, 1280)
        pt2 = self.view._widget_to_screen(360.0, 640.0)
        self.assertIsNotNone(pt2)
        self.assertAlmostEqual(pt1[0], pt2[0], delta=2)
        self.assertAlmostEqual(pt1[1], pt2[1], delta=2)


class TestScreenViewInputSignals(unittest.TestCase):
    """The preview emits the right signals for taps, swipes and scrolls."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.view = ScreenView()
        image = QImage(1080, 1920, QImage.Format.Format_RGB888)
        image.fill(0)
        self.view.set_frame(image)
        self.view.resize(360, 640)
        self.view.set_input_enabled(True)
        self.taps = []
        self.drags = []
        self.scrolls = []
        self.drawers = []
        self.view.touch.connect(lambda x, y: self.taps.append((x, y)))
        self.view.drag.connect(
            lambda x1, y1, x2, y2: self.drags.append((x1, y1, x2, y2)))
        self.view.scroll.connect(
            lambda x, y, dx, dy: self.scrolls.append((x, y, dx, dy)))
        self.view.app_drawer.connect(lambda: self.drawers.append(True))

    @staticmethod
    def _mouse_event(kind, pos, buttons=Qt.MouseButton.NoButton,
                     modifiers=Qt.KeyboardModifier.NoModifier):
        from PyQt6.QtGui import QMouseEvent
        from PyQt6.QtCore import QPointF, QEvent
        return QMouseEvent(kind, QPointF(pos[0], pos[1]),
                           Qt.MouseButton.LeftButton, buttons,
                           modifiers)

    def test_tap_emits_a_touch(self):
        """A press + release without movement is a tap."""
        from PyQt6.QtCore import QEvent
        self.view.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress, (180, 320),
            Qt.MouseButton.LeftButton))
        self.view.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease, (180, 320)))
        self.assertEqual(len(self.taps), 1)
        self.assertAlmostEqual(self.taps[0][0], 540.0, delta=3)
        self.assertAlmostEqual(self.taps[0][1], 960.0, delta=3)
        self.assertEqual(len(self.drags), 0)

    def test_drag_emits_a_swipe(self):
        """A press + move + release is a swipe."""
        from PyQt6.QtCore import QEvent
        self.view.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress, (60, 200),
            Qt.MouseButton.LeftButton))
        self.view.mouseMoveEvent(self._mouse_event(
            QEvent.Type.MouseMove, (300, 500),
            Qt.MouseButton.LeftButton))
        self.view.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease, (300, 500)))
        self.assertEqual(len(self.drags), 1)
        self.assertEqual(len(self.taps), 0)
        x1, y1, x2, y2 = self.drags[0]
        self.assertLess(x1, x2)
        self.assertLess(y1, y2)

    def test_long_press_does_not_also_send_a_tap(self):
        """A long press is one gesture: the release must not add a tap."""
        from PyQt6.QtCore import QEvent
        self.view.mousePressEvent(self._mouse_event(
            QEvent.Type.MouseButtonPress, (180, 320),
            Qt.MouseButton.LeftButton))
        self.view._emit_long_press()
        self.view.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease, (180, 320)))
        self.assertEqual(len(self.taps), 1)

    def test_alt_press_is_left_for_the_preview_window(self):
        """Option/Alt+drag moves the window, so it must not reach Android."""
        from PyQt6.QtCore import QEvent
        event = self._mouse_event(
            QEvent.Type.MouseButtonPress, (180, 320),
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier)
        self.view.mousePressEvent(event)
        self.view.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease, (180, 320),
            modifiers=Qt.KeyboardModifier.AltModifier))
        self.assertEqual(self.taps, [])
        self.assertFalse(event.isAccepted())

    def test_edge_press_reaches_the_screen(self):
        """The whole screen is touchable - the border is not a dead zone."""
        from PyQt6.QtCore import QEvent
        event = self._mouse_event(
            QEvent.Type.MouseButtonPress, (2, 320),
            Qt.MouseButton.LeftButton)
        self.view.mousePressEvent(event)
        self.view.mouseReleaseEvent(self._mouse_event(
            QEvent.Type.MouseButtonRelease, (2, 320)))
        self.assertEqual(len(self.taps), 1)
        self.assertLess(self.taps[0][0], 30.0)
        self.assertAlmostEqual(self.taps[0][1], 960.0, delta=3)

    @staticmethod
    def _wheel_event(dy, pdy=0, phase=Qt.ScrollPhase.NoScrollPhase,
                     inverted=True):
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtCore import QPointF, QPoint
        return QWheelEvent(
            QPointF(180, 320), QPointF(180, 320), QPoint(0, pdy),
            QPoint(0, dy), Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier, phase,
            inverted)

    def test_wheel_emits_a_scroll_at_the_cursor(self):
        """The wheel scrolls the item under the pointer, not screen centre."""
        self.view.wheelEvent(self._wheel_event(120))
        self.assertEqual(len(self.scrolls), 1)
        x, y, dx, dy = self.scrolls[0]
        # The helper points at widget (180,320) on a 1080x1920 screen.
        self.assertAlmostEqual(x, 540.0, delta=3)
        self.assertAlmostEqual(y, 960.0, delta=3)
        self.assertEqual((dx, dy), (0, 120))

    def test_small_upward_scroll_is_still_a_scroll(self):
        """A slow scroll must not be mistaken for the app-drawer gesture."""
        self.view.wheelEvent(self._wheel_event(-20, pdy=-10,
                                               phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(len(self.scrolls), 1)
        self.assertEqual(self.drawers, [])

    def test_two_finger_swipe_up_opens_the_app_drawer(self):
        """A trackpad swipe up is an upward burst, not a single wheel notch."""
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                -40, pdy=-20, phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(len(self.drawers), 1)
        # At most the first couple of deltas may scroll before the burst is
        # recognised; the tail of the same swipe must not keep scrolling.
        self.assertLessEqual(len(self.scrolls), 2)

    def test_two_finger_swipe_up_works_without_natural_scrolling(self):
        """The gesture follows the fingers when natural scrolling is off."""
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                40, pdy=20, phase=Qt.ScrollPhase.ScrollUpdate,
                inverted=False))
        self.assertEqual(len(self.drawers), 1)

    def test_scrolling_down_does_not_open_the_app_drawer(self):
        """Only an upward burst counts, never a downward one."""
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                40, pdy=20, phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(self.drawers, [])

    def test_momentum_after_a_swipe_does_not_reopen_the_drawer(self):
        """Momentum keeps scrolling but must not toggle the drawer twice."""
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                -40, pdy=-20, phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(len(self.drawers), 1)
        # Expire both timers, then let the post-swipe momentum keep going.
        self.view._wheel_suppress_until = 0.0
        self.view._drawer_last_at = 0.0
        for _ in range(10):
            self.view.wheelEvent(self._wheel_event(
                -80, pdy=-40, phase=Qt.ScrollPhase.ScrollMomentum))
        self.assertEqual(len(self.drawers), 1)

    def test_app_drawer_gesture_has_a_cooldown(self):
        """One physical swipe must not fire the drawer repeatedly."""
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                -40, pdy=-20, phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(len(self.drawers), 1)
        # End the tail-suppression window but keep the 1s cooldown active.
        self.view._wheel_suppress_until = 0.0
        self.view._wheel_accum = 0.0
        for _ in range(4):
            self.view.wheelEvent(self._wheel_event(
                -40, pdy=-20, phase=Qt.ScrollPhase.ScrollUpdate))
        self.assertEqual(len(self.drawers), 1)


if __name__ == "__main__":
    unittest.main()
