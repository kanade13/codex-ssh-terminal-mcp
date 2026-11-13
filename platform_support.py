import os
import platform
import sys
from pathlib import Path


def is_wsl():
    if sys.platform != "linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()
    except OSError:
        release = platform.release().lower()
    return "microsoft" in release or "wsl" in release


def platform_status():
    if sys.platform == "darwin":
        return {
            "supported": True,
            "platform": "macos",
            "message": "macOS is supported.",
        }
    if sys.platform == "linux":
        if is_wsl():
            return {
                "supported": True,
                "platform": "windows-wsl",
                "message": "Windows via WSL is supported and treated as Linux.",
            }
        return {
            "supported": True,
            "platform": "linux",
            "message": "Linux is supported.",
        }
    if sys.platform.startswith("win"):
        return {
            "supported": False,
            "platform": "windows-native",
            "message": "Native Windows is not supported yet. Run this MCP inside WSL for the current release.",
        }
    return {
        "supported": False,
        "platform": sys.platform,
        "message": f"Unsupported platform: {sys.platform}. Current support is macOS, Linux, and Windows via WSL.",
    }


def assert_supported_platform():
    status = platform_status()
    if not status["supported"]:
        raise RuntimeError(status["message"])
    return status
