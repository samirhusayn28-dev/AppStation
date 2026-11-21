# AppStation

**Lightweight Android APK testing on desktop — no Android Studio required.**

AppStation drops an APK onto a virtual Android phone running on the same computer. It is built for everyday laptops, including 4–8 GB RAM machines that struggle with Android Studio.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![PyQt6](https://img.shields.io/badge/PyQt-6.6+-green.svg)](https://pypi.org/project/PyQt6/)

---

## Features

- **Drag & drop APK** — install and launch with one action.
- **Automatic emulator management** — start, stop, and wait for boot from the app.
- **Use my phone (USB)** — no emulator, system image or BIOS settings. AppStation finds your phone over ADB and mirrors it with the same low-latency preview.
- **Embedded scrcpy preview** — low-latency screen mirroring directly in the window.
- **Trackpad gestures**
  - Two-finger upward burst opens the Android app drawer.
  - Two-finger up/down/left/right scroll smoothly at the cursor.
  - Scrolling is a single held touch gesture, so it stays proportional with no synthetic fling.
- **Responsive UI** — the window adapts from small laptops to large displays.
- **Device presets** — phone, tablet, and foldable sizes with rotation.
- **Android version replacement** — swap the guest image without rebuilding the AVD manually.
- **First-run setup wizard** — downloads the private SDK and creates the emulator.
- **Logcat panel** — live filtering and search.
- **Screenshots** — saved under `~/Pictures/AppStation` (macOS) or the user Pictures folder (Windows).
- **File watcher** — optional auto-reinstall when the APK changes.

---

## Requirements

| Component | Version |
|-----------|---------|
| Python | 3.12+ (only needed to build from source) |
| PyQt6 | 6.6+ |
| psutil | 5.9+ |
| requests | 2.31+ |
| watchdog | 3.0+ |
| PyAV | 12.0+ |

**System requirements**

- **Windows 10/11** — the **Windows Hypervisor Platform** feature (or the Android Emulator Hypervisor Driver) must be enabled to boot the x86 Android image. Firmware VT-x/SVM alone is not enough. Use **Controls → Virtualization → Fix...** to open Windows Features, tick "Windows Hypervisor Platform", then restart. If you cannot or do not want to change firmware/Windows features, choose **Use my Android phone (USB)** in the first-run wizard (or **Controls → Device → Connect phone...**) instead — phone mode does not need a hypervisor.
- **macOS** — Intel or Apple Silicon (`Hypervisor.framework`).
- **RAM** — 4 GB minimum, 8 GB recommended.
- **Disk** — about 10 GB free for the SDK and system image.
- **CPU** — 2+ cores, x86_64 or arm64.

---

## Install

Download the installer for your platform from the latest release:

**https://github.com/samirhusayn28-dev/AppStation/releases/latest**

| Platform | Asset |
|----------|-------|
| macOS Intel | `AppStation-macos-intel.dmg` |
| macOS Apple Silicon | `AppStation-macos-arm64.dmg` |
| macOS PKG | `AppStation-1.0.6.pkg` |
| Windows | `AppStation-Setup-1.0.6.exe` |
| Windows portable | `AppStation-windows-portable.zip` |

On macOS, if Gatekeeper blocks the first launch, right-click the app and choose **Open**.

---

## Build from source

```bash
python -m pip install -r requirements.txt
python -m pip install pyinstaller
```

**macOS**

```bash
pyinstaller --clean --noconfirm AppStation.spec
hdiutil create -volname "AppStation" \
  -srcfolder dist/AppStation.app -ov -format UDZO AppStation-1.0.6.dmg
```

**Windows**

```powershell
pyinstaller --clean --noconfirm AppStation.win.spec
makensis installer.nsi
```

The GitHub Actions workflow in `.github/workflows/build.yml` performs both builds on every `v*` tag and attaches the installers to that release.

---

## Tests

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -q
```

---

## License

MIT — see [LICENSE](LICENSE).
