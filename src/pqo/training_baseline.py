"""Immutable snapshots for reproducible model-training experiments."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Iterable


DEFAULT_FILES = (
    "models/xgboost/xgboost_query_time.joblib",
    "models/xgboost/metrics.json",
    "models/dqn/dqn_index_advisor.pt",
    "models/dqn/metrics.json",
    "dataset/postgresql/multidomain_training.csv",
    "dataset/postgresql/multidomain_dqn_augmented.jsonl",
    "dataset/postgresql/production_control.csv",
    "dataset/postgresql/dqn_production_control.jsonl",
    "dataset/postgresql/retail_control.csv",
    "dataset/postgresql/retail_dqn_control.jsonl",
    "dataset/postgresql/logistics_zero_shot.csv",
    "dataset/postgresql/logistics_dqn_zero_shot.jsonl",
    "dataset/postgresql/chbenchmark_control.csv",
    "dataset/postgresql/chbenchmark_dqn_control.jsonl",
)


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def create_baseline(
    root: str | Path,
    output_path: str | Path,
    *,
    files: Iterable[str] = DEFAULT_FILES,
) -> dict:
    root_path = Path(root).resolve()
    entries: dict[str, dict[str, int | str]] = {}
    for relative in files:
        path = root_path / relative
        if not path.is_file():
            raise FileNotFoundError(f"Baseline input not found: {relative}")
        entries[relative] = {
            "size_bytes": path.stat().st_size,
            "sha256": file_sha256(path),
        }

    manifest = {
        "format_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(root_path),
        "files": entries,
    }
    destination = Path(output_path)
    if not destination.is_absolute():
        destination = root_path / destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def verify_baseline(root: str | Path, manifest_path: str | Path) -> list[str]:
    root_path = Path(root).resolve()
    source = Path(manifest_path)
    if not source.is_absolute():
        source = root_path / source
    manifest = json.loads(source.read_text(encoding="utf-8"))
    errors: list[str] = []
    for relative, expected in manifest["files"].items():
        path = root_path / relative
        if not path.is_file():
            errors.append(f"missing: {relative}")
            continue
        actual = file_sha256(path)
        if actual != expected["sha256"]:
            errors.append(f"hash mismatch: {relative}")
    return errors


def _git_commit(root: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
