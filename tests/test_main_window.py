"""Tests for the AppStation main window (layout, progress, developer card).

These cover the changes that removed the device catalogue and added the
global progress bar, the Recent APKs list and the developer card.  No
emulator is started: everything is checked against the widgets themselves.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtCore import QMimeData, QPoint, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import QDragEnterEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

from core.settings import settings  # noqa: E402
from ui.main_window_v2 import MainWindowV2  # noqa: E402


class TestMainWindow(unittest.TestCase):
    """Main-window behaviour tests (no emulator required)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.win = MainWindowV2()
        self.win._open_detached_preview()
        self.win.show()
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()

    def tearDown(self):
        try:
            self.win._cleanup()
        except Exception:
            pass
        self.win.deleteLater()

    # -- device catalogue is gone ----------------------------------------
    def test_no_device_selection_widgets(self):
        """The device picker, search button and fold toggle are gone."""
        for name in ("combo_device", "btn_search_device", "combo_preset",
                     "btn_pick_device", "combo_fold", "lbl_fold",
                     "lbl_device_note"):
            self.assertFalse(hasattr(self.win, name), name)

    def test_no_drop_zone_or_detached_hint(self):
        """The big drop zone and the dark hint text are removed."""
        self.assertFalse(hasattr(self.win, "drop_zone"))
        self.assertFalse(hasattr(self.win, "lbl_detached_hint"))
        self.assertFalse(hasattr(self.win, "preview_placeholder"))

    def test_preview_has_no_device_frame(self):
        """The preview shows the screen only - no frame, bezel or cutout."""
        view = self.win.screen_view
        self.assertFalse(hasattr(view, "_frame"))
        self.assertFalse(hasattr(self.win, "_apply_generic_frame"))

    # -- main window clean-up --------------------------------------------
    def test_window_accepts_apk_drops_anywhere(self):
        """An APK can be dropped on any part of the window."""
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/example.apk")])
        event = QDragEnterEvent(
            QPoint(10, 10), Qt.DropAction.CopyAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.win.dragEnterEvent(event)
        self.assertTrue(event.isAccepted())

    # -- progress feedback ------------------------------------------------
    def test_loader_overlay_shows_the_step(self):
        """The loader overlay carries the current step text."""
        self.win.set_progress("Booting Android 60%", 60)
        self.assertTrue(self.win._overlay._busy)
        self.assertIn("60%", self.win._overlay._step)
        self.win.clear_progress()
        self.assertFalse(self.win._overlay._busy)

    def test_busy_buttons_are_disabled_and_restored(self):
        """A busy button is disabled and returns to its previous state."""
        button = self.win.btn_emu_start
        self.assertTrue(button.isEnabled())
        self.win.set_busy(button, True)
        self.assertFalse(button.isEnabled())
        self.win.set_busy(button, False)
        self.assertTrue(button.isEnabled())

        # A button that was already disabled must stay disabled.
        self.win.set_busy(self.win.btn_app_install, True)
        self.win.set_busy(self.win.btn_app_install, False)
        self.assertFalse(self.win.btn_app_install.isEnabled())

    # -- always on top ----------------------------------------------------
    def test_always_on_top_setting(self):
        """The Controls toggle persists the always-on-top preference."""
        previous = settings.always_on_top
        try:
            self.win.chk_always_on_top.setChecked(False)
            self.assertFalse(settings.always_on_top)
            self.win.chk_always_on_top.setChecked(True)
            self.assertTrue(settings.always_on_top)
        finally:
            settings.always_on_top = previous
            self.win.chk_always_on_top.setChecked(previous)

    # -- developer card ---------------------------------------------------
    def test_developer_button_in_top_bar(self):
        """The </Dev> button lives in the top bar."""
        self.assertIsNotNone(self.win.btn_dev)
        self.assertEqual(self.win.btn_dev.text(), "</Dev>")

    def test_no_left_panel_or_status_strip(self):
        """The APK panel, Recent APKs, activity area and stats strip are gone."""
        for name in ("list_recent", "lbl_activity", "preview_stack",
                     "preview_tabs", "lbl_emulator_state", "lbl_emulator_specs",
                     "lbl_app_cpu", "lbl_disk", "lbl_mode", "progress",
                     "lbl_progress", "tab_performance"):
            self.assertFalse(hasattr(self.win, name), name)

    def test_preview_has_nav_bar(self):
        """Back / home / recents / power / volume live under the preview screen."""
        win = self.win.detached_window
        self.assertTrue(hasattr(win, "nav_action"))
        # The nav bar exposes one button per navigation action.
        buttons = win.findChildren(QPushButton)
        self.assertGreaterEqual(len(buttons), 7)

    def test_transparent_loader_overlay(self):
        """The loader overlay paints no background."""
        overlay = self.win._overlay
        self.assertIsNotNone(overlay)
        self.assertTrue(overlay.testAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground))

    # -- frameless preview window ----------------------------------------
    def test_preview_window_is_frameless_and_translucent(self):
        """The detached preview keeps its frameless/transparent setup."""
        win = self.win.detached_window
        self.assertIsNotNone(win)
        self.assertTrue(win.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertTrue(win.testAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground))

    def test_preview_edge_resize_detection(self):
        """Window edges/corners are classified for move and resize."""
        win = self.win.detached_window
        w, h = win.width(), win.height()
        rect = win._screen_rect_in_window()
        classify = win.classify_point
        margin = win._margin
        edge = win._resize_edge_px
        self.assertEqual(classify(1, h // 2, w, h, margin, rect, edge),
                         "resize-left")
        self.assertEqual(classify(w - 1, h // 2, w, h, margin, rect, edge),
                         "resize-right")
        self.assertEqual(classify(w // 2, 1, w, h, margin, rect, edge),
                         "resize-top")
        self.assertEqual(classify(w // 2, h - 1, w, h, margin, rect, edge),
                         "resize-bottom")
        self.assertEqual(classify(1, 1, w, h, margin, rect, edge),
                         "resize-top-left")
        # Inside the thin margin, past the resize edge: that moves the window.
        move_x = edge + max(1, (margin - edge) // 2)
        self.assertEqual(classify(move_x, h // 2, w, h, margin, rect, edge),
                         "move")
        # A point in the middle of the screen is a touch.
        self.assertEqual(classify(rect[0] + rect[2] // 2,
                                  rect[1] + rect[3] // 2,
                                  w, h, margin, rect, edge), "screen")
        # The screen starts right at the window border: its leftmost pixel is
        # a touch, not a dead resize/ignore zone.
        self.assertEqual(classify(rect[0], h // 2, w, h, margin, rect, edge),
                         "screen")
        self.assertLessEqual(margin, 12)

        # Sizing the window to the column leaves no centring slack: the
        # screen border sits exactly at the margin on the sides and top.
        win.resize(win.window_size_for_screen_width(360))
        from PyQt6.QtWidgets import QApplication
        QApplication.processEvents()
        QApplication.processEvents()
        rect = win._screen_rect_in_window()
        self.assertAlmostEqual(rect[0], margin, delta=2)
        self.assertAlmostEqual(rect[1], margin, delta=2)
        self.assertAlmostEqual(win.width() - (rect[0] + rect[2]),
                               margin, delta=2)

    def test_responsive_main_window(self):
        """Tab widget fills the window at several sizes; no child is wider."""
        from PyQt6.QtCore import QSize
        for w, h in ((700, 500), (1000, 700), (1400, 900)):
            self.win.resize(w, h)
            # process events so layouts settle
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()
            tabs = self.win.main_tabs
            self.assertGreaterEqual(tabs.width(), int(w * 0.95),
                                    f"tabs too narrow at {w}x{h}")
            # no child widget wider than the window
            for child in self.win.findChildren(QWidget):
                if child.isVisible():
                    self.assertLessEqual(child.width(), w + 2,
                                        f"{type(child).__name__} too wide")
            # save screenshot
            pix = self.win.grab()
            pix.save(f"/tmp/appstation_{w}x{h}.png")

    # -- trackpad scrolling ------------------------------------------------
    def test_scroll_is_one_held_gesture_with_incremental_moves(self):
        """Wheel bursts become small frame-paced moves on a single finger."""
        win = self.win
        win._emulator.is_running = lambda: True
        win._boot_completed = lambda: True
        win.screen_view._screen_w, win.screen_view._screen_h = 324, 720

        events = []

        class FakeController:
            def touch_down(self, x, y, w, h):
                events.append(("down", x, y))

            def touch_move(self, x, y, w, h):
                events.append(("move", x, y))

            def touch_up(self, x, y, w, h):
                events.append(("up", x, y))

        win._input_controller = lambda: FakeController()

        for _ in range(6):
            win._on_screen_scroll(162, 500, 0, -20)

        self.assertEqual(events[0], ("down", 162.0, 500.0))
        self.assertAlmostEqual(win._scroll_pending_y, -60.0)

        for _ in range(10):
            win._scroll_step()

        moves = [e for e in events if e[0] == "move"]
        self.assertEqual(len(moves), 6)
        self.assertTrue(all(abs(moves[i][2] - moves[i - 1][2]) <= 10.0 + 1e-6
                            for i in range(1, len(moves))))
        self.assertAlmostEqual(moves[-1][2], 440.0, delta=1)
        self.assertFalse(any(e[0] == "up" for e in events))

        win._end_scroll_gesture()
        self.assertEqual(events[-1][0], "up")
        self.assertFalse(win._scroll_active)

    def test_scroll_direction_and_no_dropped_input(self):
        """Extra wheel events extend the gesture; they are never discarded."""
        win = self.win
        win._emulator.is_running = lambda: True
        win._boot_completed = lambda: True
        win.screen_view._screen_w, win.screen_view._screen_h = 324, 720

        events = []

        class FakeController:
            def touch_down(self, x, y, w, h):
                events.append(("down", x, y))

            def touch_move(self, x, y, w, h):
                events.append(("move", x, y))

            def touch_up(self, x, y, w, h):
                events.append(("up", x, y))

        win._input_controller = lambda: FakeController()

        # Two bursts arrive before the move timer gets a chance to run.
        for _ in range(4):
            win._on_screen_scroll(162, 400, 0, -20)
        for _ in range(2):
            win._on_screen_scroll(162, 400, 0, -20)
        self.assertEqual(len([e for e in events if e[0] == "down"]), 1)
        self.assertAlmostEqual(win._scroll_pending_y, -60.0)

        for _ in range(10):
            win._scroll_step()
        self.assertAlmostEqual(events[-1][2], 340.0, delta=1)
        win._end_scroll_gesture()

        # Scroll right (dx > 0) drags the finger left.
        events.clear()
        win._on_screen_scroll(162, 400, 120, 0)
        for _ in range(10):
            win._scroll_step()
        self.assertAlmostEqual(events[-1][1], 102.0, delta=1)
        win._end_scroll_gesture()

    def test_long_scroll_rebases_without_dropping_input(self):
        """A long flick restarts at the opposite edge but keeps its delta."""
        win = self.win
        win._emulator.is_running = lambda: True
        win._boot_completed = lambda: True
        win.screen_view._screen_w, win.screen_view._screen_h = 324, 720

        events = []

        class FakeController:
            def touch_down(self, x, y, w, h):
                events.append(("down", x, y))

            def touch_move(self, x, y, w, h):
                events.append(("move", x, y))

            def touch_up(self, x, y, w, h):
                events.append(("up", x, y))

        win._input_controller = lambda: FakeController()

        win._on_screen_scroll(162, 500, 0, -2000)
        self.assertAlmostEqual(win._scroll_pending_y, -1000.0)
        for _ in range(120):
            win._scroll_step()
        self.assertAlmostEqual(win._scroll_pending_y, 0.0, delta=1)

        downs = [e for e in events if e[0] == "down"]
        ups = [e for e in events if e[0] == "up"]
        self.assertGreaterEqual(len(downs), 2)
        self.assertGreaterEqual(len(ups), 1)
        self.assertTrue(win._scroll_active)
        win._end_scroll_gesture()

    # -- logcat only runs while its tab is visible ------------------------
    def test_logcat_streams_only_while_visible(self):
        """Logcat must not consume CPU while the Controls tab is shown."""
        self.win._ensure_logcat_panel()
        self.win.main_tabs.setCurrentIndex(1)  # Controls
        self.assertFalse(self.win._logcat_visible())
        self.win.main_tabs.setCurrentIndex(0)  # Logcat
        self.assertTrue(self.win._logcat_visible())


if __name__ == "__main__":
    unittest.main()
