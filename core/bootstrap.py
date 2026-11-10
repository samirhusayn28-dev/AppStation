"""Shared Android bootstrap: checks, downloads, licenses, JDK and components.

This module is the single downloader/checker used by:

* the first-run setup wizard (``ui/setup_wizard.py``),
* the version-replacement flow (through :class:`core.sdk_manager.SDKManager`),
* the Windows installer's optional "Download Android requirements" step
  (``ApkPreview.exe --bootstrap``).

It never starts the emulator.  Every network operation has a progress
callback, retries, an optional cancel event, and SHA verification where the
upstream publisher exposes a checksum.  The cmdline-tools zip checksum is read
from Google's repository XML; Adoptium's JSON API supplies the JDK SHA-256;
sdkmanager verifies its own package checksums.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree

import requests

from .platform_utils import PlatformUtils


ProgressCallback = Callable[[str, int, int, str], None]
LogCallback = Callable[[str], None]

ANDROID_REPOSITORY_XML = "https://dl.google.com/android/repository/repository2-3.xml"
ADOPTIUM_ASSETS_API = "https://api.adoptium.net/v3/assets/latest/17/hotspot"

ANDROID_LICENSES = (
    ("Android SDK License", "https://developer.android.com/studio/terms"),
    ("Android SDK Preview License",
     "https://developer.android.com/studio/terms"),
    ("Android SDK Package License",
     "https://developer.android.com/studio/terms"),
)

# Base toolchain the wizard and installer install.  System images are chosen
# later by the user (Android version page), never auto-downloaded here.
BASE_COMPONENTS = ("jdk", "cmdline-tools", "platform-tools", "emulator")

COMPONENT_LABELS = {
    "jdk": "Java 17 (JDK)",
    "cmdline-tools": "Android command-line tools",
    "platform-tools": "Android platform-tools (adb)",
    "emulator": "Android emulator",
    "licenses": "Android SDK licenses",
}

COMPONENT_SIZES_MB = {
    "jdk": 190,
    "cmdline-tools": 130,
    "platform-tools": 15,
    "emulator": 320,
}


def format_mb(mb: int) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{mb} MB"


def parse_java_major(output: str) -> Optional[int]:
    """Return the Java major version from ``java -version`` output."""
    match = re.search(r'version\s+"(\d+)(?:\.(\d+))?', output or "")
    if not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        major = int(match.group(2))
    return major


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class JDKStatus:
    found: bool
    version: Optional[int]
    path: str
    message: str


@dataclass
class SetupState:
    """Resumable wizard state, persisted to ``~/.apkpreview/setup_state.json``."""

    step: int = 0
    install_type: str = "standard"
    sdk_path: str = ""
    jdk_path: str = ""
    licenses_accepted: bool = False
    components_done: List[str] = field(default_factory=list)
    device_key: str = ""
    api_level: int = 0
    variant: str = "default"
    completed: bool = False

    @classmethod
    def path(cls) -> Path:
        return PlatformUtils.get_app_data_dir() / "setup_state.json"

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SetupState":
        state_path = Path(path) if path else cls.path()
        if not state_path.exists():
            return cls()
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            allowed = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in raw.items() if k in allowed})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Optional[Path] = None) -> bool:
        state_path = Path(path) if path else self.path()
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(asdict(self), indent=2),
                                  encoding="utf-8")
            return True
        except OSError:
            return False

    def reset(self, path: Optional[Path] = None) -> None:
        for name, value in asdict(SetupState()).items():
            setattr(self, name, value)
        self.save(path)


class BootstrapManager:
    """Checks and installs the Android toolchain without starting anything."""

    def __init__(self, sdk_dir: Optional[Path] = None,
                 progress: Optional[ProgressCallback] = None,
                 log: Optional[LogCallback] = None,
                 state_path: Optional[Path] = None):
        self.sdk_dir = Path(sdk_dir) if sdk_dir else PlatformUtils.get_sdk_dir()
        self.progress = progress
        self.log = log
        self.state = SetupState.load(state_path)
        self._state_path = state_path
        if not self.state.sdk_path:
            self.state.sdk_path = str(self.sdk_dir)

    # ------------------------------------------------------------------
    # small helpers
    # ------------------------------------------------------------------
    def _report(self, step: str, current: int, total: int, message: str = ""):
        if self.progress:
            self.progress(step, max(0, min(current, total)), max(1, total),
                          message)

    def _log(self, message: str):
        if self.log:
            self.log(message)

    def _save_state(self):
        self.state.save(self._state_path)

    def sdkmanager_path(self) -> Path:
        if PlatformUtils.IS_WINDOWS:
            return self.sdk_dir / "cmdline-tools" / "latest" / "bin" / "sdkmanager.bat"
        return self.sdk_dir / "cmdline-tools" / "latest" / "bin" / "sdkmanager"

    def avdmanager_path(self) -> Path:
        if PlatformUtils.IS_WINDOWS:
            return self.sdk_dir / "cmdline-tools" / "latest" / "bin" / "avdmanager.bat"
        return self.sdk_dir / "cmdline-tools" / "latest" / "bin" / "avdmanager"

    def adb_path(self) -> Path:
        if PlatformUtils.IS_WINDOWS:
            return self.sdk_dir / "platform-tools" / "adb.exe"
        return self.sdk_dir / "platform-tools" / "adb"

    def emulator_path(self) -> Path:
        if PlatformUtils.IS_WINDOWS:
            return self.sdk_dir / "emulator" / "emulator.exe"
        return self.sdk_dir / "emulator" / "emulator"

    def _env(self) -> dict:
        env = os.environ.copy()
        env["PATH"] = str(self.sdk_dir / "platform-tools") + os.pathsep + env.get("PATH", "")
        jdk = self.state.jdk_path or os.environ.get("JAVA_HOME", "")
        if jdk:
            env["JAVA_HOME"] = jdk
            env["PATH"] = str(Path(jdk) / "bin") + os.pathsep + env["PATH"]
        return env

    # ------------------------------------------------------------------
    # checks
    # ------------------------------------------------------------------
    def check_jdk(self) -> JDKStatus:
        """Find a usable JDK 17+ (JAVA_HOME, PATH, or our downloaded copy)."""
        candidates: List[Path] = []
        if self.state.jdk_path:
            candidates.append(Path(self.state.jdk_path) / "bin" / "java")
        java_home = os.environ.get("JAVA_HOME")
        if java_home:
            candidates.append(Path(java_home) / "bin" / "java")
        which = shutil.which("java")
        if which:
            candidates.append(Path(which))
        jdk_root = PlatformUtils.get_app_data_dir() / "jdk"
        if jdk_root.exists():
            candidates.extend(jdk_root.rglob("bin/java"))
            candidates.extend(jdk_root.rglob("bin/java.exe"))

        for java in candidates:
            if not java.exists():
                continue
            try:
                result = subprocess.run(
                    [str(java), "-version"], capture_output=True, text=True,
                    timeout=15, env=self._env())
                output = (result.stderr or "") + (result.stdout or "")
                major = parse_java_major(output)
                if major is not None and major >= 17:
                    home = self._java_home_for(java)
                    return JDKStatus(True, major, home,
                                     f"Java {major} found")
            except Exception:
                continue
        return JDKStatus(False, None, "", "Java 17 (JDK) not found")

    def _java_home_for(self, java: Path) -> str:
        """Best-effort JAVA_HOME for a java executable.

        ``/usr/bin/java`` on macOS is a launcher stub, not ``$JAVA_HOME/bin``,
        so query ``/usr/libexec/java_home`` there instead of guessing.
        """
        env_home = os.environ.get("JAVA_HOME")
        if env_home:
            candidate = Path(env_home) / "bin" / ("java.exe" if PlatformUtils.IS_WINDOWS else "java")
            if candidate.exists():
                try:
                    result = subprocess.run(
                        [str(candidate), "-version"], capture_output=True,
                        text=True, timeout=15)
                    output = (result.stderr or "") + (result.stdout or "")
                    if (parse_java_major(output) or 0) >= 17:
                        return str(Path(env_home))
                except Exception:
                    pass

        if PlatformUtils.IS_MACOS:
            try:
                result = subprocess.run(["/usr/libexec/java_home", "-v", "17"],
                                        capture_output=True, text=True, timeout=15)
                home = (result.stdout or "").strip()
                if home and (Path(home) / "bin" / "java").exists():
                    return home
            except Exception:
                pass

        if java.parent.name.lower() == "bin":
            return str(java.parent.parent)
        return str(java.parent)

    def is_component_installed(self, key: str) -> bool:
        if key == "jdk":
            return self.check_jdk().found
        if key == "cmdline-tools":
            return self.sdkmanager_path().exists()
        if key == "platform-tools":
            return self.adb_path().exists()
        if key == "emulator":
            return self.emulator_path().exists()
        if key == "licenses":
            return self.licenses_accepted()
        return False

    def components(self) -> List[dict]:
        result = []
        for key in BASE_COMPONENTS:
            result.append({
                "key": key,
                "name": COMPONENT_LABELS[key],
                "size_mb": COMPONENT_SIZES_MB.get(key, 0),
                "installed": self.is_component_installed(key),
            })
        return result

    def all_required_installed(self) -> bool:
        return all(self.is_component_installed(key) for key in BASE_COMPONENTS)

    def system_report(self) -> dict:
        memory = PlatformUtils.get_memory_info()
        cpu = PlatformUtils.get_cpu_info()
        disk = PlatformUtils.get_disk_info(self.sdk_dir)
        virtualization = PlatformUtils.check_virtualization()
        jdk = self.check_jdk()

        warnings = []
        if memory["total_mb"] < 8192:
            warnings.append("Less than 8 GB RAM: the Low performance profile "
                            "will be used.")
        if (cpu.get("logical_cores") or 0) < 4:
            warnings.append("4 or fewer CPU threads: boot and preview will be "
                            "slower.")
        if disk["free_gb"] < 10:
            warnings.append("Less than 10 GB free disk space: downloads may "
                            "fail.")
        if not virtualization.get("supported"):
            warnings.append("Hardware virtualization is unavailable. The "
                            "emulator would be extremely slow.")
        if not jdk.found:
            warnings.append("Java 17 (JDK) is missing and will be downloaded.")

        return {
            "os": PlatformUtils.get_os_version(),
            "architecture": cpu.get("architecture", "unknown"),
            "physical_cores": cpu.get("physical_cores"),
            "logical_cores": cpu.get("logical_cores"),
            "ram_mb": memory["total_mb"],
            "free_disk_gb": disk["free_gb"],
            "virtualization": virtualization,
            "jdk": jdk,
            "warnings": warnings,
        }

    def licenses_accepted(self) -> bool:
        license_file = self.sdk_dir / "licenses" / "android-sdk-license"
        return license_file.exists()

    def license_text(self) -> str:
        """Readable license summary shown before acceptance.

        The canonical text is served by Google; the wizard shows the exact
        license names and opens the official page.  We do not ship a modified
        copy of a legal document.
        """
        lines = [
            "Android SDK License Agreement",
            "",
            "By continuing you accept the Android SDK License Agreement for "
            "the packages ApkPreview downloads:",
            "",
        ]
        for name, url in ANDROID_LICENSES:
            lines.append(f"  - {name}: {url}")
        lines.extend([
            "",
            "The full legal text is available at the URLs above. sdkmanager "
            "records your acceptance in the SDK's licenses directory.",
            "",
            "If you do not accept, ApkPreview cannot download the Android "
            "command-line tools, platform-tools, emulator or system images.",
        ])
        return "\n".join(lines)

    def disk_space_ok(self, required_mb: int, headroom_mb: int = 2048) -> Tuple[bool, str]:
        try:
            free_mb = PlatformUtils.get_disk_info(self.sdk_dir)["free_gb"] * 1024
        except Exception as exc:
            return False, f"Could not check free disk space: {exc}"
        needed = required_mb + headroom_mb
        if free_mb < needed:
            return False, (f"Not enough disk space: need about "
                           f"{format_mb(needed)}, have {format_mb(int(free_mb))}.")
        return True, f"{format_mb(int(free_mb))} free"

    # ------------------------------------------------------------------
    # downloads
    # ------------------------------------------------------------------
    @staticmethod
    def _request_get(url: str, **kwargs):
        """Indirection so tests can replace network access in one place."""
        return requests.get(url, **kwargs)

    def download_file(self, url: str, dest: Path,
                      expected_sha256: Optional[str] = None,
                      expected_sha1: Optional[str] = None,
                      step: str = "download",
                      cancel=None,
                      retries: int = 3,
                      resume: bool = True) -> Tuple[bool, str]:
        """Stream a download with resume, retries and SHA verification."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last_error = ""

        for attempt in range(1, retries + 1):
            try:
                existing = part.stat().st_size if (resume and part.exists()) else 0
                headers = {"Range": f"bytes={existing}-"} if existing else {}
                response = self._request_get(url, stream=True,
                                             timeout=(30, 300), headers=headers)
                response.raise_for_status()

                if existing and response.status_code != 206:
                    # Server ignored Range: start over.
                    existing = 0
                    mode = "wb"
                else:
                    mode = "ab" if existing else "wb"

                content_length = int(response.headers.get("content-length", 0) or 0)
                total = existing + content_length
                downloaded = existing
                with open(part, mode) as f:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if cancel is not None and cancel.is_set():
                            return False, "Download cancelled"
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        total_for_progress = total or downloaded
                        self._report(step, int(downloaded * 100 / total_for_progress),
                                     total_for_progress,
                                     f"Downloading {dest.name} "
                                     f"({format_mb(downloaded // (1024 * 1024))})")

                if expected_sha256:
                    actual = sha256_file(part)
                    if actual.lower() != expected_sha256.lower():
                        part.unlink(missing_ok=True)
                        raise ValueError(
                            f"SHA-256 mismatch for {dest.name}: "
                            f"expected {expected_sha256}, got {actual}")
                if expected_sha1:
                    actual = sha1_file(part)
                    if actual.lower() != expected_sha1.lower():
                        part.unlink(missing_ok=True)
                        raise ValueError(
                            f"SHA-1 mismatch for {dest.name}: "
                            f"expected {expected_sha1}, got {actual}")

                os.replace(part, dest)
                self._report(step, 100, 100, f"Downloaded {dest.name}")
                return True, f"Downloaded {dest.name}"
            except Exception as exc:
                last_error = str(exc)
                self._log(f"Download attempt {attempt}/{retries} failed: {exc}")
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 8))

        return False, f"Download failed after {retries} attempts: {last_error}"

    def _google_checksum(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        """Find the sha1/sha256 Google publishes for a repository archive."""
        try:
            response = self._request_get(ANDROID_REPOSITORY_XML, timeout=60)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            wanted = Path(url).name
            for archive in root.iter("archive"):
                complete = archive.find("complete")
                if complete is None:
                    continue
                archive_url = complete.findtext("url") or ""
                if Path(archive_url).name != wanted:
                    continue
                checksum = complete.find("checksum")
                if checksum is not None and checksum.text:
                    kind = (checksum.get("type") or "sha1").lower()
                    value = checksum.text.strip()
                    return (value, None) if kind == "sha1" else (None, value)
        except Exception as exc:
            self._log(f"Could not read Google checksum list: {exc}")
        return None, None

    # ------------------------------------------------------------------
    # installers
    # ------------------------------------------------------------------
    def install_cmdline_tools(self, cancel=None) -> Tuple[bool, str]:
        if self.sdkmanager_path().exists():
            self._report("cmdline-tools", 100, 100, "Command-line tools present")
            return True, "Command-line tools already present"

        url = PlatformUtils.get_cmdline_tools_url()
        archive = self.sdk_dir / "cmdline-tools.zip"
        sha1, sha256 = self._google_checksum(url)
        self._report("cmdline-tools", 0, 100, "Downloading command-line tools")
        ok, message = self.download_file(
            url, archive, expected_sha1=sha1, expected_sha256=sha256,
            step="cmdline-tools", cancel=cancel)
        if not ok:
            return False, message

        self._report("cmdline-tools", 90, 100, "Extracting command-line tools")
        temp = self.sdk_dir / ".cmdline-tools-tmp"
        shutil.rmtree(temp, ignore_errors=True)
        try:
            with zipfile.ZipFile(archive, "r") as zf:
                zf.extractall(temp)
            extracted = temp / "cmdline-tools"
            if not extracted.exists():
                extracted = temp
            target = self.sdk_dir / "cmdline-tools" / "latest"
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted), str(target))
            if not PlatformUtils.IS_WINDOWS:
                for item in (target / "bin").glob("*"):
                    if item.is_file():
                        item.chmod(0o755)
            archive.unlink(missing_ok=True)
            shutil.rmtree(temp, ignore_errors=True)
            self._report("cmdline-tools", 100, 100, "Command-line tools installed")
            return True, "Command-line tools installed"
        except Exception as exc:
            return False, f"Could not extract command-line tools: {exc}"

    def accept_licenses(self, cancel=None) -> Tuple[bool, str]:
        if self.licenses_accepted():
            self.state.licenses_accepted = True
            self._save_state()
            return True, "Licenses already accepted"
        sdkmanager = self.sdkmanager_path()
        if not sdkmanager.exists():
            return False, "Command-line tools are required before accepting licenses"
        self._report("licenses", 0, 100, "Accepting Android SDK licenses")
        try:
            result = subprocess.run(
                [str(sdkmanager), "--licenses"],
                input="y\n" * 100, capture_output=True, text=True,
                timeout=180, env=self._env())
            if result.returncode == 0 or self.licenses_accepted():
                self.state.licenses_accepted = True
                self._save_state()
                self._report("licenses", 100, 100, "Licenses accepted")
                return True, "Licenses accepted"
            tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-15:])
            return False, f"License acceptance failed:\n{tail}"
        except Exception as exc:
            return False, f"License acceptance failed: {exc}"

    def install_package(self, package: str, step: Optional[str] = None,
                        cancel=None, retries: int = 2) -> Tuple[bool, str]:
        sdkmanager = self.sdkmanager_path()
        if not sdkmanager.exists():
            return False, "Command-line tools are required first"
        step = step or package
        last = ""
        for attempt in range(1, retries + 1):
            self._report(step, 0, 100, f"Installing {package}")
            try:
                proc = subprocess.Popen(
                    [str(sdkmanager), package], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, env=self._env())
                buffer: List[str] = []
                last_line = ""
                while True:
                    if cancel is not None and cancel.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            proc.kill()
                        return False, "Download cancelled"
                    chunk = proc.stdout.read(1) if proc.stdout else ""
                    if not chunk:
                        break
                    if chunk in ("\r", "\n"):
                        line = "".join(buffer).strip()
                        buffer.clear()
                        if line:
                            last_line = line
                            self._log(line)
                            match = re.search(r"(\d{1,3})\s*%", line)
                            if match:
                                self._report(step, int(match.group(1)), 100, line)
                    else:
                        buffer.append(chunk)
                returncode = proc.wait()
                if returncode == 0:
                    self._report(step, 100, 100, f"Installed {package}")
                    return True, f"Installed {package}"
                last = last_line or f"sdkmanager exited with {returncode}"
            except Exception as exc:
                last = str(exc)
            self._log(f"Install attempt {attempt}/{retries} failed: {last}")
            if attempt < retries:
                time.sleep(2)
        return False, f"Failed to install {package}: {last}"

    def install_jdk(self, cancel=None) -> Tuple[bool, str]:
        status = self.check_jdk()
        if status.found:
            self.state.jdk_path = status.path
            self._save_state()
            return True, status.message

        os_name = "windows" if PlatformUtils.IS_WINDOWS else (
            "mac" if PlatformUtils.IS_MACOS else "linux")
        arch = "aarch64" if PlatformUtils.get_arch() == "arm64" else "x64"
        api = f"{ADOPTIUM_ASSETS_API}?os={os_name}&architecture={arch}&image_type=jdk"
        self._report("jdk", 0, 100, "Finding Java 17")
        try:
            response = self._request_get(api, timeout=60)
            response.raise_for_status()
            assets = response.json()
            if not assets:
                return False, "No Java 17 build found for this platform"
            package = assets[0]["binary"]["package"]
            link = package["link"]
            checksum = package.get("checksum")
        except Exception as exc:
            return False, f"Could not query the Java 17 download: {exc}"

        suffix = ".zip" if link.lower().endswith(".zip") else ".tar.gz"
        archive = self.sdk_dir / f"jdk17{suffix}"
        ok, message = self.download_file(
            link, archive, expected_sha256=checksum, step="jdk",
            cancel=cancel)
        if not ok:
            return False, message

        self._report("jdk", 90, 100, "Extracting Java 17")
        jdk_root = PlatformUtils.get_app_data_dir() / "jdk"
        temp = jdk_root / ".extract"
        shutil.rmtree(temp, ignore_errors=True)
        try:
            temp.mkdir(parents=True, exist_ok=True)
            if suffix == ".zip":
                with zipfile.ZipFile(archive, "r") as zf:
                    zf.extractall(temp)
            else:
                with tarfile.open(archive, "r:gz") as tf:
                    try:
                        tf.extractall(temp, filter="data")
                    except TypeError:
                        tf.extractall(temp)
            java = next(iter(temp.rglob("bin/java")), None) or \
                next(iter(temp.rglob("bin/java.exe")), None)
            if java is None:
                return False, "Downloaded Java archive did not contain a JDK"
            home = java.parent.parent
            final = jdk_root / home.name
            shutil.rmtree(final, ignore_errors=True)
            shutil.move(str(home), str(final))
            shutil.rmtree(temp, ignore_errors=True)
            archive.unlink(missing_ok=True)

            os.environ["JAVA_HOME"] = str(final)
            os.environ["PATH"] = str(final / "bin") + os.pathsep + os.environ.get("PATH", "")
            self.state.jdk_path = str(final)
            self._save_state()
            self._report("jdk", 100, 100, "Java 17 installed")
            return True, f"Java 17 installed at {final}"
        except Exception as exc:
            return False, f"Could not extract Java 17: {exc}"

    def install_components(self, keys: Optional[List[str]] = None,
                           cancel=None) -> Tuple[bool, str]:
        """Install the selected components in a safe order."""
        keys = list(keys or BASE_COMPONENTS)
        # sdkmanager (and therefore every Android component) needs Java 17.
        # If the user skipped JDK in Custom mode but it is missing, install it
        # first anyway; otherwise the later steps would fail with a confusing
        # "java not found" error.
        needs_java = any(key in keys for key in
                         ("cmdline-tools", "licenses", "platform-tools",
                          "emulator"))
        if needs_java and "jdk" not in keys and not self.check_jdk().found:
            keys.insert(0, "jdk")
        if "jdk" in keys:
            ok, message = self.install_jdk(cancel=cancel)
            if not ok:
                return False, message
            self._mark_done("jdk")

        if "cmdline-tools" in keys:
            ok, message = self.install_cmdline_tools(cancel=cancel)
            if not ok:
                return False, message
            self._mark_done("cmdline-tools")

        if "cmdline-tools" in keys or self.sdkmanager_path().exists():
            if "licenses" in keys or not self.licenses_accepted():
                ok, message = self.accept_licenses(cancel=cancel)
                if not ok:
                    return False, message
                self._mark_done("licenses")

        for key, package in (("platform-tools", "platform-tools"),
                             ("emulator", "emulator")):
            if key not in keys:
                continue
            if self.is_component_installed(key):
                self._mark_done(key)
                continue
            ok, message = self.install_package(package, step=key, cancel=cancel)
            if not ok:
                return False, message
            self._mark_done(key)

        self._report("components", 100, 100, "Android requirements installed")
        return True, "Android requirements installed"

    def _mark_done(self, key: str):
        if key not in self.state.components_done:
            self.state.components_done.append(key)
            self._save_state()


def needs_setup() -> bool:
    """True when the wizard should open on launch."""
    from .settings import settings
    if not settings.first_run_completed:
        return True
    try:
        return not BootstrapManager().all_required_installed()
    except Exception:
        return True


def bootstrap_cli(argv: Optional[List[str]] = None) -> int:
    """Headless entry point used by the Windows installer."""
    argv = list(argv or sys.argv)
    quiet = "--quiet" in argv

    def progress(step: str, current: int, total: int, message: str):
        if not quiet:
            print(f"[{step}] {current}/{total} {message}", flush=True)

    def log(message: str):
        if not quiet:
            print(message, flush=True)

    manager = BootstrapManager(progress=progress, log=log)
    keys = ["jdk", "cmdline-tools", "licenses", "platform-tools", "emulator"]
    ok, message = manager.install_components(keys)
    if not quiet:
        print(message, flush=True)
    return 0 if ok else 1
