"""Defensive reading of numeric GUI preferences; never rewrite them on startup."""

import math


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
