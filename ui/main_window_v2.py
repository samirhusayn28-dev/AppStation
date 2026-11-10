"""Main window V2 for ApkPreview - rebuilt layout with wired controls.

Layout: top toolbar / splitter (preview stack | tab widget) / slim status row.
Handlers and the install worker are copied from the original ui/main_window.py;
long-running work (APK parse, install, emulator start/stop, adb actions) runs on
worker threads so the UI never blocks. Layouts only - no setGeometry/move/setFixed*.
"""

import time
import os
from pathlib import Path
from typing import Optional, Callable, Tuple

import psutil

from PyQt6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                             QSplitter, QLabel, QPushButton, QComboBox,
                             QStackedWidget, QTabWidget, QFileDialog,
                             QMessageBox, QGridLayout, QScrollArea,
                             QSizePolicy, QSpinBox, QCheckBox)
from PyQt6.QtCore import Qt, QThread, QTimer, QObject, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from .drop_zone import DropZone
from .logcat_panel import LogcatPanel
from .screen_view import ScreenView
from .device_window import DeviceWindow
from .device_picker import pick_device
from .android_versions_dialog import AndroidVersionsDialog
from core.platform_utils import PlatformUtils
from core.sdk_manager import SDKManager, ANDROID_VERSION_NAMES
from core.hardware_profile import HardwareProfiler, PerformanceMode
from core.platform_utils import PlatformUtils
from core.emulator import EmulatorManager
from core.adb import ADB, detect_package_and_activity
from core.scrcpy import ScrcpyManager
from core.apk_parser import get_apk_info
from core.device_presets import preset_manager, CATEGORY_FOLDABLE
from core.file_watcher import create_file_watcher
from core.screen_stream import ScreenStream
from core.settings import settings


class _SignalBridge(QObject):
    """Marshals events from background threads onto the main (UI) thread."""

    apk_changed = pyqtSignal(object)


class _ScreenBridge(QObject):
    """Marshals embedded-screen events from the stream thread onto the UI thread.

    Frames are NOT pushed through the bridge: the stream keeps only the latest
    decoded frame in a single slot and the UI pulls it on a QTimer (~30 Hz).
    """

    note = pyqtSignal(str)
    error = pyqtSignal(str)
    state = pyqtSignal(str)
    status = pyqtSignal(str)


