"""Device presets for ApkPreview - built-in plus avdmanager-defined devices.

The preset list is normally built from ``avdmanager list device`` (phones,
tablets and foldables; Wear OS / TV / Automotive are skipped) and cached in
``~/.apkpreview/devices.json``.  When avdmanager is missing or fails, the
built-in presets are used instead.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .adb import ADB
from .platform_utils import PlatformUtils


CATEGORY_PHONE = "Phone"
CATEGORY_TABLET = "Tablet"
CATEGORY_FOLDABLE = "Foldable"
# The order categories are shown in the preset combos.
CATEGORY_ORDER = (CATEGORY_PHONE, CATEGORY_TABLET, CATEGORY_FOLDABLE)

# The synthetic "emulator default" preset always exists.
DEFAULT_PRESET_KEY = "default"

# Resolution/density used when a device definition does not report them.
_CATEGORY_DEFAULT_SPEC = {
    CATEGORY_PHONE: (1080, 1920, 420),
    CATEGORY_TABLET: (1600, 2560, 320),
    CATEGORY_FOLDABLE: (1768, 2208, 380),
}

# Well-known specs (avdmanager does not report resolution/density on every
# platform), keyed by device-definition id or display name.
_KNOWN_SPECS: Dict[str, tuple] = {
    "Nexus One": (480, 800, 254),
    "Nexus S": (480, 800, 233),
    "Galaxy Nexus": (720, 1280, 320),
    "Nexus 4": (768, 1280, 320),
    "Nexus 5": (1080, 1920, 445),
    "Nexus 5X": (1080, 1920, 420),
    "Nexus 6": (1440, 2560, 560),
    "Nexus 6P": (1440, 2560, 515),
    "Nexus 7": (1200, 1920, 320),
    "Nexus 7 2013": (1200, 1920, 320),
    "Nexus 9": (1536, 2048, 320),
    "Nexus 10": (2560, 1600, 300),
    "pixel": (1080, 1920, 420),
    "pixel_xl": (1440, 2560, 560),
    "pixel_2": (1080, 1920, 420),
    "pixel_2_xl": (1440, 2560, 560),
    "pixel_3": (1080, 2160, 440),
    "pixel_3_xl": (1080, 2160, 400),
    "pixel_3a": (1080, 2220, 440),
    "pixel_3a_xl": (1080, 2160, 400),
    "pixel_4": (1080, 2280, 440),
    "pixel_4_xl": (1440, 3040, 560),
    "pixel_4a": (1080, 2340, 443),
    "pixel_5": (1080, 2340, 440),
    "pixel_6": (1080, 2400, 420),
    "pixel_6_pro": (1440, 3120, 512),
    "pixel_6a": (1080, 2400, 420),
    "pixel_7": (1080, 2400, 420),
    "pixel_7_pro": (1440, 3120, 512),
    "pixel_c": (2560, 1800, 308),
    "pixel_tablet": (2560, 1600, 320),
    "medium_phone": (1080, 2400, 420),
    "medium_tablet": (1600, 2560, 320),
    "small_phone": (720, 1280, 320),
    "resizable": (1080, 1920, 420),
}


@dataclass
class DevicePreset:
    """Device preset configuration."""

    name: str
    width: int
    height: int
    density: int
    description: str = ""
    category: str = "Phone"
    key: str = ""

    @property
    def size_str(self) -> str:
        return f"{self.width}x{self.height}"

    def apply(self, adb: ADB) -> tuple[bool, str]:
        """Apply this preset to the device."""
        success, msg = adb.set_screen_size(self.width, self.height)
        if not success:
            return False, f"Failed to set screen size: {msg}"

        success, msg = adb.set_density(self.density)
        if not success:
            return False, f"Failed to set density: {msg}"

        return True, f"Applied preset: {self.name} ({self.size_str} @ {self.density} dpi)"

    def reset(self, adb: ADB) -> tuple[bool, str]:
        """Reset to device defaults."""
        results = []
        success, msg = adb.reset_screen_size()
        results.append(msg)
        success, msg = adb.reset_density()
        results.append(msg)
        return True, "; ".join(results)


# Built-in device presets (used until avdmanager supplies a live list, and as a
# fallback whenever avdmanager is missing or fails).
BUILTIN_PRESETS: Dict[str, DevicePreset] = {
    "default": DevicePreset(
        name="Default (Emulator)",
        width=1080,
        height=1920,
        density=420,
        description="Default emulator configuration",
        category="Phone",
        key="default",
    ),
    "pixel7": DevicePreset(
        name="Pixel 7",
        width=1080,
        height=2400,
        density=420,
        description="Google Pixel 7 (6.3\")",
        category="Phone",
        key="pixel7",
    ),
    "pixel7pro": DevicePreset(
        name="Pixel 7 Pro",
        width=1440,
        height=3120,
        density=512,
        description="Google Pixel 7 Pro (6.7\")",
        category="Phone",
        key="pixel7pro",
    ),
    "small_phone": DevicePreset(
        name="Small Phone",
        width=720,
        height=1280,
        density=280,
        description="Small phone (6.0\")",
        category="Phone",
        key="small_phone",
    ),
    "large_phone": DevicePreset(
        name="Large Phone",
        width=1440,
        height=3120,
        density=512,
        description="Large phone (6.7\")",
        category="Phone",
        key="large_phone",
    ),
    "iphone_se": DevicePreset(
        name="Small iPhone",
        width=750,
        height=1334,
        density=326,
        description="Small phone (4.7\")",
        category="Phone",
        key="iphone_se",
    ),
    "iphone_14": DevicePreset(
        name="iPhone 14",
        width=1170,
        height=2532,
        density=460,
        description="iPhone 14 (6.1\")",
        category="Phone",
        key="iphone_14",
    ),
    "iphone_14_plus": DevicePreset(
        name="iPhone 14 Plus",
        width=1284,
        height=2778,
        density=458,
        description="iPhone 14 Plus (6.7\")",
        category="Phone",
        key="iphone_14_plus",
    ),
    "tablet_7": DevicePreset(
        name="Nexus 7",
        width=800,
        height=1280,
        density=213,
        description="7-inch tablet (Nexus 7 style)",
        category="Tablet",
        key="tablet_7",
    ),
    "tablet_10": DevicePreset(
        name="Pixel Tablet",
        width=1600,
        height=2560,
        density=320,
        description="10-inch tablet",
        category="Tablet",
        key="tablet_10",
    ),
    "tablet_11": DevicePreset(
        name="11\" Tablet",
        width=1668,
        height=2388,
        density=264,
        description="11-inch tablet",
        category="Tablet",
        key="tablet_11",
    ),
    "tablet_12_9": DevicePreset(
        name="12.9\" Tablet",
        width=2048,
        height=2732,
        density=264,
        description="12.9-inch tablet",
        category="Tablet",
        key="tablet_12_9",
    ),
    "foldable_inner": DevicePreset(
        name="Foldable Inner",
        width=1812,
        height=2176,
        density=374,
        description="Foldable inner screen (Galaxy Z Fold)",
        category="Foldable",
        key="foldable_inner",
    ),
    "foldable_outer": DevicePreset(
        name="Foldable Outer",
        width=832,
        height=2268,
        density=387,
        description="Foldable outer screen (Galaxy Z Fold)",
        category="Foldable",
        key="foldable_outer",
    ),
}


# ----------------------------------------------------------------------------
# avdmanager parsing
# ----------------------------------------------------------------------------
def _iter_device_blocks(text: str):
    """Yield ``(id, fields)`` for each device definition in avdmanager output."""
    key = None
    fields: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        m = re.match(r'^\s*id:\s*\d+\s+or\s+"([^"]+)"', line)
        if m:
            if key is not None:
                yield key, fields
            key = m.group(1)
            fields = {}
            continue

        if key is None:
            continue

        if set(stripped) <= {"-"} and len(stripped) >= 3:
            yield key, fields
            key = None
            fields = {}
            continue

        fm = re.match(r"^\s+(.*?)\s*:\s*(.*)$", line)
        if fm:
            fields[fm.group(1).strip().lower()] = fm.group(2).strip()

    if key is not None:
        yield key, fields


def _categorize(key: str, name: str, tag: str) -> Optional[str]:
    """Return ``Phone``/``Tablet``/``Foldable``, or ``None`` to exclude."""
    text = f"{key} {name} {tag}".lower()
    if any(token in text for token in ("wear", "automotive", "desktop")):
        return None
    if re.search(r"(^|[^a-z])tv([^a-z]|$)", text):
        return None

    if "fold" in text:
        return "Foldable"
    if "tablet" in text or re.search(r"(^|[^a-z])pad([^a-z]|$)", text):
        return "Tablet"
    if key in ("pixel_c",):
        return "Tablet"
    if re.match(r"nexus (7|9|10)\b", name.lower()):
        return "Tablet"
    if re.search(r"\b(7|10)in\b.*tablet", text):
        return "Tablet"
    return "Phone"


def _spec_from_fields(fields: Dict[str, str]):
    """Best-effort (width, height, density) from device detail fields."""
    width = height = density = None

    res = (fields.get("resolution") or fields.get("screen.size")
           or fields.get("screensize") or "")
    m = re.search(r"(\d+)\s*[xX*]\s*(\d+)", res)
    if m:
        width, height = int(m.group(1)), int(m.group(2))

    def _as_int(value):
        mm = re.search(r"(\d+)", value or "")
        return int(mm.group(1)) if mm else None

    width = width or _as_int(fields.get("hw.lcd.width", ""))
    height = height or _as_int(fields.get("hw.lcd.height", ""))
    density = _as_int(fields.get("hw.lcd.density", "")) \
        or _as_int(fields.get("density", "")) \
        or _as_int(fields.get("dpi", ""))
    return width, height, density


def _resolve_spec(key: str, name: str, fields: Dict[str, str], category: str):
    """Fill missing width/height/density from known specs then category defaults."""
    width, height, density = _spec_from_fields(fields)
    for lookup in (key, name):
        if lookup in _KNOWN_SPECS:
            kw, kh, kd = _KNOWN_SPECS[lookup]
            width = width or kw
            height = height or kh
            density = density or kd
    dw, dh, dd = _CATEGORY_DEFAULT_SPEC.get(category, (1080, 1920, 420))
    return width or dw, height or dh, density or dd


def _describe(fields: Dict[str, str], category: str) -> str:
    oem = fields.get("oem", "").strip()
    return f"{oem} {category}".strip() or category


def parse_avdmanager_devices(detail_output: str,
                             compact_output: str = "") -> Dict[str, DevicePreset]:
    """Parse ``avdmanager list device`` (+ ``-c``) into ``{key: DevicePreset}``.

    Phones, tablets and foldables are kept; Wear OS, TV, Automotive and Desktop
    definitions are dropped.  Resolution/density come from the detail output
    when present, otherwise from a known-specs table and finally from a
    per-category default.
    """
    presets: Dict[str, DevicePreset] = {}

    for key, fields in _iter_device_blocks(detail_output or ""):
        name = fields.get("name") or key
        tag = fields.get("tag", "")
        category = _categorize(key, name, tag)
        if category is None:
            continue
        width, height, density = _resolve_spec(key, name, fields, category)
        presets[key] = DevicePreset(
            name=name, width=width, height=height, density=density,
            description=_describe(fields, category),
            category=category, key=key,
        )

    # If the detail output produced nothing, fall back to the compact list.
    if not presets and compact_output:
        for raw in compact_output.splitlines():
            key = raw.strip()
            if not key:
                continue
            category = _categorize(key, key, "")
            if category is None:
                continue
            name = key.replace("_", " ").title()
            width, height, density = _resolve_spec(key, name, {}, category)
            presets[key] = DevicePreset(
                name=name, width=width, height=height, density=density,
                description=category, category=category, key=key,
            )

    return presets


def _devices_cache_path() -> Path:
    return PlatformUtils.get_app_data_dir() / "devices.json"


def save_devices_cache(presets: Dict[str, DevicePreset]) -> bool:
    """Persist parsed presets to ``~/.apkpreview/devices.json``."""
    try:
        data = {"devices": [
            {
                "key": preset.key,
                "name": preset.name,
                "width": preset.width,
                "height": preset.height,
                "density": preset.density,
                "description": preset.description,
                "category": preset.category,
            }
            for preset in presets.values()
        ]}
        path = _devices_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def load_devices_cache() -> Optional[Dict[str, DevicePreset]]:
    """Read cached presets, or ``None`` when absent/corrupt."""
    path = _devices_cache_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        presets: Dict[str, DevicePreset] = {}
        for entry in data.get("devices", []):
            preset = DevicePreset(
                name=entry["name"],
                width=int(entry["width"]),
                height=int(entry["height"]),
                density=int(entry["density"]),
                description=entry.get("description", ""),
                category=entry.get("category", "Phone"),
                key=entry.get("key", entry["name"]),
            )
            presets[preset.key] = preset
        return presets or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_avdmanager_presets(timeout: int = 60) -> Optional[Dict[str, DevicePreset]]:
    """Run avdmanager, parse the device list and refresh the cache.

    Returns ``None`` when avdmanager is missing/fails/returns nothing so the
    caller keeps using the built-in presets.
    """
    avdmanager = PlatformUtils.get_avdmanager_path()
    if not avdmanager.exists():
        return None

    env = {
        **os.environ,
        "PATH": (str(PlatformUtils.get_sdk_dir() / "platform-tools")
                 + os.pathsep + os.environ.get("PATH", "")),
    }
    try:
        detail = subprocess.run(
            [str(avdmanager), "list", "device"],
            capture_output=True, text=True, timeout=timeout, env=env)
        compact = subprocess.run(
            [str(avdmanager), "list", "device", "-c"],
            capture_output=True, text=True, timeout=timeout, env=env)
    except Exception:
        return None

    presets = parse_avdmanager_devices(detail.stdout or "",
                                       compact.stdout or "")
    if not presets:
        return None
    save_devices_cache(presets)
    return presets


class DevicePresetManager:
    """Manages device presets - built-in, avdmanager-provided and custom."""

    def __init__(self):
        self._presets = BUILTIN_PRESETS.copy()
        self._custom_presets: Dict[str, DevicePreset] = {}
        self._dynamic: Dict[str, DevicePreset] = {}

    # -- preset lookup ---------------------------------------------------
    def get_preset(self, name: str) -> Optional[DevicePreset]:
        """Get a preset by name (custom -> dynamic -> built-in)."""
        if name in self._custom_presets:
            return self._custom_presets[name]
        if name in self._dynamic:
            return self._dynamic[name]
        return self._presets.get(name)

    def get_all_presets(self) -> Dict[str, DevicePreset]:
        """All presets, with avdmanager-provided ones replacing the built-ins."""
        if self._dynamic:
            combined = {DEFAULT_PRESET_KEY: self._presets["default"]}
            combined.update(self._dynamic)
        else:
            combined = self._presets.copy()
        combined.update(self._custom_presets)
        return combined

    def get_grouped_presets(self):
        """``[(category, [(key, preset), ...]), ...]`` for grouped combos."""
        grouped: Dict[str, list] = {c: [] for c in CATEGORY_ORDER}
        for key, preset in self.get_all_presets().items():
            category = (preset.category
                        if preset.category in CATEGORY_ORDER else "Phone")
            grouped.setdefault(category, []).append((key, preset))

        grouped_result = []
        for category in CATEGORY_ORDER:
            items = sorted(grouped.get(category, []),
                           key=lambda kv: kv[1].name.lower())
            if items:
                grouped_result.append((category, items))
        return grouped_result

    def get_builtin_presets(self) -> Dict[str, DevicePreset]:
        """Get only built-in presets."""
        return self._presets.copy()

    def get_custom_presets(self) -> Dict[str, DevicePreset]:
        """Get only custom presets."""
        return self._custom_presets.copy()

    # -- avdmanager-provided presets ------------------------------------
    def set_dynamic_presets(self, presets: Optional[Dict[str, DevicePreset]]):
        """Adopt the avdmanager-provided list (after a worker finishes)."""
        if presets:
            self._dynamic = dict(presets)

    def clear_dynamic_presets(self):
        self._dynamic = {}

    def load_cache(self) -> Optional[Dict[str, DevicePreset]]:
        """Load avdmanager presets cached from a previous run, if any."""
        cached = load_devices_cache()
        if cached:
            self._dynamic = cached
        return cached

    # -- custom presets --------------------------------------------------
    def add_custom_preset(self, name: str, width: int, height: int,
                          density: int, description: str = "") -> bool:
        """Add a custom preset."""
        key = name.lower().replace(" ", "_")
        self._custom_presets[key] = DevicePreset(
            name=name, width=width, height=height, density=density,
            description=description, category="Phone", key=key)
        return True

    def remove_custom_preset(self, name: str) -> bool:
        """Remove a custom preset."""
        key = name.lower().replace(" ", "_")
        if key in self._custom_presets:
            del self._custom_presets[key]
            return True
        return False

    def is_custom(self, name: str) -> bool:
        key = name.lower().replace(" ", "_")
        return key in self._custom_presets

    # -- application -----------------------------------------------------
    def apply_preset(self, adb: ADB, name: str) -> tuple[bool, str]:
        preset = self.get_preset(name)
        if preset is None:
            return False, f"Unknown preset: {name}"
        return preset.apply(adb)

    def reset_to_default(self, adb: ADB) -> tuple[bool, str]:
        """Reset to default (emulator) preset."""
        return self.apply_preset(adb, "default")

    def get_current_config(self, adb: ADB) -> Optional[DevicePreset]:
        """Get current device configuration as a preset."""
        size = adb.get_screen_size()
        density = adb.get_density()
        if size and density:
            return DevicePreset(
                name="Current", width=size[0], height=size[1],
                density=density, description="Current device configuration",
                category="Phone", key="current")
        return None

    def get_all_names(self) -> Dict[str, str]:
        """Map of key -> display name for every preset."""
        return {key: preset.name
                for key, preset in self.get_all_presets().items()}

    def get_preset_names(self) -> List[str]:
        """Get list of all preset keys for UI."""
        names = list(self.get_all_presets().keys())
        names.extend(self._custom_presets.keys())
        return sorted(set(names))


# Global instance
preset_manager = DevicePresetManager()
