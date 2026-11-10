"""Controls panel for ApkPreview - emulator and app control buttons."""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
                             QLabel, QComboBox, QCheckBox, QSpinBox, QGroupBox,
                             QGridLayout, QSizePolicy)
from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QFont


class ControlsPanel(QWidget):
    """Control panel with emulator and app management buttons."""

    # Emulator signals
    start_emulator = pyqtSignal()
    stop_emulator = pyqtSignal()
    restart_emulator = pyqtSignal()
    save_snapshot = pyqtSignal()
    load_snapshot = pyqtSignal()
    
    # App signals
    install_apk = pyqtSignal()
    reinstall_apk = pyqtSignal()
    launch_app = pyqtSignal()
    stop_app = pyqtSignal()
    uninstall_app = pyqtSignal()
    clear_data = pyqtSignal()
    
    # Device preset signals
    preset_changed = pyqtSignal(str)
    rotate_screen = pyqtSignal(int)
    reset_screen = pyqtSignal()
    screenshot = pyqtSignal()
    
    # Settings signals
    settings_changed = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._emulator_running = False
        self._app_installed = False
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(8, 8, 8, 8)
        
        # Emulator group
        emu_group = QGroupBox("Emulator")
        emu_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        emu_layout = QVBoxLayout(emu_group)
        emu_layout.setSpacing(6)
        emu_layout.setContentsMargins(12, 18, 12, 12)
        
        # Emulator buttons row 1
        emu_row1 = QHBoxLayout()
        emu_row1.setSpacing(6)
        self.btn_start_emu = QPushButton("Start")
        self.btn_start_emu.setMinimumHeight(32)
        self.btn_start_emu.clicked.connect(self.start_emulator.emit)
        emu_row1.addWidget(self.btn_start_emu)
        
        self.btn_stop_emu = QPushButton("Stop")
        self.btn_stop_emu.setMinimumHeight(32)
        self.btn_stop_emu.clicked.connect(self.stop_emulator.emit)
        emu_row1.addWidget(self.btn_stop_emu)
        
        self.btn_restart_emu = QPushButton("Restart")
        self.btn_restart_emu.setMinimumHeight(32)
        self.btn_restart_emu.clicked.connect(self.restart_emulator.emit)
        emu_row1.addWidget(self.btn_restart_emu)
        emu_row1.addStretch()
        emu_layout.addLayout(emu_row1)
        
        # Snapshot buttons row 2
        snap_row = QHBoxLayout()
        snap_row.setSpacing(6)
        self.btn_save_snap = QPushButton("Save Snapshot")
        self.btn_save_snap.setMinimumHeight(32)
        self.btn_save_snap.clicked.connect(self.save_snapshot.emit)
        snap_row.addWidget(self.btn_save_snap)
        
        self.btn_load_snap = QPushButton("Load Snapshot")
        self.btn_load_snap.setMinimumHeight(32)
        self.btn_load_snap.clicked.connect(self.load_snapshot.emit)
        snap_row.addWidget(self.btn_load_snap)
        snap_row.addStretch()
        emu_layout.addLayout(snap_row)
        
        layout.addWidget(emu_group)
        
        # App group
        app_group = QGroupBox("App")
        app_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        app_layout = QVBoxLayout(app_group)
        app_layout.setSpacing(6)
        app_layout.setContentsMargins(12, 18, 12, 12)
        
        # App buttons row 1
        app_row1 = QHBoxLayout()
        app_row1.setSpacing(6)
        self.btn_install = QPushButton("Install")
        self.btn_install.setMinimumHeight(32)
        self.btn_install.clicked.connect(self.install_apk.emit)
        app_row1.addWidget(self.btn_install)
        
        self.btn_reinstall = QPushButton("Reinstall")
        self.btn_reinstall.setMinimumHeight(32)
        self.btn_reinstall.clicked.connect(self.reinstall_apk.emit)
        app_row1.addWidget(self.btn_reinstall)
        
        self.btn_launch = QPushButton("Launch")
        self.btn_launch.setMinimumHeight(32)
        self.btn_launch.clicked.connect(self.launch_app.emit)
        app_row1.addWidget(self.btn_launch)
        app_row1.addStretch()
        app_layout.addLayout(app_row1)
        
        # App buttons row 2
        app_row2 = QHBoxLayout()
        app_row2.setSpacing(6)
        self.btn_stop_app = QPushButton("Stop App")
        self.btn_stop_app.setMinimumHeight(32)
        self.btn_stop_app.clicked.connect(self.stop_app.emit)
        app_row2.addWidget(self.btn_stop_app)
        
        self.btn_uninstall = QPushButton("Uninstall")
        self.btn_uninstall.setMinimumHeight(32)
        self.btn_uninstall.clicked.connect(self.uninstall_app.emit)
        app_row2.addWidget(self.btn_uninstall)
        
        self.btn_clear_data = QPushButton("Clear Data")
        self.btn_clear_data.setMinimumHeight(32)
        self.btn_clear_data.clicked.connect(self.clear_data.emit)
        app_row2.addWidget(self.btn_clear_data)
        app_row2.addStretch()
        app_layout.addLayout(app_row2)
        
        layout.addWidget(app_group)
        
        # Device preset group
        preset_group = QGroupBox("Device Preset")
        preset_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        preset_layout = QVBoxLayout(preset_group)
        preset_layout.setSpacing(8)
        preset_layout.setContentsMargins(12, 18, 12, 12)
        
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumHeight(32)
        self.preset_combo.currentTextChanged.connect(self.preset_changed.emit)
        preset_layout.addWidget(self.preset_combo)
        
        # Rotation buttons
        rot_row = QHBoxLayout()
        rot_row.setSpacing(6)
        rot_row.addWidget(QLabel("Rotate:"))
        
        self.btn_rot_0 = QPushButton("0°")
        self.btn_rot_0.setMinimumHeight(32)
        self.btn_rot_0.clicked.connect(lambda: self.rotate_screen.emit(0))
        rot_row.addWidget(self.btn_rot_0)
        
        self.btn_rot_90 = QPushButton("90°")
        self.btn_rot_90.setMinimumHeight(32)
        self.btn_rot_90.clicked.connect(lambda: self.rotate_screen.emit(1))
        rot_row.addWidget(self.btn_rot_90)
        
        self.btn_rot_180 = QPushButton("180°")
        self.btn_rot_180.setMinimumHeight(32)
        self.btn_rot_180.clicked.connect(lambda: self.rotate_screen.emit(2))
        rot_row.addWidget(self.btn_rot_180)
        
        self.btn_rot_270 = QPushButton("270°")
        self.btn_rot_270.setMinimumHeight(32)
        self.btn_rot_270.clicked.connect(lambda: self.rotate_screen.emit(3))
        rot_row.addWidget(self.btn_rot_270)
        
        self.btn_reset_screen = QPushButton("Reset")
        self.btn_reset_screen.setMinimumHeight(32)
        self.btn_reset_screen.clicked.connect(self.reset_screen.emit)
        rot_row.addWidget(self.btn_reset_screen)
        rot_row.addStretch()
        preset_layout.addLayout(rot_row)
        
        # Screenshot button
        self.btn_screenshot = QPushButton("Screenshot")
        self.btn_screenshot.setMinimumHeight(32)
        self.btn_screenshot.clicked.connect(self.screenshot.emit)
        preset_layout.addWidget(self.btn_screenshot)
        
        layout.addWidget(preset_group)
        
        # Settings group
        settings_group = QGroupBox("Settings")
        settings_group.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        settings_layout = QVBoxLayout(settings_group)
        settings_layout.setSpacing(8)
        settings_layout.setContentsMargins(12, 18, 12, 12)
        
        # Auto-reinstall checkbox
        self.chk_auto_reinstall = QCheckBox("Auto reinstall on APK change")
        self.chk_auto_reinstall.toggled.connect(self.settings_changed.emit)
        settings_layout.addWidget(self.chk_auto_reinstall)
        
        # Idle timeout
        idle_row = QHBoxLayout()
        idle_row.setSpacing(6)
        idle_row.addWidget(QLabel("Idle timeout (min):"))
        self.spin_idle = QSpinBox()
        self.spin_idle.setRange(0, 120)
        self.spin_idle.setValue(10)
        self.spin_idle.setSpecialValueText("Disabled")
        self.spin_idle.setMinimumWidth(80)
        self.spin_idle.valueChanged.connect(self.settings_changed.emit)
        idle_row.addWidget(self.spin_idle)
        idle_row.addStretch()
        settings_layout.addLayout(idle_row)
        
        # scrcpy quality
        quality_row = QHBoxLayout()
        quality_row.setSpacing(6)
        quality_row.addWidget(QLabel("Quality:"))
        self.combo_quality = QComboBox()
        self.combo_quality.addItems(["Low (2M)", "Medium (4M)", "High (8M)", "Max (16M)"])
        self.combo_quality.setCurrentIndex(2)
        self.combo_quality.setMinimumWidth(120)
        self.combo_quality.currentIndexChanged.connect(self.settings_changed.emit)
        quality_row.addWidget(self.combo_quality)
        quality_row.addStretch()
        settings_layout.addLayout(quality_row)
        
        # Max FPS
        fps_row = QHBoxLayout()
        fps_row.setSpacing(6)
        fps_row.addWidget(QLabel("Max FPS:"))
        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(15, 60)
        self.spin_fps.setValue(30)
        self.spin_fps.setMinimumWidth(80)
        self.spin_fps.valueChanged.connect(self.settings_changed.emit)
        fps_row.addWidget(self.spin_fps)
        fps_row.addStretch()
        settings_layout.addLayout(fps_row)
        
        layout.addWidget(settings_group)
        
        layout.addStretch()
        
        # Initial state
        self.update_emulator_state(False)
        self.update_app_state(False)
    
    def update_emulator_state(self, running: bool):
        """Update button states based on emulator running state."""
        self._emulator_running = running
        self.btn_start_emu.setEnabled(not running)
        self.btn_stop_emu.setEnabled(running)
        self.btn_restart_emu.setEnabled(running)
        self.btn_save_snap.setEnabled(running)
        self.btn_load_snap.setEnabled(running)
    
    def update_app_state(self, installed: bool):
        """Update button states based on app installed state."""
        self._app_installed = installed
        self.btn_reinstall.setEnabled(installed)
        self.btn_launch.setEnabled(installed)
        self.btn_stop_app.setEnabled(installed)
        self.btn_uninstall.setEnabled(installed)
        self.btn_clear_data.setEnabled(installed)
    
    def set_presets(self, presets: list, current: str = ""):
        """Set available device presets."""
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems(presets)
        if current:
            idx = self.preset_combo.findText(current)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)
    
    def get_settings(self) -> dict:
        """Get current settings."""
        quality_map = {0: "2M", 1: "4M", 2: "8M", 3: "16M"}
        return {
            "auto_reinstall": self.chk_auto_reinstall.isChecked(),
            "idle_timeout": self.spin_idle.value(),
            "scrcpy_bitrate": quality_map.get(self.combo_quality.currentIndex(), "8M"),
            "scrcpy_max_fps": self.spin_fps.value(),
        }
    
    def load_settings(self, settings: dict):
        """Load settings into UI."""
        self.chk_auto_reinstall.setChecked(settings.get("auto_reinstall", False))
        self.spin_idle.setValue(settings.get("idle_timeout", 10))
        
        bitrate_map = {"2M": 0, "4M": 1, "8M": 2, "16M": 3}
        self.combo_quality.setCurrentIndex(bitrate_map.get(settings.get("scrcpy_bitrate", "8M"), 2))
        self.spin_fps.setValue(settings.get("scrcpy_max_fps", 30))