"""APK parsing utilities for AppStation."""

import re
import zipfile
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from .platform_utils import PlatformUtils


class APKParser:
    """Parse APK files to extract metadata."""
    
    def __init__(self, aapt2_path: Optional[Path] = None):
        self._aapt2_path = aapt2_path or PlatformUtils.get_aapt2_path()
    
    def parse(self, apk_path: Path) -> Dict:
        """Parse APK and return metadata dict."""
        result = {
            "package": None,
            "version_name": None,
            "version_code": None,
            "main_activity": None,
            "activities": [],
            "permissions": [],
            "min_sdk": None,
            "target_sdk": None,
            "icon_path": None,
            "label": None,
            "valid": False,
            "error": None,
        }
        
        # First try aapt2
        if self._aapt2_path.exists():
            self._parse_with_aapt2(apk_path, result)
        
        # Fallback to androguard
        if not result["valid"]:
            self._parse_with_androguard(apk_path, result)
        
        # Basic zip validation
        if not result["valid"]:
            result["valid"] = self._validate_apk_zip(apk_path)
            if not result["valid"]:
                result["error"] = "Not a valid APK file"
        
        return result
    
    def _parse_with_aapt2(self, apk_path: Path, result: Dict):
        """Parse using aapt2 dump badging."""
        try:
            cmd = [str(self._aapt2_path), "dump", "badging", str(apk_path)]
            proc = PlatformUtils.run_command(cmd, timeout=30)
            
            if proc.returncode != 0:
                return
            
            output = proc.stdout
            
            # Parse package info
            # package: name='com.example' versionCode='1' versionName='1.0' ...
            pkg_match = re.search(r"package: name='([^']+)'", output)
            if pkg_match:
                result["package"] = pkg_match.group(1)
            
            ver_code_match = re.search(r"versionCode='(\d+)'", output)
            if ver_code_match:
                result["version_code"] = int(ver_code_match.group(1))
            
            ver_name_match = re.search(r"versionName='([^']+)'", output)
            if ver_name_match:
                result["version_name"] = ver_name_match.group(1)
            
            # Parse SDK versions
            min_sdk_match = re.search(r"sdkVersion:'(\d+)'", output)
            if min_sdk_match:
                result["min_sdk"] = int(min_sdk_match.group(1))
            
            target_sdk_match = re.search(r"targetSdkVersion:'(\d+)'", output)
            if target_sdk_match:
                result["target_sdk"] = int(target_sdk_match.group(1))
            
            # Parse launchable activity
            # launchable-activity: name='com.example.MainActivity' ...
            act_match = re.search(r"launchable-activity: name='([^']+)'", output)
            if act_match:
                result["main_activity"] = act_match.group(1)
            
            # Parse all activities
            for match in re.finditer(r"activity: name='([^']+)'", output):
                result["activities"].append(match.group(1))
            
            # Parse permissions
            for match in re.finditer(r"uses-permission: name='([^']+)'", output):
                result["permissions"].append(match.group(1))
            
            # Parse label
            label_match = re.search(r"application-label:'([^']+)'", output)
            if label_match:
                result["label"] = label_match.group(1)
            else:
                # Try localized label
                label_match = re.search(r"application-label-([^:]+):'([^']+)'", output)
                if label_match:
                    result["label"] = label_match.group(2)
            
            # Parse icon (just note it exists, actual extraction is complex)
            if "application-icon-" in output:
                result["icon_path"] = "embedded"
            
            if result["package"]:
                result["valid"] = True
                
        except Exception as e:
            result["error"] = f"aapt2 parse error: {e}"
    
    def _parse_with_androguard(self, apk_path: Path, result: Dict):
        """Parse using androguard as fallback."""
        try:
            from androguard.core.apk import APK
            apk = APK(str(apk_path))
            
            result["package"] = apk.get_package()
            result["version_name"] = apk.get_androidversion_name()
            result["version_code"] = apk.get_androidversion_code()
            result["min_sdk"] = apk.get_min_sdk_version()
            result["target_sdk"] = apk.get_target_sdk_version()
            result["main_activity"] = apk.get_main_activity()
            result["activities"] = apk.get_activities()
            result["permissions"] = apk.get_permissions()
            result["label"] = apk.get_app_name()
            
            # Icon - androguard can extract but it's complex
            result["icon_path"] = "embedded"
            
            if result["package"]:
                result["valid"] = True
                
        except ImportError:
            pass
        except Exception as e:
            result["error"] = f"androguard parse error: {e}"
    
    def _validate_apk_zip(self, apk_path: Path) -> bool:
        """Basic validation that it's a valid APK (ZIP with AndroidManifest)."""
        try:
            with zipfile.ZipFile(apk_path, 'r') as zf:
                return "AndroidManifest.xml" in zf.namelist()
        except Exception:
            return False
    
    def extract_icon(self, apk_path: Path, output_path: Path) -> bool:
        """Extract app icon from APK (best effort)."""
        try:
            from androguard.core.apk import APK
            apk = APK(str(apk_path))
            
            # Get icon filename from manifest
            icon_name = apk.get_app_icon()
            if not icon_name:
                return False
            
            # Try to extract from resources
            # This is complex with androguard, skip for now
            return False
        except Exception:
            return False


def detect_package_and_activity(apk_path: Path) -> Tuple[Optional[str], Optional[str]]:
    """Quick detection of package and main activity."""
    parser = APKParser()
    result = parser.parse(apk_path)
    return result["package"], result["main_activity"]


def is_valid_apk(apk_path: Path) -> bool:
    """Quick check if file is a valid APK."""
    try:
        with zipfile.ZipFile(apk_path, 'r') as zf:
            return "AndroidManifest.xml" in zf.namelist()
    except Exception:
        return False


def get_apk_info(apk_path: Path) -> Dict:
    """Get comprehensive APK info."""
    parser = APKParser()
    return parser.parse(apk_path)