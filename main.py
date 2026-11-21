#!/usr/bin/env python3
"""AppStation - Lightweight Android APK testing on desktop.

A developer drops an APK file on their laptop and tests it on a virtual
Android phone running on the same laptop. No real phone is ever involved,
and Android Studio is NOT required.

Startup is kept fast on purpose:

* The QApplication and a small loading screen are created first, so something
  is on screen within a few hundred milliseconds.
* The heavy imports (the main window module and everything it pulls in) happen
  *after* the loading screen is visible, still on the GUI thread but with the
  user already looking at a progress indicator.
* No SDK, avdmanager or adb call is made on the GUI thread at launch: the
  performance-mode probe runs in a worker thread and the SDK/AVD lookups are
  cached.
"""

import sys
import os
import time
import traceback
import threading
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Enable high DPI scaling before creating QApplication.  We deliberately do
# NOT set QT_AUTO_SCREEN_SCALE_FACTOR: on Windows it applies a different factor
# per monitor and made the preview a different size/aspect from macOS.
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"

# Record when the process started so the startup time can be reported.
_T0 = time.perf_counter()

# Set by the timing helper (see report_startup()).
_STARTUP_TIMINGS = {}


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
                "AppStation Crashed",
                f"An unhandled exception occurred:\n\n{exc_type.__name__}: {exc_value}\n\n"
                f"Details saved to: {crash_log}\n\n"
                "Please report this issue on GitHub."
            )
    except Exception:
        pass


def _thread_excepthook(args):
    """Handle exceptions in threads."""
    _log_crash(args.exc_type, args.exc_value, args.exc_traceback)


def _mark(name: str):
    """Record a startup checkpoint (milliseconds since process start)."""
    _STARTUP_TIMINGS[name] = (time.perf_counter() - _T0) * 1000.0


def report_startup():
    """Print the startup timing breakdown (used for the perf report)."""
    total = (time.perf_counter() - _T0) * 1000.0
    lines = ["AppStation startup timing:"]
    for name, ms in _STARTUP_TIMINGS.items():
        lines.append(f"  {name:<24} {ms:8.1f} ms")
    lines.append(f"  {'TOTAL (to window)':<24} {total:8.1f} ms")
    text = "\n".join(lines)
    try:
        print(text, flush=True)
    except Exception:
        pass
    return text


def main():
    """Main entry point (GUI)."""
    # Install global exception hooks
    sys.excepthook = _log_crash
    threading.excepthook = _thread_excepthook

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer
    _mark("qt_imported")

    app = QApplication(sys.argv)
    app.setApplicationName("AppStation")
    app.setApplicationVersion("0.1.0")
    app.setOrganizationName("AppStation")
    app.setOrganizationDomain("apkpreview.dev")

    font = app.font()
    font.setPointSize(9)
    app.setFont(font)
    _mark("app_created")

    # Show the loading screen immediately so the user never sees a blank
    # desktop while the main window module is imported.  Its background is
    # fully transparent: only "AppStation", a spinner and the step text are
    # painted.
    from ui.loading_screen import LoadingScreen
    loading = LoadingScreen()
    loading.set_step("Starting...")
    loading.show()
    app.processEvents()
    _mark("loading_shown")

    def _build_window():
        """Import the main window and show it (deferred until after paint)."""
        try:
            loading.set_step("Loading the workspace...")
            from ui.main_window_v2 import MainWindowV2
            _mark("window_imported")

            window = MainWindowV2()
            _mark("window_created")
            window.show()
            app.processEvents()
            _mark("window_shown")

            loading.stop()
            loading.close()

            # First launch, or missing required components: open the resumable
            # wizard.  It never starts the emulator; it only installs/checks and
            # creates an AVD.
            from ui.setup_wizard import needs_setup
            if needs_setup():
                from ui.setup_wizard import SetupWizard
                wizard = SetupWizard(window)
                if wizard.exec():
                    window.refresh_after_setup()

            if os.environ.get("APPSTATION_STARTUP_TIMING"):
                report_startup()
        except Exception:
            loading.stop()
            loading.close()
            raise

    # Defer the heavy work until the loading screen has actually painted.
    QTimer.singleShot(0, _build_window)

    return app.exec()


def run_bootstrap_cli(argv=None) -> int:
    """Headless installer hook: install JDK/cmdline-tools/platform-tools/emulator."""
    from core.bootstrap import bootstrap_cli
    return bootstrap_cli(argv or sys.argv)


if __name__ == "__main__":
    if "--bootstrap" in sys.argv or "--install-requirements" in sys.argv:
        sys.exit(run_bootstrap_cli(sys.argv))
    sys.exit(main())
