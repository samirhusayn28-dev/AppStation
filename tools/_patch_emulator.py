"""One-off patch: emulator log capture, accel/GPU verification, override reset."""
from pathlib import Path

P = Path("core/emulator.py")
s = P.read_text()

OLD_START = (
    '    # -- lifecycle -------------------------------------------------------\n'
    '    def _start_once(self, cmd: List[str], wait_for_boot: bool,\n'
    '                    timeout: int) -> Tuple[bool, str]:\n'
    '        try:\n'
    '            self._process = PlatformUtils.start_detached_process(cmd)\n'
    '            self._serial = None\n'
    '            if wait_for_boot:\n'
    '                return self._wait_for_boot(timeout)\n'
    '            return True, "Emulator started"\n'
    '        except Exception as e:\n'
    '            return False, f"Failed to start emulator: {e}"\n'
)

NEW_START = (
    '    # -- lifecycle -------------------------------------------------------\n'
    '    def _emulator_log_path(self) -> Path:\n'
    '        """Where the emulator\'s own stdout/stderr is captured."""\n'
    '        try:\n'
    '            return PlatformUtils.get_app_data_dir() / "emulator.log"\n'
    '        except Exception:\n'
    '            return Path.home() / ".apkpreview" / "emulator.log"\n'
    '\n'
    '    def _start_once(self, cmd: List[str], wait_for_boot: bool,\n'
    '                    timeout: int) -> Tuple[bool, str]:\n'
    '        try:\n'
    '            log_path = self._emulator_log_path()\n'
    '            self._process = PlatformUtils.start_detached_process(\n'
    '                cmd, log_file=log_path)\n'
    '            self._serial = None\n'
    '            if wait_for_boot:\n'
    '                result = self._wait_for_boot(timeout)\n'
    '                self._verify_acceleration_and_gpu(log_path)\n'
    '                return result\n'
    '            return True, "Emulator started"\n'
    '        except Exception as e:\n'
    '            return False, f"Failed to start emulator: {e}"\n'
    '\n'
    '    def _read_emulator_log(self) -> str:\n'
    '        """Read the captured emulator log (best effort)."""\n'
    '        try:\n'
    '            path = self._emulator_log_path()\n'
    '            if path.exists():\n'
    '                return path.read_text(encoding="utf-8", errors="replace")\n'
    '        except Exception:\n'
    '            pass\n'
    '        return ""\n'
    '\n'
    '    def _verify_acceleration_and_gpu(self,\n'
    '                                 log_path: Optional[Path] = None):\n'
    '        """Check that CPU acceleration and the GPU mode are as expected.\n'
    '\n'
    '        The emulator prints the chosen accelerator and graphics adapter to\n'
    '        its log.  When the host has no usable GPU the emulator silently\n'
    '        falls back to ANGLE-on-SwiftShader (pure software rendering),\n'
    '        which is very slow, so the user is warned about it.\n'
    '        """\n'
    '        text = self._read_emulator_log()\n'
    '        if not text:\n'
    '            return\n'
    '        lowered = text.lower()\n'
    '\n'
    '        accel = ""\n'
    '        for marker, name in (\n'
    '                ("Hypervisor.Framework", "Hypervisor Framework (hvf)"),\n'
    '                ("Windows Hypervisor Platform", "WHPX"),\n'
    '                ("Android Emulator Hypervisor Driver", "AEHD"),\n'
    '                ("hax", "HAX")):\n'
    '            if marker.lower() in lowered:\n'
    '                accel = name\n'
    '                break\n'
    '        if accel:\n'
    '            self._log(f"CPU acceleration active: {accel}")\n'
    '        else:\n'
    '            self._log("WARNING: no hardware CPU acceleration found in the "\n'
    '                      "emulator log")\n'
    '\n'
    '        software = "swiftshader" in lowered or "swangle" in lowered\n'
    '        self._software_gpu = software\n'
    '        self._last_gpu_mode = ("software (ANGLE/SwiftShader)" if software\n'
    '                               else "host GPU")\n'
    '        if software:\n'
    '            self._log("WARNING: the emulator is using software rendering "\n'
    '                      "(ANGLE/SwiftShader) - no usable host GPU.  Expect a "\n'
    '                      "slow screen.")\n'
)

OLD_ANIM = (
    '    def _disable_guest_animations(self) -> None:\n'
    '        """Turn off Android\'s window/transition/animator animations."""\n'
    '        for key in ("window_animation_scale",\n'
    '                    "transition_animation_scale",\n'
    '                    "animator_duration_scale"):\n'
    '            self._run_adb(["shell", "settings", "put", "global", key, "0"],\n'
    '                          timeout=10)\n'
    '        self._log("Guest animations disabled")\n'
)

NEW_ANIM = OLD_ANIM + (
    '\n'
    '    def _reset_guest_overrides(self) -> None:\n'
    '        """Clear any leftover ``wm size`` / ``wm density`` override.\n'
    '\n'
    '        A ``wm size`` override survives inside the AVD and makes the guest\n'
    '        render at a *much* larger resolution than the emulator was\n'
    '        started with.  With software rendering that is enough to starve\n'
    '        the guest completely (no frames at all, SystemUI ANRs).  The\n'
    '        overrides came from the removed per-device feature, so they are\n'
    '        cleared on every boot.\n'
    '        """\n'
    '        for args in (["shell", "wm", "size", "reset"],\n'
    '                     ["shell", "wm", "density", "reset"]):\n'
    '            try:\n'
    '                self._run_adb(args, timeout=10)\n'
    '            except Exception:\n'
    '                pass\n'
    '        self._log("Guest wm size/density overrides reset")\n'
)

OLD_BOOT = (
    '                self._progress(100, "Emulator ready")\n'
    '                self._disable_guest_animations()\n'
)

NEW_BOOT = (
    '                self._progress(100, "Emulator ready")\n'
    '                self._reset_guest_overrides()\n'
    '                self._disable_guest_animations()\n'
)

for old, new, label in ((OLD_START, NEW_START, "start_once"),
                        (OLD_ANIM, NEW_ANIM, "animations"),
                        (OLD_BOOT, NEW_BOOT, "boot")):
    if old not in s:
        raise SystemExit(f"NOT FOUND: {label}")
    s = s.replace(old, new, 1)

P.write_text(s)
print("patched core/emulator.py")
