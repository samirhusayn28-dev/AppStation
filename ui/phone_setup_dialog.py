"""Guided USB phone setup.

A non-technical user should be able to get from "phone in a drawer" to "app on
screen" without touching BIOS or Windows Features.  This dialog explains the
one-time phone settings in plain steps, then scans ADB and lets the user pick
the device it found.  It never starts an emulator.
"""

from typing import List

from PyQt6.QtCore import QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                             QListWidgetItem, QPushButton, QVBoxLayout)

from core.adb import ADB


class _ScanWorker(QThread):
    """Scan for ADB devices off the GUI thread (adb can block)."""

    done = pyqtSignal(list)

    def run(self):
        try:
            devices: List[dict] = ADB().list_devices()
        except Exception:
            devices = []
        self.done.emit(devices)


class PhoneSetupDialog(QDialog):
    """Find a phone over USB and return its ADB serial."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect your Android phone")
        self.setMinimumWidth(480)
        self._serial = ""
        self._worker = None

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Use your own phone instead of the emulator. Nothing is installed "
            "on the computer and no BIOS settings are needed.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        steps = QLabel(
            "1. Connect the phone to this computer with a USB cable.\n"
            "2. On the phone, open Settings → About phone and tap "
            "'Build number' 7 times.\n"
            "3. Go back to Settings → Developer options and switch on "
            "'USB debugging'.\n"
            "4. If the phone asks 'Allow USB debugging?', tap Allow.\n"
            "5. Click 'Find my phone' below.")
        steps.setWordWrap(True)
        steps.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(steps)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection)
        self.list_widget.itemSelectionChanged.connect(self._update_use_button)
        layout.addWidget(self.list_widget, 1)

        self.status = QLabel("No scan yet.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.btn_scan = QPushButton("Find my phone")
        self.btn_scan.clicked.connect(self._scan)
        self.btn_use = QPushButton("Use this phone")
        self.btn_use.setEnabled(False)
        self.btn_use.clicked.connect(self._use_selected)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(self.btn_scan)
        buttons.addWidget(self.btn_use)
        buttons.addStretch(1)
        buttons.addWidget(btn_cancel)
        layout.addLayout(buttons)

    # -- scanning -----------------------------------------------------------
    def _scan(self):
        if self._worker is not None and self._worker.isRunning():
            return
        self.btn_scan.setEnabled(False)
        self.status.setText("Looking for phones...")
        self._worker = _ScanWorker()
        self._worker.done.connect(self._populate)
        self._worker.finished.connect(self._on_scan_finished)
        self._worker.start()

    def _on_scan_finished(self):
        self.btn_scan.setEnabled(True)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    def _populate(self, devices: List[dict]):
        self.list_widget.clear()
        phones = [d for d in devices
                  if not d.get("serial", "").startswith("emulator-")]
        for device in phones:
            serial = device.get("serial", "")
            state = device.get("state", "")
            model = device.get("model") or serial
            item = QListWidgetItem(f"{model}  ({state})")
            item.setData(Qt.ItemDataRole.UserRole, serial)
            item.setData(Qt.ItemDataRole.UserRole + 1, state)
            if state == "unauthorized":
                item.setToolTip("Unlock the phone and tap 'Allow USB debugging'")
            elif state != "device":
                item.setToolTip("Wait for the phone to finish connecting")
            self.list_widget.addItem(item)

        if not phones:
            self.status.setText(
                "No phone found. Check the cable, unlock the phone, and make "
                "sure 'USB debugging' is on, then scan again.")
            return
        ready = [d for d in phones if d.get("state") == "device"]
        if ready:
            self.status.setText(
                f"Found {len(ready)} phone(s) ready to use. Select one and "
                "click 'Use this phone'.")
        else:
            self.status.setText(
                "Phone found but not authorized yet. Unlock it and tap "
                "'Allow USB debugging', then scan again.")

    def _update_use_button(self):
        item = self.list_widget.currentItem()
        ok = False
        if item is not None:
            ok = (bool(item.data(Qt.ItemDataRole.UserRole))
                  and item.data(Qt.ItemDataRole.UserRole + 1) == "device")
        self.btn_use.setEnabled(ok)

    def _use_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            return
        if item.data(Qt.ItemDataRole.UserRole + 1) != "device":
            return
        self._serial = item.data(Qt.ItemDataRole.UserRole) or ""
        if self._serial:
            self.accept()

    def selected_serial(self) -> str:
        return self._serial
