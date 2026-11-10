"""Setup wizard for ApkPreview - first-run SDK and tool installation."""

import os
from PyQt6.QtWidgets import (QWizard, QWizardPage, QVBoxLayout, QHBoxLayout,
                             QLabel, QProgressBar, QPushButton, QTextEdit,
                             QCheckBox, QFileDialog, QMessageBox, QComboBox, QLineEdit)
from PyQt6.QtCore import pyqtSignal, Qt, QThread, QTimer
from PyQt6.QtGui import QFont
from core.sdk_manager import SDKManager, ANDROID_VERSION_NAMES
from core.hardware_profile import HardwareProfiler, PerformanceMode, CompatibilityVerdict
from core.settings import settings
from core.platform_utils import PlatformUtils
from .android_versions_dialog import AndroidVersionsDialog


class SetupWorker(QThread):
    """Background worker for setup steps."""

    progress = pyqtSignal(str, int, int, str)  # step, current, total, message
    step_finished = pyqtSignal(str, bool, str)  # step_name, success, message
    finished = pyqtSignal(bool, str)  # overall success, message
    error = pyqtSignal(str)  # error with full details for retry

    def __init__(self, mode: PerformanceMode):
        super().__init__()
        self._mode = mode
        self._sdk_manager = SDKManager(self._on_progress)
        self._cancelled = False

    def _on_progress(self, step: str, current: int, total: int, message: str):
        self.progress.emit(step, current, total, message)

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            success, message = self._sdk_manager.run_full_setup(self._mode)
            self.finished.emit(success, message)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            self.error.emit(tb)
            self.finished.emit(False, f"Setup failed with exception: {e}")


class WelcomePage(QWizardPage):
    """Welcome page with hardware compatibility check."""
    
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to ApkPreview")
        self.setSubTitle("Let's set up your Android testing environment")
        self._sdk_manager = SDKManager()
        self._profiler = HardwareProfiler(sdk_manager=self._sdk_manager)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        # Compatibility check
        self.lbl_checking = QLabel("Checking hardware compatibility...")
        self.lbl_checking.setFont(QFont("", 11))
        layout.addWidget(self.lbl_checking)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminate
        layout.addWidget(self.progress_bar)

        self.lbl_result = QLabel("")
        self.lbl_result.setWordWrap(True)
        layout.addWidget(self.lbl_result)

        self.lbl_details = QLabel("")
        self.lbl_details.setWordWrap(True)
        self.lbl_details.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.lbl_details)

        # Download size estimate
        self.lbl_download = QLabel("")
        self.lbl_download.setStyleSheet("color: #4ec9b0; font-weight: bold;")
        layout.addWidget(self.lbl_download)

        layout.addStretch()

        # Run compatibility check
        QTimer.singleShot(100, self._run_check)

    def _run_check(self):
        profile = self._profiler.profile()
        verdict = self._profiler.get_verdict()
        reasons = self._profiler.get_reasons()
        mode = self._profiler.get_mode()

        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)

        if verdict == CompatibilityVerdict.GOOD:
            self.lbl_checking.setText("✅ Hardware Compatibility: Good")
            self.lbl_checking.setStyleSheet("color: #4ec9b0; font-weight: bold;")
        elif verdict == CompatibilityVerdict.LOW_SPEC:
            self.lbl_checking.setText("⚠ Hardware Compatibility: Low-Spec Mode")
            self.lbl_checking.setStyleSheet("color: #dcdcaa; font-weight: bold;")
        elif verdict == CompatibilityVerdict.WARNING:
            self.lbl_checking.setText("⚠ Hardware Compatibility: Low Disk Space (Warning)")
            self.lbl_checking.setStyleSheet("color: #dcdcaa; font-weight: bold;")
        else:
            self.lbl_checking.setText("❌ Hardware Compatibility: Not Supported")
            self.lbl_checking.setStyleSheet("color: #f44747; font-weight: bold;")

        self.lbl_result.setText("\n".join(reasons))

        # Show hardware details
        p = profile
        req_disk = f" ({p.required_disk_gb} GB needed)" if p.required_disk_gb > 0 else ""
        self.lbl_details.setText(
            f"OS: {p.os} {p.os_version} | "
            f"CPU: {p.cpu_arch} ({p.cpu_physical_cores}P/{p.cpu_logical_cores}L) | "
            f"RAM: {p.total_ram_gb:.1f} GB | "
            f"Disk: {p.free_disk_gb} GB free{req_disk} | "
            f"Virtualization: {'Yes' if p.virtualization_supported else 'No'}"
        )

        # Download size estimate
        from core.sdk_manager import format_size
        size_mb = self._sdk_estimate_size(mode)
        self.lbl_download.setText(f"Estimated download size: {format_size(size_mb)}")

        # Store mode for next pages
        self.wizard().setProperty("performance_mode", mode)
        self.wizard().setProperty("hardware_profile", profile)

        # Enable next button based on verdict - allow WARNING (can proceed)
        self.wizard().button(QWizard.WizardButton.NextButton).setEnabled(
            verdict != CompatibilityVerdict.NOT_SUPPORTED
        )

    def _sdk_estimate_size(self, mode: PerformanceMode) -> int:
        """Base toolchain estimate (cmdline-tools + platform-tools + emulator).

        The Android version (system image) is chosen later, so it is not part
        of this estimate.
        """
        sizes = {
            "cmdline_tools": 200, "platform-tools": 50, "emulator": 300,
            "scrcpy": 30,
        }
        return (sizes["cmdline_tools"] + sizes["platform-tools"]
                + sizes["emulator"] + sizes["scrcpy"])

    def isComplete(self) -> bool:
        return self._profiler.get_verdict() != CompatibilityVerdict.NOT_SUPPORTED


