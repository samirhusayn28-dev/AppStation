"""Regression tests for phone (USB) mode - no emulator is ever started."""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.adb import ADB  # noqa: E402
from core.settings import Settings, settings  # noqa: E402

try:
    from PyQt6.QtWidgets import QApplication  # noqa: E402
    HAS_QT = True
except Exception:  # pragma: no cover - system python may lack PyQt6
    HAS_QT = False


DEVICES_OUTPUT = """\
List of devices attached
emulator-5554          device product:sdk_gphone64 model:Pixel_8 device:emu64a transport_id:1
0123456789ABCDEF       device product:beyondlte model:Galaxy_S23 device:beyond transport_id:2
567890ABCD            unauthorized transport_id:3
"""


def _result(stdout, returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class TestDeviceDiscovery(unittest.TestCase):
    def test_list_devices_parses_models_and_states(self):
        with mock.patch("core.adb.PlatformUtils.run_command",
                        return_value=_result(DEVICES_OUTPUT)) as run:
            devices = ADB().list_devices()

        # ADB discovery must not preselect the emulator serial.
        self.assertEqual(run.call_args.args[0][-2:], ["devices", "-l"])
        self.assertEqual(len(devices), 3)
        self.assertEqual(devices[0]["serial"], "emulator-5554")
        self.assertEqual(devices[1]["model"], "Galaxy_S23")
        self.assertEqual(devices[2]["state"], "unauthorized")

    def test_list_devices_failure_is_empty(self):
        with mock.patch("core.adb.PlatformUtils.run_command",
                        return_value=_result("", returncode=1)):
            self.assertEqual(ADB().list_devices(), [])


class TestDeviceModeSettings(unittest.TestCase):
    def test_device_mode_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Settings()
            s._settings_path = Path(tmp) / "settings.json"
            self.assertEqual(s.device_mode, "emulator")
            self.assertFalse(s.use_phone)

            s.device_mode = "phone"
            s.phone_serial = "0123456789ABCDEF"
            reloaded = Settings()
            reloaded._settings_path = s._settings_path
            reloaded.load()
            self.assertTrue(reloaded.use_phone)
            self.assertEqual(reloaded.phone_serial, "0123456789ABCDEF")

            reloaded.device_mode = "emulator"
            self.assertFalse(reloaded.use_phone)


class TestPhoneInstallWorker(unittest.TestCase):
    """Installing an APK in phone mode must never touch the emulator."""

    def test_phone_mode_skips_emulator_start(self):
        from ui.main_window_v2 import APKInstallWorker

        emulator = mock.MagicMock()
        adb = mock.MagicMock()
        scrcpy = mock.MagicMock()
        adb.wait_for_device.return_value = (True, "ready")
        adb.install.return_value = (True, "installed")
        adb.start_activity.return_value = (True, "launched")

        worker = APKInstallWorker(
            Path("/tmp/demo.apk"), emulator, adb, scrcpy,
            mock.MagicMock(), scrcpy_available=True,
            start_mirror=False, phone_mode=True)
        results = []
        worker.finished.connect(
            lambda ok, msg, pkg, act: results.append((ok, msg, pkg, act)))
        with mock.patch("ui.main_window_v2.detect_package_and_activity",
                        return_value=("com.example", ".Main")):
            worker.run()

        emulator.is_running.assert_not_called()
        emulator.start.assert_not_called()
        adb.wait_for_device.assert_called_once()
        adb.install.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0][0])


@unittest.skipUnless(HAS_QT, "PyQt6 is not installed")
class TestPhoneWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wizard(self):
        from core.bootstrap import SetupState
        from ui.setup_wizard import SetupWizard

        with mock.patch.object(SetupState, "load",
                               return_value=SetupState()), \
                mock.patch.object(SetupState, "save", return_value=True):
            wizard = SetupWizard()
        wizard.state = SetupState()
        wizard.state.save = lambda path=None: True
        wizard.bootstrap.state = wizard.state
        return wizard

    def test_phone_choice_only_downloads_adb(self):
        from ui.setup_wizard import PAGE_FINISH

        wizard = self._wizard()
        page = wizard.page(1)
        page.radio_phone.setChecked(True)
        self.assertTrue(page.validatePage())
        self.assertTrue(wizard.use_phone)
        self.assertEqual(wizard.components, ["platform-tools"])
        self.assertEqual(wizard.state.install_type, "phone")
        # No system image page in phone mode.
        self.assertEqual(wizard.page(5).nextId(), PAGE_FINISH)
        wizard.close()

    def test_dialog_only_allows_ready_phones(self):
        from ui.phone_setup_dialog import PhoneSetupDialog

        dialog = PhoneSetupDialog()
        dialog._populate([
            {"serial": "emulator-5554", "state": "device", "model": "emu"},
            {"serial": "ABC123", "state": "unauthorized", "model": "Pixel"},
            {"serial": "DEF456", "state": "device", "model": "Galaxy"},
        ])
        self.assertEqual(dialog.list_widget.count(), 2)  # emulator filtered
        dialog.list_widget.setCurrentRow(0)
        self.assertFalse(dialog.btn_use.isEnabled())
        dialog.list_widget.setCurrentRow(1)
        self.assertTrue(dialog.btn_use.isEnabled())
        dialog._use_selected()
        self.assertEqual(dialog.selected_serial(), "DEF456")
        dialog.close()

    def test_finish_page_connects_phone_and_persists(self):
        from ui.setup_wizard import PhoneSetupDialog

        wizard = self._wizard()
        wizard.use_phone = True
        wizard.components = ["platform-tools"]
        page = wizard.page(7)
        page.initializePage()
        self.assertIn("phone", page.title().lower())
        self.assertFalse(page.btn_phone.isHidden())
        self.assertTrue(page.btn_create.isHidden())
        self.assertIn("USB debugging", page.summary.toPlainText())

        with mock.patch.object(settings, "_data", dict(settings._data)), \
                mock.patch.object(type(settings), "save", return_value=True), \
                mock.patch.object(PhoneSetupDialog, "exec",
                                  return_value=1), \
                mock.patch.object(PhoneSetupDialog, "selected_serial",
                                  return_value="0123456789ABCDEF"):
            page.connect_phone()
            self.assertTrue(settings.use_phone)
            self.assertEqual(settings.phone_serial, "0123456789ABCDEF")
            self.assertTrue(settings.first_run_completed)

        self.assertTrue(wizard.state.completed)
        self.assertTrue(page.isComplete())
        wizard.close()


if __name__ == "__main__":
    unittest.main()
