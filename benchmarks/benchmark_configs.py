"""Benchmark emulator configurations on this host.

Measures, for each configuration:
  * cold-boot time  : process start -> sys.boot_completed == 1
  * launcher time   : boot complete -> launcher actually drawn
  * frame rate      : frames the guest can actually produce (screenrecord)
  * idle CPU / RAM  : host emulator CPU% and RSS while the guest is idle

Usage:  .venv/bin/python3 benchmarks/benchmark_configs.py [name ...]
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK = Path.home() / ".apkpreview" / "sdk"
ADB = SDK / "platform-tools" / "adb"
EMULATOR = SDK / "emulator" / "emulator"
AVD = "apkpreview_default"
SERIAL = "emulator-5554"

sys.path.insert(0, str(ROOT))


def sh(args, timeout=120):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def adb(*args, timeout=60):
    return sh([str(ADB), "-s", SERIAL, *args], timeout=timeout)


def stop_emulator():
    adb("emu", "kill", timeout=30)
    time.sleep(3)
    sh(["pkill", "-f", "qemu-system"], timeout=30)
    time.sleep(2)


def start_emulator(extra):
    cmd = [str(EMULATOR), "-avd", AVD, "-no-window", "-no-audio",
           "-no-boot-anim", "-netfast", "-no-snapshot-load",
           "-no-snapshot-save", "-camera-back", "none",
           "-camera-front", "none", "-accel", "on"]
    cmd += extra
    print(f"  cmd: {' '.join(cmd[1:])}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_boot(timeout=420):
    """Return (boot_seconds, launcher_seconds)."""
    t0 = time.time()
    booted = None
    while time.time() - t0 < timeout:
        r = adb("shell", "getprop", "sys.boot_completed", timeout=20)
        if r.returncode == 0 and r.stdout.strip() == "1":
            booted = time.time() - t0
            break
        time.sleep(1)
    if booted is None:
        return None, None
    # The launcher is up once the home screen is drawn: query the focused
    # activity of the launcher package.
    t1 = time.time()
    launcher = None
    while time.time() - t1 < 180:
        r = adb("shell", "dumpsys", "activity", "activities", timeout=30)
        out = r.stdout or ""
        if "mResumedActivity" in out and "launcher" in out.lower():
            launcher = time.time() - t0
            break
        time.sleep(1)
    return booted, launcher


def frame_rate(seconds=6):
    """Frames the guest can actually produce, measured with screenrecord."""
    adb("shell", "rm", "-f", "/sdcard/bench.mp4", timeout=30)
    sh([str(ADB), "-s", SERIAL, "shell",
        f"screenrecord --time-limit {seconds} --bit-rate 2000000 "
        "/sdcard/bench.mp4"], timeout=seconds + 40)
    out = Path("/tmp/bench_rec.mp4")
    if out.exists():
        out.unlink()
    sh([str(ADB), "-s", SERIAL, "pull", "/sdcard/bench.mp4", str(out)],
       timeout=60)
    if not out.exists():
        return 0.0
    try:
        import av
        container = av.open(str(out))
        n = sum(1 for _ in container.decode(video=0))
        return n / float(seconds)
    except Exception:
        return 0.0


def host_usage(pid, seconds=6):
    """Average host CPU% and peak RSS of the emulator process."""
    import psutil
    try:
        proc = psutil.Process(pid)
    except Exception:
        return 0.0, 0
    proc.cpu_percent(None)
    time.sleep(seconds)
    try:
        cpu = proc.cpu_percent(None)
        rss = proc.memory_info().rss
    except Exception:
        return 0.0, 0
    return cpu, rss // (1024 * 1024)


def guest_animation_state():
    r = adb("shell", "settings", "get", "global", "window_animation_scale")
    return (r.stdout or "").strip()


CONFIGS = {
    # name: (emulator args, avd for api30?)
    "A_host_1080_api34": ["-gpu", "host", "-skin", "1080x1920",
                           "-dpi-device", "420", "-memory", "2048",
                           "-cores", "2"],
    "B_swift_1080_api34": ["-gpu", "swiftshader_indirect",
                            "-skin", "1080x1920", "-dpi-device", "420",
                            "-memory", "2048", "-cores", "2"],
    "C_swift_720_api34": ["-gpu", "swiftshader_indirect",
                           "-skin", "720x1280", "-dpi-device", "280",
                           "-memory", "2048", "-cores", "2"],
    "D_swift_720_api30": ["-gpu", "swiftshader_indirect",
                           "-skin", "720x1280", "-dpi-device", "280",
                           "-memory", "1536", "-cores", "2"],
    "E_host_720_api34": ["-gpu", "host", "-skin", "720x1280",
                          "-dpi-device", "280", "-memory", "2048",
                          "-cores", "2"],
}


def main():
    names = sys.argv[1:] or list(CONFIGS)
    results = []
    for name in names:
        if name not in CONFIGS:
            print(f"unknown config {name}")
            continue
        print(f"\n=== {name} ===")
        stop_emulator()
        proc = start_emulator(CONFIGS[name])
        boot, launcher = wait_boot()
        if boot is None:
            print("  BOOT FAILED")
            results.append((name, None, None, 0.0, 0.0, 0))
            stop_emulator()
            continue
        fps = frame_rate()
        cpu, rss = host_usage(proc.pid)
        anim = guest_animation_state()
        print(f"  boot           : {boot:.1f}s")
        print(f"  launcher       : {launcher}s")
        print(f"  guest fps      : {fps:.2f}")
        print(f"  host cpu       : {cpu:.1f}%")
        print(f"  host rss       : {rss} MB")
        print(f"  anim scale     : {anim}")
        results.append((name, boot, launcher, fps, cpu, rss))
        stop_emulator()

    print("\n\n================ SUMMARY ================")
    print(f"{'config':24} {'boot':>8} {'launcher':>9} {'fps':>7} "
          f"{'cpu%':>7} {'rss':>6}")
    for name, boot, launcher, fps, cpu, rss in results:
        b = f"{boot:.1f}s" if boot else "FAIL"
        la = f"{launcher:.1f}s" if launcher else "-"
        print(f"{name:24} {b:>8} {la:>9} {fps:>7.2f} {cpu:>7.1f} {rss:>6}")


if __name__ == "__main__":
    main()
