"""
adb_device.py
Thin wrappers around the two ADB operations this project needs.
Import and call these from any script that drives an Android device via ADB.
"""

import io
import subprocess

from PIL import Image


def adb_screencap() -> Image.Image:
    """Capture the connected device's screen and return it as a PIL Image."""
    result = subprocess.run(
        ["adb", "exec-out", "screencap", "-p"],
        capture_output=True,
        check=True,
    )
    return Image.open(io.BytesIO(result.stdout))


def adb_tap(x: int, y: int) -> None:
    """Send a tap event to device coordinates (x, y)."""
    subprocess.run(
        ["adb", "shell", "input", "tap", str(x), str(y)],
        check=False,
    )
