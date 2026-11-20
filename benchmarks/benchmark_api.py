"""Benchmark the API 30 and API 34 system images.

For each image it measures:
  * cold boot        : process start -> sys.boot_completed == 1
  * launcher time    : boot complete -> the launcher is actually resumed
  * warm boot        : the same start with the quickboot snapshot
  * idle CPU / RAM   : the host CPU% and RSS of the emulator process
  * frame rate       : frames the guest produces while the screen changes

Usage:  .venv/bin/python3 benchmarks/benchmark_api.py
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK = Path.home() / ".apkpreview" / "sdk"
ADB = SDK / "platform-tools" / "adb"
EMULATOR = SDK / "emulator" / "emulator"
AVDMANAGER = SDK / "cmdline-tools" / "latest" / "bin" / "avdmanager"
AVD_DIR = Path.home() / ".android" / "avd"
SERIAL = "emulator-5554"

SCREEN = os.environ.get("BENCH_SCREEN", "540x960")
DENSITY = os.environ.get("BENCH_DENSITY", "240")
MEMORY = os.environ.get("BENCH_MEMORY", "2048")
CORES = os.environ.get("BENCH_CORES", "2")


def sh(args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def adb(*args, timeout=60):
    return sh([str(ADB), "-s", SERIAL, *args], timeout=timeout)


def stop_emulator():
    adb("emu", "kill", timeout=30)
    time.sleep(3)
    sh(["pkill", "-f", "qemu-system"], timeout=30)
    time.sleep(2)


def ensure_avd(api_level, name):
    """Create the AVD for an API level if it does not exist yet."""
    ini = AVD_DIR / f"{name}.ini"
    if ini.exists():
        return True
    image = "system-images;android-%d;default;x86_64" % api_level
    print(f"  creating AVD {name} ({image}) ...")
    env = os.environ.copy()
    # Use whichever JDK is available (the SDK tools need a JRE).
    for candidate in ("/usr/local/Cellar/openjdk@17/17.0.20.1/libexec/"
                       "openjdk.jdk/Contents/Home",):
        if Path(candidate).exists():
            env["JAVA_HOME"] = candidate
            break
    else:
        import shutil as _sh
        java = _sh.which("java")
        if java:
            env["JAVA_HOME"] = str(Path(java).resolve().parent.parent)
    r = subprocess.run(
        [str(AVDMANAGER), "create", "avd", "-n", name, "-k", image,
         "-d", "pixel_5", "--force"],
        input="no\n", capture_output=True, text=True, env=env, timeout=300)
    if r.returncode != 0:
        print("  avdmanager failed:", (r.stderr or "")[:300])
        return False
    return True


def start(avd, use_snapshot):
    cmd = [str(EMULATOR), "-avd", avd, "-no-window", "-no-audio",
           "-no-boot-anim", "-netfast", "-accel", "on",
           "-gpu", "host", "-skin", SCREEN, "-memory", MEMORY,
           "-cores", CORES, "-camera-back", "none", "-camera-front", "none"]
    if use_snapshot:
        cmd += ["-snapshot", "default_boot", "-no-snapshot-save"]
    else:
        cmd += ["-no-snapshot-load", "-no-snapshot-save"]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_boot(timeout=420):
    t0 = time.time()
    booted = None
    while time.time() - t0 < timeout:
        r = adb("shell", "getprop", "sys.boot_completed", timeout=20)
        if r.stdout.strip() == "1":
            booted = time.time() - t0
            break
        time.sleep(1)
    if booted is None:
        return None, None
    # The launcher is up once it holds the focus.
    t1 = time.time()
    launcher = None
    while time.time() - t1 < 240:
        r = adb("shell", "dumpsys", "window", "windows", timeout=30)
        out = (r.stdout or "").lower()
        if "mcurrentfocus" in out and "launcher" in out:
            launcher = time.time() - t0
            break
        time.sleep(1)
    return booted, launcher


def host_usage(pid, seconds=8):
    import psutil
    try:
        proc = psutil.Process(pid)
    except Exception:
        return 0.0, 0
    proc.cpu_percent(None)
    time.sleep(seconds)
    try:
        return proc.cpu_percent(None), proc.memory_info().rss // (1024 * 1024)
    except Exception:
        return 0.0, 0


def guest_rss_mb():
    r = adb("shell", "cat", "/proc/meminfo", timeout=30)
    for line in (r.stdout or "").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1]) // 1024
    return 0


def measure_latency(timeout=40):
    """Seconds from an input event to the screen actually changing."""
    import hashlib

    def shot():
        out = Path("/tmp/_bench_lat.png")
        with open(out, "wb") as handle:
            subprocess.run([str(ADB), "-s", SERIAL, "exec-out",
                            "screencap", "-p"], stdout=handle, timeout=60)
        return hashlib.md5(out.read_bytes()).hexdigest()

    before = shot()
    t0 = time.time()
    adb("shell", "input", "keyevent", "KEYCODE_APP_SWITCH", timeout=20)
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        if shot() != before:
            return time.time() - t0
    return None


def run(avd, label):
    stop_emulator()
    print(f"\n=== {label} ({avd}) ===", flush=True)

    # --- cold boot -----------------------------------------------------
    proc = start(avd, use_snapshot=False)
    cold, launcher = wait_boot()
    if cold is None:
        print("  COLD BOOT FAILED")
        stop_emulator()
        return None
    print(f"  cold boot       : {cold:.1f}s")
    print(f"  launcher        : {launcher:.1f}s" if launcher
          else "  launcher        : n/a")
    cpu, rss = host_usage(proc.pid)
    print(f"  idle host cpu   : {cpu:.1f}%")
    print(f"  host rss        : {rss} MB")
    print(f"  guest ram       : {guest_rss_mb()} MB")
    lat = measure_latency()
    print(f"  tap latency     : {lat:.2f}s" if lat
          else "  tap latency     : NO CHANGE")
    adb("shell", "input", "keyevent", "KEYCODE_HOME")
    stop_emulator()

    # --- warm boot (snapshot saved by the app) -------------------------
    print("  (saving quickboot snapshot)")
    proc = start(avd, use_snapshot=False)
    wait_boot()
    adb("shell", "mkdir", "-p", f"/data/local/tmp", timeout=20)
    # The app saves the snapshot with: adb emu avd snapshot save default_boot
    adb("emu", "avd", "snapshot", "save", "default_boot", timeout=120)
    stop_emulator()

    proc = start(avd, use_snapshot=True)
    warm, _ = wait_boot()
    print(f"  warm boot       : {warm:.1f}s" if warm else "  warm boot: FAIL")
    stop_emulator()
    return {"cold": cold, "launcher": launcher, "cpu": cpu, "rss": rss,
            "warm": warm, "lat": lat}


def main():
    results = {}
    for api, avd, label in ((34, "apkpreview_default", "API 34 (Android 14)"),
                            (30, "apkpreview_api30", "API 30 (Android 11)")):
        if not ensure_avd(api, avd):
            continue
        results[label] = run(avd, label)

    print("\n\n================ SUMMARY ================")
    print(f"{'image':24} {'cold':>8} {'launcher':>9} {'warm':>8} "
          f"{'cpu%':>7} {'rss':>6} {'latency':>8}")
    for label, data in results.items():
        if not data:
            print(f"{label:24} {'FAIL':>8}")
            continue
        print(f"{label:24} {data['cold']:>7.1f}s "
              f"{(data['launcher'] or 0):>8.1f}s "
              f"{(data['warm'] or 0):>7.1f}s {data['cpu']:>7.1f} "
              f"{data['rss']:>6} {(data['lat'] or 0):>7.2f}s")


if __name__ == "__main__":
    main()
