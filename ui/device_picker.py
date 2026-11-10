"""Searchable, grouped device picker (Brand > Category > Device).

The picker is the only place that lists all bundled devices.  It shows the
resolution, density and cutout for every entry, marks Apple devices as
viewport-only previews and marks rumored devices.  Layouts only - no
setGeometry/move/setFixed*/setMaximum* on child widgets.
"""

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox,
                             QHBoxLayout, QLabel, QLineEdit, QTreeWidget,
                             QTreeWidgetItem, QVBoxLayout)

from core.device_presets import DevicePreset, preset_manager


_APPLE_NOTE = ("Apple devices are viewport and frame previews only. The "
               "Android emulator can run an Android image at this resolution, "
               "but it cannot run iOS apps.")


class DevicePickerDialog(QDialog):
    """Grouped, searchable device chooser."""

    def __init__(self, parent=None, current_key: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Choose a device")
        self.resize(760, 620)
        self._current_key = current_key

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Model, brand, category, resolution or cutout...")
        self.search_edit.setClearButtonEnabled(True)
        search_row.addWidget(self.search_edit, 1)
        layout.addLayout(search_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Device", "Screen", "Cutout"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.tree, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Ok)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._populate()
        self.search_edit.textChanged.connect(self._filter)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(self._on_double_clicked)
        self._select_current()
        self._on_selection_changed()

    # ------------------------------------------------------------------
    def _populate(self):
        self.tree.clear()
        for brand, categories in preset_manager.get_brand_groups():
            brand_item = QTreeWidgetItem([brand, "", ""])
            brand_item.setFirstColumnSpanned(True)
            brand_item.setFlags(brand_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = brand_item.font(0)
            font.setBold(True)
            brand_item.setFont(0, font)
            self.tree.addTopLevelItem(brand_item)

            for category, items in categories:
                category_item = QTreeWidgetItem([category, "", ""])
                category_item.setFirstColumnSpanned(True)
                category_item.setFlags(
                    category_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                brand_item.addChild(category_item)

                for key, preset in items:
                    self._add_device_item(category_item, key, preset)

            brand_item.setExpanded(True)

    @staticmethod
    def _screen_text(preset: DevicePreset) -> str:
        if preset.is_foldable and preset.inner and preset.outer:
            return (f"{preset.inner.size_str} inner / "
                    f"{preset.outer.size_str} cover")
        return f"{preset.size_str} @ {preset.density} dpi"

    def _add_device_item(self, parent, key: str, preset: DevicePreset):
        name = preset.name
        if preset.rumored:
            name += "  (rumored)"
        elif preset.preview_only:
            name += "  (viewport only)"
        item = QTreeWidgetItem([name, self._screen_text(preset),
                                preset.cutout_label])
        item.setData(0, Qt.ItemDataRole.UserRole, key)
        item.setToolTip(0, preset.description)
        item.setToolTip(1, self._screen_text(preset))
        parent.addChild(item)

    # ------------------------------------------------------------------
    def _filter(self, text: str):
        needle = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            brand_item = self.tree.topLevelItem(i)
            brand_visible = False
            for j in range(brand_item.childCount()):
                category_item = brand_item.child(j)
                category_visible = False
                for k in range(category_item.childCount()):
                    device_item = category_item.child(k)
                    key = device_item.data(0, Qt.ItemDataRole.UserRole)
                    preset = preset_manager.get_preset(key)
                    matches = (not needle
                               or (preset is not None
                                   and needle in preset.search_text))
                    device_item.setHidden(not matches)
                    category_visible = category_visible or matches
                category_item.setHidden(not category_visible)
                category_item.setExpanded(True)
                brand_visible = brand_visible or category_visible
            brand_item.setHidden(not brand_visible)
            brand_item.setExpanded(True)

    def _select_current(self):
        if not self._current_key:
            return
        item = self._find_item(self._current_key)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def _find_item(self, key: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.tree.topLevelItemCount()):
            brand_item = self.tree.topLevelItem(i)
            for j in range(brand_item.childCount()):
                category_item = brand_item.child(j)
                for k in range(category_item.childCount()):
                    item = category_item.child(k)
                    if item.data(0, Qt.ItemDataRole.UserRole) == key:
                        return item
        return None

    def _on_selection_changed(self):
        item = self.tree.currentItem()
        key = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        preset = preset_manager.get_preset(key) if key else None
        ok_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_button is not None:
            ok_button.setEnabled(preset is not None)

        if preset is None:
            self.note.setText("")
        elif preset.preview_only:
            self.note.setText(_APPLE_NOTE)
        elif preset.rumored:
            self.note.setText("This device is unconfirmed/rumored. Its "
                              "specifications may change.")
        elif preset.is_foldable:
            self.note.setText("Foldable: use the Inner / Cover toggle in the "
                              "Controls tab to switch screens.")
        else:
            self.note.setText(
                f"{preset.brand} {preset.name} - {preset.size_str} @ "
                f"{preset.density} dpi - {preset.cutout_label}")

    def _on_double_clicked(self, item, _column):
        if item.data(0, Qt.ItemDataRole.UserRole):
            self.accept()

    # ------------------------------------------------------------------
    def selected_key(self) -> str:
        item = self.tree.currentItem()
        if item is None:
            return ""
        return item.data(0, Qt.ItemDataRole.UserRole) or ""


def pick_device(parent, current_key: str = "") -> Optional[str]:
    """Show the picker; return the selected device key or ``None``."""
    dialog = DevicePickerDialog(parent, current_key)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return dialog.selected_key() or None
    return None
