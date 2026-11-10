"""Android SDK Manager for ApkPreview - handles cmdline-tools, platform-tools, emulator, system images."""

import os
import re
import shutil
import subprocess
import zipfile
import tarfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, List, Tuple
import requests
from .platform_utils import PlatformUtils
from .hardware_profile import PerformanceMode, EmulatorConfig
from .settings import settings


# System-image variants sdkmanager publishes for each API level.
SYSTEM_IMAGE_VARIANTS = ("default", "google_apis", "google_apis_playstore")

# Human-readable Android version names per API level (best-effort table with a
# graceful fallback for levels we do not know about).
ANDROID_VERSION_NAMES = {
    19: "Android 4.4 (KitKat)",
    20: "Android 4.4W (KitKat Wear)",
    21: "Android 5.0 (Lollipop)",
    22: "Android 5.1 (Lollipop)",
    23: "Android 6.0 (Marshmallow)",
    24: "Android 7.0 (Nougat)",
    25: "Android 7.1 (Nougat)",
    26: "Android 8.0 (Oreo)",
    27: "Android 8.1 (Oreo)",
    28: "Android 9 (Pie)",
    29: "Android 10",
    30: "Android 11",
    31: "Android 12",
    32: "Android 12L",
    33: "Android 13",
    34: "Android 14",
    35: "Android 15",
    36: "Android 16",
}

# Rough download sizes (GB) used only when sdkmanager does not report a size.
VARIANT_SIZE_ESTIMATE_GB = {
    "default": 1.2,
    "google_apis": 1.4,
    "google_apis_playstore": 1.6,
}

# Extra free space we insist on staying above when downloading an image.
DISK_HEADROOM_GB = 5.0

# AVDs this app creates start with this prefix.  Nothing else may ever be
# deleted by the version-replacement flow, so user-made AVDs survive.
APP_AVD_PREFIX = "apkpreview_"

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMG]i?B)$", re.IGNORECASE)
_SIZE_UNITS = {
    "KB": 1024, "KIB": 1024,
    "MB": 1024 ** 2, "MIB": 1024 ** 2,
    "GB": 1024 ** 3, "GIB": 1024 ** 3,
}


@dataclass(frozen=True)
class AndroidVersion:
    """A selectable Android system image (one API level + variant + ABI)."""

    api_level: int
    variant: str
    abi: str
    installed: bool = False
    size_bytes: Optional[int] = None
    size_text: Optional[str] = None

    @property
    def package(self) -> str:
        """The sdkmanager package id, e.g. ``system-images;android-34;default;x86_64``."""
        return f"system-images;android-{self.api_level};{self.variant};{self.abi}"

    @property
    def version_name(self) -> str:
        return ANDROID_VERSION_NAMES.get(self.api_level, f"Android {self.api_level}")

    @property
    def avd_name(self) -> str:
        """Per-version AVD name: ``apkpreview_api<level>_<variant>``."""
        return f"apkpreview_api{self.api_level}_{self.variant}"

    @property
    def variant_display(self) -> str:
        return self.variant.replace("_", " ")

    def estimate_size_gb(self) -> float:
        """Download size in GB (reported if present, else a rough estimate)."""
        if self.size_bytes:
            return self.size_bytes / (1024 ** 3)
        return VARIANT_SIZE_ESTIMATE_GB.get(self.variant, 1.4)

    def size_display(self) -> str:
        """Text for the Size column: reported size, else ``--``."""
        return self.size_text or "--"


def _parse_size_token(token: str) -> Optional[int]:
    """Parse a size token like ``1.2 GB`` into bytes, or None."""
    m = _SIZE_RE.match(token.strip())
    if not m:
        return None
    try:
        value = float(m.group(1))
    except ValueError:
        return None
    unit = _SIZE_UNITS.get(m.group(2).upper())
    if unit is None:
        return None
    return int(value * unit)


def format_bytes(num: Optional[int]) -> str:
    """Human-readable size from a byte count."""
    if not num:
        return "--"
    if num >= 1024 ** 3:
        return f"{num / (1024 ** 3):.1f} GB"
    if num >= 1024 ** 2:
        return f"{num / (1024 ** 2):.0f} MB"
    return f"{num // 1024} KB"


def parse_progress_percent(line: str) -> Optional[int]:
    """Extract a 0-100 percent value from an sdkmanager output line, or None."""
    m = re.search(r"(\d{1,3})\s*%", line)
    if not m:
        return None
    try:
        value = int(m.group(1))
    except ValueError:
        return None
    return max(0, min(100, value))


