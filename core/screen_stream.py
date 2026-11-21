"""Embedded screen streaming for AppStation (Stage 2a - view only).

Streams the device screen into the app using the scrcpy *server* driven
directly over adb.  The macOS scrcpy client is never downloaded, installed or
required - only the single ``scrcpy-server`` jar from the official scrcpy
GitHub release is fetched into ``~/.apkpreview/tools``.

Pipeline
--------
1. Download ``scrcpy-server-vX.Y`` into ``~/.apkpreview/tools`` (once).
2. Kill any stale scrcpy app_process and drop all adb forward rules.
3. ``adb push`` the jar to ``/data/local/tmp/scrcpy-server.jar`` on *every*
   start, then verify with ``adb shell ls -l`` that it exists and its size
   matches the local file (the remote jar is never deleted by AppStation).
4. ``adb forward tcp:<port> localabstract:scrcpy``.
5. Start the server with ``app_process`` using ``raw_stream=true`` so the video
   socket carries a raw H.264 Annex-B stream (no scrcpy framing at all).
6. Connect to the forwarded port and decode with PyAV, converting each decoded
   frame to a ``QImage``.  We read the decoded plane bytes directly, so neither
   numpy nor Pillow is required.

If any step fails, a slow ``adb exec-out screencap`` fallback (about 3 fps) is
used and the caller is told to show a visible note.

Everything runs on a background thread.  The callbacks (``on_note``,
``on_error``, ``on_state``, ``on_status``) are invoked from that thread, so the
UI layer is responsible for marshalling them onto the Qt GUI thread.

Frames are not pushed through a callback: the decoder thread keeps only the
latest converted frame in a single slot and the UI pulls it on a ~30 Hz QTimer
(the stream can never build a frame backlog).  Every 5 seconds a "[screen]"
line reports received / decoded / painted / dropped counts and the average
decode / convert / paint milliseconds.
"""

import struct
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional, Tuple

from PyQt6.QtGui import QImage

from .adb import ADB
from .platform_utils import PlatformUtils
from .scrcpy import ScrcpyController
from .settings import settings

# PyAV is imported lazily: the decoder is only needed once a stream actually
# starts, and importing the binary module costs ~0.2 s at app startup.
_av = None
_av_import_failed = False


def _get_av():
    """Import PyAV on first use; returns None when it is unavailable."""
    global _av, _av_import_failed
    if _av is not None or _av_import_failed:
        return _av
    try:
        import av  # noqa: PLC0415 - deliberately lazy
        _av = av
    except Exception:  # pragma: no cover - exercised only on broken installs
        _av_import_failed = True
        _av = None
    return _av


# ---------------------------------------------------------------------------
# scrcpy-server source (official Genymobile release)
# ---------------------------------------------------------------------------
SCRCPY_VERSION = "5.0.1"
SCRCPY_SERVER_ASSET = f"scrcpy-server-v{SCRCPY_VERSION}"
SCRCPY_SERVER_URL = (
    "https://github.com/Genymobile/scrcpy/releases/download/"
    f"v{SCRCPY_VERSION}/{SCRCPY_SERVER_ASSET}"
)
REMOTE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"
ABSTRACT_SOCKET_NAME = "scrcpy"

FALLBACK_INTERVAL = 1.0 / 3.0  # ~3 fps for the screencap fallback
# How long the guest may stay silent before the video stream is considered
# stalled and the screencap fallback takes over.
STALL_TIMEOUT = 12.0
MAX_STREAM_FPS = 30            # hard cap for the embedded view


def server_url() -> str:
    """Return the URL of the official scrcpy-server release asset."""
    return SCRCPY_SERVER_URL


