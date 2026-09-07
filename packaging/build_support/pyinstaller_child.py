"""Start PyInstaller's isolated child without querying a broken Windows WMI."""

from __future__ import annotations

import platform
import runpy


if hasattr(platform, "_wmi"):
    platform._wmi = None

runpy.run_module("PyInstaller.isolated._child", run_name="__main__")
