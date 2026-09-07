"""Run PyInstaller with a WMI-independent parent and isolated child."""

from __future__ import annotations

import platform
from pathlib import Path


if hasattr(platform, "_wmi"):
    platform._wmi = None

from PyInstaller import __main__  # noqa: E402
from PyInstaller.isolated import _parent  # noqa: E402


_parent.CHILD_PY = Path(__file__).with_name("pyinstaller_child.py")
__main__.run()
