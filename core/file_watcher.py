"""File watcher for AppStation - monitors APK file changes for auto-reinstall."""

import time
import threading
from pathlib import Path
from typing import Optional, Callable
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class APKFileWatcher:
    """Watches an APK file for changes and triggers reinstall."""
    
    def __init__(self):
        self._observer: Optional[Observer] = None
        self._watch_path: Optional[Path] = None
        self._callback: Optional[Callable[[Path], None]] = None
        self._debounce_time = 1.0  # seconds
        self._last_modified = 0.0
        self._enabled = False
        self._auto_reinstall = False
    
    def set_callback(self, callback: Callable[[Path], None]):
        """Set callback when file changes."""
        self._callback = callback
    
    def set_auto_reinstall(self, enabled: bool):
        """Enable/disable auto reinstall on change."""
        self._auto_reinstall = enabled
    
    def watch(self, apk_path: Path) -> bool:
        """Start watching an APK file."""
        if not apk_path.exists():
            return False
        
        self.stop()
        
        self._watch_path = apk_path.resolve()
        self._last_modified = self._watch_path.stat().st_mtime
        
        # Watch the parent directory (more reliable)
        watch_dir = self._watch_path.parent
        
        class APKEventHandler(FileSystemEventHandler):
            def __init__(self, watcher):
                self.watcher = watcher
            
            def on_modified(self, event):
                if event.is_directory:
                    return
                if Path(event.src_path).resolve() == self.watcher._watch_path:
                    self.watcher._on_file_changed()
        
        self._observer = Observer()
        self._observer.schedule(APKEventHandler(self), str(watch_dir), recursive=False)
        self._observer.start()
        self._enabled = True
        return True
    
    def _on_file_changed(self):
        """Handle file change event with debouncing."""
        now = time.time()
        if now - self._last_modified < self._debounce_time:
            return
        self._last_modified = now
        
        if self._watch_path and self._watch_path.exists():
            if self._callback:
                self._callback(self._watch_path)
    
    def stop(self):
        """Stop watching."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=1.0)
            self._observer = None
        self._enabled = False
        self._watch_path = None
    
    @property
    def is_watching(self) -> bool:
        return self._enabled and self._observer is not None and self._observer.is_alive()
    
    @property
    def watched_file(self) -> Optional[Path]:
        return self._watch_path


class PollingFileWatcher:
    """Fallback file watcher using polling (no watchdog dependency)."""
    
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._watch_path: Optional[Path] = None
        self._callback: Optional[Callable[[Path], None]] = None
        self._interval = 2.0  # seconds
        self._last_modified = 0.0
        self._enabled = False
    
    def set_callback(self, callback: Callable[[Path], None]):
        self._callback = callback
    
    def watch(self, apk_path: Path) -> bool:
        if not apk_path.exists():
            return False
        
        self.stop()
        
        self._watch_path = apk_path.resolve()
        self._last_modified = self._watch_path.stat().st_mtime
        self._stop_event.clear()
        self._enabled = True
        
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        return True
    
    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                if self._watch_path and self._watch_path.exists():
                    mtime = self._watch_path.stat().st_mtime
                    if mtime > self._last_modified:
                        self._last_modified = mtime
                        if self._callback:
                            self._callback(self._watch_path)
            except Exception:
                pass
            self._stop_event.wait(self._interval)
    
    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        self._enabled = False
        self._watch_path = None
    
    @property
    def is_watching(self) -> bool:
        return self._enabled and self._thread is not None and self._thread.is_alive()


def create_file_watcher(use_polling: bool = False):
    """Create appropriate file watcher."""
    if use_polling:
        return PollingFileWatcher()
    try:
        import watchdog
        return APKFileWatcher()
    except ImportError:
        return PollingFileWatcher()