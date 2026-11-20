"""Core modules for AppStation."""

from .platform_utils import PlatformUtils
from .hardware_profile import HardwareProfiler, PerformanceMode, EmulatorConfig
from .settings import settings, Settings
from .sdk_manager import SDKManager
from .emulator import EmulatorManager, EmulatorMonitor
from .adb import ADB, detect_package_and_activity
from .scrcpy import ScrcpyManager, check_scrcpy_installed
from .apk_parser import APKParser, get_apk_info, detect_package_and_activity as parse_apk
from .file_watcher import APKFileWatcher, PollingFileWatcher, create_file_watcher

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
    "APKParser",
    "get_apk_info",
    "parse_apk",
    "APKFileWatcher",
    "PollingFileWatcher",
    "create_file_watcher",
]