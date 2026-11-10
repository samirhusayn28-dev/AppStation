#!/usr/bin/env python3
"""Measure real emulator performance for ApkPreview.

This is a benchmark, not a test: it starts the local emulator, waits for
``sys.boot_completed``, samples the emulator process tree, and stops it again.
It never downloads anything and never touches an APK.

Metrics
-------
* cold boot seconds      - emulator started with no snapshot load
* warm boot seconds      - quickboot snapshot load (``-snapshot <name>``)
* peak RAM               - maximum summed RSS of the emulator process tree
* idle CPU (60 s)        - average summed CPU% while the device is idle

Run from the repository root::

    python3 benchmarks/benchmark_emulator.py --avd apkpreview_default
    python3 benchmarks/benchmark_emulator.py --avd apkpreview_default --json out.json

The command is built with the application's own ``EmulatorManager`` when
possible, so before/after runs use the same code path.  For warm boot the
script removes the cold-only snapshot flags and adds ``-snapshot <name>``;
after Phase 4 the manager can also be asked for quickboot directly.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import psutil

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.emulator import EmulatorManager
from core.hardware_profile import PerformanceMode
from core.platform_utils import PlatformUtils
from core.settings import settings


def _adb() -> Path:
    return PlatformUtils.get_adb_path()


def _serial_for_port(port: int) -> str:
    return f"emulator-{port}"


def _running_serials() -> List[str]:
    try:
        out = subprocess.run(
            [str(_adb()), "devices"], capture_output=True, text=True,
            timeout=15).stdout
    except Exception:
        return []
    serials = []
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("emulator-"):
            serials.append(parts[0])
    return serials


def _base_command(avd: str, port: int, headless: bool,
                  quickboot: bool, snapshot: str) -> List[str]:
    """Build the emulator command, preferring the app's manager."""
    # Point the manager at the requested AVD without writing settings.json.
    settings._data["selected_avd_name"] = avd
    manager = EmulatorManager()
    cmd: List[str]
    try:
        cmd = manager.build_start_command(
            PerformanceMode.NORMAL, force_window=False,
            no_window=headless, use_snapshot=quickboot,
            snapshot_name=snapshot)
    except TypeError:
        # Pre-Phase-4 signature: build the normal command and adjust it.
        cmd = manager.build_start_command(
            PerformanceMode.NORMAL, force_window=False, no_window=headless)
        cmd = [c for c in cmd if c not in ("-no-snapshot-load",
                                           "-no-snapshot-save")]
        if not quickboot:
            cmd.extend(["-no-snapshot-load", "-no-snapshot-save"])
        else:
            cmd.extend(["-no-snapshot-save", "-snapshot", snapshot])

    if headless and "-no-window" not in cmd:
        cmd.append("-no-window")
    if "-port" not in cmd:
        cmd.extend(["-port", str(port)])
    return cmd


