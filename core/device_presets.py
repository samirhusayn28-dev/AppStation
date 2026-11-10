"""Bundled device presets for ApkPreview.

The device list is a fixed, bundled data file (``core/data/devices.json``),
grouped Brand > Category (Phone / Tablet / Foldable).  ``avdmanager list
device`` is deliberately *not* used any more: its list changes with the
installed SDK, omits resolution/density on many platforms and produced a
different picker on every machine.

Density is not stored in the data file.  It is computed at load time with a
small ppi/dp rule:

* Apple: the resolution is mapped to the real logical dp width (for example
  1170 px / 390 dp => 480 dpi), then snapped to the nearest Android density
  bucket.
* Android phones: an approximate physical diagonal is picked from the
  resolution tier and ``ppi = hypot(w, h) / diagonal`` is snapped to a bucket.
* Tablets: 10.5" class diagonal; foldable inner screens 6.7" (flip) or 7.8"
  (book-style fold), cover screens 6.3" (book) or 1.9" (flip cover).

Apple entries are **viewport/frame previews only**: the Android emulator can
run an Android image at an iPhone/iPad resolution, but it cannot run iOS apps.
The UI says so explicitly.
"""

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .adb import ADB


CATEGORY_PHONE = "Phone"
CATEGORY_TABLET = "Tablet"
CATEGORY_FOLDABLE = "Foldable"
CATEGORY_ORDER = (CATEGORY_PHONE, CATEGORY_TABLET, CATEGORY_FOLDABLE)

BRAND_ORDER = ("Apple", "Samsung", "Google")

# Kept for backwards compatibility with callers that used the synthetic
# "default" key.  There is no "default" device in the bundled data; the Reset
# button performs an adb reset instead.
DEFAULT_PRESET_KEY = "default"

CUTOUT_HOME_BUTTON = "home-button"
CUTOUT_NOTCH = "notch"
CUTOUT_DYNAMIC_ISLAND = "dynamic-island"
CUTOUT_PUNCH_HOLE = "punch-hole"
CUTOUT_NONE = "none"
CUTOUT_BEZEL = "bezel"

CUTOUTS = (CUTOUT_HOME_BUTTON, CUTOUT_NOTCH, CUTOUT_DYNAMIC_ISLAND,
           CUTOUT_PUNCH_HOLE, CUTOUT_NONE, CUTOUT_BEZEL)

CUTOUT_LABELS = {
    CUTOUT_HOME_BUTTON: "Home button",
    CUTOUT_NOTCH: "Notch",
    CUTOUT_DYNAMIC_ISLAND: "Dynamic Island",
    CUTOUT_PUNCH_HOLE: "Punch-hole",
    CUTOUT_NONE: "No cutout",
    CUTOUT_BEZEL: "Bezel",
}

# Android density buckets (dpi).
DENSITY_BUCKETS = (120, 160, 213, 240, 280, 320, 360, 400, 420, 440, 480,
                   520, 560, 600, 640)

# Apple resolution -> logical dp width.  These are the real viewport widths,
# so a preview keeps the same layout width as the physical device.
_APPLE_DP: Dict[Tuple[int, int], int] = {
    (640, 1136): 320,    # SE 1st gen
    (750, 1334): 375,    # 6 / 6s / 7 / 8 / SE 2 / SE 3
    (1080, 1920): 414,   # 6 Plus / 6s Plus / 7 Plus / 8 Plus
    (1125, 2436): 375,   # X / XS / 11 Pro
    (1242, 2688): 414,   # XS Max / 11 Pro Max
    (828, 1792): 414,    # XR / 11
    (1080, 2340): 375,   # 12 mini / 13 mini
    (1170, 2532): 390,   # 12 / 12 Pro / 13 / 13 Pro / 14 / 16e / 17e
    (1284, 2778): 428,   # 12 Pro Max / 13 Pro Max / 14 Plus
    (1179, 2556): 393,   # 14 Pro / 15 / 15 Pro / 16
    (1290, 2796): 430,   # 14 Pro Max / 15 Plus / 15 Pro Max / 16 Plus
    (1206, 2622): 402,   # 16 Pro / 17 / 17 Pro / 18 Pro
    (1320, 2868): 440,   # 16 Pro Max / 17 Pro Max / 18 Pro Max
    (1260, 2736): 420,   # iPhone Air
    (1920, 2713): 640,   # Foldable iPhone inner (rumored)
    (1422, 2088): 480,   # Foldable iPhone outer (rumored)
    (1536, 2048): 768,   # iPad 5/6, Air 2, mini 4/5, Pro 9.7
    (1620, 2160): 810,   # iPad 7/8/9
    (1640, 2360): 820,   # iPad 10/11, Air 4/5, Air 11
    (1668, 2224): 834,   # Air 3, Pro 10.5
    (1488, 2266): 744,   # mini 6/7
    (1668, 2388): 834,   # Pro 11 1st-4th
    (1668, 2420): 834,   # Pro 11 M4/M5
    (2048, 2732): 1024,  # Air 13, Pro 12.9
    (2064, 2752): 1032,  # Pro 13 M4/M5
}