class InstallPage(QWizardPage):
    """SDK and tools installation page."""

    def __init__(self):
        super().__init__()
        self.setTitle("Installing Android SDK & Tools")
        self.setSubTitle("This may take several minutes depending on your connection")
        self._worker: SetupWorker = None
        self._last_log_message = ""
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Overall progress
        self.lbl_step = QLabel("Preparing...")
        layout.addWidget(self.lbl_step)

        self.progress_overall = QProgressBar()
        self.progress_overall.setRange(0, 100)
        layout.addWidget(self.progress_overall)

        # Step progress
        self.lbl_substep = QLabel("")
        self.lbl_substep.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(self.lbl_substep)

        self.progress_step = QProgressBar()
        self.progress_step.setRange(0, 100)
        layout.addWidget(self.progress_step)

        # Log
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(150)
        self.log_view.setFont(QFont("Monospace", 9))
        self.log_view.setStyleSheet("background: #1e1e1e; color: #d4d4d4; border: 1px solid #3a3a3a;")
        layout.addWidget(self.log_view)

        # Buttons row
        btn_layout = QHBoxLayout()
        self.btn_retry = QPushButton("Retry")
        self.btn_retry.setVisible(False)
        self.btn_retry.clicked.connect(self._retry)
        btn_layout.addWidget(self.btn_retry)

        btn_layout.addStretch()

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self._cancel)
        btn_layout.addWidget(self.btn_cancel)
        layout.addLayout(btn_layout)

    def initializePage(self):
        mode = self.wizard().property("performance_mode")
        if not mode:
            mode = PerformanceMode.NORMAL

        # Reset UI state
        self.btn_retry.setVisible(False)
        self.btn_cancel.setText("Cancel")
        self.btn_cancel.clicked.disconnect()
        self.btn_cancel.clicked.connect(self._cancel)
        self.log_view.clear()
        self._last_log_message = ""

        self._worker = SetupWorker(mode)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_error(self, traceback_str: str):
        """Handle uncaught exception from worker thread."""
        self.log_view.append(f"\n❌ EXCEPTION:\n{traceback_str}")
        self._on_finished(False, "Setup failed with exception (see log above)")

    def _on_progress(self, step: str, current: int, total: int, message: str):
        # Avoid duplicate log lines
        if message != self._last_log_message:
            self.log_view.append(message)
            self._last_log_message = message

        self.lbl_step.setText(step.replace("_", " ").title())
        # Clamp progress to 100
        overall = min(100, int(current * 100 / max(1, total)))
        self.progress_overall.setValue(overall)
        self.lbl_substep.setText(message)
        self.progress_step.setValue(min(100, current if total == 100 else int(current * 100 / max(1, total))))

    def _on_finished(self, success: bool, message: str):
        self.log_view.append(f"\n{'✅' if success else '❌'} {message}")

        if success:
            self.btn_cancel.setText("Close")
            self.btn_cancel.clicked.disconnect()
            self.btn_cancel.clicked.connect(self.wizard().close)
            self.wizard().button(QWizard.WizardButton.NextButton).setEnabled(True)
        else:
            self.lbl_step.setText("Installation Failed")
            self.lbl_step.setStyleSheet("color: #f44747; font-weight: bold;")
            self.btn_cancel.setText("Close")
            self.btn_cancel.clicked.disconnect()
            self.btn_cancel.clicked.connect(self.wizard().close)
            self.btn_retry.setVisible(True)

    def _retry(self):
        """Retry the installation from the beginning."""
        self.btn_retry.setVisible(False)
        self.btn_cancel.setText("Cancel")
        self.btn_cancel.clicked.disconnect()
        self.btn_cancel.clicked.connect(self._cancel)
        self.log_view.clear()
        self._last_log_message = ""
        self.lbl_step.setStyleSheet("")
        self.progress_overall.setValue(0)
        self.progress_step.setValue(0)
        self.initializePage()

    def _cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)
        # Kill any leftover child processes
        PlatformUtils.kill_process_tree(os.getpid())
        self.wizard().close()

    def isComplete(self) -> bool:
        return self._worker is not None and not self._worker.isRunning()


