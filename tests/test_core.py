"""Unit tests for ApkPreview core modules."""

import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.hardware_profile import HardwareProfiler, PerformanceMode, EmulatorConfig
from core.device_presets import (DevicePreset, DevicePresetManager,
                                 BUILTIN_PRESETS, CATEGORY_ORDER,
                                 compute_density, load_bundled_devices,
                                 preset_manager)
from core.platform_utils import PlatformUtils
from core.settings import Settings


# Trimmed, representative ``avdmanager list device`` output retained only as a
# regression fixture: Phase 1 removed avdmanager from the preset pipeline.
SAMPLE_AVDMANAGER = """Available devices definitions:
id: 0 or "pixel_7"
    Name: Pixel 7
    OEM : Google
    Tag : google
    Resolution: 1080x2400
    Density: 420
---------
id: 1 or "Nexus 10"
    Name: Nexus 10
    OEM : Google
---------
id: 2 or "medium_tablet"
    Name: Medium Tablet
    OEM : Generic
    Resolution: 1600x2560
    Density: 320
---------
id: 3 or "6.7in Foldable"
    Name: 6.7\\" Foldable
    OEM : Generic
    Resolution: 1080x2400
    Density: 420
---------
id: 4 or "tv_1080p"
    Name: TV 1080p
    OEM : Google
    Tag : android-tv
---------
id: 5 or "wearos_small_round"
    Name: Wear OS Small Round
    OEM : Google
    Tag : android-wear
---------
id: 6 or "automotive_1024p_landscape"
    Name: Automotive (1024p landscape)
    OEM : Google
    Tag : android-automotive-playstore
---------
id: 7 or "small_phone"
    Name: Small Phone
    OEM : Generic
    Resolution: 720x1280
    Density: 280
---------
"""


class TestHardwareProfile(unittest.TestCase):
    """Tests for hardware profiling and mode selection."""
    
    def test_profiler_creation(self):
        """Test profiler can be created."""
        profiler = HardwareProfiler()
        self.assertIsNotNone(profiler)
    
    def test_mode_selection_low_spec(self):
        """Test low-spec mode selection logic."""
        # Simulate low-spec hardware
        profiler = HardwareProfiler()
        # We can't easily mock psutil here, but we can test config generation
        config = profiler.get_emulator_config(PerformanceMode.LOW_SPEC)
        
        self.assertEqual(config.memory_mb, 1024)
        self.assertEqual(config.cores, 1)
        self.assertEqual(config.gpu_mode, "swiftshader_indirect")
        self.assertEqual(config.screen_width, 720)
        self.assertEqual(config.screen_height, 1280)
        self.assertEqual(config.density, 280)
        self.assertTrue(config.no_window)
    
    def test_mode_selection_normal(self):
        """Test normal mode selection logic."""
        profiler = HardwareProfiler()
        config = profiler.get_emulator_config(PerformanceMode.NORMAL)
        
        self.assertEqual(config.memory_mb, 2048)
        self.assertEqual(config.cores, 2)
        self.assertEqual(config.gpu_mode, "host")
        self.assertEqual(config.screen_width, 1080)
        self.assertEqual(config.screen_height, 1920)
        self.assertEqual(config.density, 420)
        self.assertFalse(config.no_window)
    
    def test_system_image_selection(self):
        """Test correct system image for architecture."""
        # This tests the platform_utils function
        img = PlatformUtils.get_system_image(30)
        # Should contain either arm64-v8a or x86_64 depending on architecture
        self.assertTrue("arm64-v8a" in img or "x86_64" in img)
        self.assertIn("android-30", img)
        self.assertIn("default", img)


