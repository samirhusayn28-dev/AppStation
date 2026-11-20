"""Tests for the device-control buttons (no emulator).

Every navigation / volume / power / rotate / screenshot button must send the
right Android keycode through adb and must never raise, even when the backend
is mocked out.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ui.main_window_v2 import MainWindowV2  # noqa: E402


class TestDeviceActions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        self.win = MainWindowV2()
        self.win._open_detached_preview()
        # Mock the backend so no real adb/emulator is touched.
        self.win._emulator.is_running = mock.MagicMock(return_value=True)
        self.win._boot_completed = mock.MagicMock(return_value=True)
        adb = mock.MagicMock()
        adb.input_keyevent.return_value = (True, "ok")
        adb.set_rotation.return_value = (True, "ok")
        adb.screenshot.return_value = (True, "ok")
        adb.pull.return_value = (True, "ok")
        adb.shell.return_value = (True, "ok")
        adb.reset_screen_size.return_value = (True, "ok")
        adb.reset_density.return_value = (True, "ok")
        self.win._adb = adb

        # Make _run_adb synchronous for deterministic testing.
        def sync_run(fn, on_done=None):
            try:
                ok, msg = fn()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, str(e)
            if on_done is not None:
                on_done(ok, msg)
        self.win._run_adb = sync_run

    def tearDown(self):
        try:
            self.win._cleanup()
        except Exception:
            pass
        self.win.deleteLater()

    def test_every_nav_button_sends_the_right_keycode(self):
        expected = {
            "back": "4",
            "home": "3",
            "recents": "187",
            "vol_up": "24",
            "vol_down": "25",
            "power": "26",
        }
        for action, code in expected.items():
            with self.subTest(action=action):
                self.win._adb.input_keyevent.reset_mock()
                try:
                    self.win._on_device_action(action)
                except Exception as exc:  # pragma: no cover
                    self.fail(f"{action} raised: {exc!r}")
                self.win._adb.input_keyevent.assert_called_with(code)

    def test_rotate_and_screenshot_do_not_crash(self):
        try:
            self.win._on_device_action("rotate")
            self.win._on_device_action("screenshot")
        except Exception as exc:  # pragma: no cover
            self.fail(f"raised: {exc!r}")

    def test_qt_key_power_does_not_exist_and_is_not_used(self):
        # Regression guard: the mapping must never reference Qt.Key.Key_Power.
        import ui.main_window_v2 as m
        src = Path(m.__file__).read_text()
        self.assertNotIn("Qt.Key.Key_Power", src)


if __name__ == "__main__":
    unittest.main()