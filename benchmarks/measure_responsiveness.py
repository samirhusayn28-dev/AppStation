"""Measure real input -> visible-change latency and live frame rate.

The idle "fps" of an emulator is meaningless (an undamaged surface produces no
new frames).  What the user feels is:

  * how long a tap takes to change the screen, and
  * how many frames the guest produces while something is actually animating.

This script therefore forces a *continuous* animation in the guest and then
measures the real frame rate, plus measures the tap -> visible change latency.
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK = Path.home() / ".apkpreview" / "sdk"
ADB = SDK / "platform-tools" / "adb"
SERIAL = "emulator-5554"


def adb(*args, timeout=60):
    return subprocess.run([str(ADB), "-s", SERIAL, *args],
                          capture_output=True, text=True, timeout=timeout)


def screencap(path):
    with open(path, "wb") as handle:
        subprocess.run([str(ADB), "-s", SERIAL, "exec-out", "screencap", "-p"],
                       stdout=handle, timeout=60)


def animated_fps(seconds=6):
    """Frames per second while the guest is genuinely animating.

    A scrolling animation keeps the surface damaged, so every frame the
    compositor produces is a real frame.
    """
    # Start a continuous scroll animation in the launcher.
    adb("shell", "input", "swipe", "540", "1600", "540", "400", "3000")
    adb("shell", "rm", "-f", "/sdcard/bench.mp4")
    subprocess.run([str(ADB), "-s", SERIAL, "shell",
                    f"screenrecord --time-limit {seconds} "
                    "--bit-rate 2000000 /sdcard/bench.mp4"],
                   capture_output=True, timeout=seconds + 40)
    out = Path("/tmp/bench_rec2.mp4")
    if out.exists():
        out.unlink()
    subprocess.run([str(ADB), "-s", SERIAL, "pull", "/sdcard/bench.mp4",
                    str(out)], capture_output=True, timeout=60)
    if not out.exists():
        return 0.0
    try:
        import av
        container = av.open(str(out))
        frames = [f.pts for f in container.decode(video=0)]
        if len(frames) < 2:
            return 0.0
        span = (frames[-1] - frames[0]) / 1_000_000.0
        return (len(frames) - 1) / span if span > 0 else 0.0
    except Exception as exc:
        print("decode failed:", exc)
        return 0.0


def tap_latency(timeout=40):
    """Seconds from an input event to the screen actually changing."""
    screencap("/tmp/lat_a.png")
    before = Path("/tmp/lat_a.png").read_bytes()
    t0 = time.time()
    adb("shell", "input", "keyevent", "KEYCODE_APP_SWITCH", timeout=20)
    while time.time() - t0 < timeout:
        time.sleep(0.25)
        screencap("/tmp/lat_b.png")
        after = Path("/tmp/lat_b.png").read_bytes()
        if after != before:
            return time.time() - t0
    return None


def main():
    print(f"boot_completed : {(adb('shell', 'getprop', 'sys.boot_completed').stdout or '').strip()}")
    print(f"animated fps   : {animated_fps():.2f}")
    lat = tap_latency()
    print(f"tap latency    : {lat if lat is not None else 'NO CHANGE (frozen)'}")
    # Close whatever opened.
    adb("shell", "input", "keyevent", "KEYCODE_HOME")


if __name__ == "__main__":
    main()
