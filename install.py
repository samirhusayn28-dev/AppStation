#!/usr/bin/python3
"""
Create macOS .pkg installer for AppStation.
This generates a professional .pkg that installs AppStation.app to /Applications.
"""
import subprocess
import os
import sys

# Paths
APP_BUNDLE = os.path.join(os.getcwd(), "dist", "AppStation.app")
PKG_ID = "com.appstation.app"
PKG_VERSION = "1.0.2"
OUTPUT_PKG = "AppStation-1.0.2.pkg"

def create_pkg():
    """Create a macOS package installer for AppStation."""
    
    # Verify app bundle exists
    if not os.path.exists(APP_BUNDLE):
        print(f"Error: App bundle not found at {APP_BUNDLE}")
        print("Expected: dist/AppStation.app")
        sys.exit(1)
    
    # Create temporary payload directory
    payload_dir = "/tmp/apkpreview_payload"
    if os.path.exists(payload_dir):
        subprocess.run(["rm", "-rf", payload_dir], check=True)
    os.makedirs(payload_dir, exist_ok=True)
    
    # Copy the app bundle to payload (flattened for package)
    subprocess.run(["cp", "-R", APP_BUNDLE, os.path.join(payload_dir, "AppStation.app")], check=True)
    
    # Build the package using productbuild with --root
    pkg_path = os.path.join(os.getcwd(), OUTPUT_PKG)
    
    cmd = [
        "productbuild",
        "--root", payload_dir,
        "--identifier", PKG_ID,
        "--version", PKG_VERSION,
        pkg_path
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        print(f"✅ Successfully created: {pkg_path}")
        print(f"   Package version: {PKG_VERSION}")
        print(f"   Package identifier: {PKG_ID}")
        print("\n📋 The installer will copy AppStation.app to /Applications/")
        print("   Users can then:")
        print("   1. Open Finder → Applications → Double-click AppStation.app")
        print("   2. Or drag AppStation.app from the installer to the Applications folder")
        print("   3. Right-click → Open on first launch (for unsigned apps)")
    except subprocess.CalledProcessError as e:
        print(f"❌ Error creating package: {e.stderr}")
        sys.exit(1)
    finally:
        # Cleanup temporary payload
        subprocess.run(["rm", "-rf", payload_dir], check=True)

if __name__ == "__main__":
    create_pkg()
