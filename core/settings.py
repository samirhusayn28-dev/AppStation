"""Settings management for ApkPreview - JSON-based persistence."""

import json
from pathlib import Path
from typing import Any, Optional
from .platform_utils import PlatformUtils


class Settings:
    """Application settings with JSON persistence."""
    
    DEFAULT_SETTINGS = {
        "sdk_path": "",
        "last_apk_path": "",
        "window_geometry": None,
        "main_window_state": None,
        "screen_window_geometry": None,
        "performance_mode": "auto",  # auto, low_spec, normal
        "device_preset": "default",
        # native = real emulator window (default), embedded = in-app scrcpy view
        "display_mode": "native",
        "idle_timeout_minutes": 10,
        "auto_reinstall": False,
        "scrcpy_bitrate": "8M",
        "scrcpy_max_size": 960,
        "scrcpy_max_fps": 30,
        "show_resource_meter": True,
        "logcat_auto_filter": True,
        "logcat_level": "verbose",
        "theme": "dark",
        "first_run_completed": False,
        # Selected Android version (system image).  ``selected_api_level`` is 0
        # until the user picks a version; ``selected_avd_name`` is the AVD that
        # serves it (created on first Select).
        "selected_api_level": 0,
        "selected_variant": "",
        "selected_avd_name": "",
    }
    
    def __init__(self):
        self._settings_path = PlatformUtils.get_settings_path()
        self._data = self.DEFAULT_SETTINGS.copy()
        self.load()
    
    def load(self) -> dict:
        """Load settings from JSON file."""
        if self._settings_path.exists():
            try:
                with open(self._settings_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    # Merge with defaults (handles new settings added in updates)
                    self._data = {**self.DEFAULT_SETTINGS, **loaded}
            except (json.JSONDecodeError, OSError):
                # Corrupted or unreadable - use defaults
                self._data = self.DEFAULT_SETTINGS.copy()
        else:
            self._data = self.DEFAULT_SETTINGS.copy()
        return self._data
    
    def save(self) -> bool:
        """Save settings to JSON file."""
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._settings_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            return True
        except OSError:
            return False
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value."""
        return self._data.get(key, default)
    
    def set(self, key: str, value: Any) -> bool:
        """Set a setting value and save."""
        self._data[key] = value
        return self.save()
    
    def update(self, values: dict) -> bool:
        """Update multiple settings at once."""
        self._data.update(values)
        return self.save()
    
    def reset(self) -> bool:
        """Reset all settings to defaults."""
        self._data = self.DEFAULT_SETTINGS.copy()
        return self.save()
    
    @property
    def sdk_path(self) -> Path:
        """Get the SDK path as a Path object."""
        path = self._data.get("sdk_path", "")
        if path:
            return Path(path)
        return PlatformUtils.get_sdk_dir()
    
    @sdk_path.setter
    def sdk_path(self, value: Path | str):
        self.set("sdk_path", str(value))
    
    @property
    def last_apk_path(self) -> Optional[Path]:
        """Get the last APK path."""
        path = self._data.get("last_apk_path", "")
        if path:
            return Path(path)
        return None
    
    @last_apk_path.setter
    def last_apk_path(self, value: Optional[Path | str]):
        if value:
            self.set("last_apk_path", str(value))
        else:
            self.set("last_apk_path", "")
    
    @property
    def performance_mode(self) -> str:
        return self._data.get("performance_mode", "auto")
    
    @performance_mode.setter
    def performance_mode(self, value: str):
        self.set("performance_mode", value)
    
    @property
    def device_preset(self) -> str:
        return self._data.get("device_preset", "default")
    
    @device_preset.setter
    def device_preset(self, value: str):
        self.set("device_preset", value)
    
    @property
    def display_mode(self) -> str:
        """Where the device screen is shown.

        ``"native"`` opens the emulator's own window (default, like the first
        working build); ``"embedded"`` renders the device inside the app via
        the scrcpy stream.  Unknown values fall back to ``"native"``.
        """
        value = self._data.get("display_mode", "native")
        return value if value in ("native", "embedded") else "native"
    
    @display_mode.setter
    def display_mode(self, value: str):
        self.set("display_mode",
                 value if value in ("native", "embedded") else "native")
    
    @property
    def idle_timeout_minutes(self) -> int:
        return self._data.get("idle_timeout_minutes", 10)
    
    @idle_timeout_minutes.setter
    def idle_timeout_minutes(self, value: int):
        self.set("idle_timeout_minutes", max(0, value))
    
    @property
    def auto_reinstall(self) -> bool:
        return self._data.get("auto_reinstall", False)
    
    @auto_reinstall.setter
    def auto_reinstall(self, value: bool):
        self.set("auto_reinstall", value)
    
    @property
    def scrcpy_bitrate(self) -> str:
        return self._data.get("scrcpy_bitrate", "8M")
    
    @scrcpy_bitrate.setter
    def scrcpy_bitrate(self, value: str):
        self.set("scrcpy_bitrate", value)
    
    @property
    def scrcpy_max_size(self) -> int:
        return self._data.get("scrcpy_max_size", 960)
    
    @scrcpy_max_size.setter
    def scrcpy_max_size(self, value: int):
        self.set("scrcpy_max_size", max(0, value))
    
    @property
    def scrcpy_max_fps(self) -> int:
        return self._data.get("scrcpy_max_fps", 30)
    
    @scrcpy_max_fps.setter
    def scrcpy_max_fps(self, value: int):
        self.set("scrcpy_max_fps", max(1, min(60, value)))
    
    @property
    def screen_window_geometry(self) -> Optional[str]:
        """Hex-encoded QByteArray geometry for the floating device window."""
        value = self._data.get("screen_window_geometry")
        return value if value else None

    @screen_window_geometry.setter
    def screen_window_geometry(self, value: Optional[str]):
        self.set("screen_window_geometry", value or None)

    @property
    def avd_name(self) -> str:
        """The AVD that serves the currently selected Android version.

        Empty until the user picks a version.  Kept as a convenience alias for
        ``selected_avd_name`` so emulator code has a single source of truth.
        """
        return self._data.get("selected_avd_name", "") or ""

    @avd_name.setter
    def avd_name(self, value: str):
        self.set("selected_avd_name", value or "")

    @property
    def selected_api_level(self) -> int:
        return int(self._data.get("selected_api_level", 0) or 0)

    @selected_api_level.setter
    def selected_api_level(self, value: int):
        self.set("selected_api_level", int(value))

    @property
    def selected_variant(self) -> str:
        return self._data.get("selected_variant", "") or ""

    @selected_variant.setter
    def selected_variant(self, value: str):
        self.set("selected_variant", value or "")

    @property
    def has_selected_version(self) -> bool:
        return self.selected_api_level > 0 and bool(self.avd_name)

    def select_android_version(self, api_level: int, variant: str, avd_name: str):
        """Persist the selected Android version and its AVD in one write."""
        self._data["selected_api_level"] = int(api_level)
        self._data["selected_variant"] = variant or ""
        self._data["selected_avd_name"] = avd_name or ""
        self.save()

    @property
    def first_run_completed(self) -> bool:
        return self._data.get("first_run_completed", False)
    
    @first_run_completed.setter
    def first_run_completed(self, value: bool):
        self.set("first_run_completed", value)
    
    def get_window_geometry(self) -> Optional[bytes]:
        """Get saved window geometry."""
        geom = self._data.get("window_geometry")
        if geom:
            return bytes.fromhex(geom)
        return None
    
    def set_window_geometry(self, geometry: bytes):
        """Save window geometry."""
        if isinstance(geometry, bytes):
            self.set("window_geometry", geometry.hex())
        else:
            # QByteArray in PyQt6
            self.set("window_geometry", bytes(geometry).hex())
    
    def get_main_window_state(self) -> Optional[bytes]:
        """Get saved main window state."""
        state = self._data.get("main_window_state")
        if state:
            return bytes.fromhex(state)
        return None
    
    def set_main_window_state(self, state: bytes):
        """Save main window state."""
        if isinstance(state, bytes):
            self.set("main_window_state", state.hex())
        else:
            # QByteArray in PyQt6
            self.set("main_window_state", bytes(state).hex())
    
    def get_screen_window_geometry(self) -> Optional[bytes]:
        """Get saved screen window geometry."""
        geom = self._data.get("screen_window_geometry")
        if geom:
            return bytes.fromhex(geom)
        return None
    
    def set_screen_window_geometry(self, geometry: bytes):
        """Save screen window geometry."""
        if isinstance(geometry, bytes):
            self.set("screen_window_geometry", geometry.hex())
        else:
            # QByteArray in PyQt6
            self.set("screen_window_geometry", bytes(geometry).hex())
    
    def get_all(self) -> dict:
        """Get all settings as a dict."""
        return self._data.copy()


# Global settings instance
settings = Settings()