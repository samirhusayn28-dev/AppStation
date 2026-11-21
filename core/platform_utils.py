"""Cross-platform utilities for AppStation - all OS differences live here."""

import os
import sys
import platform
import subprocess
import shutil
import json
from pathlib import Path
from typing import Optional, Tuple, List


class PlatformUtils:
    """Centralized platform-specific operations."""
    
    # Class attributes for platform detection
    IS_WINDOWS = sys.platform == "win32"
    IS_MACOS = sys.platform == "darwin"
    IS_LINUX = sys.platform.startswith("linux")
    
    @classmethod
    def get_app_data_dir(cls) -> Path:
        """Get the application data directory (~/.apkpreview)."""
        if cls.IS_WINDOWS:
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path.home()
        return base / ".apkpreview"
    
    @classmethod
    def get_sdk_dir(cls) -> Path:
        """Get the Android SDK directory.

        Resolution order: ``APKPREVIEW_SDK_DIR``, the ``sdk_path`` saved in
        ``settings.json`` (chosen in the setup wizard), then the default
        ``~/.apkpreview/sdk``.  The settings file is read directly to avoid a
        circular import with :mod:`core.settings`.
        """
        env_path = os.environ.get("APKPREVIEW_SDK_DIR")
        if env_path:
            return Path(env_path).expanduser()
        try:
            settings_path = cls.get_settings_path()
            if settings_path.exists():
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                saved = data.get("sdk_path") or ""
                if saved:
                    return Path(saved).expanduser()
        except (OSError, ValueError, TypeError):
            pass
        return cls.get_app_data_dir() / "sdk"
    
    @classmethod
    def get_tools_dir(cls) -> Path:
        """Get the tools directory (scrcpy, etc.)."""
        return cls.get_app_data_dir() / "tools"
    
    @classmethod
    def get_settings_path(cls) -> Path:
        """Get the settings file path."""
        return cls.get_app_data_dir() / "settings.json"
    
    @classmethod
    def get_log_path(cls) -> Path:
        """Get the log file path."""
        return cls.get_app_data_dir() / "apkpreview.log"
    
    @classmethod
    def get_cmdline_tools_url(cls) -> str:
        """Get the download URL for Android command-line tools."""
        if cls.IS_WINDOWS:
            return "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
        elif cls.IS_MACOS:
            return "https://dl.google.com/android/repository/commandlinetools-mac-11076708_latest.zip"
        else:
            return "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
    
    @classmethod
    def get_cmdline_tools_dirname(cls) -> str:
        """Get the extracted directory name for command-line tools."""
        return "cmdline-tools"
    
    @classmethod
    def get_sdkmanager_path(cls) -> Path:
        """Get the path to sdkmanager."""
        sdk = cls.get_sdk_dir()
        if cls.IS_WINDOWS:
            return sdk / cls.get_cmdline_tools_dirname() / "latest" / "bin" / "sdkmanager.bat"
        return sdk / cls.get_cmdline_tools_dirname() / "latest" / "bin" / "sdkmanager"
    
    @classmethod
    def get_avdmanager_path(cls) -> Path:
        """Get the path to avdmanager."""
        sdk = cls.get_sdk_dir()
        if cls.IS_WINDOWS:
            return sdk / cls.get_cmdline_tools_dirname() / "latest" / "bin" / "avdmanager.bat"
        return sdk / cls.get_cmdline_tools_dirname() / "latest" / "bin" / "avdmanager"
    
    @classmethod
    def get_adb_path(cls) -> Path:
        """Get the path to adb."""
        sdk = cls.get_sdk_dir()
        if cls.IS_WINDOWS:
            return sdk / "platform-tools" / "adb.exe"
        return sdk / "platform-tools" / "adb"
    
    @classmethod
    def get_emulator_path(cls) -> Path:
        """Get the path to emulator."""
        sdk = cls.get_sdk_dir()
        if cls.IS_WINDOWS:
            return sdk / "emulator" / "emulator.exe"
        return sdk / "emulator" / "emulator"
    
    @classmethod
    def get_aapt2_path(cls) -> Path:
        """Get the path to aapt2."""
        sdk = cls.get_sdk_dir()
        # build-tools has versioned subdirectories
        build_tools = sdk / "build-tools"
        if build_tools.exists():
            versions = sorted([d for d in build_tools.iterdir() if d.is_dir()], reverse=True)
            if versions:
                if cls.IS_WINDOWS:
                    return versions[0] / "aapt2.exe"
                return versions[0] / "aapt2"
        return Path()
    
    @classmethod
    def get_scrcpy_path(cls) -> Path:
        """Get the path to scrcpy."""
        tools = cls.get_tools_dir()
        if cls.IS_WINDOWS:
            return tools / "scrcpy" / "scrcpy.exe"
        return tools / "scrcpy" / "scrcpy"
    
    @classmethod
    def get_arch(cls) -> str:
        """Get the CPU architecture for image selection."""
        machine = platform.machine().lower()
        if machine in ("arm64", "aarch64"):
            return "arm64"
        elif machine in ("x86_64", "amd64"):
            return "x86_64"
        elif machine.startswith("arm"):
            return "arm"
        return "x86_64"  # default fallback
    
    @classmethod
    def get_image_abi(cls) -> str:
        """Get the system-image ABI for the current CPU (via platform.machine()).

        Apple Silicon hosts run ``arm64-v8a`` images; Intel/AMD hosts run
        ``x86_64``.
        """
        return "arm64-v8a" if cls.get_arch() == "arm64" else "x86_64"

    @classmethod
    def get_system_image(cls, api_level: int) -> str:
        """Get the ``default`` system image string for the current architecture."""
        return f"system-images;android-{api_level};default;{cls.get_image_abi()}"
    
    @classmethod
    def get_cpu_info(cls) -> dict:
        """Get CPU information."""
        import psutil
        return {
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "architecture": cls.get_arch(),
        }
    
    @classmethod
    def get_memory_info(cls) -> dict:
        """Get memory information in MB."""
        import psutil
        mem = psutil.virtual_memory()
        return {
            "total_mb": mem.total // (1024 * 1024),
            "available_mb": mem.available // (1024 * 1024),
            "used_mb": mem.used // (1024 * 1024),
            "percent": mem.percent,
        }
    
    @classmethod
    def get_disk_info(cls, path: Optional[Path] = None) -> dict:
        """Get disk information in GB."""
        import psutil
        if path is None:
            path = cls.get_app_data_dir()
        # Create directory if it doesn't exist (for disk usage check)
        path.mkdir(parents=True, exist_ok=True)
        usage = psutil.disk_usage(str(path))
        return {
            "total_gb": usage.total // (1024**3),
            "free_gb": usage.free // (1024**3),
            "used_gb": usage.used // (1024**3),
        }
    
    @classmethod
    def check_virtualization_windows(cls) -> dict:
        """Check virtualization support on Windows."""
        result = {
            "supported": False,
            "hyper_v": False,
            "whpx": False,
            "aehd": False,
            "aehd_driver": False,
            "firmware_virtualization": False,
            "backend": "none",
            "message": "",
        }
        
        if not cls.IS_WINDOWS:
            result["supported"] = True
            result["message"] = "Virtualization check not needed on this platform"
            return result
        
        try:
            # Check Hyper-V
            out = subprocess.run(
                ["powershell", "-Command", "Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All | Select-Object State"],
                capture_output=True, text=True, timeout=10
            )
            if "Enabled" in out.stdout:
                result["hyper_v"] = True
            
            # Check WHPX
            out = subprocess.run(
                ["powershell", "-Command", "Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform | Select-Object State"],
                capture_output=True, text=True, timeout=10
            )
            if "Enabled" in out.stdout:
                result["whpx"] = True
            
            # Android Emulator Hypervisor Driver (AEHD) service, if installed.
            try:
                aehd = subprocess.run(
                    ["sc", "query", "aehd"], capture_output=True, text=True,
                    timeout=10)
                if aehd.returncode == 0 and "RUNNING" in aehd.stdout.upper():
                    result["aehd_driver"] = True
                    result["aehd"] = True
            except Exception:
                pass

            # Check if virtualization is enabled in BIOS (via systeminfo).
            # This is INFORMATIONAL ONLY: VT-x/SVM in firmware is necessary but
            # not sufficient.  The Android emulator still needs a real Windows
            # hypervisor (WHPX/Hyper-V) or the AEHD driver.  Treating firmware
            # support as a usable accelerator made us pass `-accel on` on hosts
            # with no backend, which makes the emulator panic and exit during
            # boot ("emulator died during boot").
            try:
                out = subprocess.run(
                    ["systeminfo"], capture_output=True, text=True, timeout=10
                )
                if "Virtualization Enabled In Firmware : Yes" in out.stdout:
                    result["firmware_virtualization"] = True
            except Exception:
                pass

            # Only an actual hypervisor/driver can accelerate the emulator.
            result["supported"] = (result["hyper_v"] or result["whpx"]
                                   or result["aehd_driver"])
            if result["whpx"]:
                result["backend"] = "whpx"
            elif result["aehd_driver"]:
                result["backend"] = "aehd"
            elif result["hyper_v"]:
                result["backend"] = "hyperv"
            else:
                result["backend"] = "none"

            if not result["supported"]:
                if result["firmware_virtualization"]:
                    result["message"] = (
                        "Virtualization is enabled in firmware, but no Windows "
                        "hypervisor is active. Enable 'Windows Hypervisor "
                        "Platform' (or Hyper-V) in Windows Features, then restart."
                    )
                else:
                    result["message"] = (
                        "Hardware virtualization is not enabled. Please enable it:\n"
                        "1. Restart your PC and enter BIOS/UEFI (usually F2, F10, Del, or Esc)\n"
                        "2. Enable 'Virtualization Technology', 'VT-x', 'SVM', or 'AMD-V'\n"
                        "3. In Windows, enable 'Windows Hypervisor Platform' or 'Hyper-V':\n"
                        "   - Open 'Turn Windows features on or off'\n"
                        "   - Check 'Windows Hypervisor Platform' (recommended)\n"
                        "   - Or check 'Hyper-V' (requires Windows Pro/Enterprise)\n"
                        "4. Restart your computer"
                    )
            else:
                result["message"] = "Virtualization is available"
                
        except Exception as e:
            result["message"] = f"Could not check virtualization: {e}"
        
        return result
    

    @classmethod
    def get_hypervisor_guidance(cls) -> str:
        """Return user-facing guidance about hypervisor availability."""
        info = cls.check_virtualization()
        if info["supported"]:
            return info["message"]
        steps = []
        if not info["hyper_v"] and not info["whpx"]:
            steps.append("Enable Hyper-V or WHPX via Windows Features (Control Panel > Programs > Turn Windows features on or off)")
        if not info["aehd_driver"]:
            steps.append("Install the Android Emulator Hypervisor Driver (AEHD) if available for your hardware")
        if not info["firmware_virtualization"]:
            steps.append("Ensure virtualization is enabled in BIOS/UEFI settings")
        if steps:
            return "Hardware virtualization not detected.\n" + "\n".join(steps)
        return info["message"]

    @classmethod
    def check_virtualization_macos(cls) -> dict:
        """Check virtualization support on macOS (Hypervisor.framework/HVF)."""
        supported = True
        try:
            out = subprocess.run(
                ["sysctl", "-n", "kern.hv_support"], capture_output=True,
                text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip() == "0":
                supported = False
        except Exception:
            pass
        return {
            "supported": supported,
            "backend": "hvf" if supported else "none",
            "hvf": supported,
            "message": ("Hypervisor.framework (HVF) is available"
                        if supported else
                        "Hypervisor.framework (HVF) is not available"),
        }

    @classmethod
    def get_acceleration_info(cls) -> dict:
        """Return the host accelerator the Android emulator should use.

        ``backend`` is one of ``hvf`` (macOS), ``whpx`` / ``aehd`` /
        ``hyperv`` (Windows), ``kvm`` (Linux), or ``none``.  The Android
        emulator auto-detects the backend; this value is used for the setup
        diagnostics and to add an explicit ``-accel on`` flag when supported.
        """
        if cls.IS_MACOS:
            info = cls.check_virtualization_macos()
            return {
                "backend": info.get("backend", "hvf"),
                "supported": bool(info.get("supported")),
                "details": info,
            }
        if cls.IS_WINDOWS:
            info = cls.check_virtualization_windows()
            return {
                "backend": info.get("backend", "none"),
                "supported": bool(info.get("supported")),
                "details": info,
            }
        kvm = Path("/dev/kvm")
        supported = kvm.exists()
        return {
            "backend": "kvm" if supported else "none",
            "supported": supported,
            "details": {
                "supported": supported,
                "backend": "kvm" if supported else "none",
                "message": ("KVM is available" if supported
                            else "KVM is not available"),
            },
        }
    
    @classmethod
    def check_virtualization(cls) -> dict:
        """Check virtualization support for current platform."""
        if cls.IS_WINDOWS:
            return cls.check_virtualization_windows()
        elif cls.IS_MACOS:
            return cls.check_virtualization_macos()
        else:
            info = cls.get_acceleration_info()
            return {
                "supported": info["supported"],
                "backend": info["backend"],
                "message": info["details"].get("message", ""),
            }
    
    @classmethod
    def run_command(cls, cmd: List[str], cwd: Optional[Path] = None, 
                    env: Optional[dict] = None, timeout: int = 300) -> subprocess.CompletedProcess:
        """Run a command with proper platform handling."""
        if cls.IS_WINDOWS and cmd[0].endswith(".bat"):
            # On Windows, run batch files through cmd
            full_cmd = ["cmd", "/c"] + cmd
        else:
            full_cmd = cmd
        
        # Ensure PATH includes platform-tools
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        
        sdk_platform_tools = str(cls.get_sdk_dir() / "platform-tools")
        if sdk_platform_tools not in merged_env.get("PATH", ""):
            merged_env["PATH"] = sdk_platform_tools + os.pathsep + merged_env.get("PATH", "")
        
        return subprocess.run(
            full_cmd,
            cwd=str(cwd) if cwd else None,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout
        )
    
    @classmethod
    def start_detached_process(cls, cmd: List[str], cwd: Optional[Path] = None,
                               env: Optional[dict] = None,
                               log_file: Optional[Path] = None
                               ) -> subprocess.Popen:
        """Start a detached process (for emulator, scrcpy).

        When ``log_file`` is given the process output is written there, which
        is how the emulator's own log is captured so the acceleration and GPU
        mode can be verified afterwards.
        """
        if cls.IS_WINDOWS and cmd[0].endswith(".bat"):
            full_cmd = ["cmd", "/c"] + cmd
        else:
            full_cmd = cmd
        
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        
        sdk_platform_tools = str(cls.get_sdk_dir() / "platform-tools")
        if sdk_platform_tools not in merged_env.get("PATH", ""):
            merged_env["PATH"] = sdk_platform_tools + os.pathsep + merged_env.get("PATH", "")
        
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL
        handle = None
        if log_file is not None:
            try:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                handle = open(log_file, "wb")
                stdout = handle
                stderr = subprocess.STDOUT
            except OSError:
                handle = None

        try:
            if cls.IS_WINDOWS:
                return subprocess.Popen(
                    full_cmd,
                    cwd=str(cwd) if cwd else None,
                    env=merged_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS,
                    stdout=stdout,
                    stderr=stderr,
                )
            return subprocess.Popen(
                full_cmd,
                cwd=str(cwd) if cwd else None,
                env=merged_env,
                start_new_session=True,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            # The child keeps its own descriptor; the parent's copy can go.
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
    
    @classmethod
    def kill_process_tree(cls, pid: int):
        """Kill a process and its children."""
        import psutil
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            # Wait a bit then force kill
            import time
            time.sleep(0.5)
            for child in children:
                if child.is_running():
                    child.kill()
            if parent.is_running():
                parent.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception:
            pass

    @classmethod
    def cleanup_child_processes(cls):
        """Kill all known child processes (sdkmanager, java, emulator, scrcpy, adb)."""
        import psutil
        try:
            current = psutil.Process(os.getpid())
            children = current.children(recursive=True)
            # Kill specific process types
            for child in children:
                try:
                    name = child.name().lower()
                    if any(x in name for x in ['sdkmanager', 'java', 'emulator', 'scrcpy', 'adb', 'qemu']):
                        child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            # Wait a bit then force kill
            import time
            time.sleep(0.5)
            for child in children:
                try:
                    if child.is_running():
                        name = child.name().lower()
                        if any(x in name for x in ['sdkmanager', 'java', 'emulator', 'scrcpy', 'adb', 'qemu']):
                            child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass

    @classmethod
    def kill_adb_server(cls):
        """Kill the adb server."""
        adb = cls.get_adb_path()
        if adb.exists():
            cls.run_command([str(adb), "kill-server"], timeout=5)
    
    @classmethod
    def get_os_version(cls) -> str:
        """Get a human-readable OS version."""
        if cls.IS_WINDOWS:
            return f"Windows {platform.release()} {platform.version()}"
        elif cls.IS_MACOS:
            return f"macOS {platform.mac_ver()[0]}"
        else:
            return f"Linux {platform.release()}"
    
    @classmethod
    def is_admin(cls) -> bool:
        """Check if running with admin/root privileges."""
        if cls.IS_WINDOWS:
            try:
                import ctypes
                return ctypes.windll.shell32.IsUserAnAdmin()
            except Exception:
                return False
        else:
            return os.geteuid() == 0
    
    @classmethod
    def open_folder(cls, path: Path):
        """Open a folder in the system file manager."""
        if cls.IS_WINDOWS:
            os.startfile(str(path))
        elif cls.IS_MACOS:
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])
    
    @classmethod
    def get_executable_extension(cls) -> str:
        """Get the executable extension for current platform."""
        return ".exe" if cls.IS_WINDOWS else ""
    
    @classmethod
    def normalize_path(cls, path: str) -> Path:
        """Normalize a path string to a Path object."""
        return Path(path).expanduser().resolve()