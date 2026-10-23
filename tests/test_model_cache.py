from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import joblib

from pqo.model_cache import load_joblib_model


def test_cache_reuses_model_and_invalidates_after_replacement():
    with TemporaryDirectory() as directory:
        target = Path(directory) / "model.joblib"
        joblib.dump({"version": 1}, target)
        with patch("joblib.load", wraps=joblib.load) as loader:
            assert load_joblib_model(target)["version"] == 1
            assert load_joblib_model(target)["version"] == 1
            assert loader.call_count == 1
            replacement = Path(directory) / "replacement.joblib"
            joblib.dump({"version": "replacement"}, replacement)
            replacement.replace(target)
            assert load_joblib_model(target)["version"] == "replacement"
            assert loader.call_count == 2
