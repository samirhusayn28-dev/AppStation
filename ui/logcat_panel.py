"""Logcat panel for ApkPreview - live log streaming with filtering."""

from PyQt6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, 
                             QLineEdit, QComboBox, QCheckBox, QPushButton, 
                             QLabel, QFileDialog, QMenu)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QThread, pyqtSlot
from PyQt6.QtGui import QColor, QTextCursor, QTextCharFormat, QFont, QAction
import subprocess
import threading
import time
from core.platform_utils import PlatformUtils
from core.settings import settings


class LogcatWorker(QThread):
    """Background thread for reading logcat."""
    
    log_line = pyqtSignal(str, str)  # line, level
    error = pyqtSignal(str)
    
    def __init__(self, package_filter: str = "", log_level: str = "verbose"):
        super().__init__()
        self._package_filter = package_filter
        self._log_level = log_level
        self._running = False
        self._process = None
    
    def set_filter(self, package: str, level: str):
        self._package_filter = package
        self._log_level = level
    
    def run(self):
        self._running = True
        adb = PlatformUtils.get_adb_path()
        
        # Build logcat command
        cmd = [str(adb), "logcat"]
        
        # Format: time, level, tag, message
        cmd.extend(["-v", "time"])
        
        # Filter by package if specified
        if self._package_filter:
            # Use pid filter - get pid first
            cmd.extend(["--pid", self._get_pid()])
        
        # Log level filter
        level_map = {
            "verbose": "V",
            "debug": "D",
            "info": "I",
            "warn": "W",
            "error": "E",
            "fatal": "F",
        }
        min_level = level_map.get(self._log_level, "V")
        cmd.append(f"*:{min_level}")
        
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True,
            )
            
            for line in self._process.stdout:
                if not self._running:
                    break
                level = self._extract_level(line)
                self.log_line.emit(line.rstrip(), level)
                
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if self._process:
                self._process.terminate()
    
    def _get_pid(self) -> str:
        """Get PID of the package."""
        adb = PlatformUtils.get_adb_path()
        try:
            result = subprocess.run(
                [str(adb), "shell", "pidof", self._package_filter],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip().split()[0]
        except Exception:
            pass
        return ""
    
    def _extract_level(self, line: str) -> str:
        """Extract log level from line."""
        # Format: MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: message
        parts = line.split()
        if len(parts) >= 4:
            level_char = parts[3][0] if parts[3] else "V"
            level_map = {
                "V": "verbose", "D": "debug", "I": "info",
                "W": "warn", "E": "error", "F": "fatal",
            }
            return level_map.get(level_char, "verbose")
        return "verbose"
    
    def stop(self):
        self._running = False
        if self._process:
            self._process.terminate()
        self.wait(2000)


class LogcatPanel(QWidget):
    """Logcat display panel with filtering and search."""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: LogcatWorker = None
        self._current_package = ""
        self._log_level = "verbose"
        self._auto_scroll = True
        self._paused = False
        self._search_text = ""
        self._max_lines = 5000
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)
        layout.setContentsMargins(4, 4, 4, 4)
        
        # Toolbar row 1: Package / Level / Auto-filter
        toolbar1 = QHBoxLayout()
        toolbar1.setSpacing(8)

        toolbar1.addWidget(QLabel("Package:"))
        self.edit_package = QLineEdit()
        self.edit_package.setPlaceholderText("Auto (current app)")
        self.edit_package.setMinimumWidth(200)
        self.edit_package.returnPressed.connect(self._apply_filter)
        toolbar1.addWidget(self.edit_package)

        toolbar1.addWidget(QLabel("Level:"))
        self.combo_level = QComboBox()
        self.combo_level.addItems(["Verbose", "Debug", "Info", "Warn", "Error", "Fatal"])
        self.combo_level.setCurrentText("Verbose")
        self.combo_level.setMinimumWidth(110)
        self.combo_level.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo_level.currentTextChanged.connect(self._apply_filter)
        toolbar1.addWidget(self.combo_level)

        self.chk_auto_filter = QCheckBox("Auto-filter by app")
        self.chk_auto_filter.setChecked(True)
        self.chk_auto_filter.toggled.connect(self._on_auto_filter_toggled)
        toolbar1.addWidget(self.chk_auto_filter)

        toolbar1.addStretch()
        layout.addLayout(toolbar1)

        # Toolbar row 2: Pause / Wrap / Search / Clear / Save
        toolbar2 = QHBoxLayout()
        toolbar2.setSpacing(8)

        self.chk_pause = QCheckBox("Pause")
        self.chk_pause.toggled.connect(self._on_pause_toggled)
        toolbar2.addWidget(self.chk_pause)

        self.chk_wrap = QCheckBox("Wrap lines")
        self.chk_wrap.setChecked(False)
        self.chk_wrap.toggled.connect(self._on_wrap_toggled)
        toolbar2.addWidget(self.chk_wrap)

        toolbar2.addWidget(QLabel("Search:"))
        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("Filter...")
        self.edit_search.setMinimumWidth(200)
        self.edit_search.textChanged.connect(self._on_search_changed)
        toolbar2.addWidget(self.edit_search)

        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear_log)
        toolbar2.addWidget(self.btn_clear)

        self.btn_save = QPushButton("Save")
        self.btn_save.clicked.connect(self._save_log)
        toolbar2.addWidget(self.btn_save)

        toolbar2.addStretch()
        layout.addLayout(toolbar2)
        
        # Log display
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFont("Monospace", 9))
        self.log_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.log_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log_view.customContextMenuRequested.connect(self._show_context_menu)
        
        # Set dark theme colors
        self.log_view.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
            }
        """)
        
        layout.addWidget(self.log_view)
        
        # Status bar
        status_layout = QHBoxLayout()
        self.lbl_status = QLabel("Disconnected")
        self.lbl_status.setStyleSheet("color: #888; font-size: 11px;")
        status_layout.addWidget(self.lbl_status)
        status_layout.addStretch()
        self.lbl_line_count = QLabel("0 lines")
        self.lbl_line_count.setStyleSheet("color: #888; font-size: 11px;")
        status_layout.addWidget(self.lbl_line_count)
        layout.addLayout(status_layout)
        
        # Color formats for log levels
        self._formats = {
            "verbose": QTextCharFormat(),
            "debug": QTextCharFormat(),
            "info": QTextCharFormat(),
            "warn": QTextCharFormat(),
            "error": QTextCharFormat(),
            "fatal": QTextCharFormat(),
        }
        self._formats["verbose"].setForeground(QColor("#888888"))
        self._formats["debug"].setForeground(QColor("#4ec9b0"))
        self._formats["info"].setForeground(QColor("#9cdcfe"))
        self._formats["warn"].setForeground(QColor("#dcdcaa"))
        self._formats["error"].setForeground(QColor("#f44747"))
        self._formats["fatal"].setForeground(QColor("#ff6b6b"))
    
    def start(self, package: str = ""):
        """Start logcat streaming."""
        if self._worker and self._worker.isRunning():
            self._worker.stop()
        
        self._current_package = package
        if self.chk_auto_filter.isChecked() and package:
            filter_pkg = package
        else:
            filter_pkg = self.edit_package.text().strip() or ""
        
        level = self.combo_level.currentText().lower()
        
        self._worker = LogcatWorker(filter_pkg, level)
        self._worker.log_line.connect(self._append_log)
        self._worker.error.connect(self._on_error)
        self._worker.start()
        
        self.lbl_status.setText("Connected")
        self.lbl_status.setStyleSheet("color: #4ec9b0; font-size: 11px;")
    
    def stop(self):
        """Stop logcat streaming."""
        if self._worker:
            self._worker.stop()
            self._worker = None
        self.lbl_status.setText("Disconnected")
        self.lbl_status.setStyleSheet("color: #888; font-size: 11px;")
    
    def set_package(self, package: str):
        """Set the package to filter by."""
        self._current_package = package
        if self.chk_auto_filter.isChecked():
            self._apply_filter()
    
    def _apply_filter(self):
        """Apply current filters."""
        if self._worker:
            filter_pkg = ""
            if self.chk_auto_filter.isChecked():
                filter_pkg = self._current_package
            else:
                filter_pkg = self.edit_package.text().strip()
            level = self.combo_level.currentText().lower()
            self._worker.set_filter(filter_pkg, level)
    
    def _on_auto_filter_toggled(self, checked: bool):
        self.edit_package.setEnabled(not checked)
        self._apply_filter()
    
    def _on_pause_toggled(self, paused: bool):
        self._paused = paused
    
    def _on_wrap_toggled(self, checked: bool):
        mode = QTextEdit.LineWrapMode.WidgetWidth if checked else QTextEdit.LineWrapMode.NoWrap
        self.log_view.setLineWrapMode(mode)
    
    def _on_search_changed(self, text: str):
        self._search_text = text.lower()
        self._highlight_search()
    
    def _highlight_search(self):
        """Highlight search matches in log."""
        # Simple approach: re-render visible lines with highlighting
        # For performance, we'll just re-apply formatting on new lines
        pass
    
    @pyqtSlot(str, str)
    def _append_log(self, line: str, level: str):
        """Append a log line to the view."""
        if self._paused:
            return
        
        # Check search filter
        if self._search_text and self._search_text not in line.lower():
            return
        
        # Trim old lines
        doc = self.log_view.document()
        if doc.blockCount() > self._max_lines:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(QTextCursor.MoveOperation.Down, 
                              QTextCursor.MoveMode.KeepAnchor, 
                              doc.blockCount() - self._max_lines)
            cursor.removeSelectedText()
        
        # Append with color
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        
        fmt = self._formats.get(level, self._formats["verbose"])
        cursor.insertText(line + "\n", fmt)
        
        # Auto scroll
        if self._auto_scroll:
            self.log_view.ensureCursorVisible()
        
        # Update line count
        self.lbl_line_count.setText(f"{doc.blockCount()} lines")
    
    @pyqtSlot(str)
    def _on_error(self, error: str):
        self.lbl_status.setText(f"Error: {error}")
        self.lbl_status.setStyleSheet("color: #f44747; font-size: 11px;")
    
    def _clear_log(self):
        self.log_view.clear()
        self.lbl_line_count.setText("0 lines")
    
    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Logcat", "logcat.txt", "Text Files (*.txt);;All Files (*)"
        )
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.log_view.toPlainText())
            except Exception as e:
                self.lbl_status.setText(f"Save failed: {e}")
    
    def _show_context_menu(self, pos):
        menu = QMenu(self)
        
        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(self.log_view.copy)
        menu.addAction(copy_action)
        
        select_all = QAction("Select All", self)
        select_all.triggered.connect(self.log_view.selectAll)
        menu.addAction(select_all)
        
        menu.addSeparator()
        
        clear_action = QAction("Clear", self)
        clear_action.triggered.connect(self._clear_log)
        menu.addAction(clear_action)
        
        menu.exec(self.log_view.mapToGlobal(pos))