"""Opt-in model trials: preserve production files and provide a verified rollback."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4


TRIAL_SHA256 = "0958ca642d1e7e782bdb72ec401fae83db27096498d8bb1cbb67ebdec8297b7f"
TRIAL_STATE_KEY = "xgb_trial_state"
MODEL_FILES = ("xgboost_query_time.joblib", "metrics.json", "feature_importance.json")


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_trial(path):
    path = Path(path).resolve()
    if file_sha(path) != TRIAL_SHA256:
        raise ValueError("Тестовые веса не совпадают с проверенным кандидатом")
    from .model_cache import load_joblib_model
    from .plan_features import PLAN_FLOW_FEATURES

    artifact = load_joblib_model(path)
    if (artifact.get("feature_version") != "plan-v1" or artifact.get("requires_query_execution") is not False
            or not set(PLAN_FLOW_FEATURES) <= set(artifact.get("model_features", []))):
        raise ValueError("Неподдерживаемый контракт тестовой модели")
    return path


def install_trial(source, destination):
    """Install the exact externally checked artifact, not a generic promotion bypass."""
    source, destination = Path(source), Path(destination)
    report = json.loads((source.parent / "report.json").read_text(encoding="utf-8"))
    required = {"validation", "acceptance", "regression", "paired_interval"}
    if (report.get("selected") != source.name or report.get("external_quality_pass") is not True
            or set(report.get("checks", {})) != required or not all(report["checks"].values())):
        raise ValueError("Кандидат не прошёл внешний протокол тестового подключения")
    validate_trial(source / MODEL_FILES[0])
    for name in MODEL_FILES:
        if not (source / name).is_file():
            raise FileNotFoundError(source / name)
    if destination.exists():
        if any(not (destination / name).is_file() or file_sha(destination / name) != file_sha(source / name)
               for name in MODEL_FILES):
            raise FileExistsError("Каталог тестовой модели занят другими файлами")
        return destination / MODEL_FILES[0]
    destination.mkdir(parents=True)
    for name in MODEL_FILES:
        shutil.copy2(source / name, destination / name)
    (destination / "trial-info.json").write_text(json.dumps({
        "status": "opt-in external-control trial, not production promotion",
        "model_sha256": TRIAL_SHA256, "source": str(source),
        "acceptance_queries": 24, "acceptance_mae_ms": 332.4447430801392,
        "scope": "aviation/retail in the measured local environment",
        "checks": report["checks"], "production_overwritten": False,
    }, indent=2), encoding="utf-8")
    return validate_trial(destination / MODEL_FILES[0])


def trial_state(preferences):
    value = preferences.value(TRIAL_STATE_KEY, "")
    return json.loads(str(value)) if value else None


def materialize_trial(resource_path, destination):
    """Keep packaged trial weights outside ephemeral PyInstaller resources."""
    source = validate_trial(resource_path)
    destination = Path(destination).resolve()
    if destination == source.parent:
        return source
    destination.mkdir(parents=True, exist_ok=True)
    for name in MODEL_FILES:
        target = destination / name
        if target.exists():
            if file_sha(target) != file_sha(source.parent / name):
                raise FileExistsError(f"Trial file differs: {target}")
        else:
            shutil.copy2(source.parent / name, target)
    return validate_trial(destination / MODEL_FILES[0])


def activate_trial(preferences, trial_path, current_path, backup_root):
    trial = validate_trial(trial_path)
    current = Path(current_path).resolve()
    if current == trial:
        return trial
    if not current.is_file():
        raise FileNotFoundError("Не найдены предыдущие веса для резервной копии")
    previous_hash = file_sha(current)
    backup = Path(backup_root) / uuid4().hex
    backup.mkdir(parents=True, exist_ok=False)
    shutil.copy2(current, backup / MODEL_FILES[0])
    for name in MODEL_FILES[1:]:
        if (current.parent / name).is_file():
            shutil.copy2(current.parent / name, backup / name)
    previous_copy = (backup / MODEL_FILES[0]).resolve()
    if file_sha(previous_copy) != previous_hash:
        raise ValueError("Резервная копия предыдущих весов повреждена")
    state = {"trial_path": str(trial), "previous_path": str(current),
             "backup_path": str(previous_copy), "previous_sha256": previous_hash,
             "activated_utc": datetime.now(timezone.utc).isoformat()}
    (backup / "trial-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    preferences.setValue(TRIAL_STATE_KEY, json.dumps(state))
    preferences.setValue("xgb_model", str(trial))
    preferences.sync()
    return trial


def rollback_trial(preferences, current_path):
    state = trial_state(preferences)
    if not state or Path(current_path).resolve() != Path(state["trial_path"]).resolve():
        raise ValueError("Тестовая модель сейчас не выбрана")
    backup = Path(state["backup_path"])
    if file_sha(backup) != state["previous_sha256"]:
        raise ValueError("Резервная копия повреждена; выбранная модель не изменена")
    original = Path(state["previous_path"])
    # Never overwrite a model changed independently after the trial started.
    target = original if original.is_file() and file_sha(original) == state["previous_sha256"] else backup
    preferences.setValue("xgb_model", str(target))
    preferences.remove(TRIAL_STATE_KEY)
    preferences.sync()
    return target
