"""Measure AppStation startup time (process start -> window visible).

Run:  .venv/bin/python3 measure_startup.py
Prints a breakdown of the phases so regressions are easy to spot.
"""
import os
import sys
import time

_T0 = time.perf_counter()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtWidgets import QApplication  # noqa: E402

_t_import = time.perf_counter()

app = QApplication(sys.argv)
app.setApplicationName("AppStation")
_t_app = time.perf_counter()

from ui.main_window_v2 import MainWindowV2  # noqa: E402

_t_mod = time.perf_counter()

window = MainWindowV2()
_t_win = time.perf_counter()

window.show()
app.processEvents()
_t_show = time.perf_counter()


def ms(a, b):
    return (b - a) * 1000.0


print("AppStation startup timing (process start -> window visible):")
print(f"  {'import PyQt6':<28} {ms(_T0, _t_import):8.1f} ms")
print(f"  {'QApplication':<28} {ms(_t_import, _t_app):8.1f} ms")
print(f"  {'import window module':<28} {ms(_t_app, _t_mod):8.1f} ms")
print(f"  {'build main window':<28} {ms(_t_mod, _t_win):8.1f} ms")
print(f"  {'show window':<28} {ms(_t_win, _t_show):8.1f} ms")
print(f"  {'TOTAL':<28} {ms(_T0, _t_show):8.1f} ms")
