"""ApkPreview - Lightweight Android APK testing on desktop."""

__version__ = "0.1.0"
__author__ = "ApkPreview Contributors"
__license__ = "MIT"

from .core.platform_utils import PlatformUtils
from .core.hardware_profile import HardwareProfiler, PerformanceMode, EmulatorConfig
from .core.settings import settings, Settings
from .core.sdk_manager import SDKManager
from .core.emulator import EmulatorManager, EmulatorMonitor
from .core.adb import ADB, detect_package_and_activity
from .core.scrcpy import ScrcpyManager, check_scrcpy_installed
from .core.device_presets import DevicePreset, DevicePresetManager, preset_manager, BUILTIN_PRESETS
from .core.apk_parser import APKParser, get_apk_info, detect_package_and_activity as parse_apk
from .core.file_watcher import APKFileWatcher, PollingFileWatcher, create_file_watcher

__all__ = [
    "PlatformUtils",
    "HardwareProfiler",
    "PerformanceMode", 
    "EmulatorConfig",
    "settings",
    "Settings",
    "SDKManager",
    "EmulatorManager",
    "EmulatorMonitor",
    "ADB",
    "detect_package_and_activity",
    "ScrcpyManager",
    "check_scrcpy_installed",
    "DevicePreset",
    "DevicePresetManager",
    "preset_manager",
    "BUILTIN_PRESETS",
    "APKParser",
    "get_apk_info",
    "parse_apk",
    "APKFileWatcher",
    "PollingFileWatcher",
    "create_file_watcher",
]