class AndroidVersionPage(QWizardPage):
    """Ask the user to pick an Android version (nothing is auto-downloaded)."""

    def __init__(self):
        super().__init__()
        self.setTitle("Choose an Android version")
        self.setSubTitle(
            "Pick the Android version to test on. The system image is only "
            "downloaded when you choose one.")
        self._sdk_manager = SDKManager()
        self._skipped = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self.lbl_explain = QLabel(
            "ApkPreview installs platform-tools and the emulator by default. "
            "Use the button below to browse available Android versions, "
            "download one, and select it.")
        self.lbl_explain.setWordWrap(True)
        layout.addWidget(self.lbl_explain)

        self.lbl_selected = QLabel("Selected: none")
        self.lbl_selected.setWordWrap(True)
        layout.addWidget(self.lbl_selected)

        row = QHBoxLayout()
        self.btn_choose = QPushButton("Choose Android version...")
        self.btn_choose.clicked.connect(self._choose)
        row.addWidget(self.btn_choose)
        self.btn_skip = QPushButton("Skip for now")
        self.btn_skip.clicked.connect(self._skip)
        row.addWidget(self.btn_skip)
        row.addStretch()
        layout.addLayout(row)

        layout.addStretch()

    def initializePage(self):
        self._refresh()

    def _refresh(self):
        api = settings.selected_api_level
        if api > 0:
            name = ANDROID_VERSION_NAMES.get(api, f"Android {api}")
            self.lbl_selected.setText(
                f"Selected: {name} (API {api}, {settings.selected_variant or 'default'})")
        else:
            self.lbl_selected.setText("Selected: none")
        self.completeChanged.emit()

    def _choose(self):
        dialog = AndroidVersionsDialog(self._sdk_manager, self)
        dialog.version_selected.connect(lambda *_: self._refresh())
        dialog.exec()
        self._refresh()

    def _skip(self):
        self._skipped = True
        self.btn_skip.setEnabled(False)
        self.completeChanged.emit()

    def isComplete(self) -> bool:
        return settings.selected_api_level > 0 or self._skipped