def _snap_density(dpi: float) -> int:
    """Snap a raw dpi value to the nearest Android density bucket."""
    return min(DENSITY_BUCKETS, key=lambda bucket: abs(bucket - dpi))


def compute_density(width_px: int, height_px: int, category: str,
                    brand: str, screen: str = "main") -> int:
    """Compute a sensible Android density (dpi) for a screen.

    ``screen`` is ``"main"``, ``"inner"`` or ``"outer"`` (foldables).
    """
    if width_px <= 0 or height_px <= 0:
        return 420

    if brand == "Apple":
        dp = _APPLE_DP.get((width_px, height_px))
        if dp is None:
            dp = 820 if category == CATEGORY_TABLET else 390
        return _snap_density(width_px / dp * 160)

    if category == CATEGORY_TABLET:
        diagonal = 10.5
    elif category == CATEGORY_FOLDABLE:
        if screen == "outer":
            diagonal = 1.9 if width_px <= 800 else 6.3
        else:
            diagonal = 6.7 if width_px <= 1200 else 7.8
    else:
        # Approximate physical diagonal from the resolution tier.  This keeps
        # old 480x800 phones at a sensible density and modern 1080p/1440p
        # phones at 420/560 dpi without a hand-maintained ppi column.
        if width_px <= 540:
            diagonal = 4.0
        elif width_px <= 800:
            diagonal = 5.0
        elif width_px <= 1080:
            diagonal = 6.1
        else:
            diagonal = 6.7

    ppi = math.hypot(width_px, height_px) / diagonal
    return _snap_density(ppi)


