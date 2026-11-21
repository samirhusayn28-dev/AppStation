"""Emulator management for AppStation - start, stop, monitor, snapshot."""

import os
import re
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional, Callable, List, Tuple, Dict
from .platform_utils import PlatformUtils
from .hardware_profile import HardwareProfiler, PerformanceMode, EmulatorConfig
from .settings import settings


class EmulatorManager:
    """Manages the Android emulator lifecycle.

    Performance features:

    * host-matching ABI is enforced by :class:`SDKManager`; the emulator only
      starts AVDs whose image matches the host CPU;
    * quickboot: a ``default_boot`` snapshot is saved after the first boot and
      loaded on later starts (falling back to a cold boot if it is invalid);
    * ``-accel on`` when HVF/WHPX/AEHD/KVM is available;
    * ``-no-audio``, ``-no-boot-anim``, ``-netfast``, cameras off, fixed
      cores/RAM and a small data partition;
    * ``-gpu host`` with an automatic ``swiftshader_indirect`` fallback;
    * real boot stages reported through :meth:`set_progress_callback`;
    * guest animations disabled via adb after boot.
    """

    BOOT_SNAPSHOT = "default_boot"

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        self._output_callback: Optional[Callable[[str], None]] = None
        self._boot_callback: Optional[Callable[[bool], None]] = None
        self._progress_callback: Optional[Callable[[int, str], None]] = None
        self._serial: Optional[str] = None

    @property
    def avd_name(self) -> str:
        """The AVD for the currently selected Android version.

        Read live from settings so switching versions (which changes the
        selected AVD) takes effect on the next start/restart without rebuilding
        the manager.
        """
        return settings.avd_name

    def set_output_callback(self, callback: Callable[[str], None]):
        """Set callback for emulator output lines."""
        self._output_callback = callback

    def set_boot_callback(self, callback: Callable[[bool], None]):
        """Set callback for boot completion (success/failure)."""
        self._boot_callback = callback

    def set_progress_callback(self, callback: Callable[[int, str], None]):
        """Set callback for real boot progress: ``callback(percent, stage)``."""
        self._progress_callback = callback

    def _progress(self, percent: int, message: str):
        if self._progress_callback:
            try:
                self._progress_callback(max(0, min(100, int(percent))), message)
            except Exception:
                pass

    def _log(self, message: str):
        """Log message via callback."""
        if self._output_callback:
            self._output_callback(message)

    def is_running(self) -> bool:
        """Check if emulator process is running."""
        if self._process is None:
            return False
        return self._process.poll() is None

    def get_pid(self) -> Optional[int]:
        """Get emulator process PID."""
        if self._process:
            return self._process.pid
        return None

    # -- AVD / snapshot helpers -----------------------------------------

    def _avd_dirs(self) -> List[Path]:
        return [
            PlatformUtils.get_app_data_dir() / "avd",
            Path.home() / ".android" / "avd",
        ]

    def _avd_dir(self, name: Optional[str] = None) -> Optional[Path]:
        name = name or self.avd_name
        if not name:
            return None
        for base in self._avd_dirs():
            candidate = base / f"{name}.avd"
            if candidate.is_dir():
                return candidate
        return None

    def has_snapshot(self, name: str = BOOT_SNAPSHOT) -> bool:
        """True when the AVD has a quickboot snapshot on disk."""
        avd = self._avd_dir()
        if avd is None:
            return False
        snap = avd / "snapshots" / name
        try:
            return snap.is_dir() and any(snap.iterdir())
        except OSError:
            return False

    def _delete_snapshot_files(self, name: str = BOOT_SNAPSHOT) -> None:
        """Remove a possibly-corrupt snapshot without starting the emulator."""
        avd = self._avd_dir()
        if avd is None:
            return
        import shutil
        try:
            shutil.rmtree(avd / "snapshots" / name, ignore_errors=True)
        except Exception:
            pass

    # -- command building -----------------------------------------------

    def build_start_command(self, mode: PerformanceMode,
                            force_window: bool = False,
                            no_window: bool = False,
                            use_snapshot: bool = True,
                            snapshot_name: str = BOOT_SNAPSHOT,
                            gpu_override: Optional[str] = None,
                            low_power: Optional[bool] = None) -> List[str]:
        """Build the emulator start command based on mode.

        ``no_window`` forces ``-no-window`` even when ``force_window`` or the
        config would otherwise request a window.  That is used by the embedded
        screen: the app renders the device itself, so the emulator must not
        open its own window.

        ``use_snapshot`` asks for a quickboot start; it is honoured only when
        the snapshot actually exists.  ``gpu_override`` is used by the
        ``-gpu host`` -> ``swiftshader_indirect`` fallback.
        """
        emulator_path = PlatformUtils.get_emulator_path()
        profiler = HardwareProfiler()
        if low_power is None:
            low_power = settings.low_power_mode
        config = profiler.get_emulator_config(mode, low_power=low_power)
        gpu_mode = gpu_override or config.gpu_mode

        warm = bool(use_snapshot and settings.quickboot
                    and self.has_snapshot(snapshot_name))

        cmd: List[str] = [str(emulator_path)]
        cmd.extend(["-avd", self.avd_name])
        cmd.extend(["-memory", str(config.memory_mb)])
        cmd.extend(["-cores", str(config.cores)])
        cmd.extend(["-gpu", gpu_mode])

        # Use `auto`, never `on`.  The emulator enables acceleration when its
        # own probe says it is ready and otherwise falls back to software
        # emulation.  `on` makes the emulator panic and exit immediately when
        # no accelerator is available, which surfaced to users as
        # "Emulator process died during boot" on Windows hosts where the host
        # probe can be optimistic.  Letting the emulator decide is authoritative.
        cmd.extend(["-accel", "auto"])

        if config.no_audio:
            cmd.append("-no-audio")
        if config.no_boot_anim:
            cmd.append("-no-boot-anim")
        cmd.append("-netfast")

        # Use window if scrcpy not available (force_window) or config says so,
        # unless the caller explicitly asks for a headless (no-window) start.
        use_window = (force_window or not config.no_window) and not no_window
        if not use_window:
            cmd.append("-no-window")

        # Screen size and density
        cmd.extend(["-skin", f"{config.screen_width}x{config.screen_height}"])
        cmd.extend(["-dpi-device", str(config.density)])

        # Additional args
        cmd.extend(config.extra_args)

        if warm:
            # Load the saved snapshot and keep it intact (do not overwrite it
            # with whatever state the user leaves behind on exit).
            cmd.extend(["-snapshot", snapshot_name, "-no-snapshot-save"])
        else:
            # Cold boot: no snapshot load, no implicit save.  A fresh
            # snapshot is saved explicitly after boot succeeds.
            cmd.extend(["-no-snapshot-load", "-no-snapshot-save"])

        # Cameras are not needed for APK testing; turning them off saves CPU
        # and avoids virtual-scene rendering work.
        cmd.extend(["-camera-back", "none", "-camera-front", "none"])

        # Filter empty strings and duplicate flags with identical values.
        clean: List[str] = []
        for item in cmd:
            if item:
                clean.append(item)
        return clean

    # -- lifecycle -------------------------------------------------------

    def _emulator_log_path(self) -> Path:
        """Where the emulator's own stdout/stderr is captured."""
        try:
            return PlatformUtils.get_app_data_dir() / "emulator.log"
        except Exception:
            return Path.home() / ".apkpreview" / "emulator.log"

    def _start_once(self, cmd: List[str], wait_for_boot: bool,
                    timeout: int) -> Tuple[bool, str]:
        try:
            log_path = self._emulator_log_path()
            self._process = PlatformUtils.start_detached_process(
                cmd, log_file=log_path)
            self._serial = None
            if wait_for_boot:
                result = self._wait_for_boot(timeout)
                self._verify_acceleration_and_gpu(log_path)
                return result
            return True, "Emulator started"
        except Exception as e:
            return False, f"Failed to start emulator: {e}"

    def _read_emulator_log(self) -> str:
        """Read the captured emulator log (best effort)."""
        try:
            path = self._emulator_log_path()
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _emulator_error_detail(self, limit: int = 1200) -> str:
        """Tail of the emulator log, included in boot-error messages.

        Without this, a failed boot only said "died during boot" and gave the
        user nothing actionable.  The emulator writes the real reason (bad
        accelerator, GPU, AVD, ...) to its own log.
        """
        text = self._read_emulator_log().strip()
        if not text:
            return ""
        if len(text) > limit:
            text = text[-limit:]
        return "Emulator log (tail):\n" + text

    def _verify_acceleration_and_gpu(self,
                                     log_path: Optional[Path] = None):
        """Check that CPU acceleration and the GPU mode are as expected.

        The emulator prints the chosen accelerator and graphics adapter to
        its log.  When the host has no usable GPU the emulator silently falls
        back to ANGLE-on-SwiftShader (pure software rendering), which is very
        slow, so the user is warned about it.
        """
        text = self._read_emulator_log()
        if not text:
            return
        lowered = text.lower()

        accel = ""
        for marker, name in (
                ("Hypervisor.Framework", "Hypervisor Framework (hvf)"),
                ("Windows Hypervisor Platform", "WHPX"),
                ("Android Emulator Hypervisor Driver", "AEHD"),
                ("hax", "HAX")):
            if marker.lower() in lowered:
                accel = name
                break
        if not accel:
            # Newer emulator builds do not always print the accelerator to
            # stdout, so fall back to the host probe (which is authoritative).
            info = PlatformUtils.get_acceleration_info()
            if info.get("supported"):
                backend = str(info.get("backend") or "").lower()
                accel = {"hvf": "Hypervisor Framework (hvf)",
                         "whpx": "WHPX",
                         "aehd": "AEHD",
                         "kvm": "KVM"}.get(backend, backend.upper() or "hardware")
        if accel:
            self._log(f"CPU acceleration active: {accel}")
        else:
            self._log("WARNING: no hardware CPU acceleration found in the "
                      "emulator log")

        # The GLES mode decides how the *guest UI* is rendered.  A Vulkan
        # SwiftShader line is harmless (Vulkan is only used for some apps);
        # what matters is ``gles_mode_selected``.  Older builds print
        # ``swangle`` instead, which is the software fallback.
        match = re.search(r"gles_mode_selected:(\w+)", lowered)
        gles = match.group(1) if match else ""
        adapter = ""
        adapters = re.findall(r"graphics adapter ([^\r\n]+)", text, re.I)
        for line in adapters:
            if "translator" in line.lower() or "opengl es" in line.lower():
                adapter = line.strip()
                break
        if not adapter and adapters:
            adapter = adapters[-1].strip()
        software = (gles in ("swiftshader", "swangle")
                    or (not gles and "swangle" in lowered))
        self._software_gpu = software
        if software:
            self._last_gpu_mode = "software (ANGLE/SwiftShader)"
            self._log("WARNING: the emulator is using software rendering "
                      "(ANGLE/SwiftShader) - no usable host GPU.  Expect a "
                      "slow screen.")
        else:
            self._last_gpu_mode = (f"host GPU ({adapter})" if adapter
                                   else "host GPU")
            self._log(f"GPU mode: {self._last_gpu_mode}")

    def _stop_process(self) -> None:
        if self._process is not None:
            try:
                PlatformUtils.kill_process_tree(self._process.pid)
            except Exception:
                pass
        self._process = None
        self._serial = None

    def start(self, mode: PerformanceMode, wait_for_boot: bool = True,
              timeout: int = 120, scrcpy_available: bool = True,
              no_window: bool = False, open_window: bool = False) -> Tuple[bool, str]:
        """Start the emulator, preferring a quickboot snapshot when present.

        ``no_window=True`` forces a headless start (embedded screen).  Pass
        ``open_window=True`` to always open the emulator's own window - used by
        the default native display mode, which renders the device itself and so
        must never inherit ``-no-window`` from the performance profile.  If both
        are set, ``no_window`` wins.
        """
        if self.is_running():
            return True, "Emulator already running"

        if not self.avd_name:
            return False, "No Android version selected - choose one in Controls > Android version"

        # x86/x86_64 Android images cannot boot without a hardware
        # accelerator.  Fail fast with actionable instructions instead of
        # launching an emulator that exits instantly with a cryptic log.
        if PlatformUtils.IS_WINDOWS:
            accel = PlatformUtils.get_acceleration_info()
            if not accel.get("supported"):
                detail = (accel.get("details") or {}).get("message", "")
                guidance = (
                    "Hardware acceleration is required to boot this Android "
                    "image, but no emulator hypervisor is active on this PC.\n\n"
                    "Fix: press Win+R, type 'optionalfeatures', check "
                    "'Windows Hypervisor Platform' (or 'Hyper-V'), click OK, "
                    "then restart your computer. Alternatively, install the "
                    "Android Emulator Hypervisor Driver (AEHD)."
                )
                if detail:
                    guidance += f"\n\nDetected: {detail}"
                return False, guidance

        self._ensure_adb_server()
        self._serial = None
        force_window = (not scrcpy_available) or open_window

        use_snapshot = bool(settings.quickboot and self.has_snapshot())
        if use_snapshot:
            self._log("Quickboot snapshot found - warm boot "
                      f"(only the first boot is cold)")
        else:
            self._log("No quickboot snapshot - cold boot")
        cmd = self.build_start_command(
            mode, force_window=force_window, no_window=no_window,
            use_snapshot=use_snapshot)
        self._log(f"Starting emulator: {' '.join(cmd)}")
        self._progress(0, "Starting emulator...")
        ok, message = self._start_once(cmd, wait_for_boot, timeout)

        if not ok and use_snapshot:
            # A stale/incompatible snapshot must never leave the app stuck.
            self._log("Quickboot snapshot failed; falling back to a cold boot")
            self._progress(0, "Quickboot failed - cold booting")
            self._stop_process()
            self._delete_snapshot_files()
            cmd = self.build_start_command(
                mode, force_window=force_window, no_window=no_window,
                use_snapshot=False)
            self._log(f"Retrying cold: {' '.join(cmd)}")
            ok, message = self._start_once(cmd, wait_for_boot, timeout)

        if not ok and self._gpu_override_needed(mode, no_window=no_window):
            # The default profile asked for host GPU but the emulator could
            # not boot.  Retry with software rendering before giving up.
            self._log("GPU 'host' failed; retrying with swiftshader_indirect")
            self._progress(0, "Retrying with software GPU")
            self._stop_process()
            cmd = self.build_start_command(
                mode, force_window=force_window, no_window=no_window,
                use_snapshot=False, gpu_override="swiftshader_indirect")
            self._log(f"Retrying with software GPU: {' '.join(cmd)}")
            ok, message = self._start_once(cmd, wait_for_boot, timeout)
            if ok:
                message = f"{message} (software GPU fallback)"

        if ok and wait_for_boot and settings.quickboot and not self.has_snapshot():
            self._progress(95, "Saving quickboot snapshot")
            saved, save_message = self.save_snapshot(self.BOOT_SNAPSHOT)
            self._log(f"Quickboot snapshot: {save_message}")
            if saved:
                self._progress(100, "Emulator ready")
        elif ok and wait_for_boot and settings.quickboot:
            self._log("Quickboot snapshot already present - not rewritten")

        return ok, message

    def _gpu_override_needed(self, mode: PerformanceMode,
                              no_window: bool = False) -> bool:
        """Return True if the default command would use host GPU and fallback
        is desired.  Simple heuristic: if the config's gpu_mode is host then
        a software retry should be offered."""
        from .hardware_profile import HardwareProfiler
        profiler = HardwareProfiler()
        config = profiler.get_emulator_config(mode, low_power=False)
        return config.gpu_mode == "host"

    def _ensure_adb_server(self):
        """Ensure adb server is running."""
        adb = PlatformUtils.get_adb_path()
        if adb.exists():
            PlatformUtils.run_command([str(adb), "start-server"], timeout=10)

    def _detect_serial(self) -> Optional[str]:
        """Find the emulator serial via adb (the app runs one at a time)."""
        adb = PlatformUtils.get_adb_path()
        try:
            result = PlatformUtils.run_command(
                [str(adb), "devices"], timeout=10)
        except Exception:
            return None
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[0].startswith("emulator-"):
                return parts[0]
        return None

    def _adb_args(self, args: List[str]) -> List[str]:
        adb = PlatformUtils.get_adb_path()
        prefix = [str(adb)]
        if self._serial:
            prefix.extend(["-s", self._serial])
        return prefix + list(args)

    def _run_adb(self, args: List[str], timeout: int = 10):
        try:
            return PlatformUtils.run_command(
                self._adb_args(args), timeout=timeout)
        except Exception:
            return None

    def _disable_guest_animations(self) -> None:
        """Turn off Android's window/transition/animator animations."""
        for key in ("window_animation_scale",
                    "transition_animation_scale",
                    "animator_duration_scale"):
            self._run_adb(["shell", "settings", "put", "global", key, "0"],
                          timeout=10)
        self._log("Guest animations disabled")

    def _reset_guest_overrides(self) -> None:
        """Clear any leftover ``wm size`` / ``wm density`` override.

        A ``wm size`` override survives inside the AVD and makes the guest
        render at a *much* larger resolution than the emulator was started
        with.  With software rendering that is enough to starve the guest
        completely (no frames at all, SystemUI ANRs).  The overrides came
        from the removed per-device feature, so they are cleared on every
        boot.
        """
        for args in (["shell", "wm", "size", "reset"],
                     ["shell", "wm", "density", "reset"]):
            try:
                self._run_adb(args, timeout=10)
            except Exception:
                pass
        self._log("Guest wm size/density overrides reset")

    def _wait_for_boot(self, timeout: int) -> Tuple[bool, str]:
        """Wait for emulator boot, reporting real stage progress."""
        start_time = time.time()
        self._log("Waiting for emulator to boot...")
        last_stage = -1

        while time.time() - start_time < timeout:
            if not self.is_running():
                detail = self._emulator_error_detail()
                return False, ("Emulator process died during boot"
                               + (f"\n{detail}" if detail else ""))

            if not self._serial:
                self._serial = self._detect_serial()
            if not self._serial:
                if last_stage != 10:
                    self._progress(10, "Waiting for ADB device")
                    last_stage = 10
                time.sleep(1)
                continue

            result = self._run_adb(
                ["shell", "getprop", "sys.boot_completed"], timeout=10)
            if (result is not None and result.returncode == 0
                    and (result.stdout or "").strip() == "1"):
                self._progress(100, "Emulator ready")
                self._reset_guest_overrides()
                self._disable_guest_animations()
                self._log("Emulator booted successfully")
                if self._boot_callback:
                    self._boot_callback(True)
                return True, "Emulator ready"

            anim = self._run_adb(
                ["shell", "getprop", "init.svc.bootanim"], timeout=10)
            anim_state = (anim.stdout or "").strip() if anim is not None else ""
            if anim_state == "stopped":
                percent, stage = 85, "Starting Android system"
            elif anim_state == "running":
                percent, stage = 55, "Booting Android"
            else:
                percent, stage = 25, "Booting Android"
            if percent != last_stage:
                self._progress(percent, stage)
                last_stage = percent

            time.sleep(1)

        error_msg = f"Boot timeout after {timeout} seconds"
        detail = self._emulator_error_detail()
        if detail:
            error_msg += f"\n{detail}"
        self._log(error_msg)
        if self._boot_callback:
            self._boot_callback(False)
        return False, error_msg

    def stop(self, force: bool = False) -> Tuple[bool, str]:
        """Stop the emulator."""
        if not self.is_running():
            return True, "Emulator not running"

        try:
            if force:
                PlatformUtils.kill_process_tree(self._process.pid)
                self._log("Emulator force stopped")
            else:
                self._run_adb(["emu", "kill"], timeout=10)
                self._log("Emulator stop requested")

                for _ in range(20):
                    if not self.is_running():
                        break
                    time.sleep(0.5)

                if self.is_running():
                    PlatformUtils.kill_process_tree(self._process.pid)
                    self._log("Emulator force killed after timeout")

            self._process = None
            self._serial = None
            return True, "Emulator stopped"

        except Exception as e:
            return False, f"Failed to stop emulator: {e}"

    def restart(self, mode: PerformanceMode, no_window: bool = False,
                open_window: bool = False) -> Tuple[bool, str]:
        """Restart the emulator (forwards the windowing of ``start``)."""
        self.stop()
        time.sleep(1)
        return self.start(mode, no_window=no_window, open_window=open_window)

    # -- snapshots -------------------------------------------------------

    def save_snapshot(self, name: str = BOOT_SNAPSHOT) -> Tuple[bool, str]:
        """Save emulator state to a snapshot."""
        if not self.is_running():
            return False, "Emulator not running"

        result = self._run_adb(
            ["emu", "avd", "snapshot", "save", name], timeout=60)
        if result is not None and result.returncode == 0:
            self._log(f"Snapshot '{name}' saved")
            return True, f"Snapshot '{name}' saved"
        detail = (result.stderr if result is not None else "") or "adb failed"
        return False, f"Snapshot save failed: {detail}"

    def load_snapshot(self, name: str = BOOT_SNAPSHOT) -> Tuple[bool, str]:
        """Load emulator state from a snapshot."""
        if not self.is_running():
            return False, "Emulator not running"

        result = self._run_adb(
            ["emu", "avd", "snapshot", "load", name], timeout=60)
        if result is not None and result.returncode == 0:
            self._log(f"Snapshot '{name}' loaded")
            return True, f"Snapshot '{name}' loaded"
        detail = (result.stderr if result is not None else "") or "adb failed"
        return False, f"Snapshot load failed: {detail}"

    def delete_snapshot(self, name: str) -> Tuple[bool, str]:
        """Delete a snapshot via the emulator console or on disk."""
        if self.is_running():
            result = self._run_adb(
                ["emu", "avd", "snapshot", "delete", name], timeout=10)
            if result is not None and result.returncode == 0:
                return True, f"Snapshot '{name}' deleted"
        self._delete_snapshot_files(name)
        if not self.has_snapshot(name):
            return True, f"Snapshot '{name}' deleted"
        return False, f"Snapshot '{name}' could not be deleted"

    def list_snapshots(self) -> List[str]:
        """List available snapshots."""
        result = self._run_adb(["emu", "avd", "snapshot", "list"], timeout=10)
        if result is not None and result.returncode == 0:
            lines = (result.stdout or "").strip().split("\n")
            snapshots = []
            for line in lines:
                line = line.strip()
                if line and not line.startswith("snapshot"):
                    snapshots.append(line)
            return snapshots
        return []

    def get_emulator_info(self) -> dict:
        """Get emulator info for resource monitoring."""
        info = {
            "running": self.is_running(),
            "pid": self.get_pid(),
            "avd_name": self.avd_name,
        }

        if self.is_running():
            try:
                import psutil
                proc = psutil.Process(self._process.pid)
                info["cpu_percent"] = proc.cpu_percent(interval=0.1)
                info["memory_mb"] = proc.memory_info().rss // (1024 * 1024)
                info["memory_percent"] = proc.memory_percent()
            except Exception:
                pass

        return info


class EmulatorMonitor:
    """Background monitor for emulator health and resource usage."""

    def __init__(self, emulator_manager, update_callback, interval: float = 2.0):
        self._emulator = emulator_manager
        self._callback = update_callback
        self._interval = interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start monitoring."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop monitoring."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def _monitor_loop(self):
        while self._running:
            try:
                info = self._emulator.get_emulator_info()
                # Add system memory info
                import psutil
                mem = psutil.virtual_memory()
                info["system_memory"] = {
                    "total_mb": mem.total // (1024 * 1024),
                    "available_mb": mem.available // (1024 * 1024),
                    "percent": mem.percent,
                }
                if self._callback:
                    self._callback(info)
            except Exception:
                pass
            time.sleep(self._interval)