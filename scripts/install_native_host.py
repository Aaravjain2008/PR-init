#!/usr/bin/env python3
"""Installer script for Chrome Native Messaging Host registration."""

import json
import os
import platform
import sys
from pathlib import Path

HOST_NAME = "com.repoguard.engine"
HOST_DESCRIPTION = "RepoGuard Evidence-Based Triage Engine Host"
EXTENSION_ID = "kdmflkfkjfgnlhijpbaeehgbcljofplm"  # Standard placeholder or extension ID


def install_manifest():
    script_dir = Path(__file__).resolve().parent.parent
    host_script = script_dir / "repoguard" / "native_host.py"
    
    # Python executable path
    python_exe = sys.executable

    manifest = {
        "name": HOST_NAME,
        "description": HOST_DESCRIPTION,
        "path": str(host_script),
        "type": "stdio",
        "allowed_origins": [
            f"chrome-extension://{EXTENSION_ID}/"
        ],
    }

    os_name = platform.system()
    if os_name == "Darwin":
        target_dir = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
    elif os_name == "Linux":
        target_dir = Path.home() / ".config" / "google-chrome" / "NativeMessagingHosts"
    elif os_name == "Windows":
        target_dir = Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "NativeMessagingHosts"
    else:
        print(f"Unsupported OS: {os_name}")
        return 1

    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{HOST_NAME}.json"

    with open(target_file, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(f"Registered Native Messaging Host:")
    print(f"  Target: {target_file}")
    print(f"  Host Script: {host_script}")
    return 0


if __name__ == "__main__":
    sys.exit(install_manifest())

