"""Emulator management for ApkPreview - start, stop, monitor, snapshot."""

import os
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional, Callable, List, Tuple
from .platform_utils import PlatformUtils
from .hardware_profile import HardwareProfiler, PerformanceMode, EmulatorConfig
from .settings import settings


class EmulatorManager:
    """Manages the Android emulator lifecycle."""
    
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_monitor = threading.Event()
        self._output_callback: Optional[Callable[[str], None]] = None
        self._boot_callback: Optional[Callable[[bool], None]] = None

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
    
    def build_start_command(self, mode: PerformanceMode, force_window: bool = False,
                            no_window: bool = False) -> List[str]:
        """Build the emulator start command based on mode.

        ``no_window`` forces ``-no-window`` even when ``force_window`` or the
        config would otherwise request a window.  That is used by the embedded
        screen (Stage 2a): the app renders the device itself, so the emulator
        must not open its own window.
        """
        emulator_path = PlatformUtils.get_emulator_path()
        profiler = HardwareProfiler()
        config = profiler.get_emulator_config(mode)
        
        cmd = [str(emulator_path)]
        cmd.extend(["-avd", self.avd_name])
        cmd.extend(["-memory", str(config.memory_mb)])
        cmd.extend(["-cores", str(config.cores)])
        cmd.extend(["-gpu", config.gpu_mode])
        
        if config.no_audio:
            cmd.append("-no-audio")
        if config.no_boot_anim:
            cmd.append("-no-boot-anim")
        if config.no_snapshot_save:
            cmd.append("-no-snapshot-save")
        
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
        
        # Useful flags for all modes
        cmd.extend([
            "-no-snapshot-load",  # Don't auto-load snapshot (we handle manually)
            "-no-snapshot-save",  # Don't auto-save on exit
            "-camera-back", "virtualscene",  # Virtual camera for testing
        ])
        
        # Filter empty strings
        cmd = [c for c in cmd if c]
        
        return cmd
    
    def start(self, mode: PerformanceMode, wait_for_boot: bool = True, 
              timeout: int = 120, scrcpy_available: bool = True,
              no_window: bool = False, open_window: bool = False) -> Tuple[bool, str]:
        """Start the emulator.

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

        # Ensure adb server is running
        self._ensure_adb_server()
        
        # Force a window when scrcpy is missing (old behaviour) or when the
        # caller explicitly wants the native emulator window.
        force_window = (not scrcpy_available) or open_window
        cmd = self.build_start_command(mode, force_window=force_window,
                                       no_window=no_window)
        self._log(f"Starting emulator: {' '.join(cmd)}")
        
        try:
            self._process = PlatformUtils.start_detached_process(cmd)
            
            if wait_for_boot:
                return self._wait_for_boot(timeout)
            
            return True, "Emulator started"
            
        except Exception as e:
            return False, f"Failed to start emulator: {e}"
    
    def _ensure_adb_server(self):
        """Ensure adb server is running."""
        adb = PlatformUtils.get_adb_path()
        if adb.exists():
            PlatformUtils.run_command([str(adb), "start-server"], timeout=10)
    
    def _wait_for_boot(self, timeout: int) -> Tuple[bool, str]:
        """Wait for emulator to finish booting."""
        adb = PlatformUtils.get_adb_path()
        start_time = time.time()
        
        self._log("Waiting for emulator to boot...")
        
        while time.time() - start_time < timeout:
            if not self.is_running():
                return False, "Emulator process died during boot"
            
            try:
                result = PlatformUtils.run_command(
                    [str(adb), "shell", "getprop", "sys.boot_completed"],
                    timeout=10
                )
                if result.returncode == 0 and result.stdout.strip() == "1":
                    self._log("Emulator booted successfully")
                    if self._boot_callback:
                        self._boot_callback(True)
                    return True, "Emulator ready"
            except Exception:
                pass
            
            time.sleep(2)
        
        error_msg = f"Boot timeout after {timeout} seconds"
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
                # Force kill
                PlatformUtils.kill_process_tree(self._process.pid)
                self._log("Emulator force stopped")
            else:
                # Graceful shutdown via adb
                adb = PlatformUtils.get_adb_path()
                PlatformUtils.run_command([str(adb), "emu", "kill"], timeout=10)
                self._log("Emulator stop requested")
                
                # Wait a bit for graceful shutdown
                for _ in range(20):
                    if not self.is_running():
                        break
                    time.sleep(0.5)
                
                # Force kill if still running
                if self.is_running():
                    PlatformUtils.kill_process_tree(self._process.pid)
                    self._log("Emulator force killed after timeout")
            
            self._process = None
            return True, "Emulator stopped"
            
        except Exception as e:
            return False, f"Failed to stop emulator: {e}"
    
    def restart(self, mode: PerformanceMode, no_window: bool = False,
                open_window: bool = False) -> Tuple[bool, str]:
        """Restart the emulator (forwards the windowing of ``start``)."""
        self.stop()
        time.sleep(1)
        return self.start(mode, no_window=no_window, open_window=open_window)
    
    def save_snapshot(self, name: str = "auto") -> Tuple[bool, str]:
        """Save emulator state to a snapshot."""
        if not self.is_running():
            return False, "Emulator not running"
        
        try:
            adb = PlatformUtils.get_adb_path()
            result = PlatformUtils.run_command(
                [str(adb), "emu", "avd", "snapshot", "save", name],
                timeout=30
            )
            if result.returncode == 0:
                self._log(f"Snapshot '{name}' saved")
                return True, f"Snapshot '{name}' saved"
            else:
                return False, f"Snapshot save failed: {result.stderr}"
        except Exception as e:
            return False, f"Snapshot error: {e}"
    
    def load_snapshot(self, name: str = "auto") -> Tuple[bool, str]:
        """Load emulator state from a snapshot."""
        if not self.is_running():
            return False, "Emulator not running"
        
        try:
            adb = PlatformUtils.get_adb_path()
            result = PlatformUtils.run_command(
                [str(adb), "emu", "avd", "snapshot", "load", name],
                timeout=30
            )
            if result.returncode == 0:
                self._log(f"Snapshot '{name}' loaded")
                return True, f"Snapshot '{name}' loaded"
            else:
                return False, f"Snapshot load failed: {result.stderr}"
        except Exception as e:
            return False, f"Snapshot error: {e}"
    
    def delete_snapshot(self, name: str) -> Tuple[bool, str]:
        """Delete a snapshot."""
        try:
            adb = PlatformUtils.get_adb_path()
            result = PlatformUtils.run_command(
                [str(adb), "emu", "avd", "snapshot", "delete", name],
                timeout=10
            )
            if result.returncode == 0:
                return True, f"Snapshot '{name}' deleted"
            return False, f"Snapshot delete failed: {result.stderr}"
        except Exception as e:
            return False, f"Snapshot error: {e}"
    
    def list_snapshots(self) -> List[str]:
        """List available snapshots."""
        try:
            adb = PlatformUtils.get_adb_path()
            result = PlatformUtils.run_command(
                [str(adb), "emu", "avd", "snapshot", "list"],
                timeout=10
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                snapshots = []
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("snapshot"):
                        snapshots.append(line)
                return snapshots
        except Exception:
            pass
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
    
    def __init__(self, emulator_manager: EmulatorManager, 
                 update_callback: Callable[[dict], None],
                 interval: float = 2.0):
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