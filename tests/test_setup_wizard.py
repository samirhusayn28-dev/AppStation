"""Offscreen tests for the nine-page setup wizard (no downloads)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from PyQt6.QtWidgets import QApplication
    HAS_QT = True
except Exception:  # pragma: no cover - system python may not have PyQt6
    HAS_QT = False

from core.bootstrap import BootstrapManager, SetupState
from core.settings import settings


@unittest.skipUnless(HAS_QT, "PyQt6 is not installed")
class TestSetupWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def _wizard(self):
        from ui.setup_wizard import SetupWizard
        # Never touch the real ~/.apkpreview/setup_state.json in tests.
        with mock.patch.object(SetupState, "load",
                               return_value=SetupState()), \
                mock.patch.object(SetupState, "save", return_value=True):
            wizard = SetupWizard()
        wizard.state = SetupState()
        wizard.state.save = lambda path=None: True
        wizard.bootstrap.state = wizard.state
        return wizard

    def test_eight_pages_and_titles(self):
        """The wizard asks for the Android version only - no device page."""
        wizard = self._wizard()
        self.assertEqual(len(wizard.pageIds()), 8)
        titles = [wizard.page(page_id).title() for page_id in wizard.pageIds()]
        self.assertIn("Welcome to AppStation", titles[0])
        self.assertIn("Standard or Custom", titles[1])
        self.assertIn("SDK location", titles[2])
        self.assertIn("System check", titles[3])
        self.assertIn("licenses", titles[4].lower())
        self.assertIn("Download", titles[5])
        self.assertIn("Android version", titles[6])
        self.assertIn("Create", titles[7])
        # No device page any more.
        self.assertFalse(any("device" in t.lower() for t in titles))
        wizard.close()

    def test_install_type_components(self):
        wizard = self._wizard()
        page = wizard.page(1)
        self.assertEqual(page.components(),
                         ["jdk", "cmdline-tools", "platform-tools", "emulator"])
        page.radio_custom.setChecked(True)
        page.checkboxes["emulator"].setChecked(False)
        self.assertNotIn("emulator", page.components())
        self.assertTrue(page.validatePage())
        self.assertEqual(wizard.state.install_type, "custom")
        wizard.close()

    def test_sdk_path_is_applied(self):
        wizard = self._wizard()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "custom sdk"
            with mock.patch.object(settings, "_data", dict(settings._data)), \
                    mock.patch.object(type(settings), "save", return_value=True):
                wizard.set_sdk_path(target)
                self.assertEqual(wizard.bootstrap.sdk_dir, target)
                self.assertEqual(Path(wizard.state.sdk_path), target)
                self.assertEqual(Path(settings.sdk_path), target)
        wizard.close()

    def test_license_page_requires_acceptance(self):
        wizard = self._wizard()
        page = wizard.page(4)
        with mock.patch.object(BootstrapManager, "licenses_accepted",
                               return_value=False):
            page.initializePage()
        self.assertIn("Android SDK License Agreement", page.text.toPlainText())
        self.assertFalse(page.isComplete())
        page.check.setChecked(True)
        self.assertTrue(page.isComplete())
        self.assertTrue(page.validatePage())
        self.assertTrue(wizard.state.licenses_accepted)
        wizard.close()

    def test_download_page_marks_installed_components_complete(self):
        wizard = self._wizard()
        page = wizard.page(5)
        with mock.patch.object(BootstrapManager, "is_component_installed",
                               return_value=True), \
                mock.patch.object(BootstrapManager, "disk_space_ok",
                                  return_value=(True, "20 GB free")):
            page.initializePage()
        self.assertTrue(page.isComplete())
        self.assertEqual(page.progress.value(), 100)
        wizard.close()

    def test_version_page_fallback_has_sizes(self):
        wizard = self._wizard()
        page = wizard.page(6)
        with mock.patch("core.sdk_manager.SDKManager.is_sdk_installed",
                        return_value=False):
            page.initializePage()
        self.assertEqual(page.combo.count(), 9)
        version = page.selected_version()
        self.assertIsNotNone(version)
        self.assertIn("API", page.combo.currentText())
        self.assertIn("GB", page.combo.currentText())
        self.assertTrue(page.validatePage())
        self.assertEqual(wizard.state.api_level, version.api_level)
        wizard.close()

    def test_finish_page_summary(self):
        wizard = self._wizard()
        wizard.state.api_level = 34
        wizard.state.variant = "default"
        page = wizard.page(7)
        page.initializePage()
        text = page.summary.toPlainText()
        self.assertIn("API 34", text)
        self.assertFalse("Device:" in text)
        self.assertFalse(page.isComplete())
        wizard.close()

    def test_resume_start_id(self):
        from ui.setup_wizard import SetupWizard
        state = SetupState(step=5)
        with mock.patch.object(SetupState, "load", return_value=state), \
                mock.patch.object(SetupState, "save", return_value=True):
            wizard = SetupWizard()
        self.assertEqual(wizard.startId(), 5)
        wizard.close()

    def test_run_if_needed_skips_when_ready(self):
        from ui.setup_wizard import SetupWizard
        with mock.patch("ui.setup_wizard.needs_setup", return_value=False):
            self.assertTrue(SetupWizard.run_if_needed())


if __name__ == "__main__":
    unittest.main()
