"""Geometry tests for the preview window (no real window is needed).

Covers:
* which region a point belongs to (screen, move margin, resize edge/corner),
* the aspect column layout (nav bar width == screen width at several sizes),
* the manual resize fallback geometry,
* the rounded screen corners (radius + corner hit testing + painted pixels).
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtCore import QPointF, QRect, QSize  # noqa: E402
from PyQt6.QtGui import QColor, QImage  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402

from ui.main_window_v2 import AspectColumnLayout, DetachedPreviewWindow  # noqa: E402
from ui.screen_view import ScreenView  # noqa: E402


class TestPreviewRegions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_region_classification(self):
        # window 200x300, margin 24, screen rect centered inside the margin
        screen_rect = (24, 24, 152, 252)
        margin = 24
        edge = 8
        W, H = 200, 300
        classify = DetachedPreviewWindow.classify_point
        # screen interior
        self.assertEqual(classify(100, 100, W, H, margin, screen_rect, edge),
                         "screen")
        # move margin (between the outer edge and the screen)
        self.assertEqual(classify(12, 100, W, H, margin, screen_rect, edge),
                         "move")
        # resize edges/corners
        self.assertEqual(classify(2, 2, W, H, margin, screen_rect, edge),
                         "resize-top-left")
        self.assertEqual(classify(W - 2, 2, W, H, margin, screen_rect, edge),
                         "resize-top-right")
        self.assertEqual(classify(2, H - 2, W, H, margin, screen_rect, edge),
                         "resize-bottom-left")
        self.assertEqual(classify(W - 2, H - 2, W, H, margin, screen_rect,
                                  edge), "resize-bottom-right")
        self.assertEqual(classify(2, 100, W, H, margin, screen_rect, edge),
                         "resize-left")
        self.assertEqual(classify(W - 2, 100, W, H, margin, screen_rect, edge),
                         "resize-right")
        self.assertEqual(classify(100, 2, W, H, margin, screen_rect, edge),
                         "resize-top")
        self.assertEqual(classify(100, H - 2, W, H, margin, screen_rect, edge),
                         "resize-bottom")

    def test_region_to_edges_maps_corners_to_combined_edges(self):
        from PyQt6.QtCore import Qt
        self.assertEqual(
            DetachedPreviewWindow._region_to_edges("resize-top-left"),
            Qt.Edge.TopEdge | Qt.Edge.LeftEdge)
        self.assertEqual(
            DetachedPreviewWindow._region_to_edges("resize-right"),
            Qt.Edge.RightEdge)


class TestAspectColumnLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def _layout(self, guest_w=720, guest_h=1600):
        parent = QWidget()
        layout = AspectColumnLayout(parent, guest_w=guest_w, guest_h=guest_h)
        # Keep the parent alive: the layout is owned by it.
        layout._test_parent = parent
        screen = QWidget(parent)
        nav = QWidget(parent)
        layout.addWidget(screen)
        layout.addWidget(nav)
        return layout, screen, nav

    def test_nav_width_equals_screen_width(self):
        layout, screen, nav = self._layout()
        for size in ((400, 900), (200, 500), (720, 1400), (300, 700),
                     (1000, 900)):
            layout.setGeometry(QRect(0, 0, *size))
            self.assertEqual(screen.width(), nav.width(),
                             f"nav != screen at {size}")
            self.assertEqual(screen.x(), nav.x(), f"nav not aligned at {size}")
            self.assertEqual(nav.y(), screen.y() + screen.height(),
                             f"nav not below screen at {size}")

    def test_screen_fills_the_column(self):
        layout, screen, nav = self._layout(720, 1600)
        for w, h in ((400, 900), (200, 500), (720, 1400), (1000, 900)):
            layout.setGeometry(QRect(0, 0, w, h))
            self.assertEqual(screen.width(), w)
            self.assertEqual(screen.height(), h - nav.height())

    def test_nav_height_is_clamped(self):
        layout, screen, nav = self._layout(720, 1600)
        for width in (200, 400, 800):
            layout.setGeometry(QRect(0, 0, width, 1600))
            self.assertGreaterEqual(nav.height(), 36)
            self.assertLessEqual(nav.height(), 56)
            # 9% of the screen width, clamped.
            expected = max(36, min(56, round(0.09 * screen.width())))
            self.assertAlmostEqual(nav.height(), expected, delta=1)

    def test_column_fills_its_rect(self):
        layout, screen, nav = self._layout()
        layout.setGeometry(QRect(0, 0, 600, 1200))
        self.assertEqual(screen.x(), 0)
        self.assertEqual(screen.y(), 0)
        self.assertEqual(screen.width(), nav.width())
        self.assertEqual(screen.height() + nav.height(), 1200)

    def test_guest_rotation_does_not_shrink_the_preview(self):
        layout, screen, nav = self._layout(720, 1600)
        layout.setGeometry(QRect(0, 0, 600, 1200))
        layout.set_guest_size(1600, 720)  # rotated
        layout.setGeometry(QRect(0, 0, 600, 1200))
        self.assertEqual(screen.width(), 600)
        self.assertEqual(screen.width(), nav.width())


class TestManualResizeGeometry(unittest.TestCase):
    """The fallback resize used when the platform lacks system resize."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_right_edge_grows_width_only(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-right", 80, 0, start, 400 / 800, QSize(220, 300),
            aspect_lock=False)
        self.assertEqual(geom.left(), 100)
        self.assertEqual(geom.top(), 100)
        self.assertEqual(geom.width(), start.width() + 80)
        self.assertEqual(geom.height(), start.height())

    def test_left_edge_keeps_right_edge_anchored(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-left", -80, 0, start, 0.5, QSize(220, 300),
            aspect_lock=False)
        self.assertEqual(geom.right(), start.right())
        self.assertEqual(geom.width(), start.width() + 80)
        self.assertEqual(geom.height(), start.height())

    def test_bottom_edge_keeps_top_anchored(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-bottom", 0, 100, start, 0.5, QSize(220, 300),
            aspect_lock=False)
        self.assertEqual(geom.top(), 100)
        self.assertEqual(geom.height(), start.height() + 100)
        self.assertEqual(geom.width(), start.width())

    def test_top_edge_keeps_bottom_anchored(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-top", 0, -100, start, 0.5, QSize(220, 300),
            aspect_lock=False)
        self.assertEqual(geom.bottom(), start.bottom())
        self.assertEqual(geom.height(), start.height() + 100)
        self.assertEqual(geom.width(), start.width())

    def test_corner_keeps_aspect_locked(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-bottom-right", 80, 160, start, 0.5, QSize(220, 300))
        margin = 10
        screen_w = geom.width() - 2 * margin
        nav_h = max(36, min(56, round(0.09 * screen_w)))
        screen_h = geom.height() - nav_h - 2 * margin
        self.assertAlmostEqual(screen_w / screen_h, 0.5, delta=0.02)

    def test_resize_never_goes_below_the_minimum(self):
        start = QRect(100, 100, 400, 800)
        geom = DetachedPreviewWindow._manual_geometry(
            "resize-bottom-right", -9999, -9999, start, 0.5,
            QSize(220, 300))
        self.assertGreaterEqual(geom.width(), 220)
        self.assertGreaterEqual(geom.height(), 300)


class TestDragState(unittest.TestCase):
    """A resize must release the mouse grab and never leave input gated."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    @staticmethod
    def _event():
        event = mock.Mock()
        event.globalPosition.return_value = QPointF(0, 0)
        return event

    def _window(self):
        pane = QWidget()
        win = DetachedPreviewWindow(pane)
        win.resize(300, 700)
        win.show()
        QApplication.processEvents()
        return win

    def test_begin_manual_grabs_mouse(self):
        win = self._window()
        try:
            win._begin_manual("resize", "resize-right", self._event())
            self.assertEqual(win._manual_mode, "resize")
            self.assertIs(win.mouseGrabber(), win)
        finally:
            win._end_manual()

    def test_end_manual_releases_grab_and_clears_state(self):
        win = self._window()
        win._begin_manual("resize", "resize-right", self._event())
        win._end_manual()
        self.assertIsNone(win._manual_mode)
        self.assertIsNone(win._resize_start_screen)
        self.assertIsNone(win.mouseGrabber())

    def test_resize_finished_reports_old_and_new_screen_sizes(self):
        win = self._window()
        seen = []
        win.resize_finished.connect(lambda *a: seen.append(a))
        win._begin_manual("resize", "resize-right", self._event())
        win.resize(340, 700)
        win._end_manual(emit_resize=True)
        self.assertEqual(len(seen), 1)
        old_w, old_h, new_w, new_h = seen[0]
        self.assertGreater(new_w, old_w)
        self.assertGreater(new_h, 0)


class TestRoundedScreen(unittest.TestCase):
    """The screen corners are rounded, painted and excluded from clicks."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.view = ScreenView()
        image = QImage(720, 1600, QImage.Format.Format_RGB888)
        image.fill(QColor(200, 40, 40))
        self.view.set_frame(image)
        self.view.resize(360, 800)
        self.view.set_input_enabled(True)

    def test_radius_scales_and_is_clamped(self):
        self.assertEqual(ScreenView.rounded_radius(100), 14)
        self.assertEqual(ScreenView.rounded_radius(360), 18)
        self.assertEqual(ScreenView.rounded_radius(400), 20)
        self.assertEqual(ScreenView.rounded_radius(720), 26)
        self.assertEqual(ScreenView.rounded_radius(2000), 26)

    def test_corner_pixels_are_not_painted(self):
        from PyQt6.QtGui import QPainter
        image = QImage(self.view.size(),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        self.view._paint_frame(painter)
        painter.end()
        # A point in the very corner must be transparent (rounded away) ...
        corner = image.pixelColor(1, 1)
        self.assertEqual(corner.alpha(), 0)
        # ... while the centre is the frame colour.
        centre = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertGreater(centre.alpha(), 0)
        self.assertGreater(centre.red(), 150)

    def test_clicks_in_the_rounded_corner_are_ignored(self):
        target = self.view._fit_rect()
        radius = self.view.corner_radius()
        self.assertIsNone(self.view._widget_to_screen(1.0, 1.0))
        # Just inside the corner arc is a valid touch.
        inside = target.left() + radius
        self.assertIsNotNone(
            self.view._widget_to_screen(inside + 1, target.top() + radius))
        # The screen centre still maps.
        self.assertIsNotNone(self.view._widget_to_screen(
            target.width() / 2, target.height() / 2))


if __name__ == "__main__":
    unittest.main()