@dataclass(frozen=True)
class ScreenSpec:
    """One physical screen of a device."""

    width: int
    height: int
    density: int
    cutout: str = CUTOUT_NONE

    @property
    def size_str(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def cutout_label(self) -> str:
        return CUTOUT_LABELS.get(self.cutout, self.cutout)


@dataclass
class DevicePreset:
    """A bundled device definition."""

    key: str
    name: str
    brand: str
    category: str
    width: int
    height: int
    density: int
    cutout: str = CUTOUT_NONE
    description: str = ""
    preview_only: bool = False
    rumored: bool = False
    inner: Optional[ScreenSpec] = None
    outer: Optional[ScreenSpec] = None
    aliases: List[str] = field(default_factory=list)

    @property
    def size_str(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def cutout_label(self) -> str:
        return CUTOUT_LABELS.get(self.cutout, self.cutout)

    @property
    def is_foldable(self) -> bool:
        # The rumored Foldable iPhone is grouped with Apple phones but still
        # has an inner and cover screen, so the toggle follows the data.
        return self.inner is not None and self.outer is not None

    @property
    def search_text(self) -> str:
        return " ".join([self.brand, self.category, self.name,
                         self.size_str, self.cutout_label, *self.aliases]).lower()

    def screen(self, mode: str = "inner") -> ScreenSpec:
        """Return the screen to apply (inner/outer for foldables)."""
        if self.is_foldable:
            if mode == "outer" and self.outer is not None:
                return self.outer
            return self.inner  # type: ignore[return-value]
        return ScreenSpec(self.width, self.height, self.density, self.cutout)

    def apply(self, adb: ADB, screen: str = "inner") -> tuple[bool, str]:
        """Apply this preset's viewport (size + density) to the device."""
        spec = self.screen(screen)
        success, msg = adb.set_screen_size(spec.width, spec.height)
        if not success:
            return False, f"Failed to set screen size: {msg}"

        success, msg = adb.set_density(spec.density)
        if not success:
            return False, f"Failed to set density: {msg}"

        suffix = ""
        if self.preview_only:
            suffix = " - viewport preview only (Android cannot run iOS apps)"
        elif self.is_foldable:
            suffix = f" - {screen} screen"
        if self.rumored:
            suffix += " - rumored/unconfirmed"
        return True, (f"Applied {self.brand} {self.name} "
                      f"({spec.size_str} @ {spec.density} dpi){suffix}")

    def reset(self, adb: ADB) -> tuple[bool, str]:
        """Reset to the emulator's own size/density."""
        results = []
        _ok, msg = adb.reset_screen_size()
        results.append(msg)
        _ok, msg = adb.reset_density()
        results.append(msg)
        return True, "; ".join(results)


# ----------------------------------------------------------------------------
# Bundled data loading
# ----------------------------------------------------------------------------
def _data_path() -> Path:
    """Locate the bundled devices.json (also inside a PyInstaller bundle)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "core" / "data" / "devices.json"
        if bundled.exists():
            return bundled
    return Path(__file__).resolve().parent / "data" / "devices.json"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "device"


def _screen_from_dict(raw: dict, category: str, brand: str,
                      screen: str) -> ScreenSpec:
    width = int(raw["width_px"])
    height = int(raw["height_px"])
    cutout = raw.get("cutout", CUTOUT_NONE)
    if cutout not in CUTOUTS:
        raise ValueError(f"Unknown cutout {cutout!r}")
    return ScreenSpec(width, height,
                      compute_density(width, height, category, brand, screen),
                      cutout)


def _preset_from_entry(entry: dict) -> DevicePreset:
    brand = str(entry["brand"])
    category = str(entry["category"])
    name = str(entry["name"])
    if category not in CATEGORY_ORDER:
        raise ValueError(f"Unknown category {category!r}")
    width = int(entry["width_px"])
    height = int(entry["height_px"])
    cutout = entry.get("cutout", CUTOUT_NONE)
    if cutout not in CUTOUTS:
        raise ValueError(f"Unknown cutout {cutout!r}")

    inner = outer = None
    if entry.get("inner"):
        inner = _screen_from_dict(entry["inner"], category, brand, "inner")
    if entry.get("outer"):
        outer = _screen_from_dict(entry["outer"], category, brand, "outer")

    density = compute_density(width, height, category, brand, "main")
    key = _slug(f"{brand} {name}")
    return DevicePreset(
        key=key,
        name=name,
        brand=brand,
        category=category,
        width=width,
        height=height,
        density=density,
        cutout=cutout,
        description=(f"{brand} {category} - {width}x{height} - "
                     f"{CUTOUT_LABELS.get(cutout, cutout)}"),
        preview_only=bool(entry.get("preview_only", False)),
        rumored=bool(entry.get("rumored", False)),
        inner=inner,
        outer=outer,
        aliases=[str(a) for a in entry.get("aliases", [])],
    )


def load_bundled_devices(path: Optional[Path] = None) -> Dict[str, DevicePreset]:
    """Load and validate the bundled device data file."""
    data_file = Path(path) if path is not None else _data_path()
    raw = json.loads(data_file.read_text(encoding="utf-8"))
    presets: Dict[str, DevicePreset] = {}
    for entry in raw.get("devices", []):
        preset = _preset_from_entry(entry)
        # ``+`` and other punctuation collapse in slugs (S6 edge / S6 edge+),
        # so disambiguate deterministically instead of dropping a device.
        base_key = preset.key
        suffix = 2
        while preset.key in presets:
            preset.key = f"{base_key}_{suffix}"
            suffix += 1
        presets[preset.key] = preset
    if not presets:
        raise ValueError(f"No devices found in {data_file}")
    return presets


# Backwards-compatible alias for code/tests that imported BUILTIN_PRESETS.
# It is now the bundled data set, not a hand-written fallback.
BUILTIN_PRESETS: Dict[str, DevicePreset] = load_bundled_devices()


class DevicePresetManager:
    """Manage the bundled device list plus optional user-added presets."""

    def __init__(self, presets: Optional[Dict[str, DevicePreset]] = None):
        self._presets = dict(presets) if presets is not None \
            else dict(BUILTIN_PRESETS)
        self._custom_presets: Dict[str, DevicePreset] = {}

    # -- lookup ----------------------------------------------------------
    def get_preset(self, key: str) -> Optional[DevicePreset]:
        if key in self._custom_presets:
            return self._custom_presets[key]
        return self._presets.get(key)

    def get_all_presets(self) -> Dict[str, DevicePreset]:
        combined = self._presets.copy()
        combined.update(self._custom_presets)
        return combined

    def get_preset_names(self) -> List[str]:
        return list(self.get_all_presets().keys())

    def get_all_names(self) -> Dict[str, str]:
        return {key: preset.name
                for key, preset in self.get_all_presets().items()}

    def get_builtin_presets(self) -> Dict[str, DevicePreset]:
        return self._presets.copy()

    def get_custom_presets(self) -> Dict[str, DevicePreset]:
        return self._custom_presets.copy()

    # -- grouping --------------------------------------------------------
    def get_brand_groups(self):
        """``[(brand, [(category, [(key, preset), ...]), ...]), ...]``.

        Brands follow ``BRAND_ORDER`` (then any extras alphabetically); within
        a brand categories follow Phone / Tablet / Foldable.  The JSON order is
        kept inside each category (roughly chronological).
        """
        all_presets = self.get_all_presets()
        brands: List[str] = [b for b in BRAND_ORDER
                             if any(p.brand == b for p in all_presets.values())]
        extras = sorted({p.brand for p in all_presets.values()} - set(brands))
        brands.extend(extras)

        result = []
        for brand in brands:
            categories = []
            for category in CATEGORY_ORDER:
                items = [(key, preset) for key, preset in all_presets.items()
                         if preset.brand == brand and preset.category == category]
                if items:
                    categories.append((category, items))
            if categories:
                result.append((brand, categories))
        return result

    def get_grouped_presets(self):
        """``[(category, [(key, preset), ...]), ...]`` across all brands.

        Kept for the legacy V1 window; the V2 picker uses ``get_brand_groups``.
        """
        grouped: Dict[str, list] = {c: [] for c in CATEGORY_ORDER}
        for key, preset in self.get_all_presets().items():
            grouped.setdefault(preset.category, []).append((key, preset))
        result = []
        for category in CATEGORY_ORDER:
            items = grouped.get(category, [])
            if items:
                result.append((category, items))
        return result

    def search(self, query: str) -> List[DevicePreset]:
        """Case-insensitive search across brand, category, name and specs."""
        needle = (query or "").strip().lower()
        if not needle:
            return list(self.get_all_presets().values())
        return [preset for preset in self.get_all_presets().values()
                if needle in preset.search_text]

    # -- custom presets --------------------------------------------------
    def add_custom_preset(self, name: str, width: int, height: int,
                          density: int, description: str = "") -> bool:
        key = _slug(name)
        self._custom_presets[key] = DevicePreset(
            key=key, name=name, brand="Custom", category=CATEGORY_PHONE,
            width=width, height=height, density=density,
            description=description or "Custom preset")
        return True

    def remove_custom_preset(self, name: str) -> bool:
        key = _slug(name)
        if key in self._custom_presets:
            del self._custom_presets[key]
            return True
        return False

    def is_custom(self, name: str) -> bool:
        return _slug(name) in self._custom_presets

    # -- application -----------------------------------------------------
    def apply_preset(self, adb: ADB, key: str,
                     screen: str = "inner") -> tuple[bool, str]:
        preset = self.get_preset(key)
        if preset is None:
            return False, f"Unknown preset: {key}"
        return preset.apply(adb, screen)

    def reset_to_default(self, adb: ADB) -> tuple[bool, str]:
        """Reset the emulator to its own size/density."""
        results = []
        _ok, msg = adb.reset_screen_size()
        results.append(msg)
        _ok, msg = adb.reset_density()
        results.append(msg)
        return True, "; ".join(results)

    def get_current_config(self, adb: ADB) -> Optional[DevicePreset]:
        size = adb.get_screen_size()
        density = adb.get_density()
        if size and density:
            return DevicePreset(
                key="current", name="Current", brand="Current",
                category=CATEGORY_PHONE, width=size[0], height=size[1],
                density=density, description="Current device configuration")
        return None


# Global instance
preset_manager = DevicePresetManager()