class SDKManager:
    """Manages Android SDK installation and components."""

    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
        self._sdk_dir = PlatformUtils.get_sdk_dir()
        self._tools_dir = PlatformUtils.get_tools_dir()

    def _report_progress(self, step: str, current: int, total: int, message: str = ""):
        """Report progress to callback."""
        if self.progress_callback:
            # Clamp progress to 100
            current = min(current, total)
            self.progress_callback(step, current, total, message)

    def is_sdk_installed(self) -> bool:
        """Check if SDK is already installed."""
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        return sdkmanager.exists()

    def _avd_dirs(self) -> List[Path]:
        """Both AVD locations we look in, newest first."""
        return [
            PlatformUtils.get_app_data_dir() / "avd",
            Path.home() / ".android" / "avd",
        ]

    def _find_avd_dir(self, name: str) -> Optional[Path]:
        """Return the ``<name>.avd`` directory if it exists in either location."""
        for base in self._avd_dirs():
            avd_dir = base / f"{name}.avd"
            if avd_dir.exists():
                return avd_dir
        return None

    def is_avd_created(self, name: str) -> bool:
        """Check if an AVD already exists."""
        return self._find_avd_dir(name) is not None

    def get_avd_version(self, name: str) -> Optional[AndroidVersion]:
        """Read an AVD's config.ini and return the Android version it uses."""
        avd_dir = self._find_avd_dir(name)
        if avd_dir is None:
            return None
        config_file = avd_dir / "config.ini"
        if not config_file.exists():
            return None
        try:
            with open(config_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip().startswith("image.sysdir.1"):
                        value = line.split("=", 1)[1].strip().replace("\\", "/")
                        parts = [p for p in value.split("/") if p]
                        # .../system-images/android-34/default/x86_64/
                        try:
                            idx = parts.index("system-images")
                        except ValueError:
                            return None
                        if len(parts) < idx + 4:
                            return None
                        api = parts[idx + 1]
                        variant = parts[idx + 2]
                        abi = parts[idx + 3]
                        if not api.startswith("android-"):
                            return None
                        return AndroidVersion(
                            api_level=int(api.split("-", 1)[1]),
                            variant=variant,
                            abi=abi,
                            installed=True,
                        )
        except Exception:
            return None
        return None

    def list_installed_avds(self) -> dict:
        """Map AVD name -> AndroidVersion for every AVD we can find."""
        found = {}
        for base in self._avd_dirs():
            if not base.exists():
                continue
            try:
                entries = list(base.glob("*.avd"))
            except OSError:
                continue
            for avd_dir in entries:
                name = avd_dir.stem
                if name in found:
                    continue
                version = self.get_avd_version(name)
                if version is not None:
                    found[name] = version
        return found

    def delete_avd(self, name: str) -> Tuple[bool, str]:
        """Delete an AVD directory and its .ini pointer.

        Returns ``False`` if either the directory or the pointer is still
        present afterwards, so callers can report a real cleanup failure
        instead of silently claiming success.
        """
        avd_dir = self._find_avd_dir(name)
        if avd_dir is None:
            return True, f"AVD '{name}' not found"
        ini = avd_dir.with_suffix(".ini")
        errors: List[str] = []
        removed_dir = False
        try:
            shutil.rmtree(avd_dir)
            removed_dir = True
        except OSError as exc:
            errors.append(f"folder: {exc}")
        if removed_dir:
            try:
                if ini.exists():
                    ini.unlink()
            except OSError as exc:
                errors.append(f"pointer: {exc}")
        if avd_dir.exists() or ini.exists():
            detail = "; ".join(errors) or "files are still present"
            return False, f"Failed to delete AVD '{name}': {detail}"
        return True, f"AVD '{name}' deleted"

    def find_legacy_avd(self) -> Optional[Tuple[str, AndroidVersion]]:
        """The pre-existing ``apkpreview_default`` AVD, if still present."""
        version = self.get_avd_version("apkpreview_default")
        if version is None:
            return None
        return "apkpreview_default", version

    def list_avds_for_version(self, version: AndroidVersion) -> List[str]:
        """Every AVD that serves ``version`` (its own name plus the legacy one).

        Names are returned as-is; callers decide whether they are safe to
        delete (only :data:`APP_AVD_PREFIX` ones ever are).
        """
        names: List[str] = []
        if self.is_avd_created(version.avd_name):
            names.append(version.avd_name)
        legacy = self.find_legacy_avd()
        if legacy is not None:
            legacy_name, legacy_version = legacy
            if (legacy_version.api_level, legacy_version.variant) == (
                    version.api_level, version.variant):
                if legacy_name not in names:
                    names.append(legacy_name)
        return names

    def list_installed_versions(self) -> List[AndroidVersion]:
        """All installed versions: on-disk images plus the legacy AVD's version."""
        abi = PlatformUtils.get_image_abi()
        versions = list(self._scan_installed_images(abi))
        seen = {(v.api_level, v.variant) for v in versions}
        legacy = self.find_legacy_avd()
        if legacy is not None:
            _, legacy_version = legacy
            key = (legacy_version.api_level, legacy_version.variant)
            if key not in seen:
                versions.append(replace(legacy_version, abi=abi))
                seen.add(key)
        versions.sort(key=lambda v: (v.api_level, v.variant))
        return versions

    # ------------------------------------------------------------------
    # Android versions (system images)
    # ------------------------------------------------------------------
    @staticmethod
    def parse_sdkmanager_list(output: str, abi: str) -> List[AndroidVersion]:
        """Parse ``sdkmanager --list`` output into AndroidVersion records.

        Only system images for ``abi`` and the three supported variants are
        kept.  A package appearing under ``Installed packages:`` is marked
        installed; a size column is picked up when sdkmanager emits one.
        """
        if not output:
            return []

        rows = {}
        order = []
        installed_pkgs = set()
        section = None

        for raw in output.splitlines():
            stripped = raw.strip()
            if not stripped:
                continue
            low = stripped.lower()
            if low.startswith("installed packages"):
                section = "installed"
                continue
            if low.startswith("available packages"):
                section = "available"
                continue
            if low.startswith("available updates"):
                section = "updates"
                continue
            if "|" not in stripped:
                continue

            parts = [p.strip() for p in stripped.split("|")]
            pkg = parts[0]
            if not pkg.startswith("system-images;"):
                continue

            seg = pkg.split(";")
            if len(seg) < 4:
                continue
            api_seg, variant, abi_seg = seg[1], seg[2], seg[3]
            if not api_seg.startswith("android-"):
                continue
            try:
                level = int(api_seg.split("-", 1)[1])
            except ValueError:
                continue
            if abi_seg != abi or variant not in SYSTEM_IMAGE_VARIANTS:
                continue

            size_bytes = None
            size_text = None
            for part in parts[1:]:
                parsed = _parse_size_token(part)
                if parsed is not None:
                    size_bytes = parsed
                    size_text = part
                    break

            if section == "installed":
                installed_pkgs.add(pkg)

            if pkg not in rows:
                rows[pkg] = {
                    "api": level, "variant": variant, "abi": abi_seg,
                    "size_bytes": size_bytes, "size_text": size_text,
                }
                order.append(pkg)
            elif rows[pkg]["size_bytes"] is None and size_bytes is not None:
                rows[pkg]["size_bytes"] = size_bytes
                rows[pkg]["size_text"] = size_text

        return [
            AndroidVersion(
                api_level=rows[pkg]["api"],
                variant=rows[pkg]["variant"],
                abi=rows[pkg]["abi"],
                installed=pkg in installed_pkgs,
                size_bytes=rows[pkg]["size_bytes"],
                size_text=rows[pkg]["size_text"],
            )
            for pkg in order
        ]

    def _run_sdkmanager_list(self, cancel=None) -> str:
        """Run ``sdkmanager --list`` and return its stdout.

        ``cancel`` is an optional ``threading.Event``; when set, the underlying
        process is terminated so a dialog can close without leaving a live
        thread behind.  All failures degrade to an empty string.
        """
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return ""
        try:
            proc = subprocess.Popen(
                [str(sdkmanager), "--list"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self._sdkmanager_env(),
            )
        except Exception:
            return ""

        lines: List[str] = []
        try:
            if proc.stdout is not None:
                while True:
                    if cancel is not None and cancel.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                        return ""
                    line = proc.stdout.readline()
                    if not line:
                        break
                    lines.append(line)
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        return "".join(lines)

    def is_system_image_installed(self, abi: str, level: int, variant: str) -> bool:
        """True if the system-image directory exists and has content."""
        image_dir = (self._sdk_dir / "system-images"
                     / f"android-{level}" / variant / abi)
        try:
            return image_dir.is_dir() and any(image_dir.iterdir())
        except OSError:
            return False

    def _scan_installed_images(self, abi: str) -> List[AndroidVersion]:
        """Discover installed system images straight from the SDK folder."""
        found: List[AndroidVersion] = []
        base = self._sdk_dir / "system-images"
        if not base.is_dir():
            return found
        for api_dir in base.iterdir():
            if not api_dir.is_dir() or not api_dir.name.startswith("android-"):
                continue
            try:
                level = int(api_dir.name.split("-", 1)[1])
            except ValueError:
                continue
            for var_dir in api_dir.iterdir():
                if not var_dir.is_dir() or var_dir.name not in SYSTEM_IMAGE_VARIANTS:
                    continue
                abi_dir = var_dir / abi
                try:
                    if abi_dir.is_dir() and any(abi_dir.iterdir()):
                        found.append(AndroidVersion(
                            api_level=level, variant=var_dir.name,
                            abi=abi, installed=True))
                except OSError:
                    continue
        return found

    def list_system_images(self, cancel=None) -> List[AndroidVersion]:
        """List selectable Android versions for the current CPU architecture.

        Combines parsed ``sdkmanager --list`` output with the actual files on
        disk and any legacy ``apkpreview_default`` AVD, so an existing install
        keeps working even if it is not in the available list.
        """
        abi = PlatformUtils.get_image_abi()
        versions = self.parse_sdkmanager_list(
            self._run_sdkmanager_list(cancel), abi)

        merged: List[AndroidVersion] = []
        seen = set()
        for version in versions:
            seen.add((version.api_level, version.variant))
            if not version.installed and self.is_system_image_installed(
                    abi, version.api_level, version.variant):
                version = replace(version, installed=True)
            merged.append(version)

        for version in self._scan_installed_images(abi):
            key = (version.api_level, version.variant)
            if key not in seen:
                merged.append(version)
                seen.add(key)

        legacy = self.find_legacy_avd()
        if legacy is not None:
            _, legacy_version = legacy
            key = (legacy_version.api_level, legacy_version.variant)
            if key not in seen:
                merged.append(replace(legacy_version, abi=abi))
                seen.add(key)

        merged.sort(key=lambda v: (
            -v.api_level,
            SYSTEM_IMAGE_VARIANTS.index(v.variant)
            if v.variant in SYSTEM_IMAGE_VARIANTS else 99,
        ))
        return merged

    def find_avd_for_version(self, version: AndroidVersion) -> Optional[str]:
        """The AVD name that already serves ``version``, if any.

        A pre-existing ``apkpreview_default`` AVD is treated as the AVD for the
        version it was created from (normally API 34 / default).
        """
        legacy = self.find_legacy_avd()
        if legacy is not None:
            legacy_name, legacy_version = legacy
            if (legacy_version.api_level == version.api_level
                    and legacy_version.variant == version.variant):
                return legacy_name
        if self.is_avd_created(version.avd_name):
            return version.avd_name
        return None

    def ensure_avd_for(self, version: AndroidVersion,
                       config: Optional[EmulatorConfig] = None,
                       data_partition_gb: float = 3.0) -> Tuple[bool, str, Optional[str]]:
        """Return (ok, message, avd_name), creating the AVD on first use.

        Each installed version gets ``apkpreview_api<level>_<variant>`` with a
        small (3 GB) data partition.  An existing legacy AVD is reused.
        """
        existing = self.find_avd_for_version(version)
        if existing is not None:
            return True, f"Using existing AVD '{existing}'", existing

        name = version.avd_name
        ok, message = self.create_avd(
            name, version.package, config=config,
            data_partition_gb=data_partition_gb)
        return ok, message, (name if ok else None)

    def install_version(self, version: AndroidVersion,
                        config: Optional[EmulatorConfig] = None,
                        data_partition_gb: float = 3.0,
                        old_version: Optional[AndroidVersion] = None,
                        on_line: Optional[Callable[[str], None]] = None,
                        cancel=None) -> Tuple[bool, str, Optional[str]]:
        """Make ``version`` usable (image + AVD) and optionally remove ``old_version``.

        Returns ``(ok, message, avd_name)``.  This is the replace-on-update
        primitive: the old version is only removed *after* the new image is
        installed and its AVD exists, so a failed or cancelled download never
        deletes anything.
        """
        if not self.is_system_image_installed(
                version.abi, version.api_level, version.variant):
            ok, message = self.install_system_image(
                version, on_line=on_line, cancel=cancel)
            if not ok:
                return False, message, None

        ok, message, avd_name = self.ensure_avd_for(
            version, config=config, data_partition_gb=data_partition_gb)
        if not ok or not avd_name:
            return False, message, None

        if old_version is not None:
            removed, message = self.remove_version(old_version)
            if not removed:
                # The new version is fully usable; report the cleanup problem
                # as a warning instead of pretending the install failed.
                return True, (f"Installed {version.version_name}; "
                              f"could not fully remove the old version: "
                              f"{message}"), avd_name

        return True, f"Installed {version.version_name}", avd_name

    def remove_version(self, version: AndroidVersion,
                       remove_image: bool = True) -> Tuple[bool, str]:
        """Delete a version's app-managed AVD(s) and (optionally) its image.

        Only AVDs named :data:`APP_AVD_PREFIX` are ever deleted, so any AVD the
        user created themselves is left completely untouched.  Returns
        ``False`` if any requested deletion failed; the message lists every
        action that was attempted.
        """
        messages: List[str] = []
        all_ok = True

        for name in self.list_avds_for_version(version):
            if not name.startswith(APP_AVD_PREFIX):
                continue
            ok, message = self.delete_avd(name)
            all_ok = all_ok and ok
            messages.append(message)

        if remove_image:
            ok, message = self.uninstall_system_image(version)
            all_ok = all_ok and ok
            messages.append(message)

        return all_ok, "; ".join(messages)

    def _jdk_home(self) -> Optional[Path]:
        """Locate the private JDK installed by the setup wizard, if present."""
        base = PlatformUtils.get_app_data_dir() / "jdk"
        if not base.is_dir():
            return None
        try:
            for name in ("java", "java.exe"):
                for java in base.rglob(f"bin/{name}"):
                    if java.is_file():
                        return java.parent.parent
        except OSError:
            return None
        return None

    def _sdkmanager_env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = (str(self._sdk_dir / "platform-tools") + os.pathsep
                       + env.get("PATH", ""))
        # A packaged app may have no system Java.  The wizard downloads a
        # private JDK under ~/.apkpreview/jdk; sdkmanager/avdmanager must use
        # it explicitly because their launcher scripts do not search there.
        jdk_home = self._jdk_home()
        if jdk_home is not None:
            env["JAVA_HOME"] = str(jdk_home)
            env["PATH"] = str(jdk_home / "bin") + os.pathsep + env["PATH"]
        return env

    def install_system_image(self, version: AndroidVersion,
                             on_line: Optional[Callable[[str], None]] = None,
                             cancel=None) -> Tuple[bool, str]:
        """Download and install one system image, streaming sdkmanager output.

        ``on_line`` receives each output line; ``cancel`` is an optional
        ``threading.Event`` that terminates the download when set.
        """
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return False, "sdkmanager not found"

        # Best-effort: pre-accept licenses so the download cannot block waiting
        # for interactive input on a fresh install.
        try:
            self.accept_licenses()
        except Exception:
            pass

        cmd = [str(sdkmanager), version.package]
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=self._sdkmanager_env(),
            )
        except Exception as e:
            return False, f"Failed to start sdkmanager: {e}"

        last_line = ""
        buffer = []

        def flush_line():
            nonlocal last_line
            line = "".join(buffer).strip()
            buffer.clear()
            if line:
                last_line = line
                if on_line is not None:
                    on_line(line)

        def stop():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()

        try:
            if proc.stdout is not None:
                # sdkmanager reports progress with '\r' (not '\n'), so split on
                # both so the progress callback fires throughout the download.
                while True:
                    if cancel is not None and cancel.is_set():
                        flush_line()
                        stop()
                        return False, "Download cancelled"
                    chunk = proc.stdout.read(1)
                    if not chunk:
                        break
                    if chunk in ("\r", "\n"):
                        flush_line()
                    else:
                        buffer.append(chunk)
                flush_line()
        finally:
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:
                pass

        returncode = proc.wait()
        if returncode == 0:
            return True, f"Installed {version.package}"
        return False, f"sdkmanager exited with code {returncode}: {last_line}"

    def uninstall_system_image(self, version: AndroidVersion) -> Tuple[bool, str]:
        """Remove an installed system image via sdkmanager."""
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return False, "sdkmanager not found"
        try:
            result = PlatformUtils.run_command(
                [str(sdkmanager), "--uninstall", version.package],
                env=self._sdkmanager_env(), timeout=300)
            if result.returncode == 0:
                if self.is_system_image_installed(
                        version.abi, version.api_level, version.variant):
                    return False, (f"sdkmanager reported success but "
                                   f"{version.package} is still present")
                return True, f"Removed {version.package}"
            stderr_lines = (result.stderr or "").strip().split("\n")[-20:]
            return False, "Uninstall failed:\n" + "\n".join(stderr_lines)
        except Exception as e:
            return False, f"Uninstall failed: {e}"

    def is_scrcpy_installed(self) -> bool:
        """Check if scrcpy is already installed."""
        # Check our local tools directory
        if PlatformUtils.get_scrcpy_path().exists():
            return True
        
        # On macOS, check system PATH and Homebrew locations
        if PlatformUtils.IS_MACOS:
            import shutil
            # Check shutil.which
            if shutil.which("scrcpy"):
                return True
            # Check common Homebrew locations
            for prefix in ("/opt/homebrew", "/usr/local"):
                bin_path = Path(prefix) / "bin" / "scrcpy"
                if bin_path.exists():
                    return True
        
        return False

    def are_licenses_accepted(self) -> bool:
        """Check if SDK licenses are already accepted."""
        license_file = self._sdk_dir / "licenses" / "android-sdk-license"
        return license_file.exists()

    def get_installed_packages(self) -> List[str]:
        """Get list of installed SDK packages."""
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return []

        try:
            result = PlatformUtils.run_command([str(sdkmanager), "--list_installed"], timeout=60)
            if result.returncode == 0:
                lines = result.stdout.strip().split("\n")
                packages = []
                for line in lines:
                    if "|" in line:
                        pkg = line.split("|")[0].strip()
                        if pkg:
                            packages.append(pkg)
                return packages
        except Exception:
            pass
        return []

    def _make_executable(self, path: Path):
        """Make a file executable (Unix only)."""
        if not PlatformUtils.IS_WINDOWS and path.exists():
            path.chmod(0o755)

    def _make_bin_executables(self, bin_dir: Path):
        """Make all files in a bin directory executable (Unix only)."""
        if not PlatformUtils.IS_WINDOWS and bin_dir.exists():
            for item in bin_dir.iterdir():
                if item.is_file():
                    item.chmod(0o755)

    def _extract_zip(self, zip_path: Path, extract_to: Path) -> Tuple[bool, str]:
        """Extract zip file and make binaries executable."""
        try:
            extract_to.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_to)
            # Make binaries executable
            if not PlatformUtils.IS_WINDOWS:
                bin_dir = extract_to / "bin"
                self._make_bin_executables(bin_dir)
                # Also check for nested cmdline-tools
                for sub in extract_to.rglob("bin"):
                    self._make_bin_executables(sub)
            return True, "Extracted successfully"
        except Exception as e:
            return False, f"Extract failed: {e}"

    def _extract_tar_gz(self, tar_path: Path, extract_to: Path) -> Tuple[bool, str]:
        """Extract tar.gz file and make binaries executable."""
        try:
            extract_to.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path, "r:gz") as tf:
                tf.extractall(extract_to)
            # Make binaries executable
            if not PlatformUtils.IS_WINDOWS:
                for sub in extract_to.rglob("*"):
                    if sub.is_file() and not sub.suffix:
                        # Heuristic: files without extension might be binaries
                        sub.chmod(0o755)
            return True, "Extracted successfully"
        except Exception as e:
            return False, f"Extract failed: {e}"

    def _download_file(self, url: str, dest: Path, step: str) -> Tuple[bool, str]:
        """Download a file with progress reporting."""
        try:
            self._report_progress(step, 0, 100, "Downloading...")
            dest.parent.mkdir(parents=True, exist_ok=True)

            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()

            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0

            with open(dest, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            progress = min(100, int(downloaded * 100 / total_size))
                            self._report_progress(step, progress, 100,
                                                f"Downloading... {downloaded//1024//1024} MB / {total_size//1024//1024} MB")
            return True, f"Downloaded {dest.name}"
        except Exception as e:
            if dest.exists():
                dest.unlink()
            return False, f"Download failed: {e}"

    def download_cmdline_tools(self) -> Tuple[bool, str]:
        """Download Android command-line tools."""
        # Check if already installed
        if self.is_sdk_installed():
            return True, "Command-line tools already present"

        url = PlatformUtils.get_cmdline_tools_url()
        zip_path = self._sdk_dir / "cmdline-tools.zip"

        success, msg = self._download_file(url, zip_path, "download_cmdline")
        if not success:
            return False, msg

        self._report_progress("download_cmdline", 100, 100, "Extracting...")

        # Extract to cmdline-tools/latest
        cmdline_dir = self._sdk_dir / PlatformUtils.get_cmdline_tools_dirname() / "latest"
        success, msg = self._extract_zip(zip_path, cmdline_dir)
        zip_path.unlink(missing_ok=True)

        if not success:
            return False, msg

        # The zip extracts to a 'cmdline-tools' subdirectory, move contents up
        extracted = cmdline_dir / "cmdline-tools"
        if extracted.exists():
            for item in extracted.iterdir():
                shutil.move(str(item), str(cmdline_dir / item.name))
            shutil.rmtree(extracted)

        # Make binaries executable
        self._make_bin_executables(cmdline_dir / "bin")

        self._report_progress("download_cmdline", 100, 100, "Command-line tools installed")
        return True, "Command-line tools installed successfully"

    def accept_licenses(self) -> Tuple[bool, str]:
        """Accept all SDK licenses."""
        # Skip if already accepted
        if self.are_licenses_accepted():
            return True, "Licenses already accepted"

        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return False, "sdkmanager not found"

        try:
            self._report_progress("accept_licenses", 0, 100, "Accepting SDK licenses...")

            # Use subprocess.run with input instead of 'yes' command
            yes_input = "y\n" * 50
            result = subprocess.run(
                [str(sdkmanager), "--licenses"],
                input=yes_input,
                capture_output=True,
                text=True,
                timeout=120,
                env=self._sdkmanager_env()
            )

            if result.returncode == 0:
                self._report_progress("accept_licenses", 100, 100, "Licenses accepted")
                return True, "Licenses accepted"
            else:
                # Log last 50 lines of stderr
                stderr_lines = result.stderr.strip().split("\n")[-50:]
                error_msg = "\n".join(stderr_lines)
                return False, f"License acceptance failed:\n{error_msg}"
        except subprocess.TimeoutExpired:
            return False, "License acceptance timed out after 120 seconds"
        except Exception as e:
            return False, f"Failed to accept licenses: {e}"

    def install_packages(self, packages: List[str]) -> Tuple[bool, str]:
        """Install SDK packages via sdkmanager."""
        sdkmanager = PlatformUtils.get_sdkmanager_path()
        if not sdkmanager.exists():
            return False, "sdkmanager not found"

        if not packages:
            return True, "No packages to install"

        try:
            batch_size = 10
            for i in range(0, len(packages), batch_size):
                batch = packages[i:i+batch_size]
                self._report_progress("install_packages",
                                    min(100, int(i * 100 / max(1, len(packages)))), 100,
                                    f"Installing {batch[0]}...")

                cmd = [str(sdkmanager)] + batch
                result = PlatformUtils.run_command(cmd, timeout=300)

                if result.returncode != 0:
                    stderr_lines = result.stderr.strip().split("\n")[-50:]
                    return False, f"Failed to install {batch}:\n" + "\n".join(stderr_lines)

            self._report_progress("install_packages", 100, 100, "Packages installed")
            return True, "All packages installed"

        except Exception as e:
            return False, f"Package installation failed: {e}"

    def get_required_packages(self, mode: PerformanceMode = None) -> List[str]:
        """Get the base SDK packages the first-run wizard installs.

        Only ``platform-tools`` and ``emulator`` are installed up front.  The
        system image (and therefore the Android version) is chosen by the user
        afterwards, so no Android version is ever auto-downloaded.
        """
        return [
            "platform-tools",
            "emulator",
        ]

    def check_and_install_missing(self, mode: PerformanceMode) -> Tuple[bool, str]:
        """Check installed packages and install missing ones."""
        installed = self.get_installed_packages()
        required = self.get_required_packages(mode)

        missing = [pkg for pkg in required if pkg not in installed]

        if not missing:
            return True, "All required packages already installed"

        self._report_progress("install_missing", 0, 100,
                            f"Installing {len(missing)} missing packages...")

        return self.install_packages(missing)

    def create_avd(self, name: str, system_image: str,
                   config: Optional[EmulatorConfig] = None,
                   force: bool = False,
                   data_partition_gb: float = 3.0) -> Tuple[bool, str]:
        """Create an AVD for an explicit ``system_image`` package.

        The caller (the Android-version selection flow) passes the package id
        of the chosen version; nothing here hard-codes an Android version.
        """
        avdmanager = PlatformUtils.get_avdmanager_path()
        if not avdmanager.exists():
            return False, "avdmanager not found"
        if not system_image:
            return False, "No system image specified"

        # Check if AVD already exists - check both .android/avd and ~/.apkpreview/avd
        if self.is_avd_created(name) and not force:
            return True, f"AVD '{name}' already exists"

        # Also check avdmanager list for AVDs created elsewhere
        try:
            result = subprocess.run(
                [str(avdmanager), "list", "avd"],
                capture_output=True,
                text=True,
                timeout=30,
                env=self._sdkmanager_env()
            )
            if result.returncode == 0 and f"Name: {name}" in result.stdout:
                if not force:
                    return True, f"AVD '{name}' already exists"
        except Exception:
            pass  # If list fails, continue with directory check

        try:
            self._report_progress("create_avd", 0, 100, f"Creating AVD '{name}'...")

            no_input = "no\n"
            cmd = [str(avdmanager), "create", "avd", "-n", name, "-k", system_image, "--force"]
            result = subprocess.run(
                cmd,
                input=no_input,
                capture_output=True,
                text=True,
                timeout=180,
                env=self._sdkmanager_env()
            )

            if result.returncode == 0:
                if config is not None:
                    self._configure_avd(name, config, data_partition_gb)
                self._report_progress("create_avd", 100, 100, f"AVD '{name}' created")
                return True, f"AVD '{name}' created successfully"
            else:
                stderr_lines = result.stderr.strip().split("\n")[-50:]
                return False, "AVD creation failed:\n" + "\n".join(stderr_lines)

        except subprocess.TimeoutExpired:
            return False, "AVD creation timed out after 180 seconds"
        except Exception as e:
            return False, f"AVD creation error: {e}"

    def _configure_avd(self, name: str, config: EmulatorConfig,
                       data_partition_gb: float = 3.0):
        """Configure AVD hardware properties (incl. a small data partition)."""
        avd_dir = self._find_avd_dir(name)
        if avd_dir is None:
            return
        config_file = avd_dir / "config.ini"

        if not config_file.exists():
            return

        try:
            with open(config_file, "r") as f:
                lines = f.readlines()

            # Small data partition (default 3 GB) keeps each version lightweight.
            data_partition_size = str(int(data_partition_gb * (1024 ** 3)))

            hw_settings = {
                "hw.ramSize": str(config.memory_mb),
                "hw.cpu.ncore": str(config.cores),
                "hw.gpu.mode": config.gpu_mode,
                "hw.lcd.width": str(config.screen_width),
                "hw.lcd.height": str(config.screen_height),
                "hw.lcd.density": str(config.density),
                "hw.audioInput": "no" if config.no_audio else "yes",
                "hw.audioOutput": "no" if config.no_audio else "yes",
                "disk.dataPartition.size": data_partition_size,
            }

            new_lines = []
            seen = set()
            for line in lines:
                key = line.split("=")[0].strip() if "=" in line else ""
                if key in hw_settings:
                    new_lines.append(f"{key} = {hw_settings[key]}\n")
                    seen.add(key)
                else:
                    new_lines.append(line)

            for key, value in hw_settings.items():
                if key not in seen:
                    new_lines.append(f"{key} = {value}\n")

            with open(config_file, "w") as f:
                f.writelines(new_lines)

        except Exception:
            pass

    def download_scrcpy(self) -> Tuple[bool, str]:
        """Download scrcpy for the current platform. On macOS, scrcpy is optional and we don't auto-download."""
        scrcpy_dir = self._tools_dir / "scrcpy"
        scrcpy_path = PlatformUtils.get_scrcpy_path()

        if scrcpy_path.exists():
            return True, "scrcpy already installed"

        # On macOS, scrcpy is optional - don't auto-download, just check system PATH
        if PlatformUtils.IS_MACOS:
            if self.is_scrcpy_installed():
                return True, "scrcpy found in system PATH"
            self._report_progress("download_scrcpy", 100, 100, "scrcpy not found (optional), using emulator window")
            return True, "scrcpy not found (optional), using emulator window"

        # Get latest scrcpy release URL
        try:
            api_url = "https://api.github.com/repos/Genymobile/scrcpy/releases/latest"
            response = requests.get(api_url, timeout=30)
            release = response.json()

            asset_url = None
            if PlatformUtils.IS_WINDOWS:
                for asset in release.get("assets", []):
                    if "win64" in asset["name"] and asset["name"].endswith(".zip"):
                        asset_url = asset["browser_download_url"]
                        break
                if not asset_url:
                    return False, "Could not find scrcpy Windows release"

            else:
                return False, ("scrcpy auto-download not supported on Linux. "
                             "Install via package manager: sudo apt install scrcpy")

            success, msg = self._download_file(asset_url, scrcpy_dir / ("scrcpy.zip" if asset_url.endswith(".zip") else "scrcpy.tar.gz"), "download_scrcpy")
            if not success:
                return False, msg

            self._report_progress("download_scrcpy", 90, 100, "Extracting...")

            archive_path = scrcpy_dir / ("scrcpy.zip" if asset_url.endswith(".zip") else "scrcpy.tar.gz")
            if asset_url.endswith(".zip"):
                success, msg = self._extract_zip(archive_path, scrcpy_dir)
            else:
                success, msg = self._extract_tar_gz(archive_path, scrcpy_dir)
            archive_path.unlink(missing_ok=True)

            if not success:
                return False, msg

            # On macOS, the binary might be in a subdirectory
            if PlatformUtils.IS_MACOS:
                for item in scrcpy_dir.rglob("scrcpy"):
                    if item.is_file():
                        target = scrcpy_dir / "scrcpy"
                        if item != target:
                            shutil.move(str(item), str(target))
                        break

            # Make executable
            self._make_executable(scrcpy_path)

            self._report_progress("download_scrcpy", 100, 100, "scrcpy installed")
            return True, "scrcpy installed successfully"

        except Exception as e:
            return False, f"scrcpy download failed: {e}"

    def run_full_setup(self, mode: PerformanceMode) -> Tuple[bool, str]:
        """Run complete first-time setup, skipping already-completed steps."""
        steps = []

        # Build step list dynamically based on what's already done
        if not self.is_sdk_installed():
            steps.append(("Checking SDK...", self.download_cmdline_tools))
        else:
            self._report_progress("setup", 20, 100, "SDK already present")

        if not self.are_licenses_accepted():
            steps.append(("Accepting licenses...", self.accept_licenses))
        else:
            self._report_progress("setup", 30, 100, "Licenses already accepted")

        # Check if packages are missing
        installed = self.get_installed_packages()
        required = self.get_required_packages(mode)
        missing = [pkg for pkg in required if pkg not in installed]
        if missing:
            steps.append(("Installing packages...", lambda: self.check_and_install_missing(mode)))
        else:
            self._report_progress("setup", 50, 100, "All packages already installed")

        # NOTE: no system image and no AVD are created here.  The Android
        # version is chosen by the user (wizard page / Android versions dialog)
        # and its AVD is created on first Select.

        if not self.is_scrcpy_installed():
            steps.append(("Downloading scrcpy...", self.download_scrcpy))
        else:
            self._report_progress("setup", 90, 100, "scrcpy already installed")

        # Execute steps
        total_steps = len(steps)
        for i, (label, step_fn) in enumerate(steps):
            self._report_progress("setup", int(i * 100 / max(1, total_steps)) + 10, 100, label)
            success, message = step_fn()
            if not success:
                return False, f"{label}: {message}"

        self._report_progress("setup", 100, 100, "Setup complete!")
        settings.first_run_completed = True
        return True, "Setup completed successfully"

    def get_total_download_size(self, mode: PerformanceMode = None) -> int:
        """Estimate total download size in MB for the base toolchain only.

        The system image is intentionally excluded - it is downloaded later
        when the user picks an Android version.
        """
        return 200 + 50 + 300 + 30  # cmdline-tools + platform-tools + emulator + scrcpy


def format_size(mb: int) -> str:
    """Format size in MB to human readable string."""
    if mb >= 1024:
        return f"{mb/1024:.1f} GB"
    return f"{mb} MB"