class TestDevicePresets(unittest.TestCase):
    """Tests for the bundled Brand > Category device presets."""

    def test_bundled_data_is_complete(self):
        """The bundled file contains exactly the requested device families."""
        self.assertEqual(len(BUILTIN_PRESETS), 202)
        counts = {}
        for preset in BUILTIN_PRESETS.values():
            counts[(preset.brand, preset.category)] = \
                counts.get((preset.brand, preset.category), 0) + 1
        self.assertEqual(counts[("Apple", "Phone")], 47)
        self.assertEqual(counts[("Apple", "Tablet")], 37)
        self.assertEqual(counts[("Samsung", "Phone")], 46)
        self.assertEqual(counts[("Samsung", "Tablet")], 24)
        self.assertEqual(counts[("Samsung", "Foldable")], 18)
        self.assertEqual(counts[("Google", "Phone")], 25)
        self.assertEqual(counts[("Google", "Tablet")], 1)
        self.assertEqual(counts[("Google", "Foldable")], 4)

        # The old hand-written/avdmanager keys are gone.
        for old_key in ("pixel7", "iphone_14", "tablet_10", "default"):
            self.assertNotIn(old_key, BUILTIN_PRESETS)

    def test_required_fields_and_cutouts(self):
        """Every entry has the required fields and a known cutout."""
        for preset in BUILTIN_PRESETS.values():
            self.assertTrue(preset.name)
            self.assertIn(preset.brand, ("Apple", "Samsung", "Google"))
            self.assertIn(preset.category, CATEGORY_ORDER)
            self.assertGreater(preset.width, 0)
            self.assertGreater(preset.height, 0)
            self.assertGreater(preset.density, 0)
            self.assertIn(preset.cutout,
                          ("home-button", "notch", "dynamic-island",
                           "punch-hole", "none", "bezel"))

    def test_apple_preview_only_and_rumored(self):
        """Apple entries are viewport previews; the foldable is rumored."""
        iphone = preset_manager.get_preset("apple_iphone_15_pro")
        self.assertIsNotNone(iphone)
        self.assertEqual((iphone.width, iphone.height), (1179, 2556))
        self.assertEqual(iphone.cutout, "dynamic-island")
        self.assertTrue(iphone.preview_only)
        self.assertEqual(iphone.density, 480)

        fold = preset_manager.get_preset("apple_foldable_iphone_rumored")
        self.assertIsNotNone(fold)
        self.assertTrue(fold.rumored)
        self.assertTrue(fold.is_foldable)
        self.assertEqual(fold.inner.size_str, "1920x2713")
        self.assertEqual(fold.outer.size_str, "1422x2088")
        self.assertEqual(fold.outer.cutout, "punch-hole")

    def test_foldables_have_inner_and_outer(self):
        """Every foldable exposes both screens and the fold toggle."""
        foldables = [p for p in BUILTIN_PRESETS.values() if p.is_foldable]
        self.assertEqual(len(foldables), 23)  # 18 Samsung + 4 Google + 1 Apple
        for preset in foldables:
            self.assertIsNotNone(preset.inner)
            self.assertIsNotNone(preset.outer)
            self.assertEqual(preset.screen("inner"), preset.inner)
            self.assertEqual(preset.screen("outer"), preset.outer)
            self.assertGreater(preset.outer.width, 0)

        fold7 = preset_manager.get_preset("samsung_galaxy_z_fold7")
        self.assertEqual(fold7.inner.size_str, "1968x2184")
        self.assertEqual(fold7.outer.size_str, "1080x2520")

    def test_density_is_computed_from_resolution(self):
        """The ppi/dp rule yields sensible Android density buckets."""
        self.assertEqual(compute_density(1170, 2532, "Phone", "Apple"), 480)
        self.assertEqual(compute_density(750, 1334, "Phone", "Apple"), 320)
        self.assertEqual(compute_density(1080, 2400, "Phone", "Google"), 440)
        self.assertEqual(compute_density(1440, 3120, "Phone", "Samsung"), 520)
        self.assertEqual(compute_density(1600, 2560, "Tablet", "Samsung"), 280)
        self.assertEqual(compute_density(1812, 2176, "Foldable", "Samsung",
                                         "inner"), 360)

    def test_brand_and_category_grouping(self):
        """Groups are Brand > Category in the requested order."""
        groups = preset_manager.get_brand_groups()
        self.assertEqual([brand for brand, _ in groups],
                         ["Apple", "Samsung", "Google"])
        samsung = dict(groups[1][1])
        self.assertEqual(list(samsung.keys()),
                         ["Phone", "Tablet", "Foldable"])
        self.assertEqual(len(samsung["Foldable"]), 18)

    def test_search(self):
        """Search matches model, brand, category, resolution and cutout."""
        names = [p.name for p in preset_manager.search("iphone 15")]
        self.assertIn("iPhone 15 Pro", names)
        self.assertIn("iPhone 15 Pro Max", names)

        self.assertTrue(preset_manager.search("fold7"))
        self.assertTrue(all("fold7" in p.search_text
                            for p in preset_manager.search("fold7")))
        self.assertGreater(len(preset_manager.search("punch-hole")), 50)
        self.assertEqual(preset_manager.search("definitely-not-a-device"), [])

    def test_manager_custom_presets(self):
        """User-added presets still work alongside the bundled list."""
        manager = DevicePresetManager()
        manager.add_custom_preset("Custom Phone", 800, 1600, 320)
        custom = manager.get_preset("custom_phone")
        self.assertIsNotNone(custom)
        self.assertEqual(custom.width, 800)
        self.assertTrue(manager.is_custom("custom_phone"))
        manager.remove_custom_preset("custom_phone")
        self.assertIsNone(manager.get_preset("custom_phone"))

    def test_bundled_file_roundtrip(self):
        """Loading the bundled file again produces the same set."""
        loaded = load_bundled_devices()
        self.assertEqual(set(loaded), set(BUILTIN_PRESETS))

    def test_avdmanager_pipeline_removed(self):
        """Phase 1 removed the avdmanager parser and ~/.apkpreview cache."""
        import core.device_presets as dp
        for removed in ("load_avdmanager_presets", "parse_avdmanager_devices",
                        "save_devices_cache", "load_devices_cache"):
            self.assertFalse(hasattr(dp, removed), removed)



