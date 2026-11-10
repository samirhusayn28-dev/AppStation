#!/usr/bin/env python3
"""ApkPreview - Lightweight Android APK testing on desktop.

A developer drops an APK file on their laptop and tests it on a virtual
Android phone running on the same laptop. No real phone is ever involved,
and Android Studio is NOT required.
"""

import sys
import os
import traceback
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Enable high DPI scaling before creating QApplication
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "1"


def _log_crash(exc_type, exc_value, exc_traceback):
    """Log crash to file and show dialog."""
    tb_lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    tb_str = "".join(tb_lines)

    crash_log = Path.home() / ".apkpreview" / "crash.log"
    crash_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(crash_log, "a") as f:
            f.write(f"\n=== Crash at {os.path.getmtime(crash_log) if crash_log.exists() else 'now'} ===\n")
            f.write(tb_str)
    except Exception:
        pass

    try:
        from PyQt6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        if app:
            QMessageBox.critical(
                None,
                "ApkPreview Crashed",
                f"An unhandled exception occurred:\n\n{exc_type.__name__}: {exc_value}\n\n"
                f"Details saved to: {crash_log}\n\n"
                "Please report this issue on GitHub."
            )
    except Exception:
        pass


def _thread_excepthook(args):
    """Handle exceptions in threads."""
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback)


def main():
    """Main entry point (GUI)."""
    # Install global exception hooks
    sys.excepthook = _log_crash
    threading.excepthook = _thread_excepthook

    from PyQt6.QtWidgets import QApplication
    from ui.main_window_v2 import MainWindowV2
    from ui.setup_wizard import SetupWizard, needs_setup

    app = QApplication(sys.argv)
    app.setApplicationName("ApkPreview")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("ApkPreview")
    app.setOrganizationDomain("apkpreview.dev")

    font = app.font()
    font.setPointSize(9)
    app.setFont(font)

    window = MainWindowV2()
    window.show()

    # First launch, or missing required components: open the resumable wizard.
    # It never starts the emulator; it only installs/checks and creates an AVD.
    if needs_setup():
        wizard = SetupWizard(window)
        if wizard.exec():
            window.refresh_after_setup()

    return app.exec()


def run_bootstrap_cli(argv=None) -> int:
    """Headless installer hook: install JDK/cmdline-tools/platform-tools/emulator."""
    from core.bootstrap import bootstrap_cli
    return bootstrap_cli(argv or sys.argv)


if __name__ == "__main__":
    if "--bootstrap" in sys.argv or "--install-requirements" in sys.argv:
        sys.exit(run_bootstrap_cli(sys.argv))
    sys.exit(main())
