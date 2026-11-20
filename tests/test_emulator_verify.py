"""Tests for the emulator acceleration / GPU verification (no emulator).

The verifier must not confuse the Vulkan SwiftShader ICD with the GLES
renderer: ``vulkan_mode_selected:swiftshader`` together with
``gles_mode_selected:host`` is a perfectly fine host-GPU configuration.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.emulator import EmulatorManager  # noqa: E402
from core.hardware_profile import PerformanceMode  # noqa: E402
from core.platform_utils import PlatformUtils  # noqa: E402


class _FakeManager(EmulatorManager):
    def __init__(self, text):
        super().__init__()
        self._text = text
        self.messages = []

    def _read_emulator_log(self):
        return self._text

    def _log(self, message):
        self.messages.append(message)


HOST_LOG = """
INFO | emuglConfig_init: vulkan_mode_selected:swiftshader gles_mode_selected:host
INFO | Graphics Adapter Vendor Google (ATI Technologies Inc.)
INFO | Graphics Adapter Android Emulator OpenGL ES Translator (AMD Radeon Pro 560 OpenGL Engine)
INFO | CPU acceleration: Hypervisor.Framework
"""

SOFTWARE_LOG = """
INFO | emuglConfig_init: vulkan_mode_selected:swiftshader gles_mode_selected:swangle
"""


class TestEmulatorVerification(unittest.TestCase):
    def test_vulkan_swiftshader_with_host_gles_is_not_software(self):
        mgr = _FakeManager(HOST_LOG)
        mgr._verify_acceleration_and_gpu()
        self.assertFalse(mgr._software_gpu)
        self.assertIn("host GPU", mgr._last_gpu_mode)
        self.assertIn("AMD Radeon Pro 560", mgr._last_gpu_mode)
        self.assertTrue(any("Hypervisor Framework" in m
                            for m in mgr.messages))

    def test_swangle_is_reported_as_software(self):
        mgr = _FakeManager(SOFTWARE_LOG)
        mgr._verify_acceleration_and_gpu()
        self.assertTrue(mgr._software_gpu)
        self.assertIn("software", mgr._last_gpu_mode)
        self.assertTrue(any("software rendering" in m for m in mgr.messages))

    def test_acceleration_falls_back_to_the_host_probe(self):
        mgr = _FakeManager("INFO | gles_mode_selected:host\n")
        info = {"supported": True, "backend": "hvf"}
        with mock.patch.object(PlatformUtils, "get_acceleration_info",
                               return_value=info):
            mgr._verify_acceleration_and_gpu()
        self.assertTrue(any("Hypervisor Framework" in m
                            for m in mgr.messages))

    def test_missing_acceleration_warns(self):
        mgr = _FakeManager("INFO | gles_mode_selected:host\n")
        with mock.patch.object(PlatformUtils, "get_acceleration_info",
                               return_value={"supported": False}):
            mgr._verify_acceleration_and_gpu()
        self.assertTrue(any("no hardware CPU acceleration" in m
                            for m in mgr.messages))

    def test_gpu_fallback_retries_with_software(self):
        """A failed host-GPU boot must retry with swiftshader_indirect."""
        mgr = EmulatorManager()
        built = []

        def fake_build(mode, **kwargs):
            built.append(kwargs.get("gpu_override"))
            return ["emulator", kwargs.get("gpu_override") or "host"]

        def fake_needed(mode, no_window=False):
            return True

        with mock.patch.object(mgr, "is_running", return_value=False), \
                mock.patch.object(EmulatorManager, "avd_name",
                                  new_callable=mock.PropertyMock,
                                  return_value="test"), \
                mock.patch.object(mgr, "_ensure_adb_server"), \
                mock.patch.object(mgr, "has_snapshot", return_value=False), \
                mock.patch.object(mgr, "build_start_command",
                                  side_effect=fake_build), \
                mock.patch.object(mgr, "_start_once",
                                  return_value=(False, "boom")) as start_once, \
                mock.patch.object(mgr, "_gpu_override_needed",
                                  side_effect=fake_needed), \
                mock.patch.object(mgr, "_stop_process"), \
                mock.patch.object(mgr, "_progress"), \
                mock.patch.object(mgr, "_log"):
            ok, _message = mgr.start(
                PerformanceMode.NORMAL,
                wait_for_boot=False, no_window=True)

        self.assertFalse(ok)
        self.assertEqual(start_once.call_count, 2)
        self.assertEqual(built, [None, "swiftshader_indirect"])


if __name__ == "__main__":
    unittest.main()
