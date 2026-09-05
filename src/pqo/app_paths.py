"""Resource and writable-data paths for source and frozen desktop runs."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def resource_root() -> Path:
    """Return the repository root or PyInstaller's unpacked resource root."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)).resolve()
    return Path(__file__).resolve().parents[2]


def writable_root() -> Path:
    """Return a stable writable directory for generated desktop artifacts."""
    override = os.environ.get("PQO_APP_DATA_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if not getattr(sys, "frozen", False):
        return resource_root()
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return base / "PredictiveQueryOptimization"
