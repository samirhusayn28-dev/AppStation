"""scrcpy wrapper for ApkPreview - screen mirroring and control."""

import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable, List, Tuple
from .platform_utils import PlatformUtils
from .settings import settings


class ScrcpyManager:
    """Manages scrcpy process for screen mirroring."""
    
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._serial: str = "emulator-5554"
        self._window_title: str = "ApkPreview Screen"
        self._bitrate: str = settings.scrcpy_bitrate
        self._max_size: int = settings.scrcpy_max_size
        self._max_fps: int = settings.scrcpy_max_fps
        self._output_callback: Optional[Callable[[str], None]] = None
    
    def set_serial(self, serial: str):
        """Set target device serial."""
        self._serial = serial
    
    def set_output_callback(self, callback: Callable[[str], None]):
        """Set callback for scrcpy output."""
        self._output_callback = callback
    
    def _log(self, message: str):
        if self._output_callback:
            self._output_callback(message)
    
    def is_running(self) -> bool:
        """Check if scrcpy is running."""
        if self._process is None:
            return False
        return self._process.poll() is None
    
    def is_available(self) -> bool:
        """Check if scrcpy binary is available."""
        # Check our local tools directory
        scrcpy_path = PlatformUtils.get_scrcpy_path()
        if scrcpy_path.exists():
            return True
        
        # On macOS, check system PATH and Homebrew locations
        if PlatformUtils.IS_MACOS:
            import shutil
            if shutil.which("scrcpy"):
                return True
            for prefix in ("/opt/homebrew", "/usr/local"):
                bin_path = Path(prefix) / "bin" / "scrcpy"
                if bin_path.exists():
                    return True
        
        return False
    
    def get_pid(self) -> Optional[int]:
        return self._process.pid if self._process else None
    
    def build_command(self, low_spec: bool = False) -> List[str]:
        """Build scrcpy command line."""
        scrcpy_path = PlatformUtils.get_scrcpy_path()
        
        if not scrcpy_path.exists():
            return []
        
        cmd = [str(scrcpy_path)]
        cmd.extend(["-s", self._serial])
        cmd.extend(["--window-title", self._window_title])
        
        # Performance settings
        cmd.extend(["--max-size", str(self._max_size)])
        cmd.extend(["--max-fps", str(self._max_fps)])
        cmd.extend(["--video-bit-rate", self._bitrate])
        
        if low_spec:
            # Extra low-spec optimizations
            cmd.extend(["--video-codec", "h264"])
            cmd.extend(["--audio-codec", "none"])
            cmd.extend(["--no-audio"])
            cmd.extend(["--disable-screensaver"])
            cmd.extend(["--stay-awake"])
            cmd.extend(["--lock-video-orientation", "0"])
        else:
            cmd.extend(["--no-audio"])
            cmd.extend(["--stay-awake"])
        
        # Window positioning - place next to main window
        # (actual positioning handled by window manager)
        
        return cmd
    
    def start(self, low_spec: bool = False) -> Tuple[bool, str]:
        """Start scrcpy."""
        if self.is_running():
            return True, "scrcpy already running"
        
        cmd = self.build_command(low_spec)
        if not cmd:
            return False, "scrcpy not found"
        
        self._log(f"Starting scrcpy: {' '.join(cmd)}")
        
        try:
            self._process = PlatformUtils.start_detached_process(cmd)
            
            # Give it a moment to start
            time.sleep(1)
            
            if self.is_running():
                self._log("scrcpy started successfully")
                return True, "scrcpy started"
            else:
                return False, "scrcpy failed to start"
                
        except Exception as e:
            return False, f"Failed to start scrcpy: {e}"
    
    def stop(self) -> Tuple[bool, str]:
        """Stop scrcpy."""
        if not self.is_running():
            return True, "scrcpy not running"
        
        try:
            if PlatformUtils.IS_WINDOWS:
                self._process.terminate()
            else:
                self._process.send_signal(subprocess.signal.SIGTERM)
            
            # Wait for graceful shutdown
            for _ in range(10):
                if not self.is_running():
                    break
                time.sleep(0.2)
            
            # Force kill if needed
            if self.is_running():
                if PlatformUtils.IS_WINDOWS:
                    self._process.kill()
                else:
                    self._process.send_signal(subprocess.signal.SIGKILL)
            
            self._process = None
            self._log("scrcpy stopped")
            return True, "scrcpy stopped"
            
        except Exception as e:
            return False, f"Failed to stop scrcpy: {e}"
    
    def restart(self, low_spec: bool = False) -> Tuple[bool, str]:
        """Restart scrcpy."""
        self.stop()
        time.sleep(0.5)
        return self.start(low_spec)
    
    def set_bitrate(self, bitrate: str):
        """Set video bitrate (e.g., '8M', '4M', '2M')."""
        self._bitrate = bitrate
        settings.scrcpy_bitrate = bitrate
    
    def set_max_size(self, size: int):
        """Set max screen size (width or height)."""
        self._max_size = max(0, size)
        settings.scrcpy_max_size = self._max_size
    
    def set_max_fps(self, fps: int):
        """Set max FPS."""
        self._max_fps = max(1, min(60, fps))
        settings.scrcpy_max_fps = self._max_fps
    
    def set_window_title(self, title: str):
        """Set window title."""
        self._window_title = title


class ScrcpyEmbedded:
    """Attempt to embed scrcpy window in Qt (experimental)."""
    
    def __init__(self):
        self._scrcpy = ScrcpyManager()
        self._window_id: Optional[int] = None
    
    def start_embedded(self, parent_widget, low_spec: bool = False) -> bool:
        """Try to start scrcpy embedded in a Qt widget."""
        # This is platform-specific and experimental
        # On Windows, we can try --window-id or SetParent
        # On macOS, it's more difficult
        # For now, just start normally
        return self._scrcpy.start(low_spec)[0]
    
    def stop(self):
        self._scrcpy.stop()


def get_scrcpy_version() -> Optional[str]:
    """Get scrcpy version."""
    scrcpy_path = PlatformUtils.get_scrcpy_path()
    if not scrcpy_path.exists():
        return None
    
    try:
        result = PlatformUtils.run_command([str(scrcpy_path), "--version"], timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def check_scrcpy_installed() -> bool:
    """Check if scrcpy is installed."""
    return PlatformUtils.get_scrcpy_path().exists()