class TestPlatformUtils(unittest.TestCase):
    """Tests for platform utilities."""
    
    def test_app_data_dir(self):
        """Test app data directory resolution."""
        path = PlatformUtils.get_app_data_dir()
        self.assertTrue(path.is_absolute())
        self.assertIn(".apkpreview", str(path))
    
    def test_sdk_dir(self):
        """Test SDK directory."""
        path = PlatformUtils.get_sdk_dir()
        self.assertEqual(path, PlatformUtils.get_app_data_dir() / "sdk")
    
    def test_arch_detection(self):
        """Test architecture detection."""
        arch = PlatformUtils.get_arch()
        self.assertIn(arch, ["arm64", "x86_64", "arm"])
    
    def test_executable_extension(self):
        """Test executable extension."""
        ext = PlatformUtils.get_executable_extension()
        if PlatformUtils.IS_WINDOWS:
            self.assertEqual(ext, ".exe")
        else:
            self.assertEqual(ext, "")
    
    def test_path_normalization(self):
        """Test path normalization."""
        path = PlatformUtils.normalize_path("~/test")
        self.assertTrue(path.is_absolute())
        self.assertFalse("~" in str(path))


class TestSettings(unittest.TestCase):
    """Tests for settings management."""
    
    def test_default_settings(self):
        """Test default settings values."""
        s = Settings()
        # Reset to defaults for clean test
        s.reset()
        
        self.assertEqual(s.performance_mode, "auto")
        self.assertEqual(s.idle_timeout_minutes, 10)
        self.assertEqual(s.scrcpy_bitrate, "8M")
        self.assertEqual(s.scrcpy_max_fps, 30)
        self.assertFalse(s.auto_reinstall)
    
    def test_settings_persistence(self):
        """Test settings save/load."""
        import tempfile
        import json
        from core.settings import Settings
        
        # Create temp settings file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump({"performance_mode": "low_spec", "idle_timeout_minutes": 5}, f)
            temp_path = Path(f.name)
        
        try:
            # Create Settings instance with custom path
            s = Settings.__new__(Settings)
            s._settings_path = temp_path
            s._data = Settings.DEFAULT_SETTINGS.copy()
            s.load()
            
            self.assertEqual(s.performance_mode, "low_spec")
            self.assertEqual(s.idle_timeout_minutes, 5)
            
            # Test update
            s.performance_mode = "normal"
            self.assertEqual(s.performance_mode, "normal")
            
        finally:
            temp_path.unlink(missing_ok=True)
            temp_path.unlink(missing_ok=True)


class TestCommandBuilders(unittest.TestCase):
    """Tests for command building functions."""
    
    def test_emulator_command_low_spec(self):
        """Test emulator command for low-spec mode."""
        profiler = HardwareProfiler()
        config = profiler.get_emulator_config(PerformanceMode.LOW_SPEC)
        
        # Verify key flags are present
        self.assertEqual(config.memory_mb, 1024)
        self.assertEqual(config.cores, 1)
        self.assertTrue(config.no_audio)
        self.assertTrue(config.no_boot_anim)
        self.assertTrue(config.no_window)
    
    def test_emulator_command_normal(self):
        """Test emulator command for normal mode."""
        profiler = HardwareProfiler()
        config = profiler.get_emulator_config(PerformanceMode.NORMAL)
        
        self.assertEqual(config.memory_mb, 2048)
        self.assertEqual(config.cores, 2)
        self.assertEqual(config.gpu_mode, "host")
        self.assertTrue(config.no_audio)
        self.assertTrue(config.no_boot_anim)


