"""Nine-step, resumable first-run setup wizard.

Pages (state is saved after every page change, so closing the wizard and
reopening it resumes where the user left off):

1. Welcome
2. Standard or Custom install
3. SDK location + JDK 17 check
4. System check (RAM/CPU/disk/virtualization)
5. SDK license display + acceptance
6. Downloads (JDK if missing, cmdline-tools, platform-tools, emulator)
7. Device (the grouped, searchable bundled picker)
8. Android version (with download size)
9. Create AVD + finish

The wizard never starts the emulator.  All downloads go through
:mod:`core.bootstrap`, which is also used by the Windows installer.
"""

import threading
from pathlib import Path

from PyQt6.QtCore import QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFileDialog,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QProgressBar, QPushButton, QRadioButton, QTextEdit,
                             QVBoxLayout, QWidget, QWizard, QWizardPage)

from core.bootstrap import (BASE_COMPONENTS, COMPONENT_LABELS,
                            COMPONENT_SIZES_MB, BootstrapManager,
                            SetupState, format_mb, needs_setup)
from core.hardware_profile import (EmulatorConfig, PerformanceMode,
                                   get_default_config_for_mode)
from core.platform_utils import PlatformUtils
from core.sdk_manager import ANDROID_VERSION_NAMES, AndroidVersion, SDKManager
from core.settings import settings


# API levels offered when sdkmanager is not installed yet.  The version page
# refreshes this list from ``sdkmanager --list`` when it is available.
FALLBACK_API_LEVELS = (36, 35, 34, 33, 32, 31, 30, 29, 28)


def _size_for(version: AndroidVersion) -> str:
    if version.size_text:
        return version.size_text
    return f"~{version.estimate_size_gb():.1f} GB"


# ---------------------------------------------------------------------------
# Workers
# ---------------------------------------------------------------------------
class BootstrapWorker(QThread):
    """Install the selected bootstrap components off the UI thread."""

    progress = pyqtSignal(str, int, int, str)
    log = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, manager: BootstrapManager, keys):
        super().__init__()
        self._manager = manager
        self._keys = list(keys)
        self.cancel_event = threading.Event()

    def run(self):
        self._manager.progress = lambda step, current, total, message: \
            self.progress.emit(step, current, total, message)
        self._manager.log = self.log.emit
        try:
            ok, message = self._manager.install_components(
                self._keys, cancel=self.cancel_event)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            ok, message = False, f"Setup failed: {exc}"
        self.done.emit(ok, message)

    def cancel(self):
        self.cancel_event.set()


class VersionsWorker(QThread):
    """Load available system images without blocking the wizard."""

    loaded = pyqtSignal(object)

    def __init__(self, sdk: SDKManager):
        super().__init__()
        self._sdk = sdk

    def run(self):
        try:
            versions = self._sdk.list_system_images()
        except Exception:
            versions = []
        self.loaded.emit(versions)


class AvdWorker(QThread):
    """Download the chosen image (if needed) and create its AVD."""

    log = pyqtSignal(str)
    done = pyqtSignal(bool, str, str)

    def __init__(self, sdk: SDKManager, version: AndroidVersion,
                 config: EmulatorConfig):
        super().__init__()
        self._sdk = sdk
        self._version = version
        self._config = config
        self.cancel_event = threading.Event()

    def run(self):
        try:
            ok, message, avd = self._sdk.install_version(
                self._version, config=self._config, old_version=None,
                on_line=self.log.emit, cancel=self.cancel_event)
        except Exception as exc:  # noqa: BLE001
            ok, message, avd = False, f"Could not create the AVD: {exc}", ""
        self.done.emit(ok, message, avd or "")

    def cancel(self):
        self.cancel_event.set()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
class WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Welcome to AppStation")
        self.setSubTitle("Set up a local Android emulator. No Android Studio, "
                         "no phone, no account.")
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "This wizard checks your computer, installs the Android tools "
            "AppStation needs, and creates one emulator device.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        bullets = QLabel(
            "• Java 17, Android command-line tools, platform-tools and the "
            "emulator\n"
            "• The Android SDK licenses\n"
            "• One Android system image and its AVD")
        bullets.setWordWrap(True)
        layout.addWidget(bullets)

        note = QLabel(
            "Downloads can be several gigabytes. The wizard can be closed and "
            "reopened: it resumes at the step you reached. The emulator is "
            "never started during setup.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)
        layout.addStretch(1)

    def initializePage(self):
        self.wizard().state.step = 0
        self.wizard().state.save()


class InstallTypePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Standard or Custom")
        self.setSubTitle("Standard installs everything needed. Custom lets you "
                         "skip a component you already have.")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        self.radio_standard = QRadioButton(
            "Standard (recommended) — install/repair all required components")
        self.radio_custom = QRadioButton("Custom — choose components")
        group = QButtonGroup(self)
        group.addButton(self.radio_standard)
        group.addButton(self.radio_custom)
        self.radio_standard.setChecked(True)
        layout.addWidget(self.radio_standard)
        layout.addWidget(self.radio_custom)

        self.checkboxes = {}
        for key in BASE_COMPONENTS:
            box = QCheckBox(f"{COMPONENT_LABELS[key]} "
                            f"({format_mb(COMPONENT_SIZES_MB.get(key, 0))})")
            box.setChecked(True)
            box.setEnabled(False)
            self.checkboxes[key] = box
            layout.addWidget(box)

        self.radio_standard.toggled.connect(self._sync_mode)
        self.radio_custom.toggled.connect(self._sync_mode)
        self._jdk_required = False
        layout.addStretch(1)

    def _sync_mode(self, _checked=False):
        custom = self.radio_custom.isChecked()
        for key, box in self.checkboxes.items():
            if key == "jdk" and self._jdk_required:
                box.setChecked(True)
                box.setEnabled(False)
            else:
                box.setEnabled(custom)

    def initializePage(self):
        state = self.wizard().state
        self._jdk_required = not self.wizard().bootstrap.check_jdk().found
        if state.install_type == "custom":
            self.radio_custom.setChecked(True)
        else:
            self.radio_standard.setChecked(True)
        self._sync_mode()

    def components(self):
        if self.radio_standard.isChecked():
            return list(BASE_COMPONENTS)
        selected = [key for key, box in self.checkboxes.items()
                    if box.isChecked()]
        if self._jdk_required and "jdk" not in selected:
            selected.insert(0, "jdk")
        return selected

    def validatePage(self):
        selected = self.components()
        if not selected:
            QMessageBox.warning(self, "Choose components",
                                "Select at least one component to install.")
            return False
        wizard = self.wizard()
        wizard.components = selected
        wizard.state.install_type = ("custom" if self.radio_custom.isChecked()
                                     else "standard")
        wizard.state.save()
        return True


class SdkPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("SDK location and Java 17")
        self.setSubTitle("AppStation keeps its own SDK by default. You can "
                         "point it at an existing Android SDK instead.")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Android SDK folder"))
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText(str(PlatformUtils.get_sdk_dir()))
        path_row.addWidget(self.edit_path, 1)
        self.btn_browse = QPushButton("Browse...")
        self.btn_browse.clicked.connect(self._browse)
        path_row.addWidget(self.btn_browse)
        layout.addLayout(path_row)

        self.lbl_jdk = QLabel("")
        self.lbl_jdk.setWordWrap(True)
        layout.addWidget(self.lbl_jdk)

        self.btn_check = QPushButton("Check Java again")
        self.btn_check.clicked.connect(self._refresh_jdk)
        layout.addWidget(self.btn_check)

        note = QLabel(
            "Java 17 is required by sdkmanager. If it is missing, step 6 "
            "downloads a private copy into AppStation's data folder; your "
            "system Java is not modified.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)
        layout.addStretch(1)

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose Android SDK folder", self.edit_path.text())
        if folder:
            self.edit_path.setText(folder)

    def _refresh_jdk(self):
        status = self.wizard().bootstrap.check_jdk()
        if status.found:
            self.lbl_jdk.setText(f"✓ {status.message}: {status.path}")
            self.lbl_jdk.setStyleSheet("color: #4ec9b0;")
        else:
            self.lbl_jdk.setText(
                "Java 17 not found. It will be downloaded in step 6.")
            self.lbl_jdk.setStyleSheet("color: #e0a030;")

    def initializePage(self):
        state = self.wizard().state
        path = state.sdk_path or str(PlatformUtils.get_sdk_dir())
        self.edit_path.setText(path)
        self._refresh_jdk()

    def validatePage(self):
        raw = self.edit_path.text().strip()
        if not raw:
            QMessageBox.warning(self, "SDK location",
                                "Choose an SDK folder.")
            return False
        path = Path(raw).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "SDK location",
                                f"Cannot create {path}:\n{exc}")
            return False
        self.wizard().set_sdk_path(path)
        return True


class SystemCheckPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("System check")
        self.setSubTitle("AppStation works best with 8 GB RAM, 4 CPU threads, "
                         "10 GB free disk and hardware virtualization.")
        layout = QVBoxLayout(self)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

    def initializePage(self):
        report = self.wizard().bootstrap.system_report()
        virt = report["virtualization"]
        lines = [
            f"Operating system: {report['os']}",
            f"Architecture: {report['architecture']}",
            f"CPU: {report['physical_cores']} physical / "
            f"{report['logical_cores']} logical threads",
            f"RAM: {report['ram_mb']} MB",
            f"Free disk: {report['free_disk_gb']:.1f} GB",
            f"Virtualization: {virt.get('message', 'unknown')}",
            f"Java: {report['jdk'].message}",
        ]
        self.details.setPlainText("\n".join(lines))
        if report["warnings"]:
            self.warning.setText("Warnings:\n• " +
                                 "\n• ".join(report["warnings"]))
            self.warning.setStyleSheet("color: #e0a030;")
        else:
            self.warning.setText("No blocking problems found.")
            self.warning.setStyleSheet("color: #4ec9b0;")


class LicensePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Android SDK licenses")
        self.setSubTitle("Read the license names and open the official text "
                         "before accepting.")
        layout = QVBoxLayout(self)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        layout.addWidget(self.text, 1)

        self.btn_open = QPushButton("Open official license page")
        self.btn_open.clicked.connect(self._open_license)
        layout.addWidget(self.btn_open)

        self.check = QCheckBox(
            "I have read and agree to the Android SDK License Agreement")
        self.check.toggled.connect(self.completeChanged.emit)
        layout.addWidget(self.check)

    def _open_license(self):
        QDesktopServices.openUrl(
            QUrl("https://developer.android.com/studio/terms"))

    def initializePage(self):
        self.text.setPlainText(self.wizard().bootstrap.license_text())
        if self.wizard().bootstrap.licenses_accepted():
            self.check.setChecked(True)

    def isComplete(self):
        return self.check.isChecked()

    def validatePage(self):
        if not self.check.isChecked():
            QMessageBox.warning(
                self, "Licenses",
                "Accept the Android SDK license to continue.")
            return False
        self.wizard().state.licenses_accepted = True
        self.wizard().state.save()
        return True


class DownloadPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Download Android requirements")
        self.setSubTitle("Downloads can be resumed, retried and cancelled. "
                         "Checksums are verified when the publisher provides "
                         "them.")
        self._worker = None
        self._complete = False

        layout = QVBoxLayout(self)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)

        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.btn_start = QPushButton("Start downloads")
        self.btn_start.clicked.connect(self.start_downloads)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.cancel_downloads)
        self.btn_cancel.setEnabled(False)
        self.btn_retry = QPushButton("Retry failed component")
        self.btn_retry.clicked.connect(self.start_downloads)
        self.btn_retry.setEnabled(False)
        buttons.addWidget(self.btn_start)
        buttons.addWidget(self.btn_cancel)
        buttons.addWidget(self.btn_retry)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)

    def initializePage(self):
        manager = self.wizard().bootstrap
        missing = [key for key in self.wizard().components
                   if not manager.is_component_installed(key)]
        required_mb = sum(COMPONENT_SIZES_MB.get(key, 0) for key in missing)
        self.summary.setText(
            f"Components to install: "
            f"{', '.join(COMPONENT_LABELS.get(k, k) for k in missing) or 'none'}"
            f"  ({format_mb(required_mb)})")
        ok, message = manager.disk_space_ok(required_mb)
        if not ok:
            self.status.setText(message)
            self.status.setStyleSheet("color: #e06060;")
            self.btn_start.setEnabled(False)
        else:
            self.status.setText(f"Disk check: {message}")
            self.status.setStyleSheet("color: #4ec9b0;")
            self.btn_start.setEnabled(not self._complete)
        if not missing:
            self._complete = True
            self.progress.setValue(100)
            self.status.setText("All required components are already installed.")
            self.btn_start.setEnabled(False)
            self.completeChanged.emit()

    def isComplete(self):
        if self._complete:
            return True
        manager = self.wizard().bootstrap
        return all(manager.is_component_installed(key)
                   for key in self.wizard().components)

    def start_downloads(self):
        if self._worker is not None and self._worker.isRunning():
            return
        manager = self.wizard().bootstrap
        self.btn_start.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.progress.setValue(0)
        self.log.clear()
        self.status.setText("Starting downloads...")
        self.status.setStyleSheet("")

        self._worker = BootstrapWorker(manager, self.wizard().components)
        self._worker.progress.connect(self._on_progress)
        self._worker.log.connect(self._append_log)
        self._worker.done.connect(self._on_done)
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def cancel_downloads(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("Cancelling after the current chunk...")

    def _on_progress(self, step, current, total, message):
        if total > 1:
            self.progress.setValue(int(current * 100 / total))
        else:
            self.progress.setValue(0)
        self.status.setText(f"{step}: {message}")

    def _append_log(self, line):
        self.log.append(line)
        self.log.verticalScrollBar().setValue(
            self.log.verticalScrollBar().maximum())

    def _on_done(self, ok, message):
        self.btn_cancel.setEnabled(False)
        if ok:
            self._complete = True
            self.progress.setValue(100)
            self.status.setText("Android requirements installed.")
            self.status.setStyleSheet("color: #4ec9b0;")
            self.wizard().state.components_done = list(self.wizard().components)
            self.wizard().state.save()
            self.completeChanged.emit()
        else:
            self.status.setText(f"Download failed: {message}")
            self.status.setStyleSheet("color: #e06060;")
            self.btn_start.setEnabled(True)
            self.btn_retry.setEnabled(True)

    def _on_thread_finished(self):
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None


class VersionPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Choose an Android version")
        self.setSubTitle("Plain 'default' images are recommended for the "
                         "fastest boot. The download size is shown.")
        self._versions = []
        self._worker = None

        layout = QVBoxLayout(self)
        self.combo = QComboBox()
        self.combo.currentIndexChanged.connect(self.completeChanged.emit)
        layout.addWidget(self.combo)

        self.details = QLabel("")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)

        self.loading = QLabel("")
        self.loading.setStyleSheet("color: #888;")
        layout.addWidget(self.loading)
        layout.addStretch(1)

    def initializePage(self):
        self._populate_fallback()
        sdk = SDKManager()
        if sdk.is_sdk_installed():
            self.loading.setText("Refreshing the list from sdkmanager...")
            self._worker = VersionsWorker(sdk)
            self._worker.loaded.connect(self._on_loaded)
            self._worker.finished.connect(self._on_finished)
            self._worker.start()
        else:
            self.loading.setText(
                "sdkmanager is not installed yet; showing known Android "
                "versions with estimated sizes.")

    def _populate_fallback(self):
        versions = []
        abi = PlatformUtils.get_image_abi()
        sdk = SDKManager()
        for level in FALLBACK_API_LEVELS:
            installed = sdk.is_system_image_installed(abi, level, "default")
            versions.append(AndroidVersion(
                api_level=level, variant="default", abi=abi,
                installed=installed))
        self._set_versions(versions)

    def _on_loaded(self, versions):
        if not versions:
            self.loading.setText(
                "sdkmanager returned no list; keeping the known versions.")
            return
        # Keep one row per (api, variant), prefer the installed/default row.
        merged = {}
        for version in versions:
            key = (version.api_level, version.variant)
            previous = merged.get(key)
            if previous is None or version.installed:
                merged[key] = version
        ordered = sorted(
            merged.values(),
            key=lambda v: (-v.api_level,
                           0 if v.variant == "default" else 1,
                           v.variant))
        self.loading.setText("List refreshed from sdkmanager.")
        self._set_versions(ordered)

    def _set_versions(self, versions):
        self._versions = versions
        state = self.wizard().state
        self.combo.blockSignals(True)
        self.combo.clear()
        for version in versions:
            name = ANDROID_VERSION_NAMES.get(version.api_level,
                                             f"Android {version.api_level}")
            label = (f"{name} (API {version.api_level}) — "
                     f"{version.variant.replace('_', ' ')} — "
                     f"{_size_for(version)}")
            if version.installed:
                label += "  [installed]"
            self.combo.addItem(label, version)
        target = 0
        for index, version in enumerate(versions):
            if (version.api_level == state.api_level
                    and version.variant == state.variant):
                target = index
                break
        else:
            for index, version in enumerate(versions):
                if version.installed:
                    target = index
                    break
            else:
                for index, version in enumerate(versions):
                    if version.api_level == 34:
                        target = index
                        break
        self.combo.setCurrentIndex(target)
        self.combo.blockSignals(False)
        self._refresh_details()
        self.completeChanged.emit()

    def _refresh_details(self):
        version = self.selected_version()
        if version is None:
            self.details.setText("")
            return
        name = ANDROID_VERSION_NAMES.get(version.api_level,
                                         f"Android {version.api_level}")
        self.details.setText(
            f"{name} (API {version.api_level}), {version.variant}, "
            f"{version.abi}. Download size {_size_for(version)}. "
            f"{'Already installed.' if version.installed else 'Will be downloaded.'}")

    def selected_version(self):
        data = self.combo.currentData()
        return data if isinstance(data, AndroidVersion) else None

    def isComplete(self):
        return self.selected_version() is not None

    def validatePage(self):
        version = self.selected_version()
        if version is None:
            return False
        state = self.wizard().state
        state.api_level = version.api_level
        state.variant = version.variant
        state.save()
        return True

    def _on_finished(self):
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None


class FinishPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Create the emulator")
        self.setSubTitle("AppStation downloads the selected image if needed, "
                         "creates its AVD, then finishes. The emulator is not "
                         "started.")
        self._worker = None
        self._complete = False

        layout = QVBoxLayout(self)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        layout.addWidget(self.summary, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.btn_create = QPushButton("Create AVD and finish")
        self.btn_create.clicked.connect(self.create_avd)
        layout.addWidget(self.btn_create)

    def initializePage(self):
        state = self.wizard().state
        name = ANDROID_VERSION_NAMES.get(state.api_level,
                                         f"Android {state.api_level}")
        lines = [
            f"SDK folder: {self.wizard().bootstrap.sdk_dir}",
            f"Android: {name} (API {state.api_level}, "
            f"{state.variant or 'default'})",
            f"Components: {', '.join(self.wizard().components)}",
        ]
        self.summary.setPlainText("\n".join(lines))
        if state.completed:
            self._complete = True
            self.progress.setValue(100)
            self.status.setText("Setup is already complete.")
            self.btn_create.setEnabled(False)
            self.completeChanged.emit()

    def isComplete(self):
        return self._complete

    def create_avd(self):
        if self._worker is not None and self._worker.isRunning():
            return
        state = self.wizard().state
        version = AndroidVersion(
            api_level=state.api_level, variant=state.variant or "default",
            abi=PlatformUtils.get_image_abi())
        if version.api_level <= 0:
            QMessageBox.warning(self, "Android version",
                                "Choose an Android version first.")
            return

        self.btn_create.setEnabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("Preparing the system image and AVD...")
        self._worker = AvdWorker(SDKManager(), version,
                                 get_default_config_for_mode(
                                     PerformanceMode.NORMAL))
        self._worker.log.connect(self._append_log)
        self._worker.done.connect(lambda ok, msg, avd:
                                  self._on_done(ok, msg, avd, version))
        self._worker.finished.connect(self._on_thread_finished)
        self._worker.start()

    def _append_log(self, line):
        self.status.setText(line)

    def _on_done(self, ok, message, avd_name, version):
        self.progress.setRange(0, 100)
        if ok:
            self._complete = True
            self.progress.setValue(100)
            self.status.setText(f"{message} — AVD '{avd_name}' is ready.")
            self.status.setStyleSheet("color: #4ec9b0;")
            settings.sdk_path = self.wizard().bootstrap.sdk_dir
            settings.select_android_version(
                version.api_level, version.variant, avd_name)
            settings.first_run_completed = True
            self.wizard().state.completed = True
            self.wizard().state.save()
            self.completeChanged.emit()
        else:
            self.progress.setValue(0)
            self.status.setText(f"Failed: {message}")
            self.status.setStyleSheet("color: #e06060;")
            self.btn_create.setEnabled(True)

    def _on_thread_finished(self):
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
class SetupWizard(QWizard):
    """Resumable nine-page setup wizard."""

    def __init__(self, parent=None, force: bool = False):
        super().__init__(parent)
        self.setWindowTitle("AppStation Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.resize(780, 640)
        self.components = list(BASE_COMPONENTS)

        self.bootstrap = BootstrapManager()
        if force:
            self.bootstrap.state.reset()
            self.bootstrap.state = SetupState.load()
        self.state = self.bootstrap.state

        self.addPage(WelcomePage())
        self.addPage(InstallTypePage())
        self.addPage(SdkPage())
        self.addPage(SystemCheckPage())
        self.addPage(LicensePage())
        self.addPage(DownloadPage())
        self.addPage(VersionPage())
        self.addPage(FinishPage())

        self.setStartId(min(max(int(self.state.step or 0), 0), 7))
        self.currentIdChanged.connect(self._save_step)
        self.finished.connect(self._on_finished)

        self.setButtonText(QWizard.WizardButton.NextButton, "Next >")
        self.setButtonText(QWizard.WizardButton.BackButton, "< Back")
        self.setButtonText(QWizard.WizardButton.CancelButton, "Cancel")
        self.setButtonText(QWizard.WizardButton.FinishButton, "Finish")
        self.setStyleSheet(self._stylesheet())

    @staticmethod
    def _stylesheet() -> str:
        return """
            QWizard, QWizardPage { background-color: #1e1e1e; }
            QLabel { color: #cccccc; }
            QPushButton {
                background-color: #007acc; color: white; border: none;
                border-radius: 4px; padding: 7px 14px; font-weight: 600;
            }
            QPushButton:hover { background-color: #005a9e; }
            QPushButton:disabled { background-color: #3a3a3a; color: #888; }
            QProgressBar {
                border: 1px solid #3a3a3a; border-radius: 4px;
                background-color: #1e1e1e; text-align: center;
            }
            QProgressBar::chunk { background-color: #007acc; border-radius: 3px; }
            QComboBox, QLineEdit {
                background-color: #252526; border: 1px solid #3a3a3a;
                border-radius: 4px; padding: 5px 8px; color: #cccccc;
            }
            QTextEdit {
                background-color: #1e1e1e; border: 1px solid #3a3a3a;
                border-radius: 4px; color: #d4d4d4;
            }
            QRadioButton, QCheckBox { color: #cccccc; }
        """

    def set_sdk_path(self, path: Path):
        path = Path(path).expanduser()
        settings.sdk_path = path
        self.state.sdk_path = str(path)
        self.state.save()
        # Rebuild the manager against the chosen folder, keeping state.
        state_path = self.bootstrap._state_path
        self.bootstrap = BootstrapManager(sdk_dir=path, state_path=state_path)
        self.bootstrap.state = self.state

    def _save_step(self, page_id: int):
        self.state.step = int(page_id)
        self.state.save()

    def _on_finished(self, result: int):
        if result == QWizard.DialogCode.Accepted:
            settings.first_run_completed = True
            self.state.completed = True
            self.state.save()

    @staticmethod
    def run_if_needed(parent=None) -> bool:
        """Open the wizard on first launch/missing components."""
        if not needs_setup():
            return True
        wizard = SetupWizard(parent)
        return wizard.exec() == QWizard.DialogCode.Accepted