# ----------------------------------------------------------------------------
# Workers (install worker copied verbatim from ui/main_window.py)
# ----------------------------------------------------------------------------
class APKInstallWorker(QThread):
    """Background worker for APK install and launch."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str, str, str)  # success, message, package, activity
    log = pyqtSignal(str)

    def __init__(self, apk_path: Path, emulator: EmulatorManager, adb: ADB,
                 scrcpy: ScrcpyManager, mode: PerformanceMode, scrcpy_available: bool = True,
                 no_window: bool = False, open_window: bool = False,
                 start_mirror: bool = True):
        super().__init__()
        self._apk_path = apk_path
        self._emulator = emulator
        self._adb = adb
        self._scrcpy = scrcpy
        self._mode = mode
        self._scrcpy_available = scrcpy_available
        self._no_window = no_window
        self._open_window = open_window
        self._start_mirror = start_mirror
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            # Step 1: Start emulator if not running
            if not self._emulator.is_running():
                self.progress.emit("Starting emulator...")
                self.log.emit("Starting emulator...")
                success, msg = self._emulator.start(self._mode, wait_for_boot=True, timeout=180, scrcpy_available=self._scrcpy_available, no_window=self._no_window, open_window=self._open_window)
                if not success:
                    self.finished.emit(False, f"Emulator start failed: {msg}", "", "")
                    return
                if self._cancelled:
                    return

            # Step 2: Wait for device to be fully ready
            self.progress.emit("Waiting for Android to finish starting...")
            self.log.emit("Waiting for Android to finish starting...")
            success, msg = self._adb.wait_for_device(180)
            if not success:
                self.finished.emit(False, f"Device not ready: {msg}", "", "")
                return
            if self._cancelled:
                return

            # Step 3: Install APK with retries
            self.progress.emit(f"Installing {self._apk_path.name}...")
            self.log.emit(f"Installing {self._apk_path.name}...")

            max_retries = 3
            retry_delay = 5
            retryable_errors = [
                "INSTALL_PARSE_FAILED",
                "Failed to load asset path",
                "device offline"
            ]

            for attempt in range(1, max_retries + 1):
                if self._cancelled:
                    return

                if attempt > 1:
                    self.progress.emit(f"Retrying install (attempt {attempt}/{max_retries})...")
                    self.log.emit(f"Retrying install (attempt {attempt}/{max_retries})...")
                    time.sleep(retry_delay)

                success, msg = self._adb.install(self._apk_path, reinstall=True)
                if success:
                    break

                # Check if error is retryable
                is_retryable = any(err in msg for err in retryable_errors)
                if not is_retryable or attempt == max_retries:
                    self.finished.emit(False, f"Install failed: {msg}", "", "")
                    return

            if not success:
                self.finished.emit(False, f"Install failed after {max_retries} attempts: {msg}", "", "")
                return

            if self._cancelled:
                return

            # Step 4: Parse package and activity
            self.progress.emit("Parsing APK...")
            self.log.emit("Parsing APK...")
            package, activity = detect_package_and_activity(self._apk_path)

            if not package:
                self.finished.emit(False, "Could not detect package name", "", "")
                return

            self.log.emit(f"Package: {package}")
            if activity:
                self.log.emit(f"Main activity: {activity}")

            if self._cancelled:
                return

            # Step 5: Launch app
            self.progress.emit("Launching app...")
            self.log.emit("Launching app...")
            if activity:
                success, msg = self._adb.start_activity(package, activity)
            else:
                # Try monkey to launch main activity
                success, msg = self._adb.shell(f"monkey -p {package} -c android.intent.category.LAUNCHER 1")

            if not success:
                self.finished.emit(False, f"Launch failed: {msg}", package, activity or "")
                return

            # Step 6: Start scrcpy if available (native mode shows the
            # emulator's own window, so no external mirror is started there).
            if self._scrcpy_available and self._start_mirror:
                self.progress.emit("Starting screen mirror...")
                self.log.emit("Starting screen mirror...")
                low_spec = (self._mode == PerformanceMode.LOW_SPEC)
                self._scrcpy.start(low_spec=low_spec)
            elif not self._start_mirror:
                self.log.emit("Using emulator window (native display mode)")
            else:
                self.log.emit("scrcpy not available, using emulator window")

            self.finished.emit(True, "App launched successfully!", package, activity or "")

        except Exception as e:
            self.finished.emit(False, f"Error: {e}", "", "")


class APKParseWorker(QThread):
    """Parse an APK off the UI thread."""

    parsed = pyqtSignal(object, object)  # path, info dict

    def __init__(self, path: Path):
        super().__init__()
        self._path = path

    def run(self):
        try:
            info = get_apk_info(self._path)
        except Exception as e:
            info = {"valid": False, "error": str(e), "package": None}
        self.parsed.emit(self._path, info)


class EmulatorWorker(QThread):
    """Run blocking emulator start/stop/restart off the UI thread."""

    result = pyqtSignal(str, bool, str)  # action, success, message

    def __init__(self, action: str, emulator: EmulatorManager, scrcpy: ScrcpyManager,
                 mode: PerformanceMode, scrcpy_available: bool = True,
                 no_window: bool = False, open_window: bool = False):
        super().__init__()
        self._action = action
        self._emulator = emulator
        self._scrcpy = scrcpy
        self._mode = mode
        self._scrcpy_available = scrcpy_available
        self._no_window = no_window
        self._open_window = open_window

    def run(self):
        try:
            if self._action == "start":
                ok, msg = self._emulator.start(
                    self._mode, wait_for_boot=True,
                    scrcpy_available=self._scrcpy_available,
                    no_window=self._no_window,
                    open_window=self._open_window)
            elif self._action == "stop":
                self._scrcpy.stop()
                ok, msg = self._emulator.stop()
            elif self._action == "restart":
                self._scrcpy.stop()
                self._emulator.stop()
                ok, msg = self._emulator.start(
                    self._mode, wait_for_boot=True,
                    scrcpy_available=self._scrcpy_available,
                    no_window=self._no_window,
                    open_window=self._open_window)
            else:
                ok, msg = False, f"Unknown emulator action: {self._action}"
        except Exception as e:
            ok, msg = False, f"Error: {e}"
        self.result.emit(self._action, ok, msg)


class AdbActionWorker(QThread):
    """Run a short blocking adb callable off the UI thread."""

    done = pyqtSignal(bool, str)

    def __init__(self, fn: Callable[[], Tuple[bool, str]]):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            ok, msg = self._fn()
        except Exception as e:
            ok, msg = False, f"Error: {e}"
        self.done.emit(ok, msg)


# ----------------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------------
class MainWindowV2(QMainWindow):
    """Main application window (rebuilt layout)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ApkPreview")
        self.setMinimumSize(1100, 750)

        # Core components
        self._profiler = HardwareProfiler()
        self._mode = PerformanceMode.NORMAL
        self._emulator = EmulatorManager()
        self._adb = ADB()
        self._scrcpy = ScrcpyManager()
        self._sdk = SDKManager()

        # Embedded screen (Stage 2a - view only). The stream runs on its own
        # thread; _ScreenBridge marshals notes/errors/state onto the UI thread.
        # Frames are pulled from the stream's latest-frame slot by _frame_timer
        # (~30 Hz) instead of being queued through a signal.
        self._screen = ScreenStream(self._adb)
        self._screen_bridge = _ScreenBridge(self)
        self._screen_bridge.note.connect(self._on_screen_note)
        self._screen_bridge.error.connect(self._on_screen_error)
        self._screen_bridge.state.connect(self._on_screen_state)
        self._screen_bridge.status.connect(self._on_screen_status)
        self._screen.set_callbacks(
            on_note=lambda msg: self._screen_bridge.note.emit(msg),
            on_error=lambda msg: self._screen_bridge.error.emit(msg),
            on_state=lambda state: self._screen_bridge.state.emit(state),
            on_status=lambda msg: self._screen_bridge.status.emit(msg))

        # The live view lives in its own top-level window now; the main window's
        # left pane shows only the drop zone / APK card.
        self.screen_view = ScreenView()
        self.device_window = DeviceWindow(self.screen_view)
        self.device_window.action.connect(self._on_device_window_action)
        self.device_window.closed.connect(self._on_device_window_closed)
        self.screen_view.painted.connect(self._on_screen_painted)
        self._last_frame_seq = -1
        # UI paint timer: pulls the stream's latest frame ~30 times a second.
        self._frame_timer = QTimer(self)
        self._frame_timer.setInterval(33)
        self._frame_timer.timeout.connect(self._on_frame_tick)

        # State
        self._current_apk: Optional[Path] = None
        self._current_package: Optional[str] = None
        self._current_activity: Optional[str] = None
        self._rotation_index: int = 0
        self._install_worker: Optional[APKInstallWorker] = None
        self._emu_worker: Optional[EmulatorWorker] = None
        self._parse_worker: Optional[APKParseWorker] = None
        self._device_screen: str = "inner"
        self._adb_workers: list = []
        # Guards use a real adb probe (worker thread), so block re-entry while
        # one is in flight.
        self._device_checking = False

        # File watcher (auto reinstall) + thread bridge + status timer
        self._file_watcher = create_file_watcher()
        self._bridge = _SignalBridge(self)
        self._bridge.apk_changed.connect(self._on_apk_changed)
        self._file_watcher.set_callback(self._on_file_changed)

        self._setup_ui()
        self._connect_signals()
        self._populate_presets()
        self._profile_mode()
        self._load_controls_settings()
        self._update_emulator_buttons(self._emulator.is_running())
        self._update_app_buttons(False)
        # Apply the persisted display mode (native by default: Screen disabled).
        self._apply_display_mode()
        # Adopt an existing apkpreview_default AVD so upgrades keep working,
        # then show the selected Android version in the Controls tab.
        self._migrate_legacy_android_version()
        self._refresh_selected_android_label()

        # Status-row sampler: cheap psutil reads every 2 s (never touches the
        # first label, which is the general status/error line).
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2000)
        self._status_timer.timeout.connect(self._update_status_row)
        self._status_timer.start()
        self._update_status_row()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _setup_ui(self):
        """Build the window layout from scratch."""
        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # (a) Top toolbar row
        root.addLayout(self._build_toolbar())

        # (b) Horizontal splitter: left preview stack | right tabs
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_preview_area())
        splitter.addWidget(self._build_tabs())
        splitter.setSizes([440, 640])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        root.addWidget(splitter, 1)

        # (c) Slim status row
        root.addLayout(self._build_status_row())

    def _build_toolbar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        self.btn_open = QPushButton("Open APK")
        self.btn_open.clicked.connect(self._on_open_clicked)
        row.addWidget(self.btn_open)

        self.combo_device = QComboBox()
        self.combo_device.setMinimumContentsLength(18)
        self.combo_device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo_device.setMinimumWidth(120)
        self.combo_device.addItem("Default Device")
        row.addWidget(self.combo_device)

        self.btn_search_device = QPushButton("Search...")
        self.btn_search_device.setToolTip(
            "Search all bundled Apple, Samsung and Google devices")
        row.addWidget(self.btn_search_device)

        self.btn_rotate = QPushButton("Rotate")
        row.addWidget(self.btn_rotate)

        self.btn_screenshot = QPushButton("Screenshot")
        row.addWidget(self.btn_screenshot)

        self.btn_reset = QPushButton("Reset size")
        row.addWidget(self.btn_reset)

        row.addStretch()

        self.btn_emulator = QPushButton("Start Emulator")
        self.btn_emulator.clicked.connect(self._on_toggle_emulator)
        row.addWidget(self.btn_emulator)

        return row

    def _build_preview_area(self) -> QWidget:
        container = QWidget()
        container.setMinimumWidth(420)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.preview_stack = QStackedWidget()
        self.drop_zone = DropZone()
        self.drop_zone.open_clicked.connect(self._on_open_clicked)
        self.drop_zone.apk_dropped.connect(self._on_apk_dropped)
        self.preview_stack.addWidget(self.drop_zone)
        self.apk_card = self._build_apk_card()
        self.preview_stack.addWidget(self.apk_card)
        # The live device screen lives in its own top-level DeviceWindow; the
        # left pane only ever shows the drop zone (0) or the APK card (1).
        layout.addWidget(self.preview_stack)

        return container

    def _build_apk_card(self) -> QWidget:
        card = QWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        layout.addStretch()

        self.card_icon = QLabel("\U0001F4E6")
        self.card_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.card_icon)

        self.card_name = QLabel("No APK loaded")
        self.card_name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.card_name)

        self.card_size = QLabel("")
        self.card_size.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.card_size)

        self.card_package = QLabel("")
        self.card_package.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.card_package)

        self.btn_change_apk = QPushButton("Change APK")
        self.btn_change_apk.clicked.connect(self._on_open_clicked)
        layout.addWidget(self.btn_change_apk, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch()

        return card

    def _build_tabs(self) -> QTabWidget:
        tabs = QTabWidget()
        tabs.setMinimumWidth(460)

        self.logcat = LogcatPanel()
        tabs.addTab(self.logcat, "Logcat")

        self.tab_controls = self._build_controls_tab()
        tabs.addTab(self.tab_controls, "Controls")

        self.tab_performance = QWidget()
        tabs.addTab(self.tab_performance, "Performance")

        return tabs

    def _build_controls_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        outer = QVBoxLayout(content)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(0)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        outer.addLayout(grid)
        outer.addStretch(1)

        def section(text: str, row: int):
            lbl = QLabel(text)
            grid.addWidget(lbl, row, 0,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        def add_button(text: str, row: int, col: int, span: int = 1,
                       attr: str = None) -> QPushButton:
            btn = QPushButton(text)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding,
                              QSizePolicy.Policy.Fixed)
            grid.addWidget(btn, row, col, 1, span)
            if attr:
                setattr(self, attr, btn)
            return btn

        # Row 0 - Emulator
        section("Emulator", 0)
        add_button("Start", 0, 1, attr="btn_emu_start")
        add_button("Stop", 0, 2, attr="btn_emu_stop")
        add_button("Restart", 0, 3, attr="btn_emu_restart")

        # Row 1 - Snapshot
        section("Snapshot", 1)
        add_button("Save", 1, 1, attr="btn_snap_save")
        add_button("Load", 1, 2, attr="btn_snap_load")

        # Rows 2-3 - App (two rows of three)
        section("App", 2)
        add_button("Install", 2, 1, attr="btn_app_install")
        add_button("Reinstall", 2, 2, attr="btn_app_reinstall")
        add_button("Launch", 2, 3, attr="btn_app_launch")
        add_button("Stop", 3, 1, attr="btn_app_stop")
        add_button("Uninstall", 3, 2, attr="btn_app_uninstall")
        add_button("Clear data", 3, 3, attr="btn_app_clear")

        # Row 4 - Device preset (own row) + search + fold toggle + note
        section("Device", 4)
        device_box = QWidget()
        device_layout = QVBoxLayout(device_box)
        device_layout.setContentsMargins(0, 0, 0, 0)
        device_layout.setSpacing(4)

        device_row = QHBoxLayout()
        device_row.setContentsMargins(0, 0, 0, 0)
        device_row.setSpacing(6)
        self.combo_preset = QComboBox()
        self.combo_preset.setMinimumWidth(120)
        self.combo_preset.addItem("Default Device")
        device_row.addWidget(self.combo_preset, 1)

        self.btn_pick_device = QPushButton("Search...")
        self.btn_pick_device.setToolTip(
            "Search all bundled Apple, Samsung and Google devices")
        self.btn_pick_device.setSizePolicy(QSizePolicy.Policy.Minimum,
                                           QSizePolicy.Policy.Fixed)
        device_row.addWidget(self.btn_pick_device)

        self.lbl_fold = QLabel("Screen")
        device_row.addWidget(self.lbl_fold)
        self.combo_fold = QComboBox()
        self.combo_fold.addItem("Inner", "inner")
        self.combo_fold.addItem("Cover", "outer")
        self.combo_fold.setMinimumWidth(80)
        device_row.addWidget(self.combo_fold)
        device_layout.addLayout(device_row)

        self.lbl_device_note = QLabel("")
        self.lbl_device_note.setWordWrap(True)
        device_layout.addWidget(self.lbl_device_note)
        grid.addWidget(device_box, 4, 1, 1, 5)

        # Row 5 - Device rotate + reset (one tight row, trailing stretch)
        rotate_row = QWidget()
        rotate_layout = QHBoxLayout(rotate_row)
        rotate_layout.setContentsMargins(0, 0, 0, 0)
        rotate_layout.setSpacing(6)
        for deg in (0, 90, 180, 270):
            btn = QPushButton(f"{deg}\u00b0")
            btn.setSizePolicy(QSizePolicy.Policy.Maximum,
                              QSizePolicy.Policy.Fixed)
            setattr(self, f"btn_rot_{deg}", btn)
            rotate_layout.addWidget(btn)
        self.btn_device_reset = QPushButton("Reset")
        # Minimum policy (not Maximum) keeps the sizeHint as a floor, so the
        # label can never be squeezed and clipped; the explicit minimum width
        # is the sizeHint width.
        self.btn_device_reset.setSizePolicy(QSizePolicy.Policy.Minimum,
                                            QSizePolicy.Policy.Fixed)
        self.btn_device_reset.setMinimumWidth(
            self.btn_device_reset.sizeHint().width())
        rotate_layout.addWidget(self.btn_device_reset)
        rotate_layout.addStretch(1)
        grid.addWidget(rotate_row, 5, 1, 1, 5)

        # Settings - one control per row (label in col 0, widget in col 1)
        section("Settings", 6)

        section("Display", 7)
        self.combo_display = QComboBox()
        self.combo_display.addItem("Native window", "native")
        self.combo_display.addItem("Embedded (experimental)", "embedded")
        self.combo_display.setMinimumWidth(120)
        grid.addWidget(self.combo_display, 7, 1)

        section("Auto reinstall", 8)
        self.chk_auto_reinstall = QCheckBox()
        self.chk_auto_reinstall.setSizePolicy(QSizePolicy.Policy.Fixed,
                                              QSizePolicy.Policy.Fixed)
        grid.addWidget(self.chk_auto_reinstall, 8, 1)

        section("Idle timeout", 9)
        self.spin_idle = QSpinBox()
        self.spin_idle.setRange(0, 120)
        self.spin_idle.setValue(10)
        self.spin_idle.setSpecialValueText("Disabled")
        self.spin_idle.setMinimumWidth(120)
        grid.addWidget(self.spin_idle, 9, 1)

        section("Quality", 10)
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(
            ["Low (2M)", "Medium (4M)", "High (8M)", "Max (16M)"])
        self.combo_quality.setCurrentIndex(2)
        self.combo_quality.setMinimumWidth(120)
        grid.addWidget(self.combo_quality, 10, 1)

        section("Max FPS", 11)
        self.combo_fps = QComboBox()
        self.combo_fps.addItems(["15 FPS", "24 FPS", "30 FPS", "60 FPS"])
        self.combo_fps.setCurrentText("30 FPS")
        self.combo_fps.setMinimumWidth(120)
        grid.addWidget(self.combo_fps, 11, 1)

        # Row 12 - Android version (selected system image) + chooser button
        section("Android version", 12)
        version_row = QWidget()
        version_layout = QHBoxLayout(version_row)
        version_layout.setContentsMargins(0, 0, 0, 0)
        version_layout.setSpacing(6)
        self.lbl_selected_android = QLabel("Not selected")
        version_layout.addWidget(self.lbl_selected_android)
        self.btn_android_version = QPushButton("Android version...")
        self.btn_android_version.setSizePolicy(QSizePolicy.Policy.Minimum,
                                               QSizePolicy.Policy.Fixed)
        version_layout.addWidget(self.btn_android_version)
        version_layout.addStretch(1)
        grid.addWidget(version_row, 12, 1, 1, 5)

        # Row 13 - Re-run the first-run setup wizard
        section("Setup", 13)
        setup_row = QWidget()
        setup_layout = QHBoxLayout(setup_row)
        setup_layout.setContentsMargins(0, 0, 0, 0)
        setup_layout.setSpacing(6)
        self.btn_rerun_setup = QPushButton("Re-run setup...")
        self.btn_rerun_setup.setToolTip(
            "Open the setup wizard again (SDK, licenses, components, device, "
            "Android version)")
        self.btn_rerun_setup.setSizePolicy(QSizePolicy.Policy.Minimum,
                                           QSizePolicy.Policy.Fixed)
        setup_layout.addWidget(self.btn_rerun_setup)
        setup_layout.addStretch(1)
        grid.addWidget(setup_row, 13, 1, 1, 5)

        # Row 14 - Performance mode
        section("Performance", 14)
        perf_row = QWidget()
        perf_layout = QHBoxLayout(perf_row)
        perf_layout.setContentsMargins(0, 0, 0, 0)
        perf_layout.setSpacing(6)
        self.combo_perf = QComboBox()
        self.combo_perf.addItems(["Auto", "Low", "Normal", "High"])
        # Map display text to setting values
        self.combo_perf.setItemData(0, "auto", role=Qt.ItemDataRole.EditRole)
        self.combo_perf.setItemData(1, "low", role=Qt.ItemDataRole.EditRole)
        self.combo_perf.setItemData(2, "normal", role=Qt.ItemDataRole.EditRole)
        self.combo_perf.setItemData(3, "high", role=Qt.ItemDataRole.EditRole)
        self.combo_perf.setCurrentText("Auto" if settings.performance_mode == "auto"
                                      else "Low" if settings.performance_mode in ("low", "low_spec")
                                      else "Normal" if settings.performance_mode == "normal"
                                      else "High")
        self.combo_perf.setMinimumWidth(120)
        perf_layout.addWidget(QLabel("Mode:"))
        perf_layout.addWidget(self.combo_perf)
        grid.addWidget(perf_row, 14, 1, 1, 5)

        # Row 15 - Low-power mode
        section("Battery", 15)
        lowpow_row = QWidget()
        lowpow_layout = QHBoxLayout(lowpow_row)
        lowpow_layout.setContentsMargins(0, 0, 0, 0)
        lowpow_layout.setSpacing(6)
        self.chk_low_power = QCheckBox("Low-power mode (battery saver)")
        self.chk_low_power.setChecked(settings.low_power_mode)
        self.chk_low_power.setMinimumWidth(180)
        lowpow_layout.addWidget(QLabel(":"))
        lowpow_layout.addWidget(self.chk_low_power)
        grid.addWidget(lowpow_row, 15, 1, 1, 5)

        # Let widget columns share the extra width evenly
        for c in range(1, 6):
            grid.setColumnStretch(c, 1)

        # The macOS style under-sizes button rows in QGridLayout (rows come out
        # ~4-6 px shorter than the widgets), which makes rows overlap. Pin each
        # row to its tallest widget's sizeHint height (never hard-coded) so rows
        # are always at least as tall as the widgets they contain.
        for r in range(grid.rowCount()):
            row_h = 0
            for c in range(grid.columnCount()):
                item = grid.itemAtPosition(r, c)
                if item is None:
                    continue
                widget = item.widget()
                if widget is not None:
                    row_h = max(row_h, widget.sizeHint().height())
                elif item.layout() is not None:
                    row_h = max(row_h, item.layout().sizeHint().height())
            if row_h:
                grid.setRowMinimumHeight(r, row_h)

        scroll.setWidget(content)
        return scroll

    def _build_status_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(16)

        self.lbl_emulator_state = QLabel("Emulator: stopped")
        row.addWidget(self.lbl_emulator_state)

        self.lbl_emulator_specs = QLabel("Emulator RAM/CPU: --")
        row.addWidget(self.lbl_emulator_specs)

        self.lbl_app_cpu = QLabel("App CPU: --")
        row.addWidget(self.lbl_app_cpu)

        self.lbl_disk = QLabel("Free disk: --")
        row.addWidget(self.lbl_disk)

        row.addStretch()

        self.lbl_mode = QLabel("Mode: Normal")
        row.addWidget(self.lbl_mode)

        # The first status-row label doubles as the general status/error line.
        self.lbl_status = self.lbl_emulator_state

        return row

    def _connect_signals(self):
        """Connect controls that are in scope for this step."""
        # Emulator
        self.btn_emu_start.clicked.connect(self._on_start_emulator)
        self.btn_emu_stop.clicked.connect(self._on_stop_emulator)
        self.btn_emu_restart.clicked.connect(self._on_restart_emulator)

        # App
        self.btn_app_install.clicked.connect(self._on_install_apk)
        self.btn_app_reinstall.clicked.connect(self._on_reinstall_apk)
        self.btn_app_launch.clicked.connect(self._on_launch_app)
        self.btn_app_stop.clicked.connect(self._on_stop_app)
        self.btn_app_uninstall.clicked.connect(self._on_uninstall_app)
        self.btn_app_clear.clicked.connect(self._on_clear_data)

        # Device preset (toolbar + Controls combos, kept in sync)
        self.combo_device.currentIndexChanged.connect(self._on_preset_combo_changed)
        self.combo_preset.currentIndexChanged.connect(self._on_preset_combo_changed)
        self.btn_search_device.clicked.connect(self._on_search_device)
        self.btn_pick_device.clicked.connect(self._on_search_device)
        self.combo_fold.currentIndexChanged.connect(self._on_fold_screen_changed)

        # Rotate
        self.btn_rotate.clicked.connect(self._on_rotate_cycle)
        self.btn_rot_0.clicked.connect(lambda: self._on_rotate(0))
        self.btn_rot_90.clicked.connect(lambda: self._on_rotate(1))
        self.btn_rot_180.clicked.connect(lambda: self._on_rotate(2))
        self.btn_rot_270.clicked.connect(lambda: self._on_rotate(3))

        # Reset screen + screenshot
        self.btn_reset.clicked.connect(self._on_reset_screen)
        self.btn_device_reset.clicked.connect(self._on_reset_screen)
        self.btn_screenshot.clicked.connect(self._on_screenshot)

        # Snapshots
        self.btn_snap_save.clicked.connect(self._on_save_snapshot)
        self.btn_snap_load.clicked.connect(self._on_load_snapshot)

        # Settings - save immediately on every change
        self.combo_display.currentIndexChanged.connect(self._on_display_mode_changed)
        self.chk_auto_reinstall.toggled.connect(self._on_settings_changed)
        self.spin_idle.valueChanged.connect(self._on_settings_changed)
        self.combo_quality.currentIndexChanged.connect(self._on_settings_changed)
        self.combo_fps.currentIndexChanged.connect(self._on_settings_changed)

        # Android version chooser
        self.btn_android_version.clicked.connect(self._on_open_android_versions)
        self.btn_rerun_setup.clicked.connect(self._on_rerun_setup)

        # Performance mode + low-power (save immediately)
        self.combo_perf.currentIndexChanged.connect(self._on_performance_changed)
        self.chk_low_power.toggled.connect(self._on_low_power_changed)

        # NOTE: the status-row timer is wired in __init__; nothing else is left.

    def _populate_presets(self):
        """Fill both combos with the bundled list grouped Brand > Category.

        Brand and category rows are non-selectable headers.  The first row is a
        "no preset" reset entry.  If the saved preset no longer exists (for
        example an old avdmanager key), the combo falls back to no preset.
        """
        current = settings.device_preset
        if current not in preset_manager.get_all_presets():
            current = ""

        for combo in (self.combo_device, self.combo_preset):
            combo.blockSignals(True)
            try:
                combo.clear()
                combo.addItem("No preset (emulator default)", "")
                for brand, categories in preset_manager.get_brand_groups():
                    combo.addItem(f"\u2014 {brand} \u2014", None)
                    self._make_header_non_selectable(combo,
                                                     combo.count() - 1)
                    for category, items in categories:
                        combo.addItem(f"   {category}", None)
                        self._make_header_non_selectable(combo,
                                                         combo.count() - 1)
                        for key, preset in items:
                            label = preset.name
                            if preset.rumored:
                                label += "  (rumored)"
                            elif preset.preview_only:
                                label += "  (viewport)"
                            combo.addItem(label, key)
                idx = combo.findData(current) if current else 0
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            finally:
                combo.blockSignals(False)

        self._update_fold_controls(settings.device_preset)

    @staticmethod
    def _make_header_non_selectable(combo, index: int):
        """Mark a combo row as a section header (not selectable)."""
        model = combo.model()
        item = model.item(index) if hasattr(model, "item") else None
        if item is not None:
            item.setFlags(Qt.ItemFlag.NoItemFlags)

    def _update_fold_controls(self, key: str):
        """Show the Inner / Cover toggle only for a selected foldable."""
        preset = preset_manager.get_preset(key) if key else None
        foldable = bool(preset is not None and preset.is_foldable)
        if hasattr(self, "combo_fold"):
            self.combo_fold.setVisible(foldable)
        if hasattr(self, "lbl_fold"):
            self.lbl_fold.setVisible(foldable)
        if foldable and hasattr(self, "combo_fold"):
            self.combo_fold.blockSignals(True)
            self.combo_fold.setCurrentIndex(
                1 if self._device_screen == "outer" else 0)
            self.combo_fold.blockSignals(False)
        if hasattr(self, "lbl_device_note"):
            self.lbl_device_note.setText(self._device_note(preset))

    @staticmethod
    def _device_note(preset) -> str:
        if preset is None:
            return ("Pick a bundled device. Apple entries are viewport/frame "
                    "previews only - the emulator cannot run iOS apps.")
        if preset.preview_only:
            return ("Apple viewport/frame preview only: the Android emulator "
                    "can use this resolution, but it cannot run iOS apps.")
        if preset.rumored:
            return ("Rumored/unconfirmed device - specifications may change. "
                    "Use Inner / Cover to switch screens.")
        if preset.is_foldable:
            return ("Foldable - use Inner / Cover to switch screens.")
        return (f"{preset.brand} {preset.category} - {preset.size_str} @ "
                f"{preset.density} dpi - {preset.cutout_label}")

    def _on_search_device(self):
        """Open the searchable Brand > Category device picker."""
        key = pick_device(self, settings.device_preset)
        if not key:
            return
        for combo in (self.combo_device, self.combo_preset):
            idx = combo.findData(key)
            if idx >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)
        self._on_preset_selected(key)

    def _sync_preset_combos(self, key: str, source):
        """Mirror the selected preset into the other combo without re-firing."""
        for combo in (self.combo_device, self.combo_preset):
            if combo is source:
                continue
            idx = combo.findData(key)
            if idx >= 0 and combo.currentIndex() != idx:
                combo.blockSignals(True)
                combo.setCurrentIndex(idx)
                combo.blockSignals(False)

    @pyqtSlot(int)
    def _on_preset_combo_changed(self, _index: int):
        source = self.sender()
        if source is None:
            return
        key = source.currentData()
        if not key:
            return
        self._sync_preset_combos(key, source)
        self._on_preset_selected(key)

    @pyqtSlot(str)
    def _on_preset_selected(self, key: str):
        """Select a device; apply its viewport when the emulator is running."""
        preset = preset_manager.get_preset(key)
        if preset is None:
            return

        settings.device_preset = key
        self._update_fold_controls(key)
        screen = self._device_screen if preset.is_foldable else "inner"

        if not self._emulator.is_running():
            note = " - viewport preview only" if preset.preview_only else ""
            self._set_status(
                f"Selected {preset.brand} {preset.name}{note}; "
                f"start the emulator to apply")
            return

        self._set_status(f"Applying {preset.name}...")
        self._run_adb(
            lambda: preset_manager.apply_preset(self._adb, key, screen),
            on_done=lambda ok, msg: self._on_preset_applied(
                key, screen, ok, msg))

    @pyqtSlot(str, str, bool, str)
    def _on_preset_applied(self, key: str, screen: str, success: bool,
                           message: str):
        preset = preset_manager.get_preset(key)
        if success and preset is not None:
            settings.device_preset = key
            spec = preset.screen(screen)
            suffix = ""
            if preset.preview_only:
                suffix = " (viewport preview only - no iOS apps)"
            elif preset.is_foldable:
                suffix = f" ({screen} screen)"
            self._set_status(
                f"Applied {preset.brand} {preset.name}: "
                f"{spec.size_str} @ {spec.density} dpi{suffix}")
        else:
            self._set_status(message)

    @pyqtSlot(int)
    def _on_fold_screen_changed(self, index: int):
        """Switch a foldable between its inner and cover screens."""
        self._device_screen = "outer" if index == 1 else "inner"
        key = settings.device_preset
        preset = preset_manager.get_preset(key)
        if preset is None or not preset.is_foldable:
            return
        screen = self._device_screen
        if not self._emulator.is_running():
            spec = preset.screen(screen)
            self._set_status(
                f"{preset.name}: {screen} screen selected "
                f"({spec.size_str}); start the emulator to apply")
            return
        self._set_status(f"Switching {preset.name} to {screen} screen...")
        self._run_adb(
            lambda: preset_manager.apply_preset(self._adb, key, screen),
            on_done=lambda ok, msg: self._on_preset_applied(
                key, screen, ok, msg))

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------
    def _profile_mode(self):
        """Detect performance mode once (not the status-row timer)."""
        try:
            mode = self._profiler.get_mode()
        except Exception:
            mode = PerformanceMode.NORMAL
        # Respect explicitly-set overrides from the UI.
        if settings.performance_mode != "auto":
            mode = PerformanceMode.from_setting(settings.performance_mode) or mode
        if settings.low_power_mode:
            mode = PerformanceMode.LOW_SPEC
        self._mode = mode
        self.lbl_mode.setText(f"Mode: {mode.label}")

    def _on_performance_changed(self, index: int):
        """User changed the performance mode combo."""
        values = ["auto", "low", "normal", "high"]
        choice = values[index] if 0 <= index < len(values) else "auto"
        settings.performance_mode = choice
        # Update the profiler and UI immediately.
        try:
            mode = PerformanceMode.from_setting(choice) or self._profiler.get_mode()
        except Exception:
            mode = PerformanceMode.NORMAL
        self._mode = mode
        self.lbl_mode.setText(f"Mode: {mode.label}")
        self._set_status(f"Performance mode set to {self._mode.label}")

    def _on_low_power_changed(self, checked: bool):
        """User toggled the low-power mode checkbox."""
        settings.low_power_mode = checked
        # Immediately re-apply the mode (may switch to LOW_SPEC).
        self._profile_mode()
        self._set_status(f"Low-power mode {'enabled' if checked else 'disabled'}")

    def _set_status(self, message: str):
        """Show a status/error message in the status row (never raise)."""
        if not message:
            return
        self.lbl_status.setText(message)

    def _update_emulator_buttons(self, running: bool):
        self.btn_emu_start.setEnabled(not running)
        self.btn_emu_stop.setEnabled(running)
        self.btn_emu_restart.setEnabled(running)
        # Snapshot buttons stay clickable so their handler can report
        # "Start the emulator first" in the status row.
        self.btn_emulator.setText("Stop Emulator" if running else "Start Emulator")
        # The floating device window's controls follow the emulator state.
        try:
            if self.device_window is not None:
                self.device_window.set_controls_enabled(running)
        except Exception:
            pass

    def _update_app_buttons(self, installed: bool):
        """Enable app buttons: install/reinstall/launch need a loaded APK;
        stop/uninstall/clear need an installed app."""
        apk_loaded = self._current_apk is not None
        self.btn_app_install.setEnabled(apk_loaded)
        self.btn_app_reinstall.setEnabled(apk_loaded)
        self.btn_app_launch.setEnabled(apk_loaded)
        self.btn_app_stop.setEnabled(installed)
        self.btn_app_uninstall.setEnabled(installed)
        self.btn_app_clear.setEnabled(installed)

    # ------------------------------------------------------------------
    # Drag and drop
    # ------------------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].toLocalFile().lower().endswith('.apk'):
                event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1:
                path = Path(urls[0].toLocalFile())
                if path.suffix.lower() == '.apk':
                    self._on_apk_dropped(path)
                    event.acceptProposedAction()

    def closeEvent(self, event):
        self._cleanup()
        event.accept()

    def _cleanup(self):
        # Stop only the things this window owns. We deliberately do NOT kill the
        # user's global adb server or sweep unrelated child processes here.
        try:
            self._status_timer.stop()
        except Exception:
            pass
        try:
            self._frame_timer.stop()
        except Exception:
            pass
        try:
            self._file_watcher.stop()
        except Exception:
            pass
        workers = [self._emu_worker, self._install_worker, self._parse_worker]
        workers.extend(self._adb_workers)
        for worker in workers:
            try:
                if worker is not None and worker.isRunning():
                    worker.wait(2000)
            except Exception:
                pass
        try:
            self.logcat.stop()
        except Exception:
            pass
        # Closing the main window closes the floating device window (and saves
        # its geometry) and therefore stops the stream.
        try:
            if self.device_window is not None:
                self.device_window.save_geometry()
                self.device_window.close()
        except Exception:
            pass
        try:
            self._screen.stop()
        except Exception:
            pass
        try:
            self._scrcpy.stop()
        except Exception:
            pass
        try:
            self._emulator.stop()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # APK loading (parse on a worker thread)
    # ------------------------------------------------------------------
    @pyqtSlot(Path)
    def _on_apk_dropped(self, path: Path):
        self._load_apk(path)

    @pyqtSlot()
    def _on_open_clicked(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open APK", "", "Android APK (*.apk)")
        if path:
            self._load_apk(Path(path))

    def _load_apk(self, apk_path: Path):
        apk_path = Path(apk_path)
        if not apk_path.exists():
            self._set_status("Error: file not found")
            return
        self._set_status(f"Loading {apk_path.name}...")
        self._parse_worker = APKParseWorker(apk_path)
        self._parse_worker.parsed.connect(self._on_apk_parsed)
        self._parse_worker.start()

    @pyqtSlot(object, object)
    def _on_apk_parsed(self, apk_path: Path, info: dict):
        if not info or not info.get("valid"):
            error = (info or {}).get("error", "Unknown error")
            self._set_status(f"Error: not a valid APK ({error})")
            return

        package = info.get("package")
        activity = info.get("main_activity")

        self._current_apk = apk_path
        self._current_package = None
        self._current_activity = activity
        self._update_apk_card(apk_path, package)
        self._update_app_buttons(False)
        try:
            settings.last_apk_path = apk_path
        except Exception:
            pass
        # Watch the loaded APK for changes (reinstall if auto-reinstall is on)
        try:
            self._file_watcher.set_auto_reinstall(
                self.chk_auto_reinstall.isChecked())
            self._file_watcher.watch(apk_path)
        except Exception:
            pass
        self._set_status(f"Loaded {apk_path.name}")

    def _update_apk_card(self, path: Path, package: Optional[str]):
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError:
            size_mb = 0.0
        self.card_name.setText(path.name)
        self.card_size.setText(f"{size_mb:.1f} MB")
        self.card_package.setText(package or "unknown package")
        self.preview_stack.setCurrentIndex(1)

    # ------------------------------------------------------------------
    # App handlers
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_install_apk(self):
        self._run_install()

    @pyqtSlot()
    def _on_reinstall_apk(self):
        self._run_install()

    def _run_install(self):
        if not self._current_apk or not self._current_apk.exists():
            self._set_status("Error: no APK loaded")
            return
        if not settings.has_selected_version:
            self._set_status(
                "Select an Android version first (Controls > Android version)")
            return
        if self._install_worker is not None and self._install_worker.isRunning():
            self._set_status("Install already in progress...")
            return

        scrcpy_available = self._scrcpy.is_available()
        self._set_status(f"Installing {self._current_apk.name}...")

        # Native mode starts the emulator with its own window and does not open
        # a mirror; embedded mode is headless and renders the device in-app.
        native = self._is_native_display()
        self._install_worker = APKInstallWorker(
            self._current_apk, self._emulator, self._adb, self._scrcpy,
            self._mode, scrcpy_available=scrcpy_available,
            no_window=not native, open_window=native, start_mirror=not native)
        self._install_worker.progress.connect(self._on_install_progress)
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.log.connect(
            lambda msg: self.logcat._append_log(msg, "verbose"))
        self._install_worker.start()

    @pyqtSlot(str)
    def _on_install_progress(self, message: str):
        self._set_status(message)

    @pyqtSlot(bool, str, str, str)
    def _on_install_finished(self, success: bool, message: str, package: str, activity: str):
        self._set_status(message)
        if success:
            self._current_package = package
            if activity:
                self._current_activity = activity
            self._adb.serial = "emulator-5554"
            self._update_app_buttons(True)
            try:
                self.logcat.start(package)
            except Exception:
                pass
            # The install worker starts + boots the emulator, so it is ready now.
            self._auto_start_screen()
        else:
            # keep the loaded APK so the user can retry; only reflect not-installed
            self._update_app_buttons(False)

    @pyqtSlot()
    def _on_launch_app(self):
        if not self._current_package:
            self._set_status("Error: app not installed")
            return
        activity = self._current_activity
        package = self._current_package
        if activity:
            fn = lambda: self._adb.start_activity(package, activity)
        else:
            fn = lambda: self._adb.shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
        self._set_status(f"Launching {package}...")
        self._run_adb(fn)

    @pyqtSlot()
    def _on_stop_app(self):
        if not self._current_package:
            return
        package = self._current_package
        self._set_status(f"Stopping {package}...")
        self._run_adb(lambda: self._adb.force_stop(package))

    @pyqtSlot()
    def _on_uninstall_app(self):
        if not self._current_package:
            return
        package = self._current_package
        reply = QMessageBox.question(
            self, "Uninstall", f"Uninstall {package}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"Uninstalling {package}...")

        def do_uninstall():
            ok, msg = self._adb.uninstall(package)
            return ok, msg

        self._run_adb(do_uninstall, on_done=self._on_uninstall_done)

    @pyqtSlot(bool, str)
    def _on_uninstall_done(self, success: bool, message: str):
        self._set_status(message)
        if success:
            self._current_package = None
            self._update_app_buttons(False)

    @pyqtSlot()
    def _on_clear_data(self):
        if not self._current_package:
            return
        package = self._current_package
        self._set_status(f"Clearing data for {package}...")
        self._run_adb(lambda: self._adb.clear_data(package))

    def _run_adb(self, fn: Callable[[], Tuple[bool, str]], on_done=None):
        """Run an adb callable on a worker thread; keep a strong reference."""
        worker = AdbActionWorker(fn)
        worker.done.connect(on_done or self._on_adb_action_done)
        self._adb_workers.append(worker)

        def _drop():
            if worker in self._adb_workers:
                self._adb_workers.remove(worker)

        worker.finished.connect(_drop)
        worker.start()

    @pyqtSlot(bool, str)
    def _on_adb_action_done(self, success: bool, message: str):
        self._set_status(message)

    # ------------------------------------------------------------------
    # Device preset / rotate / reset / screenshot  (copied from old window)
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_rotate_cycle(self):
        """Toolbar Rotate: cycle 0 -> 90 -> 180 -> 270."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._rotation_index = (self._rotation_index + 1) % 4
        self._on_rotate(self._rotation_index)

    @pyqtSlot(int)
    def _on_rotate(self, rotation: int):
        """Set a rotation directly (0, 1, 2, 3 == 0, 90, 180, 270)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._rotation_index = rotation
        self._run_adb(
            lambda: self._adb.set_rotation(rotation),
            on_done=lambda ok, msg: self._set_status(
                f"Rotation set to {rotation * 90}\u00b0" if ok else msg))

    @pyqtSlot()
    def _on_reset_screen(self):
        """Restore default size, density and rotation (wm resets + rotation 0)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return

        self._rotation_index = 0
        self._set_status("Resetting screen to default...")

        def do_reset():
            ok_size, msg_size = self._adb.reset_screen_size()
            ok_density, msg_density = self._adb.reset_density()
            ok_rot, msg_rot = self._adb.set_rotation(0)
            if not (ok_size and ok_density and ok_rot):
                return False, f"Reset failed: {msg_size}; {msg_density}; {msg_rot}"
            return True, "Screen reset to default (size, density, rotation)"

        self._run_adb(do_reset)

    @pyqtSlot()
    def _on_screenshot(self):
        """Save a screenshot to ~/Pictures/ApkPreview (copied from old window)."""
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return

        pictures_dir = Path.home() / "Pictures" / "ApkPreview"
        try:
            pictures_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._set_status(f"Screenshot failed: {e}")
            return

        from datetime import datetime
        # Mac local time (NOT the device clock) for the filename.
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = pictures_dir / f"screenshot_{timestamp}.png"
        remote_path = f"/sdcard/screenshot_{timestamp}.png"

        def do_screenshot():
            ok, msg = self._adb.screenshot(remote_path)
            if not ok:
                return False, f"Screenshot failed: {msg}"
            ok, msg = self._adb.pull(remote_path, local_path)
            if not ok:
                return False, f"Pull failed: {msg}"
            self._adb.shell(f"rm {remote_path}")
            return True, f"Screenshot saved to {local_path}"

        self._set_status("Taking screenshot...")
        self._run_adb(do_screenshot)

    # ------------------------------------------------------------------
    # Embedded screen (Stage 2a - view only; no touch / keyboard)
    # ------------------------------------------------------------------
    # adb key events for the floating device window's control bar.
    _DEVICE_KEYCODES = {
        "back": "KEYCODE_BACK",
        "home": "KEYCODE_HOME",
        "recents": "KEYCODE_APP_SWITCH",
        "vol_up": "KEYCODE_VOLUME_UP",
        "vol_down": "KEYCODE_VOLUME_DOWN",
        "power": "KEYCODE_POWER",
    }

    def _screen_status(self, message: str):
        """Echo a screen-stream stage to stdout and the status row."""
        if not message:
            return
        try:
            print(f"[screen] {message}", flush=True)
        except Exception:
            pass
        self._set_status(message)

    def _show_device_window(self):
        """Show (and focus) the floating device window."""
        try:
            self.device_window.set_controls_enabled(self._emulator.is_running())
            if not self.device_window.isVisible():
                self.device_window.show()
            self.device_window.raise_()
            self.device_window.activateWindow()
        except Exception:
            pass

    def _start_screen_stream(self):
        # Called by the embedded-mode auto-start once a device is ready.
        self._screen_status("Checking emulator...")
        if not self._emulator.is_running():
            # Same guard as the other device actions: the stream needs a device.
            self._screen_status("Start the emulator first")
            return

        # Open the floating window immediately, before the worker starts.
        self.screen_view.clear()
        self._show_device_window()
        self._last_frame_seq = -1
        self._frame_timer.start()

        max_fps = max(1, min(int(settings.scrcpy_max_fps), 30))
        max_size = max(0, int(settings.scrcpy_max_size))
        try:
            self._screen.start(
                self._adb.serial,
                max_size=max_size,
                max_fps=max_fps,
                bitrate=settings.scrcpy_bitrate)
        except Exception as e:  # noqa: BLE001
            self._screen_status(f"Error: {e}")
            self._frame_timer.stop()

    def _stop_screen_stream(self, quiet: bool = False):
        try:
            self._frame_timer.stop()
        except Exception:
            pass
        try:
            self._screen.stop()
        except Exception:
            pass
        try:
            if self.device_window is not None:
                self.device_window.hide()
        except Exception:
            pass
        if not quiet:
            self._screen_status("Screen stopped")

    def _auto_start_screen(self):
        """Open the device window and start the stream once the device is ready.

        Native mode shows the device in the emulator's own window, so it never
        opens the device window or starts the stream.
        """
        if self._is_native_display():
            return
        if not self._emulator.is_running():
            return
        if self._screen.is_running():
            self._show_device_window()
            return
        self._start_screen_stream()

    @pyqtSlot(str)
    def _on_device_window_action(self, action: str):
        """Dispatch a floating-window control button press."""
        if action == "rotate":
            self._on_rotate_cycle()
            return
        if action == "screenshot":
            self._on_screenshot()
            return
        code = self._DEVICE_KEYCODES.get(action)
        if not code:
            return
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        label = action.replace("_", " ").title()
        self._set_status(f"Sending {action.replace('_', ' ')}...")
        self._run_adb(
            lambda: self._adb.input_keyevent(code),
            on_done=lambda ok, msg: self._set_status(
                f"{label} sent" if ok else msg))

    @pyqtSlot()
    def _on_device_window_closed(self):
        """The floating window was closed: stop the stream."""
        if self._screen.is_running():
            self._stop_screen_stream(quiet=True)
        else:
            try:
                self._frame_timer.stop()
            except Exception:
                pass

    @pyqtSlot()
    def _on_frame_tick(self):
        """Pull the latest decoded frame (~30 Hz) and paint it once."""
        try:
            image, seq = self._screen.get_latest_frame()
        except Exception:
            return
        if image is None or seq == self._last_frame_seq:
            return
        self._last_frame_seq = seq
        try:
            self.screen_view.set_frame(image)
        except Exception:
            pass

    @pyqtSlot(float)
    def _on_screen_painted(self, elapsed_ms: float):
        """Record paint time for the stream's 5-second stats line."""
        try:
            self._screen.note_painted(elapsed_ms)
        except Exception:
            pass

    @pyqtSlot(str)
    def _on_screen_status(self, message: str):
        # Stage message from the stream worker (core already printed it).
        if message:
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_note(self, message: str):
        try:
            self.screen_view.set_note(message)
        except Exception:
            pass
        if message:
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_error(self, message: str):
        if message:
            self._set_status(message)

    @pyqtSlot(str)
    def _on_screen_state(self, state: str):
        if state == "streaming":
            self._set_status("Screen streaming")
        elif state == "fallback":
            self._set_status("scrcpy unavailable - using screencap fallback")
        elif state == "stopped":
            # Stream ended on its own; close the floating window so the
            # rendered view never disagrees with the stream state.
            try:
                self._frame_timer.stop()
            except Exception:
                pass
            try:
                if self.device_window is not None:
                    self.device_window.hide()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Snapshots  (copied from old window; blocking adb on a worker)
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_save_snapshot(self):
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._set_status("Saving snapshot...")
        self._run_adb(lambda: self._emulator.save_snapshot("user_snapshot"))

    @pyqtSlot()
    def _on_load_snapshot(self):
        if not self._emulator.is_running():
            self._set_status("Start the emulator first")
            return
        self._set_status("Loading snapshot...")
        self._run_adb(lambda: self._emulator.load_snapshot("user_snapshot"))

    # ------------------------------------------------------------------
    # Settings (load from core.settings, save immediately on change)
    # ------------------------------------------------------------------
    def _load_controls_settings(self):
        """Load persisted values into the Controls-tab widgets (no writes)."""
        for w in (self.combo_display, self.chk_auto_reinstall, self.spin_idle,
                  self.combo_quality, self.combo_fps):
            w.blockSignals(True)

        display_idx = self.combo_display.findData(settings.display_mode)
        if display_idx >= 0:
            self.combo_display.setCurrentIndex(display_idx)
        self.chk_auto_reinstall.setChecked(settings.auto_reinstall)
        self.spin_idle.setValue(settings.idle_timeout_minutes)

        bitrate_map = {"2M": 0, "4M": 1, "8M": 2, "16M": 3}
        self.combo_quality.setCurrentIndex(
            bitrate_map.get(settings.scrcpy_bitrate, 2))

        fps_map = {15: 0, 24: 1, 30: 2, 60: 3}
        self.combo_fps.setCurrentIndex(
            fps_map.get(settings.scrcpy_max_fps, 2))

        for w in (self.combo_display, self.chk_auto_reinstall, self.spin_idle,
                  self.combo_quality, self.combo_fps):
            w.blockSignals(False)

        self._file_watcher.set_auto_reinstall(
            self.chk_auto_reinstall.isChecked())

    @pyqtSlot()
    def _on_settings_changed(self):
        """Persist all four settings and apply to the live subsystems."""
        auto_reinstall = self.chk_auto_reinstall.isChecked()
        idle_timeout = self.spin_idle.value()
        bitrate = ["2M", "4M", "8M", "16M"][self.combo_quality.currentIndex()]
        fps_text = self.combo_fps.currentText()
        try:
            max_fps = int(fps_text.split()[0])
        except (ValueError, IndexError):
            max_fps = 30

        settings.auto_reinstall = auto_reinstall
        settings.idle_timeout_minutes = idle_timeout
        settings.scrcpy_bitrate = bitrate
        settings.scrcpy_max_fps = max_fps

        self._file_watcher.set_auto_reinstall(auto_reinstall)
        self._scrcpy.set_bitrate(bitrate)
        self._scrcpy.set_max_fps(max_fps)

        self._set_status("Settings saved")

    # ------------------------------------------------------------------
    # Display mode (native emulator window vs. embedded scrcpy view)
    # ------------------------------------------------------------------
    def _is_native_display(self) -> bool:
        """True when the device is shown in the emulator's own window."""
        try:
            return settings.display_mode != "embedded"
        except Exception:
            return True

    @pyqtSlot(int)
    def _on_display_mode_changed(self, _index: int):
        """Persist the display mode immediately and apply what we can live."""
        mode = self.combo_display.currentData() or "native"
        settings.display_mode = mode
        if self._emulator.is_running():
            self._set_status("Restart the emulator to apply")
        self._apply_display_mode()

    def _apply_display_mode(self):
        """Apply the current display mode to the embedded screen.

        Native mode has no use for the in-app screen, so any running stream is
        stopped and the device window hidden.  Embedded mode leaves the
        auto-start path free to open it.  A running emulator keeps its current
        windowing until it is restarted (start/restart read the mode fresh).
        """
        if self._is_native_display():
            if self._screen.is_running():
                self._stop_screen_stream(quiet=True)
            else:
                try:
                    if self.device_window is not None:
                        self.device_window.hide()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Android version (system image) selection
    # ------------------------------------------------------------------
    def _migrate_legacy_android_version(self):
        """Auto-select an existing ``apkpreview_default`` AVD on first run.

        Keeps a pre-upgrade install working (and listed as an installed
        version) without deleting or re-downloading anything.
        """
        try:
            if settings.has_selected_version:
                return
            legacy = self._sdk.find_legacy_avd()
            if legacy is None:
                return
            avd_name, version = legacy
            settings.select_android_version(
                version.api_level, version.variant, avd_name)
        except Exception:
            pass

    def _refresh_selected_android_label(self):
        """Show the selected version in the Controls tab."""
        try:
            api = settings.selected_api_level
            variant = settings.selected_variant
            if api > 0:
                name = ANDROID_VERSION_NAMES.get(api, f"Android {api}")
                self.lbl_selected_android.setText(
                    f"{name} (API {api}, {variant or 'default'})")
            else:
                self.lbl_selected_android.setText("Not selected")
        except Exception:
            pass

    @pyqtSlot()
    def _on_open_android_versions(self):
        """Open the Android versions dialog (list/download/remove/select)."""
        try:
            dialog = AndroidVersionsDialog(self._sdk, self,
                                           emulator=self._emulator)
        except Exception as e:
            self._set_status(f"Could not open Android versions: {e}")
            return
        dialog.version_selected.connect(self._on_android_version_selected)
        dialog.exec()
        # A version may have been removed without a selection change.
        self._refresh_selected_android_label()

    @pyqtSlot()
    def _on_rerun_setup(self):
        """Open the resumable setup wizard again."""
        from .setup_wizard import SetupWizard
        wizard = SetupWizard(self, force=True)
        if wizard.exec():
            self.refresh_after_setup()
            self._set_status("Setup completed")

    def refresh_after_setup(self):
        """Re-read the SDK/ADB paths and selected version after the wizard."""
        self._sdk = SDKManager()
        self._adb = ADB()
        self._migrate_legacy_android_version()
        self._refresh_selected_android_label()
        self._populate_presets()
        self._update_emulator_buttons(self._emulator.is_running())

    @pyqtSlot(int, str)
    def _on_android_version_selected(self, api_level: int, variant: str):
        """React to a version being selected in the dialog."""
        self._refresh_selected_android_label()
        name = ANDROID_VERSION_NAMES.get(api_level, f"Android {api_level}")
        if self._emulator.is_running():
            self._set_status("Stop the emulator to switch version")
        else:
            self._set_status(
                f"Android version set to {name} (API {api_level}, "
                f"{variant or 'default'})")

    # ------------------------------------------------------------------
    # File watcher -> auto reinstall (bridge onto the UI thread)
    # ------------------------------------------------------------------
    def _on_file_changed(self, path: Path):
        """Called from the watcher thread; forward to the UI thread."""
        try:
            self._bridge.apk_changed.emit(path)
        except Exception:
            pass

    @pyqtSlot(object)
    def _on_apk_changed(self, path: Path):
        self._set_status(f"APK changed: {Path(path).name}")
        if self.chk_auto_reinstall.isChecked():
            self._run_install()

    # ------------------------------------------------------------------
    # Status-row sampler (QTimer, every 2 s) - cheap psutil reads.
    # Never overwrites the first label (general status/error line).
    # ------------------------------------------------------------------
    def _update_status_row(self):
        # --- Emulator RAM/CPU -----------------------------------------------
        # Do not rely on the tracked Popen pid alone. Sum RSS and cpu_percent
        # over the tracked process AND all of its descendants (recursive), AND
        # any process whose name contains "qemu-system".
        #
        # psutil.Process objects are cached across ticks and cpu_percent(None)
        # is primed on the first (startup) call: a *fresh* Process object
        # always reports 0.0, so reusing instances is what makes later reads
        # show a real percentage.
        try:
            cache = getattr(self, "_emu_proc_cache", None)
            if cache is None:
                cache = {}
                self._emu_proc_cache = cache

            procs = {}

            def _track(proc):
                try:
                    procs.setdefault(proc.pid, proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Tracked process + all of its children (recursive).
            if self._emulator.is_running() and self._emulator.get_pid():
                tracked_pid = self._emulator.get_pid()
                try:
                    root = cache.get(tracked_pid) or psutil.Process(tracked_pid)
                    cache[tracked_pid] = root
                    _track(root)
                    for child in root.children(recursive=True):
                        cached = cache.get(child.pid) or child
                        cache[child.pid] = cached
                        _track(cached)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            # Any process whose name contains "qemu-system".
            for proc in psutil.process_iter(["name"]):
                try:
                    name = proc.info.get("name") or ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if "qemu-system" in name.lower():
                    cached = cache.get(proc.pid) or proc
                    cache[proc.pid] = cached
                    _track(cached)

            total_rss = 0
            total_cpu = 0.0
            found = []
            for proc in list(procs.values()):
                try:
                    rss = proc.memory_info().rss
                    cpu = proc.cpu_percent(None)  # 0.0 on the priming call
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    cache.pop(proc.pid, None)
                    continue
                total_rss += rss
                total_cpu += cpu
                found.append((proc.pid, rss, cpu))

            if found:
                self.lbl_emulator_specs.setText(
                    f"Emulator {total_rss / (1024 ** 3):.1f} GB / "
                    f"{int(round(total_cpu))}%")
            else:
                self.lbl_emulator_specs.setText("Emulator --")
        except Exception:
            self.lbl_emulator_specs.setText("Emulator --")

        # --- App CPU --------------------------------------------------------
        try:
            app_cache = getattr(self, "_app_proc_cache", None)
            if app_cache is None:
                app_cache = {}
                self._app_proc_cache = app_cache

            # Prime the CPU counter on first call
            proc = psutil.Process(os.getpid())
            app_cache[os.getpid()] = proc

            app_proc = app_cache.get(os.getpid())
            if app_proc is not None:
                app_cpu = app_proc.cpu_percent(None)  # instant percentage
                self.lbl_app_cpu.setText(f"App CPU: {int(round(app_cpu))}%")
            else:
                self.lbl_app_cpu.setText("App CPU: --")
        except Exception:
            self.lbl_app_cpu.setText("App CPU: --")

        try:
            sdk_dir = PlatformUtils.get_sdk_dir()
            usage = psutil.disk_usage(str(sdk_dir.parent))
            free_gb = int(usage.free / (1024 ** 3))
            self.lbl_disk.setText(f"Disk {free_gb} GB free")
        except Exception:
            self.lbl_disk.setText("Disk --")

        self.lbl_mode.setText(
            f"Mode: {self._mode.value.replace('_', ' ').title()}")

    # ------------------------------------------------------------------
    # Emulator handlers (blocking work on EmulatorWorker)
    # ------------------------------------------------------------------
    @pyqtSlot()
    def _on_start_emulator(self):
        self._set_status("Starting emulator...")
        self._start_emulator_action("start")

    @pyqtSlot()
    def _on_stop_emulator(self):
        self._set_status("Stopping emulator...")
        self._start_emulator_action("stop")

    @pyqtSlot()
    def _on_restart_emulator(self):
        self._set_status("Restarting emulator...")
        self._start_emulator_action("restart")

    @pyqtSlot()
    def _on_toggle_emulator(self):
        if self._emulator.is_running():
            self._on_stop_emulator()
        else:
            self._on_start_emulator()

    def _start_emulator_action(self, action: str):
        if not settings.has_selected_version:
            self._set_status(
                "Select an Android version first (Controls > Android version)")
            return
        if self._emu_worker is not None and self._emu_worker.isRunning():
            self._set_status("Emulator action already in progress...")
            return
        scrcpy_available = self._scrcpy.is_available()
        # Native mode opens the emulator's own window; embedded mode renders the
        # device in-app, so the emulator is started headless (-no-window).
        native = self._is_native_display()
        self._emu_worker = EmulatorWorker(
            action, self._emulator, self._scrcpy, self._mode, scrcpy_available,
            no_window=not native, open_window=native)
        self._emu_worker.result.connect(self._on_emulator_action_done)
        self._emu_worker.start()

    @pyqtSlot(str, bool, str)
    def _on_emulator_action_done(self, action: str, success: bool, message: str):
        if action in ("stop", "restart"):
            # Emulator stopping/restarting: close the device window and stream.
            self._stop_screen_stream(quiet=True)
        running = self._emulator.is_running()
        self._update_emulator_buttons(running)
        if success:
            self._set_status(message or "Emulator ready")
        else:
            self._set_status(f"Error: {message}")
        if success and action in ("start", "restart"):
            try:
                self.logcat.start(self._current_package or "")
            except Exception:
                pass
            # Emulator is ready: open the device window and start streaming.
            self._auto_start_screen()