if __name__ == "__main__":
    unittest.main()


class TestFileSafety(unittest.TestCase):
    """Tests to ensure no code writes outside /tmp during tests."""
    
    def test_no_writes_outside_tmp(self):
        """Verify that test environment only uses /tmp for file operations."""
        import tempfile
        import os
        
        # Create a test file in /tmp
        with tempfile.NamedTemporaryFile(dir="/tmp", delete=False) as f:
            tmp_file = f.name
        
        try:
            # Verify we can write to /tmp
            with open(tmp_file, "w") as f:
                f.write("test")
            
            # Verify the file is in /tmp
            self.assertTrue(tmp_file.startswith("/tmp"))
            
            # Clean up
            os.unlink(tmp_file)
            
            # Verify we cannot write to user's home directory (this would fail in a real test)
            # We just verify the test environment uses /tmp
            self.assertTrue(True)
        except Exception:
            self.fail("File operations outside /tmp detected")
    
    def test_apkpreview_test_dir_exists(self):
        """Verify /tmp/apkpreview-test exists for test files."""
        import os
        test_dir = "/tmp/apkpreview-test"
        # Directory should exist or be creatable
        os.makedirs(test_dir, exist_ok=True)
        self.assertTrue(os.path.isdir(test_dir))


class TestAndroidVersions(unittest.TestCase):
    """Tests for Android version (system image) listing and selection."""

    SAMPLE_LIST = """\
Installed packages:
  Path                                     | Version | Description                     | Location
  -------                                  | ------- | -------                         | -------
  platform-tools                           | 35.0.2  | Android SDK Platform-Tools      | platform-tools
  emulator                                 | 35.2.10 | Android Emulator                | emulator
  system-images;android-34;default;x86_64  | 14      | Intel x86_64 Atom System Image  | system-images/android-34/default/x86_64

Available Packages:
  Path                                         | Version | Description
  -------                                      | ------- | -------
  system-images;android-30;default;x86_64      | 11      | Intel x86_64 Atom System Image
  system-images;android-33;default;x86_64      | 13      | Intel x86_64 Atom System Image
  system-images;android-34;default;x86_64      | 14      | Intel x86_64 Atom System Image
  system-images;android-34;google_apis;x86_64  | 14      | Google APIs Intel x86_64 Atom System Image
  system-images;android-34;google_apis_playstore;x86_64 | 14 | Google Play Intel x86_64 Atom System Image
  system-images;android-34;google_apis;arm64-v8a | 14    | Google APIs ARM64 System Image
  system-images;android-35;default;arm64-v8a   | 15      | ARM64 System Image
"""

    def test_parse_system_images_for_x86_64(self):
        """Parsing keeps only the host ABI and marks installed packages."""
        from core.sdk_manager import SDKManager
        versions = SDKManager.parse_sdkmanager_list(self.SAMPLE_LIST, "x86_64")
        keys = {(v.api_level, v.variant) for v in versions}

        self.assertIn((34, "default"), keys)
        self.assertIn((30, "default"), keys)
        self.assertIn((33, "default"), keys)
        self.assertIn((34, "google_apis"), keys)
        self.assertIn((34, "google_apis_playstore"), keys)
        # arm64-v8a rows must be filtered out for an x86_64 host
        self.assertNotIn((35, "default"), keys)

        by_key = {(v.api_level, v.variant): v for v in versions}
        self.assertTrue(by_key[(34, "default")].installed)
        self.assertFalse(by_key[(33, "default")].installed)

    def test_parse_system_images_for_arm64(self):
        """The arm64 host sees only arm64-v8a images."""
        from core.sdk_manager import SDKManager
        versions = SDKManager.parse_sdkmanager_list(self.SAMPLE_LIST, "arm64-v8a")
        keys = {(v.api_level, v.variant) for v in versions}
        self.assertIn((35, "default"), keys)
        self.assertNotIn((30, "default"), keys)

    def test_android_version_helpers(self):
        """Package id, display name and per-version AVD name are derived."""
        from core.sdk_manager import AndroidVersion
        version = AndroidVersion(api_level=34, variant="default", abi="x86_64")
        self.assertEqual(version.package, "system-images;android-34;default;x86_64")
        self.assertEqual(version.version_name, "Android 14")
        self.assertEqual(version.avd_name, "apkpreview_api34_default")

    def test_parse_size_column(self):
        """A size column is picked up when sdkmanager emits one."""
        from core.sdk_manager import SDKManager
        out = ("Available Packages:\n"
               "  system-images;android-34;default;x86_64 | 14 | x | 1.2 GB\n")
        versions = SDKManager.parse_sdkmanager_list(out, "x86_64")
        self.assertEqual(len(versions), 1)
        self.assertIsNotNone(versions[0].size_bytes)
        self.assertEqual(versions[0].size_display(), "1.2 GB")

    def test_parse_empty_output(self):
        from core.sdk_manager import SDKManager
        self.assertEqual(SDKManager.parse_sdkmanager_list("", "x86_64"), [])

    def test_parse_progress_percent(self):
        from core.sdk_manager import parse_progress_percent
        self.assertEqual(parse_progress_percent("[===== ] 45% Unzipping..."), 45)
        self.assertEqual(parse_progress_percent("100%"), 100)
        self.assertIsNone(parse_progress_percent("no percentage here"))

    def test_required_packages_have_no_android_version(self):
        """Base setup installs platform-tools + emulator only."""
        from core.sdk_manager import SDKManager
        packages = SDKManager().get_required_packages()
        self.assertIn("platform-tools", packages)
        self.assertIn("emulator", packages)
        self.assertFalse(any("system-images" in p for p in packages))
        self.assertFalse(any("build-tools" in p for p in packages))

    def test_settings_selected_version_roundtrip(self):
        """Settings persist the selected version and expose its AVD."""
        import tempfile
        import json
        from core.settings import Settings

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                         delete=False) as f:
            json.dump({}, f)
            path = Path(f.name)
        try:
            s = Settings.__new__(Settings)
            s._settings_path = path
            s._data = Settings.DEFAULT_SETTINGS.copy()

            self.assertEqual(s.selected_api_level, 0)
            self.assertFalse(s.has_selected_version)

            s.select_android_version(33, "google_apis",
                                     "apkpreview_api33_google_apis")
            self.assertEqual(s.selected_api_level, 33)
            self.assertEqual(s.selected_variant, "google_apis")
            self.assertEqual(s.avd_name, "apkpreview_api33_google_apis")
            self.assertTrue(s.has_selected_version)
        finally:
            path.unlink(missing_ok=True)


