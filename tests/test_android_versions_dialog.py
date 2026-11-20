"""Offscreen UI tests for Android version replacement/removal guards.

No sdkmanager/avdmanager calls and no emulator starts: the dialog's refresh
worker is patched out and the emulator is a tiny fake.
"""

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from PyQt6.QtWidgets import QApplication, QLabel
    HAS_QT = True
except Exception:  # pragma: no cover
    HAS_QT = False

from core.sdk_manager import AndroidVersion


class FakeEmulator:
    def __init__(self, running: bool):
        self.running = running

    def is_running(self) -> bool:
        return self.running


class FakeSDK:
    def list_system_images(self, cancel=None):
        return []

    def install_version(self, *args, **kwargs):
        raise AssertionError("test must patch install_version")


@unittest.skipUnless(HAS_QT, "PyQt6 is not installed")
class TestVersionDialogGuards(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def setUp(self):
        # These guards compare the running AVD with the *selected* version in
        # settings.  Pin the selection so the tests do not depend on the
        # developer's persisted settings.json.
        from core.settings import settings
        # Mutate the in-memory data only: no disk write, no persisted state.
        self._saved_selection = (settings._data.get("selected_api_level"),
                                 settings._data.get("selected_variant"))
        settings._data["selected_api_level"] = 99
        settings._data["selected_variant"] = "test"

    def tearDown(self):
        from core.settings import settings
        level, variant = self._saved_selection
        if level is None:
            settings._data.pop("selected_api_level", None)
        else:
            settings._data["selected_api_level"] = level
        if variant is None:
            settings._data.pop("selected_variant", None)
        else:
            settings._data["selected_variant"] = variant

    def _dialog(self, running=False):
        from ui.android_versions_dialog import AndroidVersionsDialog
        with mock.patch.object(AndroidVersionsDialog, "refresh",
                               lambda self: None):
            dialog = AndroidVersionsDialog(
                sdk=FakeSDK(), emulator=FakeEmulator(running))
        return dialog

    def test_replace_dialog_shows_net_space_and_defaults_to_removal(self):
        from ui.android_versions_dialog import _ReplaceDialog
        old = AndroidVersion(34, "default", "x86_64", installed=True)
        new = AndroidVersion(33, "default", "x86_64")
        dialog = _ReplaceDialog(old, new, free_gb=10.0)
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertIn("frees about", text)
        self.assertIn("net change", text)
        self.assertIn("after replacement", text)
        self.assertTrue(dialog.chk_remove.isChecked())
        self.assertTrue(dialog.chosen_remove_old())
        dialog.chk_remove.setChecked(False)
        dialog.accept()
        self.assertFalse(dialog.remove_old)
        self.assertFalse(dialog.chosen_remove_old())
        dialog.close()

    def test_remove_refuses_when_any_emulator_is_running(self):
        dialog = self._dialog(running=True)
        old = AndroidVersion(34, "default", "x86_64", installed=True)
        dialog._versions = [old]
        dialog._populate_table()
        dialog.table.selectRow(0)
        with mock.patch.object(dialog, "_refuse_running") as refuse, \
                mock.patch.object(dialog, "_confirm_remove") as confirm, \
                mock.patch.object(dialog, "_start_task") as start:
            dialog._remove_version(old)
        refuse.assert_called_once()
        confirm.assert_not_called()
        start.assert_not_called()
        dialog.close()

    def test_replace_refuses_after_confirmation_if_emulator_running(self):
        dialog = self._dialog(running=True)
        old = AndroidVersion(34, "default", "x86_64", installed=True)
        new = AndroidVersion(33, "default", "x86_64")
        dialog._versions = [old, new]
        dialog._populate_table()
        dialog.table.selectRow(1)
        with mock.patch.object(dialog, "_confirm_replace",
                               return_value=(True, True)), \
                mock.patch.object(dialog, "_refuse_running") as refuse, \
                mock.patch.object(dialog, "_start_task") as start:
            dialog._on_download()
        refuse.assert_called_once()
        start.assert_not_called()
        dialog.close()

    def test_replace_download_allowed_when_running_but_removal_unchecked(self):
        dialog = self._dialog(running=True)
        old = AndroidVersion(34, "default", "x86_64", installed=True)
        new = AndroidVersion(33, "default", "x86_64")
        dialog._versions = [old, new]
        dialog._populate_table()
        dialog.table.selectRow(1)
        with mock.patch.object(dialog, "_confirm_replace",
                               return_value=(True, False)), \
                mock.patch.object(dialog, "_free_gb", return_value=100.0), \
                mock.patch.object(dialog, "_refuse_running") as refuse, \
                mock.patch.object(dialog, "_start_task") as start:
            dialog._on_download()
        refuse.assert_not_called()
        start.assert_called_once()
        dialog.close()

    def test_replace_task_passes_old_and_selects_new_version(self):
        dialog = self._dialog(running=False)
        old = AndroidVersion(34, "default", "x86_64", installed=True)
        new = AndroidVersion(33, "default", "x86_64")
        dialog._versions = [old, new]
        dialog._populate_table()
        dialog.table.selectRow(1)
        captured = {}

        def fake_start(kind, fn):
            captured["kind"] = kind
            captured["fn"] = fn

        with mock.patch.object(dialog, "_free_gb", return_value=100.0), \
                mock.patch.object(dialog, "_confirm_replace",
                                  return_value=(True, True)), \
                mock.patch.object(dialog, "_start_task",
                                  side_effect=fake_start), \
                mock.patch.object(dialog._sdk, "install_version",
                                  return_value=(True, "installed",
                                                "apkpreview_api33_default")) as install, \
                mock.patch("ui.android_versions_dialog.HardwareProfiler."
                           "get_emulator_config", return_value=None), \
                mock.patch("ui.android_versions_dialog.settings."
                           "select_android_version") as select:
            dialog._on_download()
            self.assertEqual(captured["kind"], "install")
            ok, message, payload = captured["fn"](
                lambda *args: None, threading.Event())

        self.assertTrue(ok, message)
        self.assertEqual(payload, new)
        install.assert_called_once()
        self.assertIs(install.call_args.kwargs["old_version"], old)
        select.assert_called_once_with(
            33, "default", "apkpreview_api33_default")
        dialog.close()


if __name__ == "__main__":
    unittest.main()
