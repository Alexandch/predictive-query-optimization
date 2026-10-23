"""Bounded inference cache invalidated when an artifact is replaced."""

from functools import lru_cache
from pathlib import Path


def load_joblib_model(path):
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return _load(str(resolved), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)


@lru_cache(maxsize=8)
def _load(path, modified_ns, changed_ns, size):
    import joblib

    return joblib.load(path)
