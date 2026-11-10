"""Hardware profiling and performance mode selection for ApkPreview."""

import psutil
import platform
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol
from .platform_utils import PlatformUtils


class PerformanceMode(Enum):
    """Performance mode for the emulator."""
    LOW_SPEC = "low_spec"
    NORMAL = "normal"
    HIGH = "high"
    NOT_SUPPORTED = "not_supported"

    @classmethod
    def from_setting(cls, value: str) -> Optional["PerformanceMode"]:
        """Map a persisted/manual setting to a mode.

        ``auto`` (and unknown values) return ``None`` so the caller can use
        hardware detection.  ``low`` is accepted as an alias for the historical
        ``low_spec`` value.
        """
        mapping = {
            "low": cls.LOW_SPEC,
            "low_spec": cls.LOW_SPEC,
            "normal": cls.NORMAL,
            "high": cls.HIGH,
        }
        return mapping.get(str(value or "").strip().lower())

    @property
    def label(self) -> str:
        return {
            self.LOW_SPEC: "Low",
            self.NORMAL: "Normal",
            self.HIGH: "High",
            self.NOT_SUPPORTED: "Not supported",
        }.get(self, self.value.replace("_", " ").title())


class CompatibilityVerdict(Enum):
    """Compatibility check verdict."""
    GOOD = "good"
    LOW_SPEC = "low_spec"
    WARNING = "warning"  # Low disk but can continue
    NOT_SUPPORTED = "not_supported"


# Protocol for SDK manager to avoid circular import
class SDKManagerProtocol(Protocol):
    def get_installed_packages(self) -> list[str]: ...
    def get_required_packages(self, mode: PerformanceMode) -> list[str]: ...
    def is_sdk_installed(self) -> bool: ...
    def are_licenses_accepted(self) -> bool: ...
    def is_avd_created(self, name: str) -> bool: ...
    def is_scrcpy_installed(self) -> bool: ...


@dataclass
class HardwareProfile:
    """Hardware profile of the host machine."""
    os: str
    os_version: str
    cpu_arch: str
    cpu_physical_cores: int
    cpu_logical_cores: int
    total_ram_mb: int
    available_ram_mb: int
    free_disk_gb: int
    virtualization_supported: bool
    virtualization_details: dict
    required_disk_gb: int = 0  # Space needed for remaining downloads
    
    @property
    def total_ram_gb(self) -> float:
        return self.total_ram_mb / 1024
    
    @property
    def available_ram_gb(self) -> float:
        return self.available_ram_mb / 1024


@dataclass
class EmulatorConfig:
    """Emulator configuration based on performance mode."""
    api_level: int
    memory_mb: int
    cores: int
    gpu_mode: str
    screen_width: int
    screen_height: int
    density: int
    no_audio: bool = True
    no_boot_anim: bool = True
    no_snapshot_save: bool = True
    no_window: bool = False
    extra_args: list = None
    
    def __post_init__(self):
        if self.extra_args is None:
            self.extra_args = []


