# -*- mode: python ; coding: utf-8 -*-
import os
import sys

PROJECT_ROOT = os.path.abspath(SPECPATH)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from PyInstaller.utils.hooks import collect_submodules
    _discovered = collect_submodules("ui") + collect_submodules("core")
except Exception:
    _discovered = []

hiddenimports = sorted(set(["platform"] + _discovered + [
    "ui", "core",
    "ui.main_window_v2", "ui.setup_wizard",
    "ui.logcat_panel", "ui.loading_screen",
    "ui.screen_view", "ui.android_versions_dialog",
    "core.platform_utils", "core.hardware_profile", "core.settings",
    "core.sdk_manager", "core.emulator", "core.adb", "core.scrcpy",
    "core.apk_parser", "core.file_watcher",
    "core.bootstrap", "core.screen_stream", "av",
]))

datas = []

a = Analysis(
    ["main.py"],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AppStation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="AppStation",
)
app = BUNDLE(
    coll,
    name="AppStation.app",
    icon=os.path.join(PROJECT_ROOT, "AppStation.icns"),
    bundle_identifier="com.appstation.app",
    info_plist={
        "CFBundleName": "AppStation",
        "CFBundleDisplayName": "AppStation",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "10.15",
    },
)
