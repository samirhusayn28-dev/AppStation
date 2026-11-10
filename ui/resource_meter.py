"""Resource meter panel for ApkPreview - shows emulator and system resource usage."""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
                             QProgressBar, QFrame, QSizePolicy)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, pyqtSlot
from PyQt6.QtGui import QFont, QColor
import psutil
from core.platform_utils import PlatformUtils


class ResourceMeter(QWidget):
    """Live resource usage display for emulator and system - single row of three meters."""
    
    low_memory_warning = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._emulator_pid = None
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._update_stats)
        self._update_timer.setInterval(2000)  # 2 seconds
        self._warning_threshold = 85  # percent
        self._warning_shown = False
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(6, 6, 6, 6)
        
        # Single row of three meters
        meters_layout = QHBoxLayout()
        meters_layout.setSpacing(12)
        meters_layout.setContentsMargins(0, 0, 0, 0)
        
        # Emulator RAM meter
        self.emu_ram_meter = self._create_meter("Emulator RAM", "#9cdcfe")
        meters_layout.addWidget(self.emu_ram_meter)
        
        # System RAM meter
        self.sys_ram_meter = self._create_meter("System RAM", "#dcdcaa")
        meters_layout.addWidget(self.sys_ram_meter)
        
        # Disk meter
        self.disk_meter = self._create_meter("Disk", "#c586c0")
        meters_layout.addWidget(self.disk_meter)
        
        layout.addLayout(meters_layout)
        
        # Warning label (hidden by default)
        self.lbl_warning = QLabel("")
        self.lbl_warning.setStyleSheet("color: #f44747; font-weight: bold; font-size: 11px;")
        self.lbl_warning.setWordWrap(True)
        self.lbl_warning.setVisible(False)
        layout.addWidget(self.lbl_warning)
        
        layout.addStretch()
        
        # Style
        self.setStyleSheet("""
            QLabel {
                color: #cccccc;
                font-size: 12px;
            }
            QProgressBar {
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                background-color: #1e1e1e;
                text-align: center;
                height: 14px;
                min-width: 100px;
            }
            QProgressBar::chunk {
                border-radius: 2px;
            }
        """)
        
        self.setMinimumWidth(360)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
    
    def _create_meter(self, label_text: str, color: str) -> QWidget:
        """Create a single meter widget with label and thin progress bar."""
        meter = QWidget()
        meter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        
        layout = QHBoxLayout(meter)
        layout.setSpacing(6)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Label
        label = QLabel(f"{label_text}:")
        label.setStyleSheet("color: #cccccc; font-size: 12px; font-weight: medium;")
        label.setMinimumWidth(80)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(label)
        
        # Value label
        value_label = QLabel("--")
        value_label.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: bold;")
        value_label.setMinimumWidth(60)
        value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(value_label)
        
        # Progress bar
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid #3a3a3a;
                border-radius: 3px;
                background-color: #1e1e1e;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 2px;
            }}
        """)
        layout.addWidget(bar)
        
        # Store references
        meter.label = label
        meter.value_label = value_label
        meter.bar = bar
        
        return meter
    
    def start_monitoring(self, emulator_pid: int = None):
        """Start resource monitoring."""
        self._emulator_pid = emulator_pid
        self._update_timer.start()
        self._update_stats()
    
    def stop_monitoring(self):
        """Stop resource monitoring."""
        self._update_timer.stop()
    
    def set_emulator_pid(self, pid: int):
        """Set the emulator process PID to monitor."""
        self._emulator_pid = pid
    
    @pyqtSlot()
    def _update_stats(self):
        """Update all resource stats."""
        # System memory
        mem = psutil.virtual_memory()
        sys_ram_percent = int(mem.percent)
        sys_ram_used_gb = mem.used / (1024**3)
        sys_ram_total_gb = mem.total / (1024**3)
        
        sys_meter = self.sys_ram_meter
        sys_meter.bar.setValue(sys_ram_percent)
        sys_meter.value_label.setText(f"{sys_ram_used_gb:.1f}/{sys_ram_total_gb:.1f} GB")
        
        # Disk (SDK directory)
        try:
            sdk_dir = PlatformUtils.get_sdk_dir()
            usage = psutil.disk_usage(str(sdk_dir.parent))
            free_gb = usage.free / (1024**3)
            total_gb = usage.total / (1024**3)
            used_percent = (usage.used / usage.total) * 100
            
            disk_meter = self.disk_meter
            disk_meter.bar.setValue(int(used_percent))
            disk_meter.value_label.setText(f"{free_gb:.1f}/{total_gb:.1f} GB")
        except Exception:
            self.disk_meter.value_label.setText("--")
        
        # Emulator process
        if self._emulator_pid:
            try:
                proc = psutil.Process(self._emulator_pid)
                if proc.is_running():
                    emu_mem = proc.memory_info().rss / (1024 * 1024)
                    emu_mem_percent = proc.memory_percent()
                    
                    emu_meter = self.emu_ram_meter
                    emu_meter.bar.setValue(int(emu_mem_percent))
                    emu_meter.value_label.setText(f"{int(emu_mem)} MB")
                else:
                    self._clear_emu_stats()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._clear_emu_stats()
        else:
            self._clear_emu_stats()
        
        # Check low memory warning
        self._check_memory_warning(mem.percent)
    
    def _clear_emu_stats(self):
        """Clear emulator stats when not running."""
        emu_meter = self.emu_ram_meter
        emu_meter.bar.setValue(0)
        emu_meter.value_label.setText("--")
    
    def _check_memory_warning(self, system_percent: float):
        """Check and show low memory warning."""
        if system_percent >= self._warning_threshold:
            if not self._warning_shown:
                self.lbl_warning.setText(
                    f"⚠ Low system memory: {system_percent:.0f}% used. "
                    "Consider closing other apps or enabling auto-sleep."
                )
                self.lbl_warning.setVisible(True)
                self._warning_shown = True
                self.low_memory_warning.emit(
                    f"System memory at {system_percent:.0f}%"
                )
        else:
            if self._warning_shown:
                self.lbl_warning.setVisible(False)
                self._warning_shown = False
    
    def set_warning_threshold(self, percent: int):
        """Set memory warning threshold."""
        self._warning_threshold = max(50, min(95, percent))