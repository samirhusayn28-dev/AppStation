"""Main window for ApkPreview - ties together all UI components."""

import os
import sys
import time
import threading
from pathlib import Path
from typing import Optional
from PyQt6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, 
                             QSplitter, QLabel, QMessageBox, QFileDialog,
                             QApplication, QMenuBar, QStatusBar, QToolBar)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QSettings, QThread, pyqtSlot
from PyQt6.QtGui import QAction, QIcon, QFont, QDragEnterEvent, QDropEvent

from .drop_zone import DropZone
from .controls_panel import ControlsPanel
from .logcat_panel import LogcatPanel
from .resource_meter import ResourceMeter
from .setup_wizard import SetupWizard
from core.platform_utils import PlatformUtils
from core.hardware_profile import HardwareProfiler, PerformanceMode
from core.sdk_manager import SDKManager
from core.emulator import EmulatorManager, EmulatorMonitor
from core.adb import ADB, detect_package_and_activity
from core.scrcpy import ScrcpyManager
from core.file_watcher import create_file_watcher
from core.device_presets import preset_manager
from core.apk_parser import get_apk_info
from core.settings import settings


class APKInstallWorker(QThread):
    """Background worker for APK install and launch."""
    
    progress = pyqtSignal(str)
    finished = pyqtSignal(bool, str, str, str)  # success, message, package, activity
    log = pyqtSignal(str)
    
    def __init__(self, apk_path: Path, emulator: EmulatorManager, adb: ADB, 
                 scrcpy: ScrcpyManager, mode: PerformanceMode, scrcpy_available: bool = True):
        super().__init__()
        self._apk_path = apk_path
        self._emulator = emulator
        self._adb = adb
        self._scrcpy = scrcpy
        self._mode = mode
        self._scrcpy_available = scrcpy_available
        self._cancelled = False
    
    def cancel(self):
        self._cancelled = True
    
    def run(self):
        try:
            # Step 1: Start emulator if not running
            if not self._emulator.is_running():
                self.progress.emit("Starting emulator...")
                self.log.emit("Starting emulator...")
                success, msg = self._emulator.start(self._mode, wait_for_boot=True, timeout=180, scrcpy_available=self._scrcpy_available)
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
            
            # Step 6: Start scrcpy if available
            if self._scrcpy_available:
                self.progress.emit("Starting screen mirror...")
                self.log.emit("Starting screen mirror...")
                low_spec = (self._mode == PerformanceMode.LOW_SPEC)
                self._scrcpy.start(low_spec=low_spec)
            else:
                self.log.emit("scrcpy not available, using emulator window")
            
            self.finished.emit(True, "App launched successfully!", package, activity or "")
            
        except Exception as e:
            self.finished.emit(False, f"Error: {e}", "", "")


