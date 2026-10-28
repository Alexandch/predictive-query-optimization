"""Install/activate a reversible trial without overwriting production weights."""

import argparse
from pathlib import Path

from PyQt6.QtCore import QSettings

from pqo.app_paths import resource_root, writable_root
from pqo.model_trial import activate_trial, install_trial, rollback_trial


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    if args.activate and args.rollback:
        parser.error("Choose activation or rollback")
    root = resource_root()
    preferences = QSettings("PQO", "PredictiveQueryOptimization")
    current = Path(str(preferences.value("xgb_model", str(root / "models/xgboost/xgboost_query_time.joblib"))))
    if args.rollback:
        print("Restored model:", rollback_trial(preferences, current))
        return
    trial = install_trial(root / "artifacts/heavy-fresh-training-20261027-v3/identity",
                          root / "models/xgboost/trial")
    if args.activate:
        activate_trial(preferences, trial, current, writable_root() / "artifacts/model-trial-backups")
        print("Selected test model:", trial)
    else:
        print("Installed test model (selection unchanged):", trial)


if __name__ == "__main__":
    main()
