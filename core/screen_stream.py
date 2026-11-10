"""Embedded screen streaming for ApkPreview (Stage 2a - view only).

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
   matches the local file (the remote jar is never deleted by ApkPreview).
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

import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional, Tuple

from PyQt6.QtGui import QImage

from .adb import ADB
from .platform_utils import PlatformUtils
from .settings import settings

try:  # PyAV is optional at import time so the app still starts without it.
    import av
except Exception:  # pragma: no cover - exercised only on broken installs
    av = None


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
                SCRCPY_SERVER_URL, headers={"User-Agent": "ApkPreview"})
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
        return av is not None


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
        self._server_proc = None
        self._log_thread: Optional[threading.Thread] = None
        self._forward_port: Optional[int] = None
        # Diagnostics: last scrcpy-server stdout/stderr lines + the last
        # connection error, so a final connect failure can explain itself.
        self._server_log: deque = deque(maxlen=200)
        self._last_connect_error: Optional[BaseException] = None

        # Latest decoded frame: a single slot the UI pulls from (~30 Hz).  A new
        # frame overwrites whatever was not consumed yet (older frames drop).
        self._frame_lock = threading.Lock()
        self._latest_image: Optional[QImage] = None
        self._latest_seq = 0

        # Rolling performance counters (reset every 5 s by _stats_loop).
        self._stats_lock = threading.Lock()
        self._stats = {
            "received": 0, "decoded": 0, "painted": 0, "dropped": 0,
            "decode_ms": 0.0, "convert_ms": 0.0, "paint_ms": 0.0,
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

    def _publish_frame(self, image: QImage):
        """Store the latest decoded frame, dropping any unread previous one."""
        if image is None:
            return
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
            f"avg decode={avg(snap['decode_ms'], snap['received']):.1f}ms "
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
            note = "scrcpy unavailable - using adb screencap fallback (~3 fps)"
            self._emit_state("fallback")
            self._status(note)
            self._emit_note(note)
            try:
                self._run_screencap(serial)
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    self._emit_error(
                        f"screencap fallback error: {type(exc).__name__}: {exc}")

        if not self._stop.is_set():
            self._status("Screen stream ended")
            self._emit_state("stopped")

    def _run_scrcpy(self, serial: str, max_size: int, max_fps: int,
                    bitrate: int) -> bool:
        if av is None:
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
        started = time.perf_counter()
        self._status("Stopping old scrcpy-server...")
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
                    f"(ApkPreview does not remove it): {_one_line(detail)}")
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
        sock = self._connect_with_retries(port, timeout=15.0)
        if sock is None:
            detail = self._last_connect_error
            detail = (f"{type(detail).__name__}: {detail}" if detail
                      else "no successful connection")
            self._emit_error(f"Could not connect to scrcpy-server ({detail})")
            self._log_server_output_on_failure()
            return False
        self._step_done("connecting", t0)

        self._sock = sock
        self._decode_loop(sock, max_fps)
        return self._stop.is_set()

    def _step_done(self, name: str, t0: float):
        """Log how long a start step took (elapsed ms) with the [screen] prefix."""
        self._status(f"{name}: {int((time.perf_counter() - t0) * 1000)} ms")

    # -- device preparation / server process ------------------------------
    def _kill_old_server(self, adb_bin: str, serial: str):
        """Kill any stale scrcpy app_process so its abstract socket is freed."""
        try:
            subprocess.run(
                [adb_bin, "-s", serial, "shell",
                 "pkill -f com.genymobile.scrcpy.Server"],
                capture_output=True, timeout=10)
        except Exception:
            pass
        # Fallback for devices whose toybox has no pkill: scan ps and kill.
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell", "ps", "-A"],
                capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                for line in (result.stdout or "").splitlines():
                    if "com.genymobile.scrcpy.Server" in line:
                        fields = line.split()
                        if len(fields) >= 2 and fields[1].isdigit():
                            subprocess.run(
                                [adb_bin, "-s", serial, "shell",
                                 "kill", "-9", fields[1]],
                                capture_output=True, timeout=5)
        except Exception:
            pass

    def _remote_jar_size(self, adb_bin: str, serial: str) -> Optional[int]:
        """Return the size of the remote jar, or None when it is absent.

        Used to skip re-pushing a jar that is already up to date (the remote
        jar is never deleted by ApkPreview).
        """
        try:
            result = subprocess.run(
                [adb_bin, "-s", serial, "shell", "ls", "-l",
                 REMOTE_SERVER_PATH],
                capture_output=True, text=True, timeout=10)
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
                capture_output=True, text=True, timeout=10)
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
            text=True, bufsize=1, start_new_session=True)
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
        #     tunnel_forward=true audio=false control=false \
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
            "control=false",
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
                capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except Exception:
            return False

    def _connect_with_retries(self, port: int, timeout: float = 15.0):
        """Connect to the adb-forwarded scrcpy socket.

        Mirrors scrcpy's own client logic: a refused *or reset* connection only
        means the server is not ready yet, so keep retrying.  Once a TCP
        connection is accepted we wait (up to 5s) for the server's first byte.
        ``raw_stream=true`` disables the dummy byte, so the first byte is real
        H.264 data - it is checked with ``MSG_PEEK`` so it is not consumed.
        """
        import socket
        deadline = time.time() + timeout
        attempt = 0
        while time.time() < deadline and not self._stop.is_set():
            attempt += 1
            try:
                sock = socket.create_connection(("127.0.0.1", port),
                                                timeout=1.0)
            except OSError as exc:
                self._last_connect_error = exc
                print(f"[screen] connect attempt {attempt}: not ready yet "
                      f"({type(exc).__name__}: {exc})", flush=True)
                time.sleep(0.2)
                continue

            try:
                sock.settimeout(5.0)
                probe = sock.recv(1, socket.MSG_PEEK)
                if not probe:
                    # Accepted, then immediately closed: the abstract socket
                    # does not exist yet, so the server is not listening.
                    self._last_connect_error = ConnectionResetError(
                        "connection accepted then closed by the server")
                    print(f"[screen] connect attempt {attempt}: accepted then "
                          "closed by server (not ready yet)", flush=True)
                    try:
                        sock.close()
                    except Exception:
                        pass
                    time.sleep(0.2)
                    continue
                sock.settimeout(None)
                print(f"[screen] connect attempt {attempt}: connected - first "
                      "byte received from scrcpy-server", flush=True)
                return sock
            except socket.timeout:
                self._last_connect_error = TimeoutError(
                    "no data from scrcpy-server within 5s")
                print(f"[screen] connect attempt {attempt}: connected but no "
                      "video data within 5s", flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
                time.sleep(0.2)
                continue
            except OSError as exc:
                self._last_connect_error = exc
                print(f"[screen] connect attempt {attempt}: reset by server "
                      f"({type(exc).__name__}: {exc})", flush=True)
                try:
                    sock.close()
                except Exception:
                    pass
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
        """Low-latency PyAV decode: auto threading, no B-frame buffering."""
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
            context.flags |= av.codec.context.Flags.low_delay
            context.flags2 |= av.codec.context.Flags2.fast
        except Exception:
            pass

    def _decode_loop(self, sock, max_fps: int):
        source = _SocketFile(sock)
        container = av.open(source, format="h264")
        self._configure_decoder(container)
        got_first_frame = False
        try:
            iterator = container.decode(video=0)
            while not self._stop.is_set():
                t0 = time.perf_counter()
                try:
                    frame = next(iterator)
                except StopIteration:
                    break
                t1 = time.perf_counter()
                with self._stats_lock:
                    self._stats["received"] += 1
                    self._stats["decode_ms"] += (t1 - t0) * 1000.0

                t2 = time.perf_counter()
                image = self._frame_to_qimage(frame)
                t3 = time.perf_counter()
                if image is None:
                    continue
                with self._stats_lock:
                    self._stats["decoded"] += 1
                    self._stats["convert_ms"] += (t3 - t2) * 1000.0

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

    @staticmethod
    def _frame_to_qimage(frame) -> Optional[QImage]:
        return _video_frame_to_qimage(frame)

    def _run_screencap(self, serial: str):
        adb_bin = str(PlatformUtils.get_adb_path())
        cmd = [adb_bin, "-s", serial, "exec-out", "screencap", "-p"]
        while not self._stop.is_set():
            started = time.time()
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=15)
                data = result.stdout
                if data and not self._stop.is_set():
                    image = QImage()
                    if image.loadFromData(data):
                        with self._stats_lock:
                            self._stats["received"] += 1
                            self._stats["decoded"] += 1
                        self._publish_frame(image)
            except Exception:
                pass
            remaining = FALLBACK_INTERVAL - (time.time() - started)
            if remaining > 0:
                self._stop.wait(remaining)

    # -- teardown ---------------------------------------------------------
    def _close_socket(self):
        sock = self._sock
        if sock is None:
            return
        try:
            import socket as _socket
            sock.shutdown(_socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        self._sock = None

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
        # NOTE: the pushed remote jar is intentionally left in place.  Nothing
        # in ApkPreview ever deletes /data/local/tmp/scrcpy-server.jar (scrcpy
        # itself is started with cleanup=false).
        if self._forward_port is not None:
            try:
                subprocess.run(
                    [str(PlatformUtils.get_adb_path()), "-s", self._serial,
                     "forward", "--remove", f"tcp:{self._forward_port}"],
                    capture_output=True, timeout=10)
            except Exception:
                pass
            self._forward_port = None

    # -- callbacks (invoked from the worker thread) -----------------------
    def _on_download_progress(self, percent: int):
        self._status(f"Downloading scrcpy-server... {percent}%")
        self._emit_note(f"Downloading scrcpy-server... {percent}%")

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
