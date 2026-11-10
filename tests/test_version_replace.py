"""End-to-end tests for replacing an Android version.

These tests use a temporary SDK/AVD tree.  They never call the real
``sdkmanager``/``avdmanager`` and never start an emulator.  The point is the
ordering guarantee: the new image and AVD must exist before the old image and
AVD folder/.ini are deleted, and a failed download/AVD creation must leave the
old version untouched.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.platform_utils import PlatformUtils
from core.sdk_manager import SDKManager, AndroidVersion


def _write_avd(base: Path, name: str, api: int, variant: str = "default",
               abi: str = "x86_64") -> Path:
    avd_dir = base / f"{name}.avd"
    avd_dir.mkdir(parents=True, exist_ok=True)
    (avd_dir / "config.ini").write_text(
        f"image.sysdir.1 = system-images/android-{api}/{variant}/{abi}/\n",
        encoding="utf-8")
    (base / f"{name}.ini").write_text("pointer", encoding="utf-8")
    return avd_dir


class TestVersionReplaceEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sdk_dir = self.root / "sdk"
        self.avd_base = self.root / "avd"
        self.avd_base.mkdir(parents=True)
        self.sdk = SDKManager()
        self.sdk._sdk_dir = self.sdk_dir
        self.old = AndroidVersion(34, "default", "x86_64", installed=True)
        self.new = AndroidVersion(33, "default", "x86_64")

    def tearDown(self):
        self._tmp.cleanup()

    def _old_files(self):
        image = (self.sdk_dir / "system-images" / "android-34" / "default"
                 / "x86_64")
        image.mkdir(parents=True, exist_ok=True)
        (image / "system.img").write_text("old", encoding="utf-8")
        _write_avd(self.avd_base, "apkpreview_api34_default", 34)
        return image

    def _new_files(self):
        image = (self.sdk_dir / "system-images" / "android-33" / "default"
                 / "x86_64")
        image.mkdir(parents=True, exist_ok=True)
        (image / "system.img").write_text("new", encoding="utf-8")
        _write_avd(self.avd_base, "apkpreview_api33_default", 33)
        return image

    def test_success_creates_new_then_deletes_old(self):
        old_image = self._old_files()
        order = []

        def fake_install(version, on_line=None, cancel=None):
            order.append("install")
            self._new_files()
            return True, "downloaded"

        def fake_ensure(version, config=None, data_partition_gb=3.0):
            order.append("avd")
            _write_avd(self.avd_base, version.avd_name, version.api_level)
            return True, "created", version.avd_name

        def fake_uninstall(version):
            order.append("uninstall")
            shutil.rmtree(old_image)
            return True, "old image removed"

        with mock.patch.object(self.sdk, "_avd_dirs",
                               return_value=[self.avd_base]), \
                mock.patch.object(PlatformUtils, "get_image_abi",
                                  return_value="x86_64"), \
                mock.patch.object(self.sdk, "install_system_image",
                                  side_effect=fake_install), \
                mock.patch.object(self.sdk, "ensure_avd_for",
                                  side_effect=fake_ensure), \
                mock.patch.object(self.sdk, "uninstall_system_image",
                                  side_effect=fake_uninstall):
            ok, message, avd = self.sdk.install_version(
                self.new, old_version=self.old)

        self.assertTrue(ok, message)
        self.assertEqual(avd, "apkpreview_api33_default")
        self.assertEqual(order, ["install", "avd", "uninstall"])
        self.assertTrue((self.sdk_dir / "system-images" / "android-33"
                         / "default" / "x86_64" / "system.img").exists())
        self.assertTrue((self.avd_base
                         / "apkpreview_api33_default.avd").is_dir())
        self.assertTrue((self.avd_base
                         / "apkpreview_api33_default.ini").exists())
        self.assertFalse(old_image.exists())
        self.assertFalse((self.avd_base
                          / "apkpreview_api34_default.avd").exists())
        self.assertFalse((self.avd_base
                          / "apkpreview_api34_default.ini").exists())

    def test_download_failure_leaves_old_untouched(self):
        old_image = self._old_files()

        with mock.patch.object(self.sdk, "_avd_dirs",
                               return_value=[self.avd_base]), \
                mock.patch.object(PlatformUtils, "get_image_abi",
                                  return_value="x86_64"), \
                mock.patch.object(self.sdk, "install_system_image",
                                  return_value=(False, "network error")), \
                mock.patch.object(self.sdk, "ensure_avd_for") as ensure, \
                mock.patch.object(self.sdk, "uninstall_system_image") as uninstall:
            ok, message, avd = self.sdk.install_version(
                self.new, old_version=self.old)

        self.assertFalse(ok)
        self.assertIn("network error", message)
        ensure.assert_not_called()
        uninstall.assert_not_called()
        self.assertTrue(old_image.exists())
        self.assertTrue((self.avd_base
                         / "apkpreview_api34_default.avd").exists())
        self.assertTrue((self.avd_base
                         / "apkpreview_api34_default.ini").exists())
        self.assertFalse((self.sdk_dir / "system-images" / "android-33"
                          / "default" / "x86_64").exists())

    def test_avd_failure_leaves_old_untouched(self):
        old_image = self._old_files()

        def fake_install(version, on_line=None, cancel=None):
            self._new_files()
            return True, "downloaded"

        with mock.patch.object(self.sdk, "_avd_dirs",
                               return_value=[self.avd_base]), \
                mock.patch.object(PlatformUtils, "get_image_abi",
                                  return_value="x86_64"), \
                mock.patch.object(self.sdk, "install_system_image",
                                  side_effect=fake_install), \
                mock.patch.object(self.sdk, "ensure_avd_for",
                                  return_value=(False, "avdmanager failed",
                                                None)), \
                mock.patch.object(self.sdk, "uninstall_system_image") as uninstall:
            ok, message, avd = self.sdk.install_version(
                self.new, old_version=self.old)

        self.assertFalse(ok)
        self.assertIn("avdmanager failed", message)
        uninstall.assert_not_called()
        self.assertTrue(old_image.exists())
        self.assertTrue((self.avd_base
                         / "apkpreview_api34_default.avd").exists())
        self.assertTrue((self.avd_base
                         / "apkpreview_api34_default.ini").exists())
        # The new image may already be downloaded; that is fine.  The old
        # version must still be complete and usable.
        self.assertTrue((self.sdk_dir / "system-images" / "android-33"
                         / "default" / "x86_64" / "system.img").exists())

    def test_cleanup_failure_is_a_warning_not_install_failure(self):
        with mock.patch.object(self.sdk, "is_system_image_installed",
                               return_value=True), \
                mock.patch.object(self.sdk, "ensure_avd_for",
                                  return_value=(True, "ok",
                                                "apkpreview_api33_default")), \
                mock.patch.object(self.sdk, "remove_version",
                                  return_value=(False, "AVD is locked")):
            ok, message, avd = self.sdk.install_version(
                self.new, old_version=self.old)

        self.assertTrue(ok, message)
        self.assertIn("could not fully remove", message)
        self.assertIn("AVD is locked", message)
        self.assertEqual(avd, "apkpreview_api33_default")


class _FakeStdout:
    def __init__(self, text):
        self._text = text
        self._pos = 0

    def read(self, size=1):
        chunk = self._text[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeProc:
    def __init__(self, text):
        self.stdout = _FakeStdout(text)

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


class TestRealCreateAndRemoveFlow(unittest.TestCase):
    """Exercise real install_system_image/create_avd/remove_version code paths.

    Only the external executables are simulated.  This covers the full
    ordering without touching a real SDK or emulator.
    """

    def test_install_version_full_flow(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk_dir = root / "sdk"
            avd_base = root / "avd"
            avd_base.mkdir(parents=True)
            old_image = (sdk_dir / "system-images" / "android-34"
                         / "default" / "x86_64")
            old_image.mkdir(parents=True)
            (old_image / "system.img").write_text("old", encoding="utf-8")
            _write_avd(avd_base, "apkpreview_api34_default", 34)

            sdk = SDKManager()
            sdk._sdk_dir = sdk_dir
            old = AndroidVersion(34, "default", "x86_64", installed=True)
            new = AndroidVersion(33, "default", "x86_64")
            sdkmanager = root / "sdkmanager"
            sdkmanager.write_text("", encoding="utf-8")
            avdmanager = root / "avdmanager"
            avdmanager.write_text("", encoding="utf-8")

            def fake_popen(cmd, **kwargs):
                package = cmd[1]
                _, api, variant, abi = package.split(";")
                image = (sdk_dir / "system-images" / api / variant / abi)
                image.mkdir(parents=True, exist_ok=True)
                (image / "system.img").write_text("new", encoding="utf-8")
                return _FakeProc("100% Installed\n")

            def fake_run(cmd, **kwargs):
                if "create" in cmd:
                    name = cmd[cmd.index("-n") + 1]
                    avd_dir = avd_base / f"{name}.avd"
                    avd_dir.mkdir(parents=True, exist_ok=True)
                    (avd_dir / "config.ini").write_text(
                        "image.sysdir.1 = system-images/android-33/default/"
                        "x86_64/\n", encoding="utf-8")
                    (avd_base / f"{name}.ini").write_text(
                        "pointer", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            def fake_run_command(cmd, **kwargs):
                package = cmd[-1]
                _, api, variant, abi = package.split(";")
                shutil.rmtree(sdk_dir / "system-images" / api / variant / abi)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(sdk, "_avd_dirs",
                                   return_value=[avd_base]), \
                    mock.patch.object(PlatformUtils, "get_image_abi",
                                      return_value="x86_64"), \
                    mock.patch.object(PlatformUtils, "get_sdkmanager_path",
                                      return_value=sdkmanager), \
                    mock.patch.object(PlatformUtils, "get_avdmanager_path",
                                      return_value=avdmanager), \
                    mock.patch.object(sdk, "accept_licenses",
                                      return_value=(True, "ok")), \
                    mock.patch("core.sdk_manager.subprocess.Popen",
                               side_effect=fake_popen), \
                    mock.patch("core.sdk_manager.subprocess.run",
                               side_effect=fake_run), \
                    mock.patch.object(PlatformUtils, "run_command",
                                      side_effect=fake_run_command):
                ok, message, avd = sdk.install_version(
                    new, old_version=old)

            self.assertTrue(ok, message)
            self.assertEqual(avd, "apkpreview_api33_default")
            self.assertTrue((sdk_dir / "system-images" / "android-33"
                             / "default" / "x86_64" / "system.img").exists())
            self.assertTrue((avd_base
                             / "apkpreview_api33_default.avd").is_dir())
            self.assertTrue((avd_base
                             / "apkpreview_api33_default.ini").exists())
            self.assertFalse(old_image.exists())
            self.assertFalse((avd_base
                              / "apkpreview_api34_default.avd").exists())
            self.assertFalse((avd_base
                              / "apkpreview_api34_default.ini").exists())


class TestDeleteAvd(unittest.TestCase):
    def test_delete_avd_reports_real_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_avd(base, "apkpreview_api34_default", 34)
            sdk = SDKManager()
            with mock.patch.object(sdk, "_avd_dirs", return_value=[base]), \
                    mock.patch("core.sdk_manager.shutil.rmtree",
                               side_effect=OSError("permission denied")):
                ok, message = sdk.delete_avd("apkpreview_api34_default")
            self.assertFalse(ok)
            self.assertIn("permission denied", message)
            self.assertTrue((base / "apkpreview_api34_default.avd").exists())
            self.assertTrue((base / "apkpreview_api34_default.ini").exists())


if __name__ == "__main__":
    unittest.main()
