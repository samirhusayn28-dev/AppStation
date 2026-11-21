"""Regression tests for Windows emulator boot failures (no emulator used).

1. Firmware virtualization alone must NOT be reported as a usable accelerator.
   The old code set ``aehd=True`` from a systeminfo string, which made us pass
   ``-accel on``; the emulator panics and exits when no accelerator exists.
2. The start command must use ``-accel auto`` so the emulator's own probe is
   authoritative and a missing backend cannot kill the process.
3. Boot errors must include the captured emulator log tail.
4. The GUI app must never open a visible console for its child processes
   (adb, sdkmanager, powershell, systeminfo, optionalfeatures, ...).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.emulator import EmulatorManager  # noqa: E402
from core.hardware_profile import EmulatorConfig, PerformanceMode  # noqa: E402
from core.platform_utils import PlatformUtils  # noqa: E402


def _completed(stdout="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


class TestWindowsVirtualizationDetection(unittest.TestCase):
    """Firmware virtualization is necessary but not sufficient for accel."""

    def _check(self, *, hyperv="Disabled", whpx="Disabled",
               aehd_running=False, firmware=True):
        def fake_run(cmd, **kwargs):
            joined = " ".join(cmd)
            if "Microsoft-Hyper-V-All" in joined:
                return _completed(f"State\n{hyperv}\n")
            if "HypervisorPlatform" in joined:
                return _completed(f"State\n{whpx}\n")
            if cmd and cmd[0] == "sc":
                return _completed(
                    "RUNNING" if aehd_running else "",
                    returncode=0 if aehd_running else 1,
                )
            if cmd and cmd[0] == "systeminfo":
                text = ("Virtualization Enabled In Firmware : Yes"
                        if firmware else
                        "Virtualization Enabled In Firmware : No")
                return _completed(text)
            return _completed()

        with mock.patch.object(PlatformUtils, "IS_WINDOWS", True), \
                mock.patch("core.platform_utils.subprocess.run",
                           side_effect=fake_run):
            return PlatformUtils.check_virtualization_windows()

    def test_firmware_only_is_not_supported(self):
        info = self._check(firmware=True)
        self.assertTrue(info["firmware_virtualization"])
        self.assertFalse(info["hyper_v"])
        self.assertFalse(info["whpx"])
        self.assertFalse(info["aehd"])
        self.assertFalse(info["supported"])
        self.assertEqual(info["backend"], "none")
        self.assertIn("Windows Hypervisor Platform", info["message"])

    def test_hyperv_alone_is_not_supported(self):
        info = self._check(hyperv="Enabled")
        self.assertTrue(info["hyper_v"])
        self.assertFalse(info["supported"])
        self.assertEqual(info["backend"], "none")

    def test_whpx_is_supported(self):
        info = self._check(whpx="Enabled")
        self.assertTrue(info["supported"])
        self.assertEqual(info["backend"], "whpx")

    def test_aehd_driver_is_supported(self):
        info = self._check(aehd_running=True)
        self.assertTrue(info["supported"])
        self.assertEqual(info["backend"], "aehd")

    def test_windows_probes_hide_console(self):
        """Every host probe must run without spawning a console window."""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return _completed()

        with mock.patch.object(PlatformUtils, "IS_WINDOWS", True), \
                mock.patch("core.platform_utils.subprocess.CREATE_NO_WINDOW",
                           0x08000000, create=True), \
                mock.patch("core.platform_utils.subprocess.run",
                           side_effect=fake_run):
            PlatformUtils.check_virtualization_windows()

        self.assertGreaterEqual(len(calls), 4)
        for kwargs in calls:
            self.assertEqual(kwargs.get("creationflags"), 0x08000000)


class TestNoConsoleWindows(unittest.TestCase):
    """The GUI app must not open a terminal for any child process."""

    def test_no_window_flags_on_windows(self):
        with mock.patch.object(PlatformUtils, "IS_WINDOWS", True), \
                mock.patch("core.platform_utils.subprocess.CREATE_NO_WINDOW",
                           0x08000000, create=True):
            self.assertEqual(PlatformUtils.no_window_flags(), 0x08000000)

    def test_no_window_flags_on_other_platforms(self):
        with mock.patch.object(PlatformUtils, "IS_WINDOWS", False):
            self.assertEqual(PlatformUtils.no_window_flags(), 0)

    def test_start_detached_process_sets_no_window_on_windows(self):
        with mock.patch.object(PlatformUtils, "IS_WINDOWS", True), \
                mock.patch.object(PlatformUtils, "get_sdk_dir",
                                  return_value=Path("sdk")), \
                mock.patch("core.platform_utils.subprocess.CREATE_NO_WINDOW",
                           0x08000000, create=True), \
                mock.patch("core.platform_utils.subprocess"
                           ".CREATE_NEW_PROCESS_GROUP",
                           0x00000200, create=True), \
                mock.patch("core.platform_utils.subprocess.Popen") as popen:
            PlatformUtils.start_detached_process(["adb", "devices"])

        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & 0x08000000)
        self.assertTrue(flags & 0x00000200)


class TestAccelStartCommand(unittest.TestCase):
    """`auto` cannot make the emulator panic when acceleration is missing."""

    def test_start_command_uses_accel_auto(self):
        config = EmulatorConfig(
            api_level=30,
            memory_mb=1536,
            cores=2,
            gpu_mode="host",
            screen_width=480,
            screen_height=800,
            density=240,
            no_audio=True,
            no_boot_anim=True,
            no_snapshot_save=True,
            no_window=False,
        )
        profiler = SimpleNamespace(get_emulator_config=lambda *a, **k: config)
        mgr = EmulatorManager()
        with mock.patch.object(PlatformUtils, "get_emulator_path",
                               return_value=Path("emulator")), \
                mock.patch.object(PlatformUtils, "get_acceleration_info",
                                  side_effect=AssertionError(
                                      "start command must not force accel")), \
                mock.patch("core.emulator.HardwareProfiler",
                           return_value=profiler), \
                mock.patch("core.emulator.settings",
                           SimpleNamespace(low_power_mode=False,
                                           quickboot=False)), \
                mock.patch.object(EmulatorManager, "avd_name",
                                  new_callable=mock.PropertyMock,
                                  return_value="test"), \
                mock.patch.object(mgr, "has_snapshot", return_value=False):
            cmd = mgr.build_start_command(PerformanceMode.NORMAL,
                                          no_window=True)

        self.assertIn("-accel", cmd)
        self.assertEqual(cmd[cmd.index("-accel") + 1], "auto")
        self.assertNotIn("on", cmd)


class TestWindowsStartGuidance(unittest.TestCase):
    """No accelerator must fail fast with actionable instructions."""

    def test_start_fails_fast_without_accelerator(self):
        mgr = EmulatorManager()
        accel = {"supported": False, "backend": "none",
                 "details": {"message": "no hypervisor"}}
        with mock.patch.object(PlatformUtils, "IS_WINDOWS", True), \
                mock.patch.object(PlatformUtils, "get_acceleration_info",
                                  return_value=accel), \
                mock.patch.object(EmulatorManager, "avd_name",
                                  new_callable=mock.PropertyMock,
                                  return_value="test"), \
                mock.patch.object(mgr, "_start_once") as start_once:
            ok, message = mgr.start(PerformanceMode.NORMAL)

        self.assertFalse(ok)
        self.assertIn("optionalfeatures", message)
        self.assertIn("Windows Hypervisor Platform", message)
        self.assertIn("no hypervisor", message)
        start_once.assert_not_called()


class TestBootErrorDiagnostics(unittest.TestCase):
    def test_dead_process_message_includes_emulator_log(self):
        mgr = EmulatorManager()
        with mock.patch.object(mgr, "is_running", return_value=False), \
                mock.patch.object(mgr, "_read_emulator_log",
                                  return_value="FATAL: no accelerator found"):
            ok, message = mgr._wait_for_boot(timeout=2)
        self.assertFalse(ok)
        self.assertIn("died during boot", message)
        self.assertIn("FATAL: no accelerator found", message)


if __name__ == "__main__":
    unittest.main()
