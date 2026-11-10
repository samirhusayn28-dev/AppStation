"""Android versions dialog - list / download / remove / select system images.

Features:
  * a table of selectable Android system images for the host CPU;
  * Download / Select replace an already-installed version after confirmation;
  * the old version is only removed after the new image + AVD exist, so a
    failed or cancelled download never deletes anything;
  * a per-version Remove button (with confirmation);
  * a free-disk guard (download size + ``DISK_HEADROOM_GB``).

Long-running work runs on worker threads so the UI never blocks.  Layouts only
- no setGeometry/move/setFixed* on any child widget.
"""

import threading
from typing import Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QTableWidget, QTableWidgetItem, QProgressBar,
                             QVBoxLayout, QHeaderView, QAbstractItemView,
                             QMessageBox, QCheckBox)

from core.sdk_manager import (SDKManager, AndroidVersion, DISK_HEADROOM_GB,
                              parse_progress_percent)
from core.hardware_profile import HardwareProfiler
from core.platform_utils import PlatformUtils
from core.settings import settings


class _TaskWorker(QThread):
    """Run one blocking SDK task off the UI thread.

    ``fn(progress_cb, cancel_event)`` must return ``(ok, message, payload)``.
    ``progress_cb(percent, message)`` uses ``percent < 0`` to mean "no known
    percentage, only the message changed".
    """

    progress = pyqtSignal(int, str)
    done = pyqtSignal(bool, str, object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        def report(percent: int, message: str = ""):
            self.progress.emit(int(percent), message)

        try:
            ok, message, payload = self._fn(report, self._cancel)
        except Exception as e:  # never let a worker take down the app
            ok, message, payload = False, f"Error: {e}", None
        self.done.emit(bool(ok), str(message), payload)


class _ReplaceDialog(QDialog):
    """Confirmation for replacing an installed Android version.

    Shows the free space before and after the (optional) removal and exposes
    the "Remove old version after download" choice via :attr:`remove_old`.
    """

    def __init__(self, old: AndroidVersion, new: AndroidVersion,
                 free_gb: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Replace Android version")
        self.remove_old = True

        layout = QVBoxLayout(self)

        old_label = QLabel(
            f"Replace {old.version_name} (API {old.api_level}, "
            f"{old.variant_display})?")
        layout.addWidget(old_label)

        old_size = old.estimate_size_gb()
        new_size = new.estimate_size_gb()
        net_change = old_size - new_size
        free_after = max(0.0, free_gb + net_change)
        info = QLabel(
            f"The old version's system image and its emulator AVD (apps and "
            f"snapshots inside it) will be deleted only after the new version "
            f"is installed and its AVD is ready.\n\n"
            f"Removing the old version frees about {old_size:.1f} GB; the new "
            f"download uses about {new_size:.1f} GB "
            f"(net change {net_change:+.1f} GB).\n\n"
            f"Free disk space: {free_gb:.1f} GB now, about "
            f"{free_after:.1f} GB after replacement.")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.chk_remove = QCheckBox("Remove old version after download")
        self.chk_remove.setChecked(True)
        layout.addWidget(self.chk_remove)

        buttons = QHBoxLayout()
        self.btn_continue = QPushButton("Continue")
        self.btn_cancel = QPushButton("Cancel")
        buttons.addStretch()
        buttons.addWidget(self.btn_continue)
        buttons.addWidget(self.btn_cancel)
        layout.addLayout(buttons)

        self.btn_continue.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

    def chosen_remove_old(self) -> bool:
        return self.remove_old and self.chk_remove.isChecked()

    def accept(self):
        self.remove_old = self.chk_remove.isChecked()
        super().accept()


class AndroidVersionsDialog(QDialog):
    """Table of Android system images with Download / Remove / Select actions."""

    # Emitted after a version is successfully selected: (api_level, variant).
    version_selected = pyqtSignal(int, str)

    def __init__(self, sdk: Optional[SDKManager] = None, parent=None,
                 emulator=None):
        super().__init__(parent)
        self._sdk = sdk if sdk is not None else SDKManager()
        self._emulator = emulator
        self._versions = []
        self._busy = False
        self._task_kind = ""
        self._worker = None
        self._row_buttons = []

        self.setMinimumSize(760, 480)

        self._build_ui()
        self._refresh_disk_label()
        self._populate_table()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        self.lbl_disk = QLabel("Free disk space: --")
        header.addWidget(self.lbl_disk)
        header.addStretch()
        self.btn_refresh = QPushButton("Refresh")
        header.addWidget(self.btn_refresh)
        layout.addLayout(header)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Android", "API", "Variant", "Status", "Size", "Remove"])
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.lbl_status = QLabel("")
        layout.addWidget(self.lbl_status)

        buttons = QHBoxLayout()
        self.btn_download = QPushButton("Download")
        self.btn_remove = QPushButton("Remove")
        self.btn_select = QPushButton("Select")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.setVisible(False)
        self.btn_close = QPushButton("Close")
        buttons.addWidget(self.btn_download)
        buttons.addWidget(self.btn_remove)
        buttons.addWidget(self.btn_select)
        buttons.addWidget(self.btn_cancel)
        buttons.addStretch()
        buttons.addWidget(self.btn_close)
        layout.addLayout(buttons)

        self.btn_refresh.clicked.connect(self.refresh)
        self.btn_download.clicked.connect(self._on_download)
        self.btn_remove.clicked.connect(self._on_remove)
        self.btn_select.clicked.connect(self._on_select)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_close.clicked.connect(self.accept)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self._update_buttons()

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------
    def _refresh_disk_label(self):
        free = self._free_gb()
        self.lbl_disk.setText(f"Free disk space: {free:.1f} GB")

    def _free_gb(self) -> float:
        try:
            return float(PlatformUtils.get_disk_info().get("free_gb", 0) or 0)
        except Exception:
            return 0.0

    def _is_selected(self, version: AndroidVersion) -> bool:
        return (settings.selected_api_level == version.api_level
                and settings.selected_variant == version.variant)

    def _populate_table(self):
        self.table.setRowCount(0)
        self._row_buttons = []
        for version in self._versions:
            row = self.table.rowCount()
            self.table.insertRow(row)

            name_item = QTableWidgetItem(version.version_name)
            name_item.setData(
                Qt.ItemDataRole.UserRole,
                (version.api_level, version.variant))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, QTableWidgetItem(f"API {version.api_level}"))
            self.table.setItem(row, 2, QTableWidgetItem(version.variant_display))

            status = "Installed" if version.installed else "Not installed"
            if self._is_selected(version):
                status += "  \u2022 selected"
            self.table.setItem(row, 3, QTableWidgetItem(status))
            self.table.setItem(row, 4, QTableWidgetItem(version.size_display()))

            if version.installed:
                btn = QPushButton("Remove")
                btn.setEnabled(not self._busy)
                btn.clicked.connect(
                    lambda _checked=False, v=version: self._remove_version(v))
                self.table.setCellWidget(row, 5, btn)
                self._row_buttons.append(btn)

        if not self._versions:
            self._set_status("No Android versions found.")
        self._update_buttons()

    def _current_version(self) -> Optional[AndroidVersion]:
        items = self.table.selectedItems()
        if not items:
            return None
        row = items[0].row()
        key_item = self.table.item(row, 0)
        if key_item is None:
            return None
        key = key_item.data(Qt.ItemDataRole.UserRole)
        for version in self._versions:
            if (version.api_level, version.variant) == key:
                return version
        return None

    def _update_buttons(self):
        version = self._current_version()
        installed = bool(version and version.installed)
        self.btn_download.setEnabled(
            bool(version) and not installed and not self._busy)
        self.btn_remove.setEnabled(installed and not self._busy)
        self.btn_select.setEnabled(installed and not self._busy)
        self.btn_refresh.setEnabled(not self._busy)
        for btn in self._row_buttons:
            btn.setEnabled(not self._busy)

    # ------------------------------------------------------------------
    # Emulator guard
    # ------------------------------------------------------------------
    def _emulator_running(self) -> bool:
        if self._emulator is None:
            return False
        try:
            return bool(self._emulator.is_running())
        except Exception:
            return False

    def _version_is_running(self, version: AndroidVersion) -> bool:
        """True if the running emulator is the given (old) version."""
        if not self._emulator_running():
            return False
        try:
            return ((version.api_level, version.variant)
                    == (settings.selected_api_level,
                        settings.selected_variant))
        except Exception:
            return False

    def _refuse_running(self):
        message = "Stop the emulator first"
        self._set_status(message)
        QMessageBox.warning(self, "Emulator running", message)

    # ------------------------------------------------------------------
    # Busy state / worker plumbing
    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool):
        self._busy = busy
        self.progress.setVisible(busy)
        self.btn_cancel.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)  # indeterminate until a % arrives
        else:
            self.progress.setRange(0, 100)
        self._update_buttons()

    def _set_status(self, message: str):
        self.lbl_status.setText(message or "")

    def _start_task(self, kind: str, fn):
        if self._busy:
            return
        self._task_kind = kind
        self._set_busy(True)
        self._worker = _TaskWorker(fn, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(self._on_task_done)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_progress(self, percent: int, message: str):
        if percent >= 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(percent)
        if message:
            self._set_status(message.strip())

    def _on_task_done(self, ok: bool, message: str, payload):
        kind = self._task_kind
        self._task_kind = ""
        self._worker = None
        self._set_busy(False)
        self._refresh_disk_label()
        self._set_status(message)

        if kind == "list":
            if ok:
                self._versions = payload or []
            self._populate_table()
        elif kind == "install":
            self.refresh()  # re-list so installed flags update
            if ok and isinstance(payload, AndroidVersion):
                self.version_selected.emit(
                    payload.api_level, payload.variant)
        elif kind == "remove":
            self.refresh()
        elif kind == "select":
            if ok and isinstance(payload, AndroidVersion):
                self.version_selected.emit(
                    payload.api_level, payload.variant)
            self.refresh()

    # ------------------------------------------------------------------
    # Replacement helpers
    # ------------------------------------------------------------------
    def _other_installed_version(
            self, target: AndroidVersion) -> Optional[AndroidVersion]:
        """The installed version the replace flow would remove, if any.

        Prefers the currently selected version (which is what the user thinks
        of as "the old one").
        """
        candidates = [
            v for v in self._versions
            if v.installed
            and (v.api_level, v.variant) != (target.api_level, target.variant)]
        if not candidates:
            return None
        selected = (settings.selected_api_level, settings.selected_variant)
        for v in candidates:
            if (v.api_level, v.variant) == selected:
                return v
        candidates.sort(key=lambda v: -v.api_level)
        return candidates[0]

    def _confirm_replace(self, old: AndroidVersion,
                         new: AndroidVersion):
        """Ask whether to replace ``old``.  Returns ``(go, remove_old)``."""
        dialog = _ReplaceDialog(old, new, self._free_gb(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False, False
        return True, dialog.remove_old

    def _confirm_remove(self, version: AndroidVersion) -> bool:
        confirm = QMessageBox.question(
            self, "Remove Android version",
            f"Remove {version.version_name} (API {version.api_level}, "
            f"{version.variant_display})?\n\n"
            "This deletes the downloaded system image and the emulator "
            "(apps and snapshots inside it).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return confirm == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def refresh(self):
        """Re-list Android versions (runs on a worker thread)."""
        if self._busy:
            return

        def task(report, cancel):
            report(-1, "Listing Android versions...")
            versions = self._sdk.list_system_images(cancel=cancel)
            return True, "Android versions updated", versions

        self._start_task("list", task)

    def _on_download(self):
        version = self._current_version()
        if version is None or version.installed:
            return

        old = self._other_installed_version(version)
        remove_old = False
        if old is not None:
            if self._version_is_running(old):
                self._refuse_running()
                return
            go, remove_old = self._confirm_replace(old, version)
            if not go:
                return
            # Deleting the old image/AVD while *any* emulator is running is
            # unsafe, even when the running AVD is a different version.
            if remove_old and self._emulator_running():
                self._refuse_running()
                return

        free_gb = self._free_gb()
        needed_gb = version.estimate_size_gb()
        if free_gb < needed_gb + DISK_HEADROOM_GB:
            message = (
                f"Not enough disk space: {free_gb:.1f} GB free, need about "
                f"{needed_gb + DISK_HEADROOM_GB:.1f} GB "
                f"({needed_gb:.1f} GB download + "
                f"{DISK_HEADROOM_GB:.0f} GB headroom).")
            self._set_status(message)
            QMessageBox.warning(self, "Not enough disk space", message)
            return

        if old is not None and remove_old:
            def replace_task(report, cancel):
                def on_line(line: str):
                    percent = parse_progress_percent(line)
                    report(-1 if percent is None else percent, line)

                ok, message, avd_name = self._sdk.install_version(
                    version,
                    config=HardwareProfiler().get_emulator_config(),
                    old_version=old, on_line=on_line, cancel=cancel)
                if not ok or not avd_name:
                    return False, message, None
                settings.select_android_version(
                    version.api_level, version.variant, avd_name)
                return True, message, version

            self._start_task("install", replace_task)
            return

        def download_task(report, cancel):
            def on_line(line: str):
                percent = parse_progress_percent(line)
                report(-1 if percent is None else percent, line)

            ok, message = self._sdk.install_system_image(
                version, on_line=on_line, cancel=cancel)
            return ok, message, None

        self._start_task("install", download_task)

    def _on_select(self):
        version = self._current_version()
        if version is None or not version.installed:
            return

        old = self._other_installed_version(version)
        remove_old = False
        if old is not None:
            if self._version_is_running(old):
                self._refuse_running()
                return
            go, remove_old = self._confirm_replace(old, version)
            if not go:
                return
            # Deleting the old image/AVD while *any* emulator is running is
            # unsafe, even when the running AVD is a different version.
            if remove_old and self._emulator_running():
                self._refuse_running()
                return

        def task(report, cancel):
            report(-1, f"Preparing {version.version_name}...")
            config = HardwareProfiler().get_emulator_config()
            if old is not None and remove_old:
                ok, message, avd_name = self._sdk.install_version(
                    version, config=config, old_version=old)
            else:
                ok, message, avd_name = self._sdk.ensure_avd_for(
                    version, config=config, data_partition_gb=3.0)
            if not ok or not avd_name:
                return False, message, None
            settings.select_android_version(
                version.api_level, version.variant, avd_name)
            return True, f"Selected {version.version_name} (AVD: {avd_name})", version

        self._start_task("select", task)

    def _on_remove(self):
        version = self._current_version()
        if version is None or not version.installed:
            return
        self._remove_version(version)

    def _remove_version(self, version: AndroidVersion):
        if version is None or not version.installed or self._busy:
            return
        # Refuse while any emulator is running: the emulator may hold the
        # image or AVD files open even if it is not the selected version.
        if self._emulator_running():
            self._refuse_running()
            return
        if not self._confirm_remove(version):
            return

        def task(report, cancel):
            report(-1, f"Removing {version.version_name}...")
            ok, message = self._sdk.remove_version(version)
            if self._is_selected(version):
                settings.select_android_version(0, "", "")
            return ok, message, version

        self._start_task("remove", task)

    def _on_cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self._set_status("Cancelling...")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(5000)
        super().closeEvent(event)