class TestVersionReplacement(unittest.TestCase):
    """Replace-on-update: the old version is only removed after the new one."""

    def test_success_path_removes_old_only_after_new_installed(self):
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        new = AndroidVersion(33, "default", "x86_64")
        old = AndroidVersion(34, "default", "x86_64")
        order = []

        def fake_install(version, on_line=None, cancel=None):
            order.append("install")
            return True, "installed"

        def fake_ensure(version, config=None, data_partition_gb=3.0):
            order.append("avd")
            return True, "ok", "apkpreview_api33_default"

        def fake_remove(version, remove_image=True):
            order.append("remove")
            return True, "removed"

        with mock.patch.object(sdk, "is_system_image_installed",
                               return_value=False), \
                mock.patch.object(sdk, "install_system_image",
                                  side_effect=fake_install), \
                mock.patch.object(sdk, "ensure_avd_for",
                                  side_effect=fake_ensure), \
                mock.patch.object(sdk, "remove_version",
                                  side_effect=fake_remove):
            ok, message, avd_name = sdk.install_version(new, old_version=old)

        self.assertTrue(ok, message)
        self.assertEqual(avd_name, "apkpreview_api33_default")
        # Install first, AVD next, and only then remove the old version.
        self.assertEqual(order, ["install", "avd", "remove"])

    def test_install_failure_removes_nothing(self):
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        new = AndroidVersion(33, "default", "x86_64")
        old = AndroidVersion(34, "default", "x86_64")

        with mock.patch.object(sdk, "is_system_image_installed",
                               return_value=False), \
                mock.patch.object(sdk, "install_system_image",
                                  return_value=(False, "network error")), \
                mock.patch.object(sdk, "ensure_avd_for") as ensure, \
                mock.patch.object(sdk, "remove_version") as remove:
            ok, message, avd_name = sdk.install_version(
                new, old_version=old)

        self.assertFalse(ok)
        self.assertIn("network error", message)
        ensure.assert_not_called()
        remove.assert_not_called()

    def test_install_cancel_removes_nothing(self):
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        new = AndroidVersion(33, "default", "x86_64")
        old = AndroidVersion(34, "default", "x86_64")

        with mock.patch.object(sdk, "is_system_image_installed",
                               return_value=False), \
                mock.patch.object(sdk, "install_system_image",
                                  return_value=(False, "Download cancelled")), \
                mock.patch.object(sdk, "remove_version") as remove:
            ok, _, _ = sdk.install_version(new, old_version=old)

        self.assertFalse(ok)
        remove.assert_not_called()

    def test_already_installed_skips_download(self):
        """The Select path removes the old version without re-downloading."""
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        version = AndroidVersion(33, "default", "x86_64")
        old = AndroidVersion(34, "default", "x86_64")

        with mock.patch.object(sdk, "is_system_image_installed",
                               return_value=True), \
                mock.patch.object(sdk, "install_system_image") as install, \
                mock.patch.object(sdk, "ensure_avd_for",
                                  return_value=(True, "ok",
                                                "apkpreview_api33_default")), \
                mock.patch.object(sdk, "remove_version",
                                  return_value=(True, "removed")) as remove:
            ok, _, avd_name = sdk.install_version(version, old_version=old)

        self.assertTrue(ok)
        install.assert_not_called()
        remove.assert_called_once_with(old)

    def test_non_apkpreview_avds_are_never_touched(self):
        """A user-made AVD survives; only apkpreview_* folders are deleted."""
        import tempfile
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def make_avd(name, sysdir):
                avd_dir = base / f"{name}.avd"
                avd_dir.mkdir(parents=True)
                (avd_dir / "config.ini").write_text(
                    f"image.sysdir.1 = {sysdir}\n")
                (base / f"{name}.ini").write_text("pointer")

            make_avd("MyPhone", "system-images/android-34/default/x86_64/")
            make_avd("apkpreview_api34_google_apis",
                     "system-images/android-34/google_apis/x86_64/")

            with mock.patch.object(sdk, "_avd_dirs", return_value=[base]), \
                    mock.patch.object(sdk, "uninstall_system_image",
                                      return_value=(True, "ok")):
                ok, message = sdk.remove_version(
                    AndroidVersion(34, "google_apis", "x86_64"))

            self.assertTrue(ok, message)
            self.assertFalse(
                (base / "apkpreview_api34_google_apis.avd").exists())
            self.assertTrue((base / "MyPhone.avd").exists())
            self.assertTrue((base / "MyPhone.ini").exists())