class MainWindow(QMainWindow):
    """Main application window."""
    
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
        self._file_watcher = create_file_watcher()
        self._current_apk: Optional[Path] = None
        self._current_package: Optional[str] = None
        self._install_worker: Optional[APKInstallWorker] = None
        
        # Setup UI
        self._setup_ui()
        self._setup_menu()
        self._setup_status_bar()
        self._apply_theme()
        self._load_settings()
        
        # Connect signals
        self._connect_signals()
        
        # Run hardware profiling
        self._profile_hardware()
        
        # Check if setup needed
        QTimer.singleShot(500, self._check_setup)
    
    def _setup_ui(self):
        """Setup main UI layout."""
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Main splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Left panel - controls and drop zone
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_layout.setSpacing(8)

        # Drop zone
        self.drop_zone = DropZone()
        self.drop_zone.apk_dropped.connect(self._on_apk_dropped)
        self.drop_zone.open_clicked.connect(self._on_open_clicked)
        left_layout.addWidget(self.drop_zone, 1)

        # Controls panel
        self.controls = ControlsPanel()
        left_layout.addWidget(self.controls)

        # Resource meter
        self.resource_meter = ResourceMeter()
        left_layout.addWidget(self.resource_meter)

        splitter.addWidget(left_widget)

        # Right panel - logcat
        self.logcat = LogcatPanel()
        splitter.addWidget(self.logcat)

        # Set splitter proportions (left: 380px min, right: rest)
        splitter.setSizes([380, 720])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)

        # Set left panel minimum width - no max width limit
        left_widget.setMinimumWidth(380)
    
    def _setup_menu(self):
        """Setup menu bar."""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        open_action = QAction("&Open APK...", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_clicked)
        file_menu.addAction(open_action)
        
        file_menu.addSeparator()
        
        settings_action = QAction("&Settings...", self)
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self._show_settings)
        file_menu.addAction(settings_action)
        
        file_menu.addSeparator()
        
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        
        # Emulator menu
        emu_menu = menubar.addMenu("&Emulator")
        
        start_emu = QAction("&Start", self)
        start_emu.triggered.connect(self._on_start_emulator)
        emu_menu.addAction(start_emu)
        
        stop_emu = QAction("S&top", self)
        stop_emu.triggered.connect(self._on_stop_emulator)
        emu_menu.addAction(stop_emu)
        
        restart_emu = QAction("&Restart", self)
        restart_emu.triggered.connect(self._on_restart_emulator)
        emu_menu.addAction(restart_emu)
        
        emu_menu.addSeparator()
        
        save_snap = QAction("&Save Snapshot", self)
        save_snap.triggered.connect(self._on_save_snapshot)
        emu_menu.addAction(save_snap)
        
        load_snap = QAction("&Load Snapshot", self)
        load_snap.triggered.connect(self._on_load_snapshot)
        emu_menu.addAction(load_snap)
        
        # Device menu
        device_menu = menubar.addMenu("&Device")
        
        for preset_name in preset_manager.get_preset_names():
            action = QAction(preset_name.replace("_", " ").title(), self)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, n=preset_name: self._on_preset_selected(n))
            device_menu.addAction(action)
            if preset_name == settings.device_preset:
                action.setChecked(True)
        
        device_menu.addSeparator()
        
        rotate_menu = device_menu.addMenu("&Rotate")
        for i, label in enumerate(["0° (Portrait)", "90°", "180°", "270°"]):
            action = QAction(label, self)
            action.triggered.connect(lambda checked, r=i: self._on_rotate(r))
            rotate_menu.addAction(action)
        
        reset_action = QAction("&Reset Size & Density", self)
        reset_action.triggered.connect(self._on_reset_screen)
        device_menu.addAction(reset_action)
        
        # Tools menu
        tools_menu = menubar.addMenu("&Tools")
        
        screenshot_action = QAction("&Screenshot", self)
        screenshot_action.setShortcut("Ctrl+S")
        screenshot_action.triggered.connect(self._take_screenshot)
        tools_menu.addAction(screenshot_action)
        
        record_action = QAction("Start &Recording", self)
        record_action.setShortcut("Ctrl+R")
        record_action.triggered.connect(self._toggle_recording)
        tools_menu.addAction(record_action)
        self._record_action = record_action
        
        tools_menu.addSeparator()
        
        gps_action = QAction("Set &GPS Location...", self)
        gps_action.triggered.connect(self._set_gps)
        tools_menu.addAction(gps_action)
        
        battery_action = QAction("Set &Battery Level...", self)
        battery_action.triggered.connect(self._set_battery)
        tools_menu.addAction(battery_action)
        
        network_action = QAction("&Network Speed...", self)
        network_action.triggered.connect(self._set_network)
        tools_menu.addAction(network_action)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)
        
        github_action = QAction("&GitHub Repository", self)
        github_action.triggered.connect(lambda: self._open_url("https://github.com/apkpreview"))
        help_menu.addAction(github_action)
    
    def _setup_status_bar(self):
        """Setup status bar."""
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        
        self.lbl_status = QLabel("Ready")
        self.status_bar.addWidget(self.lbl_status)
        
        self.lbl_mode = QLabel("")
        self.status_bar.addPermanentWidget(self.lbl_mode)
    
    def _apply_theme(self):
        """Apply dark theme with explicit font sizes and padding."""
        self.setStyleSheet("""
            /* Global font settings */
            QMainWindow {
                background-color: #1e1e1e;
                font-size: 13px;
            }
            QWidget {
                background-color: #1e1e1e;
                color: #cccccc;
                font-size: 13px;
            }
            QLabel {
                font-size: 12px;
                color: #cccccc;
            }
            QMenuBar {
                background-color: #252526;
                color: #cccccc;
                border-bottom: 1px solid #3a3a3a;
                font-size: 13px;
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background-color: #007acc;
            }
            QMenu {
                background-color: #252526;
                border: 1px solid #3a3a3a;
                color: #cccccc;
                font-size: 13px;
            }
            QMenu::item {
                padding: 6px 24px;
            }
            QMenu::item:selected {
                background-color: #007acc;
            }
            QStatusBar {
                background-color: #007acc;
                color: white;
                font-size: 12px;
                padding: 2px 8px;
            }
            QSplitter::handle {
                background-color: #3a3a3a;
            }
            QSplitter::handle:horizontal {
                width: 2px;
            }
            QGroupBox {
                border: 1px solid #3a3a3a;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 12px;
                font-size: 13px;
                font-weight: medium;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 6px;
                color: #888888;
                font-size: 12px;
            }
            QPushButton {
                background-color: #3a3a3a;
                color: #cccccc;
                border: 1px solid #4a4a4a;
                border-radius: 4px;
                padding: 8px 16px;
                font-size: 13px;
                min-height: 28px;
            }
            QPushButton:hover {
                background-color: #4a4a4a;
                border-color: #5a5a5a;
            }
            QPushButton:pressed {
                background-color: #2a2a2a;
            }
            QPushButton:disabled {
                background-color: #2a2a2a;
                color: #6a6a6a;
                border-color: #3a3a3a;
            }
            QPushButton:default {
                background-color: #007acc;
                border-color: #007acc;
            }
            QPushButton:default:hover {
                background-color: #005a9e;
            }
            QComboBox {
                background-color: #252526;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 13px;
                min-height: 28px;
                min-width: 80px;
            }
            QComboBox:hover {
                border-color: #4a4a4a;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 5px solid transparent;
                border-right: 5px solid transparent;
                border-top: 6px solid #cccccc;
                margin-right: 8px;
            }
            QSpinBox {
                background-color: #252526;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 6px 12px;
                font-size: 13px;
                min-height: 28px;
                min-width: 80px;
            }
            QSpinBox:hover {
                border-color: #4a4a4a;
            }
            QSpinBox::up-button, QSpinBox::down-button {
                border: none;
                background: transparent;
                width: 20px;
            }
            QSpinBox::up-arrow, QSpinBox::down-arrow {
                border: 3px solid transparent;
            }
            QSpinBox::up-arrow {
                border-bottom: 4px solid #cccccc;
            }
            QSpinBox::down-arrow {
                border-top: 4px solid #cccccc;
            }
            QCheckBox {
                font-size: 13px;
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QProgressBar {
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                background-color: #1e1e1e;
                text-align: center;
                height: 14px;
            }
            QProgressBar::chunk {
                border-radius: 2px;
            }
            QScrollBar:vertical {
                background: #1e1e1e;
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background: #3a3a3a;
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: #4a4a4a;
            }
            QTextEdit {
                background-color: #1e1e1e;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                color: #d4d4d4;
                font-family: "Monospace", "Menlo", "Consolas", monospace;
                font-size: 12px;
                padding: 4px;
            }
            QTextEdit:focus {
                border-color: #007acc;
            }
            QMessageBox {
                background-color: #252526;
                color: #cccccc;
            }
            QMessageBox QLabel {
                font-size: 13px;
                color: #cccccc;
            }
            QMessageBox QPushButton {
                min-width: 80px;
                padding: 8px 16px;
            }
        """)
    
    def _connect_signals(self):
        """Connect all signals."""
        # Controls panel
        self.controls.start_emulator.connect(self._on_start_emulator)
        self.controls.stop_emulator.connect(self._on_stop_emulator)
        self.controls.restart_emulator.connect(self._on_restart_emulator)
        self.controls.save_snapshot.connect(self._on_save_snapshot)
        self.controls.load_snapshot.connect(self._on_load_snapshot)
        self.controls.install_apk.connect(self._on_install_apk)
        self.controls.reinstall_apk.connect(self._on_reinstall_apk)
        self.controls.launch_app.connect(self._on_launch_app)
        self.controls.stop_app.connect(self._on_stop_app)
        self.controls.uninstall_app.connect(self._on_uninstall_app)
        self.controls.clear_data.connect(self._on_clear_data)
        self.controls.preset_changed.connect(self._on_preset_selected)
        self.controls.rotate_screen.connect(self._on_rotate)
        self.controls.reset_screen.connect(self._on_reset_screen)
        self.controls.screenshot.connect(self._on_screenshot)
        self.controls.settings_changed.connect(self._on_settings_changed)
        
        # Emulator
        self._emulator.set_output_callback(self._on_emulator_output)
        self._emulator.set_boot_callback(self._on_emulator_booted)
        
        # File watcher
        self._file_watcher.set_callback(self._on_file_changed)
        
        # Resource meter
        self.resource_meter.low_memory_warning.connect(self._on_low_memory)
        
        # Monitor emulator
        self._emu_monitor = EmulatorMonitor(self._emulator, self._on_emulator_info_update)
    
    def _profile_hardware(self):
        """Run hardware profiling."""
        self._profiler.profile()
        mode = self._profiler.get_mode()
        
        self._mode = mode
        self.lbl_mode.setText(f"Mode: {mode.value.replace('_', ' ').title()}")
        
        # Update controls with presets
        self.controls.set_presets(preset_manager.get_preset_names(), settings.device_preset)
    
    def _check_setup(self):
        """Check if first-run setup is needed."""
        if not settings.first_run_completed:
            if SetupWizard.run_if_needed(self):
                # Re-profile after setup
                self._profile_hardware()
            else:
                # User cancelled setup
                self.close()
    
    # Event handlers
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
        """Handle window close - clean shutdown."""
        self._cleanup()
        event.accept()
    
    def _cleanup(self):
        """Clean up processes on exit."""
        # Stop file watcher
        self._file_watcher.stop()

        # Stop scrcpy
        self._scrcpy.stop()

        # Stop emulator
        self._emulator.stop()

        # Kill adb server
        PlatformUtils.kill_adb_server()

        # Kill any remaining child processes (sdkmanager, java, emulator, scrcpy, adb)
        PlatformUtils.cleanup_child_processes()

        # Save settings
        self._save_settings()
    
    # Slot methods
    @pyqtSlot(Path)
    def _on_apk_dropped(self, path: Path):
        """Handle APK dropped on drop zone."""
        self._install_apk(path)
    
    @pyqtSlot()
    def _on_open_clicked(self):
        """Handle Open APK button click."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open APK", "", "Android APK (*.apk)"
        )
        if path:
            self._install_apk(Path(path))
    
    def _install_apk(self, apk_path: Path):
        """Install and launch APK."""
        if not apk_path.exists():
            QMessageBox.warning(self, "Error", "APK file not found")
            return
        
        # Validate APK
        info = get_apk_info(apk_path)
        if not info["valid"]:
            QMessageBox.warning(self, "Invalid APK", 
                              f"Not a valid APK file: {info.get('error', 'Unknown error')}")
            return
        
        # Check scrcpy availability
        scrcpy_available = self._scrcpy.is_available()
        
        # Update UI
        size_mb = apk_path.stat().st_size / (1024 * 1024)
        self.drop_zone.set_apk_info(apk_path.name, size_mb)
        self.controls.update_app_state(False)
        self.lbl_status.setText(f"Installing {apk_path.name}...")
        
        # Show scrcpy note if missing
        if not scrcpy_available:
            self.lbl_status.setText("scrcpy not found, using emulator window")
            if not hasattr(self, '_scrcpy_note_shown'):
                QMessageBox.information(self, "scrcpy not found", 
                    "scrcpy not found in system PATH.\n"
                    "The emulator will start with its own window visible.\n"
                    "Install scrcpy via 'brew install scrcpy' for better screen mirroring.")
                self._scrcpy_note_shown = True
        
        # Save as current
        self._current_apk = apk_path
        settings.last_apk_path = apk_path
        
        # Start file watcher
        self._file_watcher.set_auto_reinstall(self.controls.chk_auto_reinstall.isChecked())
        self._file_watcher.watch(apk_path)
        
        # Start install worker
        self._install_worker = APKInstallWorker(
            apk_path, self._emulator, self._adb, self._scrcpy, self._mode, scrcpy_available=scrcpy_available
        )
        self._install_worker.progress.connect(self._on_install_progress)
        self._install_worker.finished.connect(self._on_install_finished)
        self._install_worker.log.connect(lambda msg: self.logcat._append_log(msg, "verbose"))
        self._install_worker.start()
    
    @pyqtSlot(str)
    def _on_install_progress(self, message: str):
        self.lbl_status.setText(message)
    
    @pyqtSlot(bool, str, str, str)
    def _on_install_finished(self, success: bool, message: str, package: str, activity: str):
        self.lbl_status.setText(message)
        if success:
            self._current_package = package
            self.controls.update_app_state(True)
            self.logcat.start(package)
            self._adb.serial = "emulator-5554"  # Default
            # Update resource meter with emulator PID
            pid = self._emulator.get_pid()
            if pid:
                self.resource_meter.set_emulator_pid(pid)
                self.resource_meter.start_monitoring(pid)
                self._emu_monitor.start()
        else:
            QMessageBox.warning(self, "Install Failed", message)
            self.drop_zone.reset()
            self.controls.update_app_state(False)
    
    @pyqtSlot()
    def _on_start_emulator(self):
        self.lbl_status.setText("Starting emulator...")
        self.controls.btn_start_emu.setEnabled(False)
        scrcpy_available = self._scrcpy.is_available()
        success, msg = self._emulator.start(self._mode, wait_for_boot=True, scrcpy_available=scrcpy_available)
        if success:
            self.controls.update_emulator_state(True)
            self.resource_meter.start_monitoring(self._emulator.get_pid())
            self._emu_monitor.start()
            self.logcat.start(self._current_package or "")
        else:
            QMessageBox.warning(self, "Error", msg)
        self.controls.update_emulator_state(self._emulator.is_running())
        self.lbl_status.setText(msg)
    
    @pyqtSlot()
    def _on_stop_emulator(self):
        self.lbl_status.setText("Stopping emulator...")
        self._scrcpy.stop()
        self.logcat.stop()
        self._emu_monitor.stop()
        self.resource_meter.stop_monitoring()
        success, msg = self._emulator.stop()
        self.controls.update_emulator_state(False)
        self.controls.update_app_state(False)
        self.lbl_status.setText(msg)
    
    @pyqtSlot()
    def _on_restart_emulator(self):
        self.lbl_status.setText("Restarting emulator...")
        self._scrcpy.stop()
        self.logcat.stop()
        self._emu_monitor.stop()
        self._emulator.stop()
        self._emulator.start(self._mode, wait_for_boot=True)
        self.controls.update_emulator_state(self._emulator.is_running())
        if self._emulator.is_running():
            self.resource_meter.start_monitoring(self._emulator.get_pid())
            self._emu_monitor.start()
            self.logcat.start(self._current_package or "")
        self.lbl_status.setText("Emulator restarted")
    
    @pyqtSlot()
    def _on_save_snapshot(self):
        success, msg = self._emulator.save_snapshot("user_snapshot")
        QMessageBox.information(self, "Snapshot", msg)
    
    @pyqtSlot()
    def _on_load_snapshot(self):
        success, msg = self._emulator.load_snapshot("user_snapshot")
        QMessageBox.information(self, "Snapshot", msg)
    
    @pyqtSlot()
    def _on_install_apk(self):
        if self._current_apk:
            self._install_apk(self._current_apk)
    
    @pyqtSlot()
    def _on_reinstall_apk(self):
        if self._current_apk:
            self._install_apk(self._current_apk)
    
    @pyqtSlot()
    def _on_launch_app(self):
        if self._current_package:
            # Get activity from APK
            if self._current_apk:
                _, activity = detect_package_and_activity(self._current_apk)
                if activity:
                    self._adb.start_activity(self._current_package, activity)
                    self.lbl_status.setText(f"Launched {self._current_package}")
    
    @pyqtSlot()
    def _on_stop_app(self):
        if self._current_package:
            self._adb.force_stop(self._current_package)
            self.lbl_status.setText(f"Stopped {self._current_package}")
    
    @pyqtSlot()
    def _on_uninstall_app(self):
        if self._current_package:
            reply = QMessageBox.question(
                self, "Uninstall", f"Uninstall {self._current_package}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._adb.uninstall(self._current_package)
                self._current_package = None
                self.controls.update_app_state(False)
                self.drop_zone.reset()
                self.lbl_status.setText("App uninstalled")
    
    @pyqtSlot()
    def _on_clear_data(self):
        if self._current_package:
            self._adb.clear_data(self._current_package)
            self.lbl_status.setText("App data cleared")
    
    @pyqtSlot(str)
    def _on_preset_selected(self, name: str):
        if self._emulator.is_running():
            success, msg = preset_manager.apply_preset(self._adb, name)
            self.lbl_status.setText(msg)
            if success:
                settings.device_preset = name
        else:
            QMessageBox.information(self, "Device Preset", 
                                  "Start the emulator first to apply device presets")
    
    @pyqtSlot(int)
    def _on_rotate(self, rotation: int):
        if self._emulator.is_running():
            self._adb.set_rotation(rotation)
            self.lbl_status.setText(f"Rotation set to {rotation * 90}°")
    
    @pyqtSlot()
    def _on_reset_screen(self):
        if self._emulator.is_running():
            success, msg = preset_manager.reset_to_default(self._adb)
            self.lbl_status.setText(msg)
    
    @pyqtSlot()
    def _on_screenshot(self):
        if not self._emulator.is_running():
            QMessageBox.warning(self, "Error", "Emulator not running")
            return
        
        # Save to ~/Pictures/ApkPreview
        pictures_dir = Path.home() / "Pictures" / "ApkPreview"
        pictures_dir.mkdir(parents=True, exist_ok=True)
        
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = pictures_dir / f"screenshot_{timestamp}.png"
        remote_path = f"/sdcard/screenshot_{timestamp}.png"
        
        success, msg = self._adb.screenshot(remote_path)
        if not success:
            QMessageBox.warning(self, "Error", f"Screenshot failed: {msg}")
            return
        
        success, msg = self._adb.pull(remote_path, local_path)
        if not success:
            QMessageBox.warning(self, "Error", f"Pull failed: {msg}")
            return
        
        # Clean up remote file
        self._adb.shell(f"rm {remote_path}")
        
        self.lbl_status.setText(f"Screenshot saved to {local_path}")
        QMessageBox.information(self, "Success", f"Screenshot saved to {local_path}")
    
    @pyqtSlot()
    def _on_settings_changed(self):
        s = self.controls.get_settings()
        settings.auto_reinstall = s["auto_reinstall"]
        settings.idle_timeout_minutes = s["idle_timeout"]
        settings.scrcpy_bitrate = s["scrcpy_bitrate"]
        settings.scrcpy_max_fps = s["scrcpy_max_fps"]
        self._file_watcher.set_auto_reinstall(s["auto_reinstall"])
        self._scrcpy.set_bitrate(s["scrcpy_bitrate"])
        self._scrcpy.set_max_fps(s["scrcpy_max_fps"])
    
    @pyqtSlot(str)
    def _on_emulator_output(self, line: str):
        self.logcat._append_log(f"[EMU] {line}", "verbose")
    
    @pyqtSlot(bool)
    def _on_emulator_booted(self, success: bool):
        if success:
            self.lbl_status.setText("Emulator ready")
        else:
            self.lbl_status.setText("Emulator boot failed")
    
    @pyqtSlot(dict)
    def _on_emulator_info_update(self, info: dict):
        # Update resource meter with emulator info
        pass  # Resource meter polls directly
    
    @pyqtSlot(Path)
    def _on_file_changed(self, path: Path):
        """Handle APK file change."""
        self.lbl_status.setText(f"APK changed: {path.name}")
        if self.controls.chk_auto_reinstall.isChecked():
            self._install_apk(path)
        else:
            # Show notification - could add a toast or button
            pass
    
    @pyqtSlot(str)
    def _on_low_memory(self, message: str):
        self.status_bar.showMessage(message, 10000)
    
    def _show_settings(self):
        # TODO: Implement settings dialog
        pass
    
    def _take_screenshot(self):
        if not self._emulator.is_running():
            QMessageBox.warning(self, "Error", "Emulator not running")
            return
        
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Screenshot", "screenshot.png", "PNG (*.png)"
        )
        if path:
            remote = "/sdcard/screenshot.png"
            success, msg = self._adb.screenshot(remote)
            if success:
                success, msg = self._adb.pull(remote, Path(path))
                if success:
                    QMessageBox.information(self, "Success", f"Screenshot saved to {path}")
                else:
                    QMessageBox.warning(self, "Error", f"Pull failed: {msg}")
            else:
                QMessageBox.warning(self, "Error", f"Screenshot failed: {msg}")
    
    def _toggle_recording(self):
        if not self._emulator.is_running():
            QMessageBox.warning(self, "Error", "Emulator not running")
            return
        
        if self._record_action.text() == "Start &Recording":
            path, _ = QFileDialog.getSaveFileName(
                self, "Save Recording", "recording.mp4", "MP4 (*.mp4)"
            )
            if path:
                remote = "/sdcard/recording.mp4"
                success, msg = self._adb.screenrecord(remote, time_limit=180)
                if success:
                    self._record_action.setText("Stop &Recording")
                    self._record_path = path
                    self._record_remote = remote
                    self.lbl_status.setText("Recording...")
                else:
                    QMessageBox.warning(self, "Error", f"Recording failed: {msg}")
        else:
            self._adb.stop_screenrecord()
            # Pull the file
            success, msg = self._adb.pull(self._record_remote, Path(self._record_path))
            if success:
                QMessageBox.information(self, "Success", f"Recording saved to {self._record_path}")
            else:
                QMessageBox.warning(self, "Error", f"Pull failed: {msg}")
            self._record_action.setText("Start &Recording")
            self.lbl_status.setText("Recording stopped")
    
    def _set_gps(self):
        # TODO: Implement GPS dialog
        pass
    
    def _set_battery(self):
        # TODO: Implement battery dialog
        pass
    
    def _set_network(self):
        # TODO: Implement network dialog
        pass
    
    def _show_about(self):
        QMessageBox.about(self, "About ApkPreview",
            "ApkPreview v0.1.0\n\n"
            "Lightweight Android APK testing on desktop.\n"
            "No Android Studio required.\n\n"
            "MIT License - Open Source")
    
    def _open_url(self, url: str):
        import webbrowser
        webbrowser.open(url)
    
    def _load_settings(self):
        """Load window geometry and settings."""
        # Window geometry
        geom = settings.get_window_geometry()
        if geom:
            self.restoreGeometry(geom)
        
        state = settings.get_main_window_state()
        if state:
            self.restoreState(state)
        
        # Controls settings
        self.controls.load_settings(settings.get_all())
    
    def _save_settings(self):
        """Save window geometry and settings."""
        settings.set_window_geometry(self.saveGeometry())
        settings.set_main_window_state(self.saveState())
        
        # Controls settings
        s = self.controls.get_settings()
        settings.update(s)