class SettingsPage(QWizardPage):
    """Final settings page."""

    def __init__(self):
        super().__init__()
        self.setTitle("Configuration")
        self.setSubTitle("Review and adjust settings")
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Performance mode
        layout.addWidget(QLabel("Performance Mode:"))
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Auto (Recommended)", "Low-Spec", "Normal"])
        layout.addWidget(self.combo_mode)

        # SDK path
        layout.addWidget(QLabel("SDK Location:"))
        sdk_layout = QHBoxLayout()
        self.edit_sdk = QLineEdit()
        self.edit_sdk.setText(str(PlatformUtils.get_sdk_dir()))
        self.edit_sdk.setReadOnly(True)
        sdk_layout.addWidget(self.edit_sdk)
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._browse_sdk)
        sdk_layout.addWidget(self.btn_browse)
        layout.addLayout(sdk_layout)

        # Auto-start options
        self.chk_auto_start = QCheckBox("Start emulator on app launch")
        self.chk_auto_start.setChecked(True)
        layout.addWidget(self.chk_auto_start)

        self.chk_minimize = QCheckBox("Minimize to tray on close")
        layout.addWidget(self.chk_minimize)

        layout.addStretch()

    def initializePage(self):
        # Set initial mode
        mode = self.wizard().property("performance_mode")
        if mode:
            mode_map = {PerformanceMode.LOW_SPEC: 1, PerformanceMode.NORMAL: 2}
            self.combo_mode.setCurrentIndex(mode_map.get(mode, 0))

    def _browse_sdk(self):
        path = QFileDialog.getExistingDirectory(self, "Select SDK Directory")
        if path:
            self.edit_sdk.setText(path)

    def validatePage(self) -> bool:
        # Save settings
        mode_idx = self.combo_mode.currentIndex()
        mode_map = {0: "auto", 1: "low_spec", 2: "normal"}
        settings.performance_mode = mode_map.get(mode_idx, "auto")
        settings.sdk_path = self.edit_sdk.text()
        settings.first_run_completed = True
        return True


class SetupWizard(QWizard):
    """Main setup wizard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("ApkPreview Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(600, 500)

        # Set dark theme
        self.setStyleSheet("""
            QWizard {
                background-color: #1e1e1e;
            }
            QLabel {
                color: #cccccc;
            }
            QPushButton {
                background-color: #007acc;
                color: white;
                border: none;
                border-radius: 4px;
                padding: 8px 16px;
                font-weight: 600;
            }
            QPushButton:hover { background-color: #005a9e; }
            QPushButton:disabled { background-color: #3a3a3a; color: #888; }
            QProgressBar {
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                background-color: #1e1e1e;
                text-align: center;
            }
            QProgressBar::chunk { background-color: #007acc; border-radius: 3px; }
            QComboBox, QLineEdit {
                background-color: #252526;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                padding: 4px 8px;
                color: #cccccc;
            }
            QTextEdit {
                background-color: #1e1e1e;
                border: 1px solid #3a3a3a;
                border-radius: 4px;
                color: #d4d4d4;
            }
        """)

        self.addPage(WelcomePage())
        self.addPage(InstallPage())
        self.addPage(AndroidVersionPage())
        self.addPage(SettingsPage())

        self.setButtonText(QWizard.WizardButton.NextButton, "Next >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< Back")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancel")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Finish")

        self.finished.connect(self._on_finished)

    def _on_finished(self, result: int):
        if result == QWizard.DialogCode.Accepted:
            settings.first_run_completed = True

    @staticmethod
    def run_if_needed(parent=None) -> bool:
        """Run wizard if first run, return True if ready to proceed."""
        if settings.get("first_run_completed", False):
            return True

        wizard = SetupWizard(parent)
        result = wizard.exec()
        return result == QWizard.DialogCode.Accepted