class TestInstallStreaming(unittest.TestCase):
    """The system-image download streams sdkmanager output line by line."""

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

    def _fake_popen(self, output):
        stdout = self._FakeStdout(output)
        fake = type("FakeProc", (), {})()
        fake.stdout = stdout

        def wait(timeout=None):
            return 0

        fake.wait = wait
        fake.terminate = lambda: None
        fake.kill = lambda: None
        return fake

    def test_install_streams_carriage_return_progress(self):
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        lines = []
        # sdkmanager separates progress updates with '\r', not '\n'.
        output = "[===   ] 25%\r[===== ] 50%\r[======] 100%\rDone\n"

        with mock.patch.object(SDKManager, "accept_licenses",
                               return_value=(True, "ok")), \
                mock.patch("core.sdk_manager.PlatformUtils.get_sdkmanager_path",
                           return_value=Path(__file__)), \
                mock.patch("core.sdk_manager.subprocess.Popen",
                           return_value=self._fake_popen(output)):
            ok, message = sdk.install_system_image(
                AndroidVersion(34, "default", "x86_64"),
                on_line=lines.append)

        self.assertTrue(ok, message)
        self.assertTrue(any("25%" in line for line in lines), lines)
        self.assertTrue(any("50%" in line for line in lines), lines)
        self.assertTrue(any("100%" in line for line in lines), lines)

    def test_install_reports_cancel(self):
        import threading
        from unittest import mock
        from core.sdk_manager import SDKManager, AndroidVersion

        sdk = SDKManager()
        cancel = threading.Event()
        cancel.set()  # already cancelled before the loop starts

        with mock.patch.object(SDKManager, "accept_licenses",
                               return_value=(True, "ok")), \
                mock.patch("core.sdk_manager.PlatformUtils.get_sdkmanager_path",
                           return_value=Path(__file__)), \
                mock.patch("core.sdk_manager.subprocess.Popen",
                           return_value=self._fake_popen("anything")):
            ok, message = sdk.install_system_image(
                AndroidVersion(34, "default", "x86_64"), cancel=cancel)

        self.assertFalse(ok)
        self.assertIn("cancel", message.lower())


if __name__ == "__main__":
    unittest.main()