def parse_bitrate(value) -> int:
    """Convert a Quality setting ("8M", "8000000", 8) into bits per second."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    try:
        if text.endswith("m"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("k"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text))
    except Exception:
        return 8_000_000


class ScrcpyServer:
    """Locates / downloads the official scrcpy-server jar (the server only)."""

    @staticmethod
    def url() -> str:
        return SCRCPY_SERVER_URL

    @staticmethod
    def local_path(tools_dir: Optional[Path] = None) -> Path:
        base = Path(tools_dir) if tools_dir else PlatformUtils.get_tools_dir()
        return base / SCRCPY_SERVER_ASSET

    @classmethod
    def is_downloaded(cls, tools_dir: Optional[Path] = None) -> bool:
        path = cls.local_path(tools_dir)
        try:
            return path.exists() and path.stat().st_size > 0
        except OSError:
            return False

    @classmethod
    def ensure_downloaded(cls, tools_dir: Optional[Path] = None,
                          progress_cb: Optional[Callable[[int], None]] = None,
                          timeout: int = 120) -> Tuple[bool, str]:
        """Download the server jar if missing.  Returns (ok, path-or-error)."""
        path = cls.local_path(tools_dir)
        if cls.is_downloaded(tools_dir):
            return True, str(path)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".part")
            import urllib.request

            request = urllib.request.Request(
                SCRCPY_SERVER_URL, headers={"User-Agent": "AppStation"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                with open(tmp, "wb") as handle:
                    while True:
                        chunk = response.read(64 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
                        if progress_cb and total:
                            try:
                                progress_cb(int(received * 100 / total))
                            except Exception:
                                pass
            tmp.replace(path)
            return True, str(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the status row
            return False, f"scrcpy-server download failed: {exc}"

    @staticmethod
    def is_available() -> bool:
        """True when PyAV is importable (the decoder we rely on)."""
        return _get_av() is not None


def _video_frame_to_qimage(frame) -> Optional[QImage]:
    """Convert an av.VideoFrame to a QImage without numpy/Pillow.

    The YUV -> RGB conversion happens inside PyAV (``frame.reformat``); the
    QImage is then built straight from that rgb24 plane buffer and copied once
    so it owns its pixels.  No per-pixel Python work, no per-plane ``bytes()``
    copy.
    """
    try:
        rgb = frame.reformat(format="rgb24")
    except Exception:
        rgb = frame
    try:
        plane = rgb.planes[0]
        image = QImage(plane, rgb.width, rgb.height, plane.line_size,
                       QImage.Format.Format_RGB888).copy()
        if image.isNull():
            return None
        return image
    except Exception:
        # Fallback for builds where QImage cannot wrap the PyAV buffer.
        try:
            plane = rgb.planes[0]
            data = bytes(plane)
            image = QImage(data, rgb.width, rgb.height, plane.line_size,
                           QImage.Format.Format_RGB888).copy()
            return None if image.isNull() else image
        except Exception:
            return None


class _SocketFile:
    """Read-only file-like wrapper so PyAV can demux straight off the socket."""

    def __init__(self, sock):
        self._sock = sock

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = 64 * 1024
        try:
            return self._sock.recv(size)
        except OSError:
            return b""

    def seekable(self) -> bool:
        return False


def _raw_screencap_to_qimage(data: bytes) -> Optional[QImage]:
    """Convert a raw ``adb exec-out screencap`` buffer into a QImage.

    The raw format is a 12-byte header (width, height, format as little
    endian uint32) followed by the raw pixels.  RGBA_8888 needs a copy
    because the buffer is not aligned the way QImage expects.
    """
    try:
        if len(data) < 12:
            return None
        width, height, fmt = struct.unpack_from("<III", data, 0)
        if width <= 0 or height <= 0 or width > 8192 or height > 8192:
            return None
        pixels = data[12:]
        if fmt == 1:  # RGBA_8888
            expected = width * height * 4
            if len(pixels) < expected:
                return None
            image = QImage(pixels, width, height, width * 4,
                           QImage.Format.Format_RGBA8888)
            return image.copy()
        if fmt == 2:  # RGBX_8888
            expected = width * height * 4
            if len(pixels) < expected:
                return None
            image = QImage(pixels, width, height, width * 4,
                           QImage.Format.Format_RGBX8888)
            return image.copy()
    except Exception:
        return None
    return None


def _free_tcp_port() -> int:
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _one_line(text: str) -> str:
    """Collapse multi-line command output so it fits the status row."""
    return " ".join(str(text).split())


class ScreenStream:
    """Owns the scrcpy-server connection + PyAV decoder (view only)."""

    def __init__(self, adb: Optional[ADB] = None,
                 tools_dir: Optional[Path] = None):
        self._adb = adb or ADB()
        self._tools_dir = tools_dir
        self._serial = self._adb.serial

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._sock = None
        self._control_sock = None
        self._controller: Optional["ScrcpyController"] = None
        self._server_proc = None
        self._log_thread: Optional[threading.Thread] = None
        self._forward_port: Optional[int] = None
        # Diagnostics: last scrcpy-server stdout/stderr lines + the last
        # connection error, so a final connect failure can explain itself.
        self._server_log: deque = deque(maxlen=200)
        self._last_connect_error: Optional[BaseException] = None
        self._stalled = False
        self._hwaccel = None
        self._fallback_announced = False
        self._fallback_stop = threading.Event()
        # Watchdog state: the time of the last frame the guest presented.
        self._last_frame_at = time.monotonic()
        self._watchdog_stop = threading.Event()

        # Latest decoded frame: a single slot the UI pulls from (~30 Hz).  A new
        # frame overwrites whatever was not consumed yet (older frames drop).
        self._frame_lock = threading.Lock()
        self._latest_image: Optional[QImage] = None
        self._latest_seq = 0

        # Rolling performance counters (reset every 5 s by _stats_loop).
        self._stats_lock = threading.Lock()
        self._stats = {
            "received": 0, "decoded": 0, "painted": 0, "dropped": 0,
            # ``wait_ms`` is the time spent *waiting for bytes* on the
            # socket; ``decode_ms`` is the real CPU decode time.  They used
            # to be lumped together, which made a starved stream look like a
            # slow decoder.
            "decode_ms": 0.0, "wait_ms": 0.0, "convert_ms": 0.0,
            "paint_ms": 0.0,
        }
        self._stats_stop = threading.Event()
        self._stats_thread: Optional[threading.Thread] = None

        self.on_note: Optional[Callable[[str], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_state: Optional[Callable[[str], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None

    # -- configuration ----------------------------------------------------
    def set_serial(self, serial: str):
        self._serial = serial or self._serial

    def set_callbacks(self, on_note=None, on_error=None,
                      on_state=None, on_status=None):
        if on_note is not None:
            self.on_note = on_note
        if on_error is not None:
            self.on_error = on_error
        if on_state is not None:
            self.on_state = on_state
        if on_status is not None:
            self.on_status = on_status

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def controller(self) -> Optional[ScrcpyController]:
        """Return the low-latency input controller, or None in fallback mode."""
        controller = self._controller
        if controller is None or controller.is_closed:
            return None
        return controller

    # -- lifecycle --------------------------------------------------------
    def start(self, serial: Optional[str] = None, max_size: int = 960,
              max_fps: int = MAX_STREAM_FPS, bitrate=None):
        """Start streaming on a background thread (non-blocking)."""
        if self.is_running():
            return
        self.set_serial(serial or self._serial)
        fps = max(1, min(int(max_fps or MAX_STREAM_FPS), MAX_STREAM_FPS))
        bits = parse_bitrate(bitrate if bitrate is not None
                             else settings.scrcpy_bitrate)
        self._stop.clear()
        with self._frame_lock:
            self._latest_image = None
        self._thread = threading.Thread(
            target=self._run,
            args=(self._serial, int(max_size), fps, bits),
            name="ScreenStream", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        """Signal the thread to stop and wait briefly for it to finish."""
        self._stop.set()
        self._close_socket()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
        self._thread = None

    # -- frame slot (the UI pulls from here) ------------------------------
    def get_latest_frame(self) -> Tuple[Optional[QImage], int]:
        """Return the latest decoded frame plus its sequence number.

        The slot is cleared on read: a frame can only be painted once, and a
        newer frame that arrives before the UI reads the previous one is
        counted as dropped (frames are never queued).
        """
        with self._frame_lock:
            image = self._latest_image
            seq = self._latest_seq
            self._latest_image = None
            return image, seq

    def _publish_frame(self, image: QImage, from_fallback: bool = False):
        """Store the latest decoded frame, dropping any unread previous one."""
        if image is None:
            return
        self._last_frame_at = time.monotonic()
        if not from_fallback:
            # The fast path is alive: the screencap fallback is no longer
            # needed and must not keep stealing CPU/adb bandwidth.
            self._fallback_stop.set()
        with self._frame_lock:
            if self._latest_image is not None:
                with self._stats_lock:
                    self._stats["dropped"] += 1
            self._latest_image = image
            self._latest_seq += 1

    def note_painted(self, elapsed_ms: float):
        """Called by the UI after painting a frame (feeds the 5 s stats line)."""
        with self._stats_lock:
            self._stats["painted"] += 1
            self._stats["paint_ms"] += float(elapsed_ms)

    # -- stats (printed every 5 s while streaming) ------------------------
    def _start_stats(self):
        self._stats_stop.clear()
        with self._stats_lock:
            for key, value in self._stats.items():
                self._stats[key] = 0 if isinstance(value, int) else 0.0
        self._stats_thread = threading.Thread(
            target=self._stats_loop, name="ScreenStats", daemon=True)
        self._stats_thread.start()

    def _stop_stats(self):
        self._stats_stop.set()

    def _stats_loop(self):
        while not self._stats_stop.wait(5.0):
            self._report_stats()

    def _report_stats(self):
        with self._stats_lock:
            snap = dict(self._stats)
            for key in self._stats:
                self._stats[key] = 0 if isinstance(self._stats[key], int) else 0.0

        def avg(total, count):
            return (total / count) if count else 0.0

        self._status(
            "stats 5s: "
            f"received={snap['received']} decoded={snap['decoded']} "
            f"painted={snap['painted']} dropped={snap['dropped']} | "
            f"avg wait={avg(snap['wait_ms'], snap['received']):.1f}ms "
            f"decode={avg(snap['decode_ms'], snap['received']):.1f}ms "
            f"convert={avg(snap['convert_ms'], snap['decoded']):.1f}ms "
            f"paint={avg(snap['paint_ms'], snap['painted']):.1f}ms")

    # -- worker -----------------------------------------------------------
    def _run(self, serial: str, max_size: int, max_fps: int, bitrate: int):
        self._start_stats()
        try:
            self._run_inner(serial, max_size, max_fps, bitrate)
        finally:
            self._stop_stats()

    def _run_inner(self, serial: str, max_size: int, max_fps: int, bitrate: int):
        """Run the video stream.

        The scrcpy server (full rate) and the ``adb screencap`` fallback are
        started *together*.  The fallback guarantees that the preview shows
        something within a second; as soon as the scrcpy stream delivers its
        first frame the fallback is stopped and the fast path takes over.
        """
        self._fallback_stop.clear()
        fallback = threading.Thread(
            target=self._run_screencap, args=(serial,),
            name="ScreenFallback", daemon=True)
        fallback.start()

        streamed = False
        try:
            streamed = self._run_scrcpy(serial, max_size, max_fps, bitrate)
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                self._emit_error(
                    f"scrcpy stream error: {type(exc).__name__}: {exc}")
        finally:
            self._teardown_scrcpy()

        if not streamed and not self._stop.is_set():
            self._emit_state("fallback")
            self._status("Using the adb screencap fallback (the emulator is "
                         "not sending video frames)")
            # The fast path may have stopped the fallback when its first
            # frame arrived and then died: bring the fallback back so the
            # preview keeps updating instead of freezing.
            if self._fallback_stop.is_set():
                self._fallback_stop.clear()
                threading.Thread(
                    target=self._run_screencap, args=(serial,),
                    name="ScreenFallback", daemon=True).start()

        # The fallback keeps running when scrcpy failed.
        if streamed or self._stop.is_set():
            self._fallback_stop.set()

        if not self._stop.is_set():
            self._status("Screen stream ended")
            self._emit_state("stopped")

    def _run_scrcpy(self, serial: str, max_size: int, max_fps: int,
                    bitrate: int) -> bool:
        if _get_av() is None:
            self._emit_error("PyAV ('av') is not installed - cannot decode screen")
            return False

        self._status("Downloading scrcpy-server...")
        ok, path_or_error = ScrcpyServer.ensure_downloaded(
            self._tools_dir, progress_cb=self._on_download_progress)
        if not ok:
            self._emit_error(path_or_error)
            return False
        jar = Path(path_or_error)

        jar = Path(path_or_error)
        try:
            local_size = jar.stat().st_size
        except OSError as exc:
            self._emit_error(f"scrcpy-server missing locally: {exc}")
            return False

        adb_bin = str(PlatformUtils.get_adb_path())

        # Stop any stale server so the abstract socket is free.
        # This is pure housekeeping: it is logged only, never shown to the
        # user, because a leftover server is normal (a previous run was killed
        # before it could clean up its own socket).
        started = time.perf_counter()
        self._log_only("Stopping old scrcpy-server...")
        self._kill_old_server(adb_bin, serial)
        self._step_done("Stopping old scrcpy-server", started)

        # Fast reconnect: only push when the remote jar is missing or its size
        # differs.  The remote jar is NEVER deleted (scrcpy runs cleanup=false),
        # so a warm start skips the whole push/verify round-trip.
        started = time.perf_counter()
        remote_size = self._remote_jar_size(adb_bin, serial)
        if remote_size == local_size:
            self._status("scrcpy-server already up to date on device")
        else:
            self._status("Pushing scrcpy-server...")
            ok, msg = self._adb.push(jar, REMOTE_SERVER_PATH)
            if not ok:
                self._emit_error(f"Failed to push scrcpy-server: {msg}")
                return False
            ok, detail = self._verify_remote_jar(adb_bin, serial, jar)
            if not ok:
                self._emit_error(
                    "scrcpy-server missing or size mismatch on device "
                    f"(AppStation does not remove it): {_one_line(detail)}")
                return False
        self._step_done("scrcpy-server ready", started)

        # Only our own forwarding port is ever managed here (we deliberately do
        # NOT run `adb forward --remove-all`).
        t0 = time.perf_counter()
        port = _free_tcp_port()
        self._forward_port = port
        self._status("Setting up adb forward...")
        if not self._adb_forward(adb_bin, serial, port):
            self._emit_error("adb forward failed (could not open the stream port)")
            return False
        self._step_done("adb forward", t0)

        t0 = time.perf_counter()
        self._status("Starting scrcpy-server...")
        try:
            self._start_server(
                adb_bin, serial,
                self._server_command(max_size, max_fps, bitrate))
        except Exception as exc:  # noqa: BLE001
            self._emit_error(f"Failed to start scrcpy-server: {exc}")
            return False
        self._step_done("starting scrcpy-server", t0)

        t0 = time.perf_counter()
        self._status("Connecting to screen...")
        self._last_connect_error = None
        connected = self._connect_with_retries(port, timeout=15.0)
        if connected is None:
            detail = self._last_connect_error
            detail = (f"{type(detail).__name__}: {detail}" if detail
                      else "no successful connection")
            self._emit_error(f"Could not connect to scrcpy-server ({detail})")
            self._log_server_output_on_failure()
            return False
        sock, control_sock = connected
        self._step_done("connecting", t0)

        self._sock = sock
        self._control_sock = control_sock
        self._controller = ScrcpyController(control_sock)
        # Watchdog: when the guest stops presenting frames the decode blocks
        # forever, so a background check aborts the stream and the caller
        # falls back to the screencap path.  It is *not* a one-shot timer: a
        # healthy stream can run for hours, so the deadline is measured from
        # the most recent frame.
        self._last_frame_at = time.monotonic()
        self._watchdog_stop.clear()
        watchdog = threading.Thread(target=self._watchdog_loop,
                                    name="ScreenWatchdog", daemon=True)
        watchdog.start()
        try:
            self._decode_loop(sock, max_fps)
        finally:
            self._watchdog_stop.set()
            watchdog.join(timeout=1.0)
        return self._stop.is_set()

    def _watchdog_loop(self):
        """Abort the stream only when no frame has arrived for STALL_TIMEOUT."""
        while not self._stop.is_set() and not self._watchdog_stop.is_set():
            if self._watchdog_stop.wait(0.5):
                break
            if time.monotonic() - self._last_frame_at > STALL_TIMEOUT:
                self._abort_stalled_stream()
                break

    def _abort_stalled_stream(self):
        """Called by the watchdog when no frame arrived in time."""
        if self._stop.is_set():
            return
        self._log_only(f"No frame for {STALL_TIMEOUT:.0f}s - aborting the "
                       "scrcpy stream")
        try:
            self._close_socket()
        except Exception:
            pass
        self._stalled = True

    def _step_done(self, name: str, t0: float):
        """Log how long a start step took (elapsed ms) with the [screen] prefix.

        Logged only: the step timings are diagnostics, not user-facing steps.
        The user-facing progress is reported by the explicit ``_status`` calls.
        """
        self._log_only(f"{name}: {int((time.perf_counter() - t0) * 1000)} ms")

    # -- device preparation / server process ------------------------------
    def _kill_old_server(self, adb_bin: str, serial: str):
        """Kill any stale scrcpy app_process so its abstract socket is freed."""
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell",
                 "pkill -f com.genymobile.scrcpy.Server"],
                capture_output=True, timeout=10,
                creationflags=PlatformUtils.no_window_flags())
            # pkill exists: 0 = killed, 1 = nothing to kill.  Either way the
            # device is ready, so skip the expensive `ps` scan.
            if result.returncode in (0, 1):
                return
        except Exception:
            pass
        # Fallback for devices whose toybox has no pkill: scan ps and kill.
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell", "ps", "-A"],
                capture_output=True, text=True, timeout=10,
                creationflags=PlatformUtils.no_window_flags())
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    if "com.genymobile.scrcpy.Server" in line:
                        fields = line.split()
                        if len(fields) >= 2 and fields[1].isdigit():
                            subprocess.run(
                                [adb_bin, "-s", serial, "shell",
                                 "kill", "-9", fields[1]],
                                capture_output=True, timeout=5,
                                creationflags=PlatformUtils.no_window_flags())
        except Exception:
            pass

    def _remote_jar_size(self, adb_bin: str, serial: str) -> Optional[int]:
        """Return the size of the remote jar, or None when it is absent.

        Used to skip re-pushing a jar that is already up to date (the remote
        jar is never deleted by AppStation).
        """
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell", "ls", "-l",
                 REMOTE_SERVER_PATH],
                capture_output=True, text=True, timeout=10,
                creationflags=PlatformUtils.no_window_flags())
        except Exception:
            return None
        if result.returncode != 0:
            return None
        return self._remote_size_from_ls(result.stdout or "")

    @staticmethod
    def _remote_size_from_ls(output: str) -> Optional[int]:
        """Extract the byte size from ``ls -l`` output for the server jar."""
        for line in str(output).splitlines():
            if "scrcpy-server.jar" not in line:
                continue
            fields = line.split()
            # Standard `ls -l`: perms links owner group size date time name.
            if len(fields) >= 5 and fields[4].isdigit():
                return int(fields[4])
            # Fallback for other ls-like formats: first numeric token after col 0.
            for token in fields[1:]:
                if token.isdigit():
                    return int(token)
        return None

    def _verify_remote_jar(self, adb_bin: str, serial: str,
                           local_jar: Path) -> Tuple[bool, str]:
        """Confirm the pushed jar exists on device and matches the local size."""
        try:
            local_size = local_jar.stat().st_size
        except OSError as exc:
            return False, f"local jar unavailable: {exc}"
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell", "ls", "-l",
                 REMOTE_SERVER_PATH],
                capture_output=True, text=True, timeout=10,
                creationflags=PlatformUtils.no_window_flags())
        except Exception as exc:  # noqa: BLE001
            return False, f"ls -l failed: {exc}"
        output = (result.stdout or "").strip()
        if result.returncode != 0 or not output:
            return False, output or f"ls -l exit code {result.returncode}"
        remote_size = self._remote_size_from_ls(output)
        if remote_size is None:
            return False, output
        if remote_size != local_size:
            return False, (
                f"{output} (expected {local_size} bytes, device has "
                f"{remote_size})")
        return True, output

    def _start_server(self, adb_bin: str, serial: str, command):
        """Start scrcpy-server, capturing its stdout/stderr for diagnostics."""
        # Pass the remote command to ``adb shell`` as ONE string, exactly like
        # the manually-verified command line.  Passing it as many separate
        # argv tokens makes adb re-escape and join them, and that behaviour
        # differs between adb builds - the most likely reason the server never
        # started.
        remote_cmd = " ".join(str(part) for part in command)
        argv = [adb_bin, "-s", serial, "shell", remote_cmd]
        self._status(f"app_process command: {remote_cmd}")
        print(f"[screen] adb argv: {argv}", flush=True)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, start_new_session=True,
            creationflags=PlatformUtils.no_window_flags())
        self._server_log.clear()
        self._server_proc = proc
        self._log_thread = threading.Thread(
            target=self._pump_server_output, args=(proc,),
            name="ScrcpyServerLog", daemon=True)
        self._log_thread.start()
        # Give the device shell a moment, then report liveness so a server that
        # dies instantly is visible instead of surfacing as a connect timeout.
        time.sleep(0.5)
        poll = proc.poll()
        print(f"[screen] server pid: {proc.pid}, poll() after start: {poll}",
              flush=True)
        self._status(f"scrcpy-server pid={proc.pid} poll={poll}")
        return proc

    def _pump_server_output(self, proc):
        """Echo server output as ``[screen]`` lines; surface the first error."""
        stream = proc.stdout
        if stream is None:
            return
        first_error_shown = False
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                try:
                    print(f"[screen] server: {line}", flush=True)
                except Exception:
                    pass
                self._server_log.append(line)
                lowered = line.lower()
                if not first_error_shown and (
                        "error" in lowered or "exception" in lowered
                        or "not found" in lowered):
                    first_error_shown = True
                    self._status(f"scrcpy-server: {line}")
        except Exception as exc:  # noqa: BLE001
            print(f"[screen] server log read error: {exc}", flush=True)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _server_command(max_size: int, max_fps: int, bitrate: int):
        # Option order mirrors the manually-verified command line:
        #   CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / \
        #     com.genymobile.scrcpy.Server 5.0.1 raw_stream=true \
        #     tunnel_forward=true audio=false control=true \
        #     max_size=1280 max_fps=30 cleanup=false
        # ``raw_stream=true`` disables device/stream/frame meta and the dummy
        # byte, leaving a plain H.264 Annex-B stream on the video socket.
        # ``log_level=info`` and ``video_bit_rate`` are the only additions and
        # are both valid scrcpy-server v5.0.1 options.
        return [
            f"CLASSPATH={REMOTE_SERVER_PATH}",
            "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_VERSION,
            "raw_stream=true",
            "tunnel_forward=true",
            "audio=false",
            "control=true",
            f"max_size={int(max_size)}",
            f"max_fps={int(max_fps)}",
            "log_level=info",
            f"video_bit_rate={int(bitrate)}",
            "cleanup=false",
        ]

    def _adb_forward(self, adb_bin: str, serial: str, port: int) -> bool:
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "forward", f"tcp:{port}",
                 f"localabstract:{ABSTRACT_SOCKET_NAME}"],
                capture_output=True, text=True, timeout=10,
                creationflags=PlatformUtils.no_window_flags())
            return result.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _close_socket_quietly(sock):
        if sock is None:
            return
        try:
            sock.close()
        except Exception:
            pass

    def _connect_with_retries(self, port: int, timeout: float = 15.0):
        """Connect to the adb-forwarded scrcpy video + control sockets.

        Mirrors scrcpy's own client logic: a refused *or reset* connection only
        means the server is not ready yet, so keep retrying.  The server
        accepts the video socket first and then the control socket; without
        the second connection it never starts encoding, so both are opened
        before waiting for the first video byte.  ``raw_stream=true`` disables
        the dummy byte, so that first byte is real H.264 data - it is checked
        with ``MSG_PEEK`` so it is not consumed.

        Returns ``(video_sock, control_sock)`` or ``None``.
        """
        import socket
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline and not self._stop.is_set():
            attempt += 1
            video_sock = None
            control_sock = None
            try:
                video_sock = socket.create_connection(("127.0.0.1", port),
                                                      timeout=1.0)
            except OSError as exc:
                self._last_connect_error = exc
                print(f"[screen] connect attempt {attempt}: not ready yet "
                      f"({type(exc).__name__}: {exc})", flush=True)
                time.sleep(0.2)
                continue

            try:
                # The control socket must be connected immediately after the
                # video socket: the server blocks in accept() until it arrives.
                control_sock = socket.create_connection(("127.0.0.1", port),
                                                        timeout=1.0)
            except OSError as exc:
                self._last_connect_error = exc
                print(f"[screen] connect attempt {attempt}: video connected "
                      f"but control socket failed "
                      f"({type(exc).__name__}: {exc})", flush=True)
                self._close_socket_quietly(video_sock)
                time.sleep(0.2)
                continue

            try:
                video_sock.settimeout(5.0)
                probe = video_sock.recv(1, socket.MSG_PEEK)
                if not probe:
                    # Accepted, then immediately closed: the abstract socket
                    # does not exist yet, so the server is not listening.
                    self._last_connect_error = ConnectionResetError(
                        "connection accepted then closed by the server")
                    print(f"[screen] connect attempt {attempt}: accepted then "
                          "closed by server (not ready yet)", flush=True)
                    self._close_socket_quietly(video_sock)
                    self._close_socket_quietly(control_sock)
                    time.sleep(0.2)
                    continue
                video_sock.settimeout(None)
                control_sock.settimeout(None)
                print(f"[screen] connect attempt {attempt}: connected - first "
                      "byte received from scrcpy-server", flush=True)
                return video_sock, control_sock
            except socket.timeout:
                self._last_connect_error = TimeoutError(
                    "no data from scrcpy-server within 5s")
                print(f"[screen] connect attempt {attempt}: connected but no "
                      "video data within 5s", flush=True)
                self._close_socket_quietly(video_sock)
                self._close_socket_quietly(control_sock)
                time.sleep(0.2)
                continue
            except OSError as exc:
                self._last_connect_error = exc
                print(f"[screen] connect attempt {attempt}: reset by server "
                      f"({type(exc).__name__}: {exc})", flush=True)
                self._close_socket_quietly(video_sock)
                self._close_socket_quietly(control_sock)
                time.sleep(0.2)
                continue

        print(f"[screen] connect failed after {attempt} attempt(s) in "
              f"{timeout:.0f}s", flush=True)
        return None

    def _log_server_output_on_failure(self):
        """Dump the buffered scrcpy-server output when connecting fails."""
        print("[screen] --- scrcpy-server output (last lines) ---", flush=True)
        lines = list(self._server_log)
        if not lines:
            print("[screen] server output: <empty> "
                  "(server produced no stdout/stderr)", flush=True)
        else:
            for line in lines[-40:]:
                print(f"[screen] server: {line}", flush=True)
        print("[screen] --- end scrcpy-server output ---", flush=True)

    def _configure_decoder(self, container):
        """Low-latency PyAV decode: auto threading, no B-frame buffering.

        Hardware decoding is requested first (VideoToolbox on macOS, D3D11 /
        DXVA on Windows); when it is unavailable the software decoder is used
        and the reason is logged.
        """
        try:
            stream = container.streams.video[0]
        except Exception:
            return
        try:
            stream.thread_type = "AUTO"
        except Exception:
            pass
        try:
            context = stream.codec_context
            context.thread_type = "AUTO"
            av = _get_av()
            if av is not None:
                context.flags |= av.codec.context.Flags.low_delay
                context.flags2 |= av.codec.context.Flags2.fast
        except Exception:
            pass
        self._enable_hwaccel(stream)

    def _enable_hwaccel(self, stream) -> bool:
        """Try to enable hardware decoding for this stream."""
        try:
            candidates = []
            if PlatformUtils.IS_MACOS:
                candidates = ["videotoolbox", "videotoolbox-vld"]
            elif PlatformUtils.IS_WINDOWS:
                candidates = ["d3d11va", "dxva2-vld", "d3d11va-vld"]
            else:
                candidates = ["vaapi", "vdpau"]
            for name in candidates:
                try:
                    stream.codec_context.options = {"hwaccel": name}
                    stream.codec_context.thread_type = "AUTO"
                    self._hwaccel = name
                    self._log_only(f"Hardware decoding requested: {name}")
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        self._hwaccel = None
        self._log_only("Hardware decoding unavailable - using software decode")
        return False

    def _decode_loop(self, sock, max_fps: int):
        """Decode the H.264 stream, always keeping only the newest frame.

        The decoder runs on the stream thread, never on the GUI thread, and
        the frame slot holds a single frame: a newer frame overwrites an
        unread one, so the pipeline can never build a backlog.
        """
        source = _SocketFile(sock)
        av = _get_av()
        if av is None:
            return
        # A raw H.264 stream from scrcpy has no container metadata, so PyAV
        # would otherwise keep *probing* until it has enough bytes.  On a
        # static screen scrcpy sends one small IDR and then nothing, which
        # made av.open() block for up to ~90 s - the preview stayed on the
        # slow screencap fallback the whole time.  The stream is known to be
        # H.264, so probe only the first packet.
        try:
            container = av.open(
                source, format="h264",
                options={"probesize": "1024", "analyzeduration": "0"})
        except Exception:
            container = av.open(source, format="h264")
        self._configure_decoder(container)
        got_first_frame = False
        started = time.perf_counter()
        try:
            try:
                stream = container.streams.video[0]
                codec = stream.codec_context
            except Exception:
                stream = None
                codec = None

            # Demux packets so the time spent *waiting for data* can be
            # measured separately from the real decode cost.  The wait is
            # the gap between finishing one packet and the next one
            # arriving; the decode is the CPU work on the packet itself.
            last_end = time.perf_counter()
            for packet in container.demux(video=0):
                if self._stop.is_set():
                    break
                if packet.size == 0:
                    continue
                t0 = time.perf_counter()
                wait_ms = (t0 - last_end) * 1000.0

                try:
                    frames = codec.decode(packet) if codec is not None \
                        else [packet.decode()]
                except Exception:
                    frames = []
                t1 = time.perf_counter()
                decode_ms = (t1 - t0) * 1000.0
                last_end = t1

                with self._stats_lock:
                    self._stats["received"] += 1
                    self._stats["wait_ms"] += wait_ms
                    self._stats["decode_ms"] += decode_ms

                for frame in frames:
                    if frame is None:
                        continue
                    t3 = time.perf_counter()
                    image = self._frame_to_qimage(frame)
                    t4 = time.perf_counter()
                    if image is None:
                        continue
                    # Touch events must be tagged with the encoder's video
                    # size, which is smaller than the device screen whenever
                    # max_size kicks in.  Keep the controller in sync.
                    controller = self._controller
                    if controller is not None:
                        controller.set_video_size(image.width(),
                                                  image.height())
                    with self._stats_lock:
                        self._stats["decoded"] += 1
                        self._stats["convert_ms"] += (t4 - t3) * 1000.0

                    if not got_first_frame:
                        got_first_frame = True
                        self._status("Screen streaming")
                        self._emit_state("streaming")
                    self._publish_frame(image)
        finally:
            try:
                container.close()
            except Exception:
                pass
            if not got_first_frame and not self._stop.is_set():
                # The server accepted the connection but the guest never
                # produced a frame.  This happens when the emulator has no
                # usable GPU: the software renderer never presents a buffer.
                waited = time.perf_counter() - started
                self._emit_error(
                    f"No video frame from the guest after {waited:.0f}s "
                    f"(the emulator is not presenting frames)")
                self._emit_state("stalled")

    @staticmethod
    def _frame_to_qimage(frame) -> Optional[QImage]:
        return _video_frame_to_qimage(frame)

    def _run_screencap(self, serial: str):
        """Capture the guest screen with ``adb exec-out screencap``.

        The raw format (no PNG encoding on the guest) is used because it is
        noticeably faster: the guest does not have to compress the frame.
        Only the newest frame is kept, so the pipeline never queues.
        """
        adb_bin = str(PlatformUtils.get_adb_path())
        cmd = [adb_bin, "-s", serial, "exec-out", "screencap"]
        # Give scrcpy a head start: a screencap during server startup competes
        # with the encoder for emulator CPU and delays the fast path.  If
        # scrcpy fails, the fallback still appears ~2s later.
        if self._stop.wait(2.0):
            return
        while not self._stop.is_set() and not self._fallback_stop.is_set():
            started = time.time()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=20,
                                        creationflags=PlatformUtils.no_window_flags())
                data = result.stdout
                if data and not self._stop.is_set():
                    image = _raw_screencap_to_qimage(data)
                    if image is None:
                        image = QImage()
                        image.loadFromData(data)
                    if not image.isNull():
                        with self._stats_lock:
                            self._stats["received"] += 1
                            self._stats["decoded"] += 1
                        self._publish_frame(image, from_fallback=True)
                        if not self._fallback_announced:
                            self._fallback_announced = True
                            self._log_only("screencap fallback is live")
            except Exception:
                pass
            remaining = FALLBACK_INTERVAL - (time.time() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    # -- teardown ---------------------------------------------------------
    def _close_socket(self):
        sock = self._sock
        control_sock = self._control_sock
        self._sock = None
        self._control_sock = None
        self._controller = None
        if sock is not None:
            try:
                import socket as _socket
                sock.shutdown(_socket.SHUT_RDWR)
            except Exception:
                pass
        self._close_socket_quietly(sock)
        self._close_socket_quietly(control_sock)

    def _teardown_scrcpy(self):
        self._close_socket()
        proc = self._server_proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._server_proc = None
        # The local `adb shell` going away does not always stop the on-device
        # app_process, so the device side is cleaned up explicitly.  This is
        # what stops a server from surviving a preview close or an emulator
        # stop and having to be killed at the next start.
        self.kill_remote_server()
        # NOTE: the pushed remote jar is intentionally left in place.  Nothing
        # in AppStation ever deletes /data/local/tmp/scrcpy-server.jar (scrcpy
        # itself is started with cleanup=false).
        if self._forward_port is not None:
            try:
                subprocess.run(
                    [str(PlatformUtils.get_adb_path()), "-s", self._serial,
                     "forward", "--remove", f"tcp:{self._forward_port}"],
                    capture_output=True, timeout=10,
                    creationflags=PlatformUtils.no_window_flags())
            except Exception:
                pass
            self._forward_port = None

    # -- callbacks (invoked from the worker thread) -----------------------
    def _on_download_progress(self, percent: int):
        self._status(f"Downloading scrcpy-server... {percent}%")
        self._emit_note(f"Downloading scrcpy-server... {percent}%")

    def _log_only(self, message: str):
        """Log a housekeeping message to stdout only (never shown in the UI).

        Used for internal cleanup steps such as killing a leftover
        scrcpy-server, which are normal background work and should not be
        reported as user-facing progress.
        """
        try:
            print(f"[screen] {message}", flush=True)
        except Exception:
            pass

    def kill_remote_server(self):
        """Kill the scrcpy-server running on the device.

        Called when the emulator stops and when the preview is closed so a
        leftover server never lingers on the device.  The local ``adb shell``
        process is not enough: terminating it does not always stop the
        on-device ``app_process``, which is why the device side is killed
        explicitly.
        """
        try:
            adb_bin = str(PlatformUtils.get_adb_path())
        except Exception:
            return
        try:
            self._kill_old_server(adb_bin, self._serial)
            self._log_only("Remote scrcpy-server stopped")
        except Exception:
            pass

    def _status(self, message: str):
        """Print a stage message to stdout and forward it to the UI."""
        try:
            print(f"[screen] {message}", flush=True)
        except Exception:
            pass
        fn = self.on_status
        if fn is not None:
            try:
                fn(message)
            except Exception:
                pass

    def _emit_note(self, message: str):
        fn = self.on_note
        if fn is not None:
            try:
                fn(message)
            except Exception:
                pass

    def _emit_error(self, message: str):
        try:
            print(f"[screen] ERROR: {message}", flush=True)
        except Exception:
            pass
        fn = self.on_error
        if fn is not None:
            try:
                fn(message)
            except Exception:
                pass

    def _emit_state(self, state: str):
        fn = self.on_state
        if fn is not None:
            try:
                fn(state)
            except Exception:
                pass
