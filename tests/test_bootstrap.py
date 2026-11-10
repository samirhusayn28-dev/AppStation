"""Unit tests for the shared bootstrap/downloader (no real network)."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import (BASE_COMPONENTS, BootstrapManager, JDKStatus,
                            SetupState, format_mb, needs_setup,
                            parse_java_major, sha256_file)
from core.platform_utils import PlatformUtils


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, content=b"", status=200, headers=None, chunks=None):
        self.content = content
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        if self._chunks is not None:
            yield from self._chunks
            return
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]

    def json(self):
        return json.loads(self.content)


class TestJavaVersionParsing(unittest.TestCase):
    def test_parse_java_major(self):
        self.assertEqual(parse_java_major('openjdk version "17.0.9" 2023-10-17'), 17)
        self.assertEqual(parse_java_major('java version "1.8.0_392"'), 8)
        self.assertEqual(parse_java_major('openjdk version "21.0.1" 2023-10-17'), 21)
        self.assertIsNone(parse_java_major("no version here"))

    def test_format_mb(self):
        self.assertEqual(format_mb(512), "512 MB")
        self.assertEqual(format_mb(2048), "2.0 GB")


class TestSetupState(unittest.TestCase):
    def test_roundtrip_and_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup_state.json"
            state = SetupState(step=5, install_type="custom",
                               sdk_path="/tmp/sdk", device_key="google_pixel_8",
                               api_level=34, components_done=["jdk"])
            self.assertTrue(state.save(path))
            loaded = SetupState.load(path)
            self.assertEqual(loaded.step, 5)
            self.assertEqual(loaded.install_type, "custom")
            self.assertEqual(loaded.device_key, "google_pixel_8")
            self.assertEqual(loaded.components_done, ["jdk"])

            loaded.reset(path)
            fresh = SetupState.load(path)
            self.assertEqual(fresh.step, 0)
            self.assertEqual(fresh.components_done, [])

    def test_missing_or_corrupt_state_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "setup_state.json"
            self.assertEqual(SetupState.load(path).step, 0)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(SetupState.load(path).step, 0)


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.manager = BootstrapManager(sdk_dir=self.root / "sdk",
                                        state_path=self.root / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_with_sha256(self):
        payload = b"hello apkpreview" * 1000
        dest = self.root / "archive.zip"
        expected = __import__("hashlib").sha256(payload).hexdigest()

        def fake_get(_url, **kwargs):
            return FakeResponse(payload, headers={"content-length": str(len(payload))})

        with mock.patch.object(BootstrapManager, "_request_get", staticmethod(fake_get)):
            ok, message = self.manager.download_file(
                "https://example.invalid/archive.zip", dest,
                expected_sha256=expected)
        self.assertTrue(ok, message)
        self.assertEqual(dest.read_bytes(), payload)
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_download_resumes_from_partial_file(self):
        payload = b"0123456789" * 100
        dest = self.root / "archive.zip"
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(payload[:250])

        def fake_get(_url, **kwargs):
            self.assertEqual(kwargs["headers"]["Range"], "bytes=250-")
            return FakeResponse(payload[250:], status=206,
                                headers={"content-length": str(len(payload) - 250)})

        with mock.patch.object(BootstrapManager, "_request_get", staticmethod(fake_get)):
            ok, message = self.manager.download_file(
                "https://example.invalid/archive.zip", dest)
        self.assertTrue(ok, message)
        self.assertEqual(dest.read_bytes(), payload)

    def test_download_sha_mismatch_removes_part(self):
        payload = b"payload"
        dest = self.root / "archive.zip"

        def fake_get(_url, **kwargs):
            return FakeResponse(payload, headers={"content-length": str(len(payload))})

        with mock.patch.object(BootstrapManager, "_request_get", staticmethod(fake_get)):
            ok, message = self.manager.download_file(
                "https://example.invalid/archive.zip", dest,
                expected_sha256="0" * 64, retries=1)
        self.assertFalse(ok)
        self.assertIn("SHA-256 mismatch", message)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_download_retries_then_succeeds(self):
        payload = b"retry-me"
        dest = self.root / "archive.zip"
        calls = {"count": 0}

        def fake_get(_url, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary network failure")
            return FakeResponse(payload, headers={"content-length": str(len(payload))})

        with mock.patch.object(BootstrapManager, "_request_get", staticmethod(fake_get)), \
                mock.patch("core.bootstrap.time.sleep", lambda _seconds: None):
            ok, message = self.manager.download_file(
                "https://example.invalid/archive.zip", dest, retries=3)
        self.assertTrue(ok, message)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(dest.read_bytes(), payload)

    def test_google_checksum_parsing(self):
        xml = b"""<sdk:sdk-repository xmlns:sdk="http://schemas.android.com/sdk">
          <remotePackage path="cmdline-tools;latest">
            <archives><archive>
              <complete>
                <size>1</size>
                <checksum type="sha1">abcdef123456</checksum>
                <url>commandlinetools-mac-11076708_latest.zip</url>
              </complete>
            </archive></archives>
          </remotePackage>
        </sdk:sdk-repository>"""

        def fake_get(_url, **kwargs):
            return FakeResponse(xml)

        with mock.patch.object(BootstrapManager, "_request_get", staticmethod(fake_get)):
            sha1, sha256 = self.manager._google_checksum(
                "https://dl.google.com/android/repository/"
                "commandlinetools-mac-11076708_latest.zip")
        self.assertEqual(sha1, "abcdef123456")
        self.assertIsNone(sha256)


class TestChecks(unittest.TestCase):
    def test_components_and_disk_space(self):
        with tempfile.TemporaryDirectory() as tmp:
            sdk = Path(tmp) / "sdk"
            (sdk / "cmdline-tools" / "latest" / "bin").mkdir(parents=True)
            (sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager").write_text("")
            (sdk / "platform-tools").mkdir(parents=True)
            (sdk / "platform-tools" / "adb").write_text("")
            (sdk / "emulator").mkdir(parents=True)
            (sdk / "emulator" / "emulator").write_text("")

            manager = BootstrapManager(sdk_dir=sdk, state_path=Path(tmp) / "s.json")
            with mock.patch.object(BootstrapManager, "check_jdk",
                                   return_value=JDKStatus(True, 17, "/jdk", "ok")), \
                    mock.patch.object(PlatformUtils, "get_disk_info",
                                      return_value={"free_gb": 20.0, "total_gb": 100.0}):
                components = {c["key"]: c["installed"] for c in manager.components()}
                self.assertTrue(all(components[key] for key in BASE_COMPONENTS))
                self.assertTrue(manager.all_required_installed())
                ok, message = manager.disk_space_ok(1000)
                self.assertTrue(ok)
                self.assertIn("20", message)
                ok, message = manager.disk_space_ok(50000)
                self.assertFalse(ok)
                self.assertIn("Not enough disk space", message)

    def test_check_jdk_finds_java_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "jdk"
            (home / "bin").mkdir(parents=True)
            java = home / "bin" / "java"
            java.write_text("#!/bin/sh\n")
            manager = BootstrapManager(sdk_dir=Path(tmp) / "sdk",
                                       state_path=Path(tmp) / "s.json")
            fake = mock.Mock(stderr='openjdk version "17.0.9"', stdout="",
                             returncode=0)
            with mock.patch.dict(os.environ, {"JAVA_HOME": str(home)}), \
                    mock.patch.object(PlatformUtils, "IS_MACOS", False), \
                    mock.patch("core.bootstrap.subprocess.run", return_value=fake):
                status = manager.check_jdk()
            self.assertTrue(status.found)
            self.assertEqual(status.version, 17)
            self.assertEqual(status.path, str(home))

    def test_system_report_warnings(self):
        manager = BootstrapManager(sdk_dir=Path("/tmp/sdk"),
                                   state_path=Path("/tmp/nonexistent-state.json"))
        with mock.patch.object(PlatformUtils, "get_memory_info",
                               return_value={"total_mb": 4096}), \
                mock.patch.object(PlatformUtils, "get_cpu_info",
                                  return_value={"logical_cores": 2,
                                                "physical_cores": 2,
                                                "architecture": "x86_64"}), \
                mock.patch.object(PlatformUtils, "get_disk_info",
                                  return_value={"free_gb": 3.0, "total_gb": 100.0}), \
                mock.patch.object(PlatformUtils, "check_virtualization",
                                  return_value={"supported": False,
                                                "message": "no hypervisor"}), \
                mock.patch.object(BootstrapManager, "check_jdk",
                                  return_value=JDKStatus(False, None, "",
                                                         "missing")):
            report = manager.system_report()
        self.assertEqual(report["ram_mb"], 4096)
        self.assertEqual(report["architecture"], "x86_64")
        text = " ".join(report["warnings"])
        self.assertIn("8 GB RAM", text)
        self.assertIn("CPU threads", text)
        self.assertIn("disk space", text)
        self.assertIn("virtualization", text)
        self.assertIn("Java 17", text)

    def test_install_components_forces_missing_jdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = BootstrapManager(sdk_dir=Path(tmp) / "sdk",
                                       state_path=Path(tmp) / "s.json")
            with mock.patch.object(BootstrapManager, "check_jdk",
                                   return_value=JDKStatus(False, None, "",
                                                          "missing")), \
                    mock.patch.object(BootstrapManager, "install_jdk",
                                      return_value=(True, "jdk installed")) as jdk, \
                    mock.patch.object(BootstrapManager, "install_cmdline_tools",
                                      return_value=(True, "tools installed")), \
                    mock.patch.object(BootstrapManager, "licenses_accepted",
                                      return_value=True), \
                    mock.patch.object(BootstrapManager, "is_component_installed",
                                      return_value=True):
                ok, message = manager.install_components(["cmdline-tools"])
        self.assertTrue(ok, message)
        jdk.assert_called_once()

    def test_license_text_has_links(self):
        manager = BootstrapManager(sdk_dir=Path("/tmp/sdk"),
                                   state_path=Path("/tmp/nonexistent-state.json"))
        text = manager.license_text()
        self.assertIn("Android SDK License Agreement", text)
        self.assertIn("developer.android.com/studio/terms", text)

    def test_needs_setup(self):
        from core.settings import settings

        with mock.patch.object(settings, "_data",
                               {**settings._data, "first_run_completed": False}):
            self.assertTrue(needs_setup())
        with mock.patch.object(settings, "_data",
                               {**settings._data, "first_run_completed": True}), \
                mock.patch.object(BootstrapManager, "all_required_installed",
                                  return_value=False):
            self.assertTrue(needs_setup())
        with mock.patch.object(settings, "_data",
                               {**settings._data, "first_run_completed": True}), \
                mock.patch.object(BootstrapManager, "all_required_installed",
                                  return_value=True):
            self.assertFalse(needs_setup())


if __name__ == "__main__":
    unittest.main()
