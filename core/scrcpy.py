"""scrcpy wrapper for AppStation - screen mirroring and control."""

import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Callable, List, Tuple
from .platform_utils import PlatformUtils
from .settings import settings


class ScrcpyController:
    """Send input events over the scrcpy control socket.

    The scrcpy server exposes a control channel next to the video socket.
    Using it instead of ``adb shell input`` avoids spawning an adb process
    (and a full device round-trip) for every tap/swipe/key, which is what
    made the preview feel laggy.  All messages are big-endian and follow the
    scrcpy v5.0.1 control protocol.
    """

    # Control message types (ControlMessage.java)
    TYPE_INJECT_KEYCODE = 0
    TYPE_INJECT_TOUCH_EVENT = 2
    TYPE_INJECT_SCROLL_EVENT = 3

    # MotionEvent actions
    ACTION_DOWN = 0
    ACTION_UP = 1
    ACTION_MOVE = 2

    # A generic touch pointer (SC_POINTER_ID_GENERIC_FINGER)
    POINTER_ID = -2

    def __init__(self, sock):
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = False
        # The scrcpy server only accepts touch events whose coordinates are
        # expressed in the *encoded video* size (e.g. 324x720 when max_size is
        # 720), not the device's native screen size (e.g. 720x1600).  The
        # decoder reports the real video size here; callers keep passing
        # coordinates in whatever frame size they are showing and get scaled.
        self._video_size: Optional[Tuple[int, int]] = None

    @property
    def is_closed(self) -> bool:
        return self._closed

    def set_video_size(self, width: int, height: int):
        """Record the encoded video size used by the scrcpy server."""
        try:
            width = int(width)
            height = int(height)
        except (TypeError, ValueError):
            return
        if width > 0 and height > 0:
            self._video_size = (width, height)

    def _to_video_space(self, x: float, y: float,
                        screen_w: int, screen_h: int):
        """Scale a point from the displayed frame size to the video size."""
        screen_w = max(1, int(screen_w or 1))
        screen_h = max(1, int(screen_h or 1))
        video_w, video_h = self._video_size or (screen_w, screen_h)
        if (video_w, video_h) != (screen_w, screen_h):
            x = x * video_w / float(screen_w)
            y = y * video_h / float(screen_h)
        return x, y, video_w, video_h

    def _send(self, payload: bytes) -> bool:
        if self._closed or self._sock is None:
            return False
        try:
            with self._lock:
                self._sock.sendall(payload)
            return True
        except Exception:
            # The stream died (or the server closed the control channel):
            # mark the controller unusable so the UI falls back to adb input.
            self._closed = True
            return False

    def _touch(self, action: int, x: float, y: float,
               screen_w: int, screen_h: int) -> bool:
        x, y, screen_w, screen_h = self._to_video_space(
            x, y, screen_w, screen_h)
        x = max(0, min(int(round(x)), max(0, int(screen_w) - 1)))
        y = max(0, min(int(round(y)), max(0, int(screen_h) - 1)))
        sw = max(1, min(int(screen_w), 0xffff))
        sh = max(1, min(int(screen_h), 0xffff))
        # 1.0 pressure -> 0xffff (the protocol's special "full" value)
        payload = struct.pack(
            ">BBqiiHHHii",
            self.TYPE_INJECT_TOUCH_EVENT, action & 0xff, self.POINTER_ID,
            x, y, sw, sh, 0xffff, 0, 0)
        return self._send(payload)

    def touch_down(self, x: float, y: float,
                   screen_w: int, screen_h: int) -> bool:
        """Begin a touch gesture at (x, y)."""
        return self._touch(self.ACTION_DOWN, x, y, screen_w, screen_h)

    def touch_move(self, x: float, y: float,
                   screen_w: int, screen_h: int) -> bool:
        """Continue a touch gesture at (x, y)."""
        return self._touch(self.ACTION_MOVE, x, y, screen_w, screen_h)

    def touch_up(self, x: float, y: float,
                 screen_w: int, screen_h: int) -> bool:
        """End a touch gesture at (x, y)."""
        return self._touch(self.ACTION_UP, x, y, screen_w, screen_h)

    def tap(self, x: float, y: float, screen_w: int, screen_h: int,
            lift_ms: int = 30) -> Tuple[bool, str]:
        """Inject a tap (ACTION_DOWN immediately followed by ACTION_UP)."""
        if not self._touch(self.ACTION_DOWN, x, y, screen_w, screen_h):
            return False, "control socket unavailable"
        if lift_ms > 0:
            time.sleep(lift_ms / 1000.0)
        if not self._touch(self.ACTION_UP, x, y, screen_w, screen_h):
            return False, "control socket unavailable"
        return True, f"Tap at {int(x)},{int(y)} sent"

    def swipe(self, x1: float, y1: float, x2: float, y2: float,
              screen_w: int, screen_h: int,
              duration_ms: int = 220) -> Tuple[bool, str]:
        """Inject a smooth swipe (DOWN, interpolated MOVEs, UP)."""
        if not self._touch(self.ACTION_DOWN, x1, y1, screen_w, screen_h):
            return False, "control socket unavailable"
        steps = max(2, min(24, int(duration_ms / 12)))
        delay = max(0.004, duration_ms / 1000.0 / steps)
        for i in range(1, steps):
            t = i / float(steps)
            x = x1 + (x2 - x1) * t
            y = y1 + (y2 - y1) * t
            self._touch(self.ACTION_MOVE, x, y, screen_w, screen_h)
            time.sleep(delay)
        self._touch(self.ACTION_MOVE, x2, y2, screen_w, screen_h)
        if not self._touch(self.ACTION_UP, x2, y2, screen_w, screen_h):
            return False, "control socket unavailable"
        return True, (f"Swipe ({int(x1)},{int(y1)})->"
                      f"({int(x2)},{int(y2)}) sent")

    def scroll(self, x: float, y: float, hscroll: float, vscroll: float,
               screen_w: int, screen_h: int) -> Tuple[bool, str]:
        """Inject a scroll event (mouse-wheel style)."""
        x, y, screen_w, screen_h = self._to_video_space(
            x, y, screen_w, screen_h)
        sw = max(1, min(int(screen_w), 0xffff))
        sh = max(1, min(int(screen_h), 0xffff))
        hx = max(-1.0, min(1.0, float(hscroll)))
        vy = max(-1.0, min(1.0, float(vscroll)))
        payload = struct.pack(
            ">BiiHHhhi",
            self.TYPE_INJECT_SCROLL_EVENT,
            int(round(x)), int(round(y)), sw, sh,
            int(round(hx * 32767)), int(round(vy * 32767)), 0)
        if not self._send(payload):
            return False, "control socket unavailable"
        return True, "Scroll sent"

    def key(self, keycode: int, repeat: int = 0, meta_state: int = 0,
            hold_ms: int = 20) -> Tuple[bool, str]:
        """Inject a key press (ACTION_DOWN followed by ACTION_UP)."""
        keycode = int(keycode)
        down = struct.pack(">BBiii", self.TYPE_INJECT_KEYCODE, 0,
                           keycode, int(repeat), int(meta_state))
        up = struct.pack(">BBiii", self.TYPE_INJECT_KEYCODE, 1,
                         keycode, int(repeat), int(meta_state))
        if not self._send(down):
            return False, "control socket unavailable"
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        if not self._send(up):
            return False, "control socket unavailable"
        return True, "Key event sent"

    def close(self):
        self._closed = True
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class ScrcpyManager:
    """Manages scrcpy process for screen mirroring."""
    
    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._serial: str = "emulator-5554"
        self._window_title: str = "AppStation Screen"
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