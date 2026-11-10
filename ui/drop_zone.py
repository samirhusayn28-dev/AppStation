"""Drag-and-drop zone widget for ApkPreview."""

from pathlib import Path
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QFileDialog
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QFont, QColor, QPainter, QBrush, QPen


class DropZone(QWidget):
    """Large drag-and-drop zone for APK files."""
    
    apk_dropped = pyqtSignal(Path)
    open_clicked = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._hovering = False
        self._setup_ui()
    
    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(16)
        
        # Icon/Label
        self.icon_label = QLabel("📱")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(64)
        self.icon_label.setFont(font)
        
        # Title
        self.title_label = QLabel("Drop APK here")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(20)
        font.setWeight(QFont.Weight.Medium)
        self.title_label.setFont(font)
        
        # Subtitle
        self.subtitle_label = QLabel("or click to browse")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = QFont()
        font.setPointSize(12)
        self.subtitle_label.setFont(font)
        
        # Open button
        self.open_btn = QPushButton("Open APK")
        self.open_btn.setFixedWidth(160)
        self.open_btn.setFixedHeight(40)
        self.open_btn.clicked.connect(self._on_open_clicked)
        
        layout.addStretch()
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.open_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        
        # Style
        self.setMinimumSize(400, 350)
        self.setStyleSheet("""
            DropZone {
                background-color: #1e1e1e;
                border: 2px dashed #3a3a3a;
                border-radius: 12px;
            }
            DropZone:hover {
                border-color: #007acc;
                background-color: #252526;
            }
            QLabel {
                color: #cccccc;
            }
            QPushButton {
                background-color: #007acc;
                color: white;
                border: none;
                border-radius: 6px;
                font-weight: 600;
            }
            QPushButton:hover {
                background-color: #005a9e;
            }
            QPushButton:pressed {
                background-color: #004578;
            }
        """)
    
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1 and urls[0].toLocalFile().lower().endswith('.apk'):
                event.acceptProposedAction()
                self._hovering = True
                self.update()
                return
        event.ignore()
    
    def dragLeaveEvent(self, event):
        self._hovering = False
        self.update()
    
    def dropEvent(self, event: QDropEvent):
        self._hovering = False
        self.update()
        
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if len(urls) == 1:
                path = Path(urls[0].toLocalFile())
                if path.suffix.lower() == '.apk':
                    self.apk_dropped.emit(path)
                    event.acceptProposedAction()
                    return
        event.ignore()
    
    def _on_open_clicked(self):
        self.open_clicked.emit()
    
    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hovering:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor("#007acc"), 3, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rect = self.rect().adjusted(1, 1, -1, -1)
            painter.drawRoundedRect(rect, 10, 10)
    
    def set_apk_info(self, name: str, size_mb: float):
        """Update display with APK info."""
        self.icon_label.setText("✅")
        self.title_label.setText(name)
        self.subtitle_label.setText(f"{size_mb:.1f} MB • Ready to install")
        self.open_btn.setText("Change APK")
    
    def reset(self):
        """Reset to default state."""
        self.icon_label.setText("📱")
        self.title_label.setText("Drop APK here")
        self.subtitle_label.setText("or click to browse")
        self.open_btn.setText("Open APK")