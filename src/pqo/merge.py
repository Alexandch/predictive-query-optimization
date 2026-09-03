"""Validation and lossless concatenation of collected CSV datasets."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable


def merge_datasets(
    input_paths: Iterable[str | Path],
    output_path: str | Path,
) -> int:
    sources = [Path(path) for path in input_paths]
    if len(sources) < 2:
        raise ValueError("At least two input datasets are required")

    fieldnames: list[str] | None = None
    rows: list[dict[str, str]] = []
    for source in sources:
        with source.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError(f"Dataset has no header: {source}")
            if fieldnames is None:
                fieldnames = reader.fieldnames
            elif reader.fieldnames != fieldnames:
                raise ValueError(f"Dataset columns do not match: {source}")
            rows.extend(reader)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