def _wait_for_device(serial: str, proc: subprocess.Popen, timeout: int) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if proc.poll() is not None:
            return False
        try:
            result = subprocess.run(
                [str(_adb()), "-s", serial, "get-state"],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and "device" in result.stdout:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _boot_completed(serial: str) -> bool:
    try:
        result = subprocess.run(
            [str(_adb()), "-s", serial, "shell", "getprop",
             "sys.boot_completed"],
            capture_output=True, text=True, timeout=10)
        return result.returncode == 0 and result.stdout.strip() == "1"
    except Exception:
        return False


def _tree(root_pid: int, cache: Dict[int, psutil.Process]) -> List[psutil.Process]:
    procs: Dict[int, psutil.Process] = {}

    def add(proc: psutil.Process):
        try:
            procs.setdefault(proc.pid, proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    try:
        root = cache.get(root_pid) or psutil.Process(root_pid)
        cache[root_pid] = root
        add(root)
        for child in root.children(recursive=True):
            cached = cache.get(child.pid) or child
            cache[child.pid] = cached
            add(cached)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # qemu-system may be re-parented; include it too.
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "qemu-system" in name.lower():
            cached = cache.get(proc.pid) or proc
            cache[proc.pid] = cached
            add(cached)
    return list(procs.values())


def _sample_tree(root_pid: int,
                 cache: Dict[int, psutil.Process]) -> Tuple[int, float]:
    """Return (total RSS bytes, total CPU percent)."""
    rss = 0
    cpu = 0.0
    for proc in _tree(root_pid, cache):
        try:
            rss += proc.memory_info().rss
            cpu += proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cache.pop(proc.pid, None)
    return rss, cpu


def _save_snapshot(serial: str, name: str) -> Tuple[bool, str]:
    try:
        result = subprocess.run(
            [str(_adb()), "-s", serial, "emu", "avd", "snapshot", "save",
             name],
            capture_output=True, text=True, timeout=60)
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except Exception as exc:
        return False, str(exc)


def _stop(serial: str, proc: subprocess.Popen) -> None:
    try:
        subprocess.run([str(_adb()), "-s", serial, "emu", "kill"],
                       capture_output=True, text=True, timeout=15)
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
        return
    except Exception:
        pass
    try:
        PlatformUtils.kill_process_tree(proc.pid)
    except Exception:
        pass


def run_once(avd: str, port: int, headless: bool, quickboot: bool,
             snapshot: str, boot_timeout: int, idle_seconds: int,
             settle_seconds: int) -> Dict:
    serial = _serial_for_port(port)
    cmd = _base_command(avd, port, headless, quickboot, snapshot)
    print("command:", " ".join(cmd), flush=True)

    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    cache: Dict[int, psutil.Process] = {}
    peak_rss = 0
    start = time.time()

    # Prime CPU counters so the first idle sample is meaningful.
    _sample_tree(proc.pid, cache)

    boot_ok = False
    while time.time() - start < boot_timeout:
        if proc.poll() is not None:
            break
        if _boot_completed(serial):
            boot_ok = True
            break
        rss, _cpu = _sample_tree(proc.pid, cache)
        peak_rss = max(peak_rss, rss)
        time.sleep(0.5)

    boot_seconds = time.time() - start
    if not boot_ok:
        _stop(serial, proc)
        raise RuntimeError(
            f"emulator did not boot within {boot_timeout}s "
            f"(exit={proc.poll()})")

    # Let boot-time work settle before measuring idle.
    settle_end = time.time() + settle_seconds
    while time.time() < settle_end:
        rss, _cpu = _sample_tree(proc.pid, cache)
        peak_rss = max(peak_rss, rss)
        time.sleep(0.5)

    cpu_samples: List[float] = []
    rss_samples: List[int] = []
    idle_end = time.time() + idle_seconds
    while time.time() < idle_end:
        rss, cpu = _sample_tree(proc.pid, cache)
        peak_rss = max(peak_rss, rss)
        rss_samples.append(rss)
        cpu_samples.append(cpu)
        time.sleep(2)

    result = {
        "mode": "warm" if quickboot else "cold",
        "boot_seconds": round(boot_seconds, 2),
        "peak_ram_mb": round(peak_rss / (1024 ** 2), 1),
        "idle_cpu_avg_percent": round(statistics.mean(cpu_samples), 1)
        if cpu_samples else 0.0,
        "idle_cpu_samples": len(cpu_samples),
        "idle_ram_avg_mb": round(
            statistics.mean(rss_samples) / (1024 ** 2), 1)
        if rss_samples else 0.0,
        "serial": serial,
        "command": cmd,
    }
    return result, proc, serial


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--avd", default="apkpreview_default")
    parser.add_argument("--port", type=int, default=5580)
    parser.add_argument("--window", action="store_true",
                        help="show the emulator window (default: headless)")
    parser.add_argument("--boot-timeout", type=int, default=180)
    parser.add_argument("--idle-seconds", type=int, default=60)
    parser.add_argument("--settle-seconds", type=int, default=8)
    parser.add_argument("--snapshot", default="bench_warm")
    parser.add_argument("--skip-warm", action="store_true")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    if not _adb().exists():
        print(f"adb not found at {_adb()}", file=sys.stderr)
        return 2
    if not PlatformUtils.get_emulator_path().exists():
        print(f"emulator not found at {PlatformUtils.get_emulator_path()}",
              file=sys.stderr)
        return 2
    running = _running_serials()
    if running:
        print(f"An emulator is already running: {', '.join(running)}. "
              "Stop it first so the benchmark is reproducible.",
              file=sys.stderr)
        return 2

    results: Dict[str, Dict] = {}
    proc = None
    serial = _serial_for_port(args.port)

    try:
        print("=== cold boot ===", flush=True)
        cold, proc, serial = run_once(
            args.avd, args.port, not args.window, False, args.snapshot,
            args.boot_timeout, args.idle_seconds, args.settle_seconds)
        results["cold"] = cold

        if not args.skip_warm:
            print("=== saving quickboot snapshot ===", flush=True)
            ok, message = _save_snapshot(serial, args.snapshot)
            print(f"snapshot save: {ok} {message}", flush=True)
            _stop(serial, proc)
            proc = None
            time.sleep(2)

            print("=== warm boot (quickboot snapshot) ===", flush=True)
            warm, proc, serial = run_once(
                args.avd, args.port, not args.window, True, args.snapshot,
                args.boot_timeout, args.idle_seconds, args.settle_seconds)
            results["warm"] = warm
    finally:
        if proc is not None:
            _stop(serial, proc)

    print()
    for key in ("cold", "warm"):
        if key not in results:
            continue
        r = results[key]
        print(f"{key:5s}  boot {r['boot_seconds']:6.2f}s  "
              f"peak RAM {r['peak_ram_mb']:7.1f} MB  "
              f"idle CPU {r['idle_cpu_avg_percent']:5.1f}%  "
              f"idle RAM {r['idle_ram_avg_mb']:7.1f} MB")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2),
                                   encoding="utf-8")
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
