"""Defensive reading of numeric GUI preferences; never rewrite them on startup."""

import math
import os
from pathlib import Path


def open_preferences(settings_type):
    """An explicit INI isolates diagnostics; normal startup keeps native settings.

    Never fall back to the user's registry when isolation was requested but the
    path is invalid/unwritable. PQO_APP_DATA_ROOT alone remains compatible with
    existing smoke tools that intentionally use the current connection profile.
    """
    configured = os.environ.get("PQO_SETTINGS_FILE")
    if configured is None:
        return settings_type("PQO", "PredictiveQueryOptimization")
    path = Path(configured).expanduser()
    if not configured.strip() or not path.is_absolute() or path.is_dir():
        raise ValueError("PQO_SETTINGS_FILE must be an absolute INI file path")
    path.parent.mkdir(parents=True, exist_ok=True)
    preferences = settings_type(str(path), settings_type.Format.IniFormat)
    preferences.setFallbacksEnabled(False)
    if not preferences.isWritable():
        raise ValueError(
            "PQO_SETTINGS_FILE is not writable; native settings were not opened"
        )
    return preferences


def read_number(preferences, key, default, *, minimum, maximum, integer=False):
    try:
        value = preferences.value(key, default)
        if isinstance(value, bool):
            return default
        value = float(value)
        if not math.isfinite(value) or not minimum <= value <= maximum:
            return default
        if integer:
            return int(value) if value.is_integer() else default
        return value
    except (TypeError, ValueError, OverflowError):
        return default
