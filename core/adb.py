"""ADB wrapper for AppStation - install, launch, shell commands, device management."""

import subprocess
import time
from pathlib import Path
from typing import Optional, List, Tuple, Dict
from .platform_utils import PlatformUtils
from .settings import settings


class ADB:
    """ADB command wrapper."""
    
    def __init__(self, serial: Optional[str] = None):
        self._adb_path = PlatformUtils.get_adb_path()
        self._serial = serial or "emulator-5554"
    
    @property
    def serial(self) -> str:
        return self._serial
    
    @serial.setter
    def serial(self, value: str):
        self._serial = value
    
    def _run(self, args: List[str], timeout: int = 30) -> subprocess.CompletedProcess:
        """Run adb command."""
        cmd = [str(self._adb_path)]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(args)
        return PlatformUtils.run_command(cmd, timeout=timeout)
    
    def wait_for_device(self, timeout: int = 180) -> Tuple[bool, str]:
        """Wait for device to be fully booted and ready for app installation."""
        start = time.time()
        while time.time() - start < timeout:
            # Check device is online
            result = self._run(["wait-for-device"], timeout=5)
            if result.returncode != 0:
                time.sleep(1)
                continue
            
            # Check boot completed
            boot = self._run(["shell", "getprop", "sys.boot_completed"], timeout=5)
            if boot.returncode != 0 or boot.stdout.strip() != "1":
                time.sleep(1)
                continue
            
            # Check boot animation stopped
            bootanim = self._run(["shell", "getprop", "init.svc.bootanim"], timeout=5)
            if bootanim.returncode != 0 or bootanim.stdout.strip() != "stopped":
                time.sleep(1)
                continue
            
            # Check package manager is ready
            pm = self._run(["shell", "pm", "path", "android"], timeout=10)
            if pm.returncode != 0 or not pm.stdout.strip():
                time.sleep(1)
                continue
            
            # All checks passed, wait 3 more seconds for stability
            time.sleep(3)
            return True, "Device ready"
        
        return False, "Device wait timeout"
    
    def get_devices(self) -> List[Dict[str, str]]:
        """Get list of connected devices."""
        result = self._run(["devices", "-l"])
        devices = []
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n")[1:]:  # Skip header
                parts = line.split()
                if len(parts) >= 2:
                    devices.append({
                        "serial": parts[0],
                        "state": parts[1],
                        "model": "",
                        "device": "",
                    })
        return devices
    
    def install(self, apk_path: Path, reinstall: bool = True, 
                grant_permissions: bool = True) -> Tuple[bool, str]:
        """Install an APK."""
        args = ["install"]
        if reinstall:
            args.append("-r")
        if grant_permissions:
            args.append("-g")
        args.append(str(apk_path))
        
        result = self._run(args, timeout=120)
        if result.returncode == 0:
            return True, "Install successful"
        return False, result.stderr.strip() or result.stdout.strip()
    
    def uninstall(self, package_name: str) -> Tuple[bool, str]:
        """Uninstall a package."""
        result = self._run(["uninstall", package_name], timeout=30)
        if result.returncode == 0:
            return True, "Uninstall successful"
        return False, result.stderr.strip()
    
    def clear_data(self, package_name: str) -> Tuple[bool, str]:
        """Clear app data."""
        result = self._run(["shell", "pm", "clear", package_name], timeout=30)
        if result.returncode == 0:
            return True, "Data cleared"
        return False, result.stderr.strip()
    
    def start_activity(self, package: str, activity: str, 
                       action: str = "android.intent.action.MAIN",
                       category: str = "android.intent.category.LAUNCHER",
                       extras: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
        """Start an activity."""
        args = ["shell", "am", "start", "-n", f"{package}/{activity}"]
        args.extend(["-a", action])
        args.extend(["-c", category])
        
        if extras:
            for key, value in extras.items():
                args.extend(["-e", key, value])
        
        result = self._run(args, timeout=30)
        if result.returncode == 0:
            return True, "Activity started"
        return False, result.stderr.strip()
    
    def force_stop(self, package: str) -> Tuple[bool, str]:
        """Force stop an app."""
        result = self._run(["shell", "am", "force-stop", package], timeout=10)
        if result.returncode == 0:
            return True, "App stopped"
        return False, result.stderr.strip()

    def input_keyevent(self, keycode: str) -> Tuple[bool, str]:
        """Send a hardware key event (adb shell input keyevent KEYCODE_...)."""
        result = self._run(["shell", "input", "keyevent", keycode], timeout=10)
        if result.returncode == 0:
            return True, "Key event sent"
        return False, result.stderr.strip() or result.stdout.strip()

    def input_touch(self, x: float, y: float) -> Tuple[bool, str]:
        """Send a touch tap at screen coordinates (adb shell input tap x y)."""
        result = self._run(
            ["shell", "input", "tap", str(int(x)), str(int(y))], timeout=10)
        if result.returncode == 0:
            return True, f"Tap at {int(x)},{int(y)} sent"
        return False, result.stderr.strip() or result.stdout.strip()

    def input_swipe(self, x1: float, y1: float, x2: float, y2: float,
                    duration_ms: int = 300) -> Tuple[bool, str]:
        """Send a swipe gesture (adb shell input swipe x1 y1 x2 y2 duration)."""
        result = self._run(
            ["shell", "input", "swipe",
             str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
             str(int(duration_ms))], timeout=10)
        if result.returncode == 0:
            return True, f"Swipe ({int(x1)},{int(y1)})->({int(x2)},{int(y2)}) sent"
        return False, result.stderr.strip() or result.stdout.strip()
    
    def get_package_info(self, package: str) -> Optional[Dict]:
        """Get package info (version, path, etc.)."""
        result = self._run(["shell", "dumpsys", "package", package], timeout=10)
        if result.returncode != 0:
            return None
        
        info = {}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if "versionName=" in line:
                info["version_name"] = line.split("versionName=")[1].split()[0]
            elif "versionCode=" in line:
                info["version_code"] = line.split("versionCode=")[1].split()[0]
            elif "path=" in line and "apk" in line:
                info["path"] = line.split("path=")[1].split()[0]
        return info if info else None
    
    def shell(self, command: str, timeout: int = 30) -> Tuple[bool, str]:
        """Run a shell command on device."""
        result = self._run(["shell", command], timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    
    def shell_silent(self, command: str, timeout: int = 30) -> str:
        """Run shell command and return stdout (empty on error)."""
        result = self._run(["shell", command], timeout=timeout)
        return result.stdout.strip() if result.returncode == 0 else ""
    
    # Device configuration commands
    def set_screen_size(self, width: int, height: int) -> Tuple[bool, str]:
        """Set screen resolution (wm size)."""
        return self.shell(f"wm size {width}x{height}")
    
    def reset_screen_size(self) -> Tuple[bool, str]:
        """Reset screen resolution to default."""
        return self.shell("wm size reset")
    
    def get_screen_size(self) -> Optional[Tuple[int, int]]:
        """Get current screen resolution."""
        result = self.shell("wm size")
        if result[0]:
            output = result[1]
            # Check for override size first (when an override is active)
            if "Override size:" in output:
                size_str = output.split("Override size:")[1].strip()
            elif "Physical size:" in output:
                size_str = output.split("Physical size:")[1].strip()
            else:
                return None
            try:
                w, h = map(int, size_str.split("x"))
                return w, h
            except Exception:
                pass
        return None
    
    def set_density(self, dpi: int) -> Tuple[bool, str]:
        """Set screen density (wm density)."""
        return self.shell(f"wm density {dpi}")
    
    def reset_density(self) -> Tuple[bool, str]:
        """Reset screen density to default."""
        return self.shell("wm density reset")
    
    def get_density(self) -> Optional[int]:
        """Get current screen density."""
        result = self.shell("wm density")
        if result[0]:
            output = result[1]
            # Check for override density first (when an override is active)
            if "Override density:" in output:
                density_str = output.split("Override density:")[1].strip()
            elif "Physical density:" in output:
                density_str = output.split("Physical density:")[1].strip()
            else:
                return None
            try:
                return int(density_str.strip())
            except Exception:
                pass
        return None
    
    def shell_checked(self, command: str, timeout: int = 15) -> Tuple[bool, str]:
        """Run a shell command and include stderr in the returned message."""
        result = self._run(["shell", command], timeout=timeout)
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        return result.returncode == 0, output


    def set_rotation(self, rotation: int) -> Tuple[bool, str]:
        """Set screen rotation (0, 1, 2, 3 for 0°, 90°, 180°, 270°)."""
        if rotation not in (0, 1, 2, 3):
            return False, "Invalid rotation value"
        # Disable auto-rotation, set user rotation
        self.shell("settings put system accelerometer_rotation 0")
        return self.shell(f"settings put system user_rotation {rotation}")
    
    def set_auto_rotate(self, enabled: bool) -> Tuple[bool, str]:
        """Enable/disable auto-rotation."""
        return self.shell(f"settings put system accelerometer_rotation {1 if enabled else 0}")
    
    # Emulator console commands
    def emu_command(self, command: str, timeout: int = 10) -> Tuple[bool, str]:
        """Run emulator console command via adb emu."""
        result = self._run(["emu", command], timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    
    def set_gps_location(self, lat: float, lon: float) -> Tuple[bool, str]:
        """Set GPS location."""
        return self.emu_command(f"geo fix {lon} {lat}")
    
    def set_battery_level(self, level: int) -> Tuple[bool, str]:
        """Set battery level (0-100)."""
        return self.emu_command(f"power capacity {level}")
    
    def set_battery_charging(self, charging: bool) -> Tuple[bool, str]:
        """Set battery charging state."""
        return self.emu_command(f"power ac {'on' if charging else 'off'}")
    
    def set_network_speed(self, upload_kbps: int, download_kbps: int, 
                          latency_ms: int = 0) -> Tuple[bool, str]:
        """Set network speed emulation."""
        return self.emu_command(f"netstatus {upload_kbps} {download_kbps} {latency_ms}")
    
    def set_network_offline(self, offline: bool = True) -> Tuple[bool, str]:
        """Set network offline/online."""
        return self.emu_command(f"netstatus {'offline' if offline else 'full'}")
    
    def send_sms(self, from_number: str, message: str) -> Tuple[bool, str]:
        """Send fake SMS."""
        return self.emu_command(f"sms send {from_number} {message}")
    
    def make_call(self, number: str) -> Tuple[bool, str]:
        """Simulate incoming call."""
        return self.emu_command(f"gsm call {number}")
    
    def cancel_call(self, number: str) -> Tuple[bool, str]:
        """Cancel simulated call."""
        return self.emu_command(f"gsm cancel {number}")
    
    def post_notification(self, title: str, text: str, package: str = "") -> Tuple[bool, str]:
        """Post a fake notification."""
        cmd = f"cmd notification post -t '{title}' 'bigtext' '{text}'"
        if package:
            cmd += f" -p {package}"
        return self.shell(cmd)
    
    def set_dark_mode(self, enabled: bool) -> Tuple[bool, str]:
        """Toggle dark mode."""
        return self.shell(f"cmd uimode night {'yes' if enabled else 'no'}")
    
    def set_locale(self, locale: str) -> Tuple[bool, str]:
        """Set system locale (e.g., 'en_US', 'fr_FR')."""
        return self.shell(f"setprop persist.sys.locale {locale}; setprop persist.sys.language {locale.split('_')[0]}; setprop persist.sys.country {locale.split('_')[1] if '_' in locale else 'US'}")
    
    def set_font_scale(self, scale: float) -> Tuple[bool, str]:
        """Set font scale (e.g., 1.0, 1.15, 1.3)."""
        return self.shell(f"settings put system font_scale {scale}")
    
    # Performance monitoring
    def get_meminfo(self, package: str) -> Optional[Dict]:
        """Get memory info for a package."""
        result = self._run(["shell", "dumpsys", "meminfo", package], timeout=10)
        if result.returncode != 0:
            return None
        
        info = {}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if "TOTAL" in line and "PSS" in line:
                parts = line.split()
                try:
                    info["total_pss_kb"] = int(parts[1])
                except Exception:
                    pass
            elif "Native Heap" in line:
                parts = line.split()
                try:
                    info["native_heap_kb"] = int(parts[2])
                except Exception:
                    pass
            elif "Dalvik Heap" in line:
                parts = line.split()
                try:
                    info["dalvik_heap_kb"] = int(parts[2])
                except Exception:
                    pass
        return info if info else None
    
    def get_cpu_usage(self, package: str) -> Optional[float]:
        """Get CPU usage percentage for a package."""
        result = self._run(["shell", "top", "-n", "1", "-d", "0.5"], timeout=10)
        if result.returncode != 0:
            return None
        
        for line in result.stdout.split("\n"):
            if package in line:
                parts = line.split()
                # top output format varies, try to find CPU%
                for i, part in enumerate(parts):
                    if "%" in part:
                        try:
                            return float(part.replace("%", ""))
                        except Exception:
                            pass
        return None
    
    def get_fps(self, package: str) -> Optional[Dict]:
        """Get FPS info via gfxinfo."""
        result = self._run(["shell", "dumpsys", "gfxinfo", package], timeout=10)
        if result.returncode != 0:
            return None
        
        # Parse gfxinfo output (simplified)
        info = {"frames": 0, "janky": 0, "vsync": 0}
        for line in result.stdout.split("\n"):
            line = line.strip()
            if "Total frames rendered:" in line:
                try:
                    info["frames"] = int(line.split(":")[1].strip())
                except Exception:
                    pass
            elif "Janky frames:" in line:
                try:
                    info["janky"] = int(line.split(":")[1].strip().split("(")[0].strip())
                except Exception:
                    pass
        return info
    
    # File operations
    def push(self, local: Path, remote: str) -> Tuple[bool, str]:
        """Push file to device."""
        result = self._run(["push", str(local), remote], timeout=60)
        return result.returncode == 0, result.stderr.strip() or "Push successful"
    
    def pull(self, remote: str, local: Path) -> Tuple[bool, str]:
        """Pull file from device."""
        result = self._run(["pull", remote, str(local)], timeout=60)
        return result.returncode == 0, result.stderr.strip() or "Pull successful"
    
    def screenrecord(self, remote_path: str, time_limit: int = 180, 
                     bitrate: int = 4000000, size: Optional[str] = None) -> Tuple[bool, str]:
        """Start screen recording on device."""
        args = ["shell", "screenrecord", f"--time-limit={time_limit}", 
                f"--bit-rate={bitrate}"]
        if size:
            args.append(f"--size={size}")
        args.append(remote_path)
        
        # This runs in background, we return immediately
        try:
            proc = PlatformUtils.start_detached_process(
                [str(self._adb_path)] + (["-s", self._serial] if self._serial else []) + args
            )
            return True, f"Recording started (PID: {proc.pid})"
        except Exception as e:
            return False, str(e)
    
    def stop_screenrecord(self) -> Tuple[bool, str]:
        """Stop screen recording (sends Ctrl+C)."""
        result = self._run(["shell", "pkill", "screenrecord"], timeout=5)
        return result.returncode == 0, "Recording stopped"
    
    def screenshot(self, remote_path: str) -> Tuple[bool, str]:
        """Take a screenshot."""
        result = self._run(["shell", "screencap", "-p", remote_path], timeout=10)
        if result.returncode == 0:
            return True, "Screenshot taken"
        return False, result.stderr.strip()
    
    # Reverse port forwarding for dev servers
    def reverse(self, device_port: int, host_port: int) -> Tuple[bool, str]:
        """Reverse port forwarding (device:port -> host:port)."""
        result = self._run(["reverse", f"tcp:{device_port}", f"tcp:{host_port}"], timeout=10)
        return result.returncode == 0, result.stderr.strip() or "Reverse port set"
    
    def reverse_remove(self, device_port: int) -> Tuple[bool, str]:
        """Remove reverse port forwarding."""
        result = self._run(["reverse", "--remove", f"tcp:{device_port}"], timeout=10)
        return result.returncode == 0, result.stderr.strip() or "Reverse port removed"
    
    def reverse_list(self) -> List[Tuple[int, int]]:
        """List reverse port forwards."""
        result = self._run(["reverse", "--list"], timeout=10)
        forwards = []
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if "tcp:" in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            device = int(parts[0].split(":")[1])
                            host = int(parts[1].split(":")[1])
                            forwards.append((device, host))
                        except Exception:
                            pass
        return forwards


def detect_package_and_activity(apk_path: Path, aapt2_path: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """Extract package name and launcher activity from APK."""
    if aapt2_path is None:
        aapt2_path = PlatformUtils.get_aapt2_path()
    
    if not aapt2_path.exists():
        return None, None
    
    try:
        # Use aapt2 dump badging
        result = PlatformUtils.run_command([str(aapt2_path), "dump", "badging", str(apk_path)], timeout=30)
        if result.returncode != 0:
            return None, None
        
        output = result.stdout
        package = None
        activity = None
        
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("package:"):
                # package: name='com.example.app' versionCode='1' versionName='1.0'
                import re
                match = re.search(r"name='([^']+)'", line)
                if match:
                    package = match.group(1)
            elif line.startswith("launchable-activity:"):
                # launchable-activity: name='com.example.app.MainActivity' ...
                match = re.search(r"name='([^']+)'", line)
                if match:
                    activity = match.group(1)
                    break  # First launchable activity is usually the main one
        
        return package, activity
        
    except Exception:
        pass
    
    # Fallback to androguard if available
    try:
        from androguard.core.apk import APK
        apk = APK(str(apk_path))
        package = apk.get_package()
        activity = apk.get_main_activity()
        return package, activity
    except Exception:
        pass
    
    return None, None