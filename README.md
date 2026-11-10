# ApkPreview

**Lightweight Android APK testing on desktop — no Android Studio required.**

A developer drops an APK file on their laptop and tests it on a virtual Android phone running on the same laptop. No real phone is ever involved. Designed for **low-end laptops (4-8 GB RAM, weak CPU)** who cannot run Android Studio comfortably.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![PyQt6](https://img.shields.io/badge/PyQt-6.6+-green.svg)](https://pypi.org/project/PyQt6/)

---

## Features

### MVP (Current)
- **Drag & drop APK** — Large drop zone, file association, "Open APK" button
- **Automatic emulator management** — Starts/stops emulator, waits for boot
- **APK install & launch** — `adb install -r`, parses package/activity via `aapt2`/`androguard`, launches with `am start`
- **Screen mirroring via scrcpy** — Low latency, mouse=touch, keyboard works, separate window
- **Controls** — Stop emulator, restart app, uninstall app, clear app data
- **File watcher** — Detects APK changes, one-click reinstall (optional auto)
- **Resource meter** — Live emulator/system RAM/CPU, low memory warnings
- **Dark theme** — Clean, minimal UI

### Phase 2 (Planned)
- **One emulator, many sizes** — Device presets (Pixel 7, iPhone sizes, tablets, foldables) via `wm size`/`wm density`
- **Rotation** — Portrait/landscape via accelerometer settings
- **Auto-sleep** — Snapshot + stop emulator when idle, quick restore on next APK
- **Logcat panel** — Live stream, auto-filter by package, level filter, search, save
- **Screenshot & screen record** — `adb screencap`/`screenrecord` with pull
- **Emulator console** — GPS, battery, network, SMS, calls, notifications, dark mode, locale, font scale
- **Performance panel** — RAM (`dumpsys meminfo`), CPU (`top`), FPS (`dumpsys gfxinfo`)
- **Virtual camera** — Webcam/virtualscene in AVD config
- **Android version selector** — API 29-34 from UI

### Phase 3 (Future)
- **Hot reload** — Detect Expo/Flutter/RN dev servers, `adb reverse` for instant updates
- **Fallback web preview** — Phone-frame browser for Expo Web/Flutter Web when emulator can't run
- **Embedded scrcpy** — Try embedding scrcpy window in main window
- **Multi-AVD management** — Create/delete multiple AVDs

---

## Requirements

| Component | Version |
|-----------|---------|
| Python | 3.11+ |
| PyQt6 | 6.6+ |
| psutil | 5.9+ |
| androguard | 4.0+ (fallback APK parser) |
| requests | 2.31+ (downloads) |

**System requirements:**
- **Windows 10/11** — Hardware virtualization (WHPX/Hyper-V) enabled in BIOS + Windows Features
- **macOS** — Intel or Apple Silicon (Hypervisor.framework built-in)
- **RAM** — 4 GB minimum, 8 GB recommended
- **Disk** — 10 GB free for SDK + system images
- **CPU** — 2+ cores, x86_64 or arm64

---

## Installation

### From Source (Development)

```bash
# Clone
git clone https://github.com/yourusername/apkpreview.git
cd apkpreview

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run
python main.py
```

### Packaged Releases (Coming Soon)

Download from [GitHub Releases](https://github.com/yourusername/apkpreview/releases):
- **Windows**: `ApkPreview-Setup.exe` (NSIS installer) or portable `.exe`
- **macOS Intel**: `ApkPreview.dmg`
- **macOS Apple Silicon**: `ApkPreview-arm64.dmg`

---

## First Run

On first launch, ApkPreview runs a **nine-step, resumable Setup Wizard**:

1. **Welcome** — Explains what will be installed and that no emulator is
   started during setup.
2. **Standard or Custom** — Standard installs/repairs Java 17, the Android
   command-line tools, platform-tools and the emulator. Custom lets you choose
   components you already have.
3. **SDK location + Java 17 check** — Defaults to `~/.apkpreview/sdk`; you can
   point it at an existing Android SDK. A missing JDK is downloaded later into
   `~/.apkpreview/jdk` without changing system Java.
4. **System check** — RAM, CPU threads, free disk, Java and hardware
   virtualization. Warnings do not block setup.
5. **Android SDK licenses** — Shows the exact license names and the official
   link; you must tick the consent box before continuing.
6. **Downloads** — Progress, cancel, retry, resume from a partial file, disk
   space check and SHA verification where the publisher provides checksums.
   Closing the wizard and reopening it resumes at the step you reached.
7. **Device** — Searchable bundled list grouped **Brand > Category** (Phone,
   Tablet, Foldable), with foldable inner/outer screens and an Apple
   viewport-only note.
8. **Android version** — Shows the system-image download size; plain `default`
   images are recommended for the fastest boot.
9. **Create the AVD and finish** — Downloads the selected image if needed,
   creates/reuses one AVD, then finishes. The emulator is never started.

**Base download**: roughly 650 MB (Java 17 + command-line tools +
platform-tools + emulator). Android system images (~1.2-1.6 GB each) are
downloaded only after you choose one.

You can reopen the wizard at any time from **Controls → Setup → Re-run
setup...**. The same downloader is available headlessly for the Windows
installer as `ApkPreview.exe --bootstrap`.

---

## Usage

1. **Launch ApkPreview**
2. **Drop an APK** on the drop zone, or click "Open APK"
3. **Wait** — Emulator starts (first time takes 30-60s, subsequent launches ~5s with quick boot)
4. **Test** — App launches automatically, screen appears in "ApkPreview Screen" window
5. **Iterate** — Rebuild APK, drop again → "Reinstall and relaunch"

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Ctrl+O` | Open APK |
| `Ctrl+S` | Screenshot |
| `Ctrl+R` | Start/stop screen recording |
| `Ctrl+,` | Settings |
| `Ctrl+Q` | Quit |

---

## Low-Spec Tips

If you have ≤8 GB RAM or ≤4 CPU cores:

1. **Use Low-Spec mode** (auto-selected, or Settings → Performance Mode → Low-Spec)
2. **Close other apps** — Browser, IDE, Slack use significant RAM
3. **Enable auto-sleep** (Phase 2) — Emulator snapshots and stops when idle
4. **Reduce scrcpy quality** — Settings → Quality → Low (2M) or Medium (4M)
5. **Use smaller device preset** — "Small Phone" (720x1280) uses less GPU memory

---

## Troubleshooting

### "Virtualization not available" (Windows)

**Enable in BIOS:**
1. Restart PC, enter BIOS/UEFI (F2, F10, Del, Esc)
2. Enable: "Virtualization Technology", "VT-x", "SVM", or "AMD-V"
3. Save & exit

**Enable in Windows:**
1. Open "Turn Windows features on or off"
2. Check **"Windows Hypervisor Platform"** (recommended, works on Home/Pro)
3. Or check **"Hyper-V"** (requires Windows Pro/Enterprise)
4. Restart

**Verify:**
```powershell
systeminfo
# Look for: "Virtualization Enabled In Firmware : Yes"
```

### Emulator won't start

- **Check RAM**: Close apps, ensure 2+ GB free
- **Check disk**: 10+ GB free on SDK drive
- **Try swiftshader**: Settings → GPU → "swiftshader_indirect"
- **Check logs**: View → Logcat for emulator output

### "adb: command not found"

- ApkPreview uses its own bundled adb at `~/.apkpreview/sdk/platform-tools/adb`
- No system-wide adb installation needed

### APK install fails

- **INSTALL_FAILED_VERSION_DOWNGRADE**: Uninstall first (Uninstall button) or use "Reinstall"
- **INSTALL_FAILED_INSUFFICIENT_STORAGE**: Free up emulator space (Wipe Data in AVD Manager)
- **INSTALL_PARSE_FAILED_MANIFEST_MALFORMED**: APK may be corrupt or built incorrectly

### scrcpy shows black screen

- Wait for app to fully launch (check logcat)
- Try: `Settings → Quality → Low (2M)`
- Restart emulator

### Antivirus/firewall blocks emulator

- Add `~/.apkpreview/sdk/emulator/qemu-system-*` to exclusions
- Allow `adb.exe` through firewall

---

## Project Structure

```
apkpreview/
├── main.py                 # Entry point
├── requirements.txt        # Python dependencies
├── LICENSE                 # MIT License
├── README.md               # This file
├── core/                   # Business logic
│   ├── __init__.py
│   ├── platform_utils.py   # All OS differences (paths, processes, arch)
│   ├── hardware_profile.py # HW detection, performance modes
│   ├── settings.py         # JSON settings persistence
│   ├── sdk_manager.py      # SDK download, packages, AVD creation
│   ├── emulator.py         # Emulator lifecycle, snapshots
│   ├── adb.py              # ADB wrapper, install, launch, shell
│   ├── scrcpy.py           # scrcpy wrapper
│   ├── device_presets.py   # Device size/density presets
│   ├── apk_parser.py       # APK metadata extraction
│   └── file_watcher.py     # APK file change detection
├── ui/                     # PyQt6 UI components
│   ├── __init__.py
│   ├── main_window.py      # Main window, menus, integration
│   ├── drop_zone.py        # Drag-and-drop APK zone
│   ├── controls_panel.py   # Emulator/app controls, settings
│   ├── logcat_panel.py     # Live logcat with filtering
│   ├── resource_meter.py   # RAM/CPU/disk monitor
│   └── setup_wizard.py     # First-run setup wizard
└── tests/
    └── test_core.py        # Unit tests
```

---

## Building

### PyInstaller (Development)

```bash
# Install PyInstaller
pip install pyinstaller

# Build
pyinstaller --clean --noconfirm \
  --name ApkPreview \
  --windowed \
  --icon=assets/icon.ico \
  --add-data "core;core" \
  --add-data "ui;ui" \
  main.py
```

### GitHub Actions (CI/CD)

See `.github/workflows/build.yml` for matrix build:
- `windows-latest` → `.exe` + NSIS installer
- `macos-13` (Intel) → `.app` + `.dmg`
- `macos-latest` (Apple Silicon) → `.app` + `.dmg`

---

## Configuration

Settings stored in `~/.apkpreview/settings.json`:

```json
{
  "sdk_path": "~/.apkpreview/sdk",
  "last_apk_path": "~/Downloads/app.apk",
  "performance_mode": "auto",
  "device_preset": "default",
  "idle_timeout_minutes": 10,
  "auto_reinstall": false,
  "scrcpy_bitrate": "8M",
  "scrcpy_max_size": 1024,
  "scrcpy_max_fps": 30,
  "theme": "dark",
  "first_run_completed": true
}
```

---

## Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| **Python + PyQt6** | Lightweight, fast startup, native look, no Electron bloat |
| **psutil only extra dep** | Minimal dependencies, cross-platform system info |
| **Command-line tools only** | No Android Studio, ~1.5 GB vs ~8 GB |
| **scrcpy for display** | Best performance, low latency, audio optional |
| **aapt2 + androguard** | aapt2 is fast/official, androguard as fallback |
| **Single AVD, dynamic size** | No need for multiple emulators, `wm size` is instant |
| **Snapshots for sleep** | Saves RAM, restores in seconds vs cold boot |
| **All OS logic in platform_utils.py** | Single place for Windows/macOS differences |

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Commit changes: `git commit -m 'Add amazing feature'`
4. Push to branch: `git push origin feature/amazing-feature`
5. Open a Pull Request

### Development Guidelines

- **Keep it lightweight** — Every dependency must justify its weight
- **Low-spec first** — Test on 4 GB RAM VMs
- **No blocking UI** — All long operations in QThread/QProcess
- **Clean shutdown** — Kill emulator, scrcpy, adb on exit
- **Cross-platform** — Windows/macOS parity, Linux best-effort

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgments

- [scrcpy](https://github.com/Genymobile/scrcpy) — Amazing screen mirroring
- [Android Command-line Tools](https://developer.android.com/studio/command-line) — SDK without Studio
- [androguard](https://github.com/androguard/androguard) — APK parsing fallback
- [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) — Native Qt bindings for Python

---

## Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/apkpreview/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/apkpreview/discussions)
- **Security**: Email security@apkpreview.dev

---

*Built for developers who just want to test their APK, not fight their tooling.*