class HardwareProfiler:
    """Detects hardware capabilities and selects optimal performance mode."""
    
    # Minimum requirements (hard blocks)
    MIN_RAM_MB = 4096  # 4 GB
    MIN_CORES = 2
    
    # Thresholds for mode selection
    LOW_SPEC_RAM_THRESHOLD_MB = 8192  # 8 GB
    LOW_SPEC_CORES_THRESHOLD = 4
    # High is opt-in (manual override) on very strong machines.  Auto mode
    # stays on Normal for typical 8-16 GB laptops.
    HIGH_RAM_THRESHOLD_MB = 16384
    HIGH_CORES_THRESHOLD = 8
    
    # Disk warning threshold
    LOW_DISK_WARNING_GB = 5
    
    def __init__(self, sdk_manager: Optional[SDKManagerProtocol] = None):
        self._sdk_manager = sdk_manager
        self._profile: Optional[HardwareProfile] = None
        self._mode: Optional[PerformanceMode] = None
        self._verdict: Optional[CompatibilityVerdict] = None
        self._reasons: list = []
        self._warnings: list = []
    
    def profile(self) -> HardwareProfile:
        """Run full hardware detection and return profile."""
        if self._profile is not None:
            return self._profile
        
        cpu_info = PlatformUtils.get_cpu_info()
        mem_info = PlatformUtils.get_memory_info()
        disk_info = PlatformUtils.get_disk_info()
        virt_info = PlatformUtils.check_virtualization()
        
        # Compute required disk space from what's actually missing
        required_disk_gb = 0
        if self._sdk_manager:
            required_disk_gb = self._compute_required_disk_gb()
        
        self._profile = HardwareProfile(
            os=platform.system(),
            os_version=PlatformUtils.get_os_version(),
            cpu_arch=cpu_info["architecture"],
            cpu_physical_cores=cpu_info["physical_cores"] or 0,
            cpu_logical_cores=cpu_info["logical_cores"] or 0,
            total_ram_mb=mem_info["total_mb"],
            available_ram_mb=mem_info["available_mb"],
            free_disk_gb=disk_info["free_gb"],
            virtualization_supported=virt_info["supported"],
            virtualization_details=virt_info,
            required_disk_gb=required_disk_gb,
        )
        
        self._evaluate_compatibility()
        self._select_mode()
        
        return self._profile
    
    def _compute_required_disk_gb(self) -> int:
        """Compute required disk space from missing components."""
        if not self._sdk_manager:
            return 0
        
        # Estimate mode based on current profile's RAM/CPU (not disk)
        temp_mode = PerformanceMode.NORMAL
        if (self._profile and (
            self._profile.total_ram_mb <= self.LOW_SPEC_RAM_THRESHOLD_MB or 
            self._profile.cpu_logical_cores <= self.LOW_SPEC_CORES_THRESHOLD)):
            temp_mode = PerformanceMode.LOW_SPEC
        
        # Get missing components
        installed = self._sdk_manager.get_installed_packages()
        required = self._sdk_manager.get_required_packages(temp_mode)
        missing = [pkg for pkg in required if pkg not in installed]
        
        # Check other components.  No system image or AVD is part of base setup:
        # the Android version is chosen (and downloaded) later by the user.
        needs_cmdline = not self._sdk_manager.is_sdk_installed()
        needs_scrcpy = not self._sdk_manager.is_scrcpy_installed()
        
        # Size estimates in MB
        sizes = {
            "cmdline_tools": 200,
            "platform-tools": 50,
            "emulator": 300,
            "scrcpy": 30,
        }
        
        total_mb = 0
        if needs_cmdline:
            total_mb += sizes["cmdline_tools"]
        
        for pkg in missing:
            if "platform-tools" in pkg:
                total_mb += sizes["platform-tools"]
            elif "emulator" in pkg:
                total_mb += sizes["emulator"]
        
        if needs_scrcpy:
            total_mb += sizes["scrcpy"]
        
        # Add 2x for extraction overhead
        total_mb *= 2
        
        # Convert to GB, round up
        return max(1, (total_mb + 1023) // 1024)
    
    def _evaluate_compatibility(self):
        """Evaluate compatibility and set verdict with reasons."""
        self._reasons = []
        self._warnings = []
        p = self._profile
        
        # HARD BLOCKS: Virtualization and RAM
        # Check virtualization
        if not p.virtualization_supported:
            self._verdict = CompatibilityVerdict.NOT_SUPPORTED
            self._reasons.append(p.virtualization_details.get("message", "Virtualization not available"))
            return
        
        # Check minimum RAM
        if p.total_ram_mb < self.MIN_RAM_MB:
            self._verdict = CompatibilityVerdict.NOT_SUPPORTED
            self._reasons.append(f"Insufficient RAM: {p.total_ram_mb} MB (minimum {self.MIN_RAM_MB} MB)")
            return
        
        # Check minimum cores
        if p.cpu_logical_cores < self.MIN_CORES:
            self._verdict = CompatibilityVerdict.NOT_SUPPORTED
            self._reasons.append(f"Insufficient CPU cores: {p.cpu_logical_cores} (minimum {self.MIN_CORES})")
            return
        
        # DISK: Never hard-block. Check if free space < required.
        if p.free_disk_gb < p.required_disk_gb:
            self._verdict = CompatibilityVerdict.WARNING
            self._warnings.append(
                f"Low disk space: {p.free_disk_gb} GB free, {p.required_disk_gb} GB needed for remaining downloads. "
                "Free up space or installation may fail."
            )
        elif p.free_disk_gb < self.LOW_DISK_WARNING_GB:
            self._verdict = CompatibilityVerdict.WARNING
            self._warnings.append(
                f"Low disk space: {p.free_disk_gb} GB free. 5 GB+ recommended for smooth operation."
            )
        
        # Check for low-spec (based on RAM/CPU only)
        is_low_spec = (
            p.total_ram_mb <= self.LOW_SPEC_RAM_THRESHOLD_MB or
            p.cpu_logical_cores <= self.LOW_SPEC_CORES_THRESHOLD
        )
        
        if is_low_spec:
            if self._verdict != CompatibilityVerdict.WARNING:
                self._verdict = CompatibilityVerdict.LOW_SPEC
            reasons = []
            if p.total_ram_mb <= self.LOW_SPEC_RAM_THRESHOLD_MB:
                reasons.append(f"RAM {p.total_ram_mb} MB ≤ {self.LOW_SPEC_RAM_THRESHOLD_MB} MB")
            if p.cpu_logical_cores <= self.LOW_SPEC_CORES_THRESHOLD:
                reasons.append(f"CPU cores {p.cpu_logical_cores} ≤ {self.LOW_SPEC_CORES_THRESHOLD}")
            self._reasons.append("Low-spec hardware detected: " + "; ".join(reasons))
        else:
            if self._verdict != CompatibilityVerdict.WARNING:
                self._verdict = CompatibilityVerdict.GOOD
            self._reasons.append("Hardware meets recommended specifications")
        
        # Add warnings to reasons for display
        self._reasons.extend(self._warnings)
    
    def _select_mode(self):
        """Automatically select performance mode based on hardware (RAM/CPU only)."""
        p = self._profile
        
        # Mode is based on RAM/CPU, NOT disk
        is_low_spec = (
            p.total_ram_mb <= self.LOW_SPEC_RAM_THRESHOLD_MB or
            p.cpu_logical_cores <= self.LOW_SPEC_CORES_THRESHOLD
        )
        
        if self._verdict == CompatibilityVerdict.NOT_SUPPORTED:
            self._mode = PerformanceMode.NOT_SUPPORTED
        elif is_low_spec:
            self._mode = PerformanceMode.LOW_SPEC
        elif (p.total_ram_mb > self.HIGH_RAM_THRESHOLD_MB
              and p.cpu_logical_cores > self.HIGH_CORES_THRESHOLD):
            self._mode = PerformanceMode.HIGH
        else:
            self._mode = PerformanceMode.NORMAL
    
    def get_mode(self) -> PerformanceMode:
        """Get the selected performance mode."""
        if self._mode is None:
            self.profile()
        return self._mode
    
    def get_verdict(self) -> CompatibilityVerdict:
        """Get the compatibility verdict."""
        if self._verdict is None:
            self.profile()
        return self._verdict
    
    def get_reasons(self) -> list:
        """Get the reasons for the verdict."""
        if not self._reasons:
            self.profile()
        return self._reasons
    
    def get_warnings(self) -> list:
        """Get warnings (non-blocking issues)."""
        if not self._warnings:
            self.profile()
        return self._warnings
    
    def can_proceed(self) -> bool:
        """Check if user can proceed (only hard blocks prevent)."""
        return self.get_verdict() != CompatibilityVerdict.NOT_SUPPORTED
    
    def get_emulator_config(self, mode: Optional[PerformanceMode] = None,
                            low_power: bool = False) -> EmulatorConfig:
        """Get emulator configuration for the given mode.

        ``low_power`` applies a battery-saver override on top of the selected
        profile: one core, 1 GB RAM, software GPU and a small screen.  The
        preview layer also lowers its frame rate when this is enabled.
        """
        if mode is None:
            mode = self.get_mode()

        if low_power:
            return EmulatorConfig(
                api_level=30,
                memory_mb=1024,
                cores=1,
                gpu_mode="swiftshader_indirect",
                screen_width=720,
                screen_height=1280,
                density=280,
                no_audio=True,
                no_boot_anim=True,
                no_snapshot_save=True,
                no_window=False,
            )

        if mode == PerformanceMode.LOW_SPEC:
            # Android 10/11 (API 29/30) - lighter images
            api_level = 30  # Android 11
            config = EmulatorConfig(
                api_level=api_level,
                memory_mb=1024,
                cores=1,
                gpu_mode="swiftshader_indirect",
                screen_width=720,
                screen_height=1280,
                density=280,
                no_audio=True,
                no_boot_anim=True,
                no_snapshot_save=True,
                no_window=True,  # Run headless, show via scrcpy
            )
        elif mode == PerformanceMode.NORMAL:
            # Android 14 (API 34)
            api_level = 34
            config = EmulatorConfig(
                api_level=api_level,
                memory_mb=2048,
                cores=2,
                gpu_mode="host",
                screen_width=1080,
                screen_height=1920,
                density=420,
                no_audio=True,
                no_boot_anim=True,
                no_snapshot_save=True,
                no_window=False,
            )
        elif mode == PerformanceMode.HIGH:
            # Opt-in profile for strong desktops.  Cap cores at 4 so the host
            # stays responsive; the user chose this explicitly.
            config = EmulatorConfig(
                api_level=34,
                memory_mb=4096,
                cores=4,
                gpu_mode="host",
                screen_width=1440,
                screen_height=2560,
                density=560,
                no_audio=True,
                no_boot_anim=True,
                no_snapshot_save=True,
                no_window=False,
            )
        else:
            # Not supported - return minimal config
            config = EmulatorConfig(
                api_level=30,
                memory_mb=512,
                cores=1,
                gpu_mode="swiftshader_indirect",
                screen_width=720,
                screen_height=1280,
                density=280,
            )
        
        # NOTE: the system image is no longer baked into the hardware config.
        # The Android version (and therefore its image) is chosen by the user and
        # passed explicitly when the AVD is created.

        return config
    
    def get_compatibility_summary(self) -> dict:
        """Get a summary for display in the UI."""
        self.profile()
        return {
            "verdict": self._verdict.value,
            "mode": self._mode.value,
            "reasons": self._reasons,
            "warnings": self._warnings,
            "can_proceed": self.can_proceed(),
            "profile": {
                "os": self._profile.os,
                "os_version": self._profile.os_version,
                "cpu_arch": self._profile.cpu_arch,
                "cpu_cores": f"{self._profile.cpu_physical_cores}P / {self._profile.cpu_logical_cores}L",
                "total_ram_gb": round(self._profile.total_ram_gb, 1),
                "available_ram_gb": round(self._profile.available_ram_gb, 1),
                "free_disk_gb": self._profile.free_disk_gb,
                "required_disk_gb": self._profile.required_disk_gb,
                "virtualization": "Yes" if self._profile.virtualization_supported else "No",
            }
        }
    
    def override_mode(self, mode: PerformanceMode):
        """Allow user to override the auto-selected mode."""
        self._mode = mode


def get_default_config_for_mode(mode: PerformanceMode) -> EmulatorConfig:
    """Get default emulator config for a mode (without hardware profiling)."""
    profiler = HardwareProfiler()
    # Create a minimal profile for config generation
    profiler._profile = HardwareProfile(
        os=platform.system(),
        os_version=PlatformUtils.get_os_version(),
        cpu_arch=PlatformUtils.get_arch(),
        cpu_physical_cores=psutil.cpu_count(logical=False) or 2,
        cpu_logical_cores=psutil.cpu_count(logical=True) or 4,
        total_ram_mb=8192,
        available_ram_mb=4096,
        free_disk_gb=20,
        virtualization_supported=True,
        virtualization_details={},
    )
    return profiler.get_emulator_config(mode)