"""Validation and lossless concatenation of collected CSV datasets."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from .dqn_features import (
    collect_action_database_context,
    DEFAULT_ACTION_ENCODING,
    encode_action,
    GENERIC_V3_ACTION_ENCODING,
)
from .index_actions import IndexAction, IndexActionKind


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


def merge_dqn_experience(
    input_paths: Iterable[str | Path],
    output_path: str | Path,
    *,
    encoding_version: str = DEFAULT_ACTION_ENCODING,
    connection=None,
) -> int:
    """Merge JSONL experience and deterministically re-encode every action."""
    sources = [Path(path) for path in input_paths]
    if len(sources) < 2:
        raise ValueError("At least two input experience files are required")
    destination = Path(output_path)
    if destination.resolve() in {source.resolve() for source in sources}:
        raise ValueError("Output must not overwrite an input experience file")
    missing = [str(source) for source in sources if not source.is_file()]
    if missing:
        raise FileNotFoundError(f"DQN input not found: {', '.join(missing)}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    count = 0
    context_cache: dict[tuple, dict] = {}
    with destination.open("w", encoding="utf-8", newline="\n") as target:
        for source in sources:
            with source.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    data = record["action"]
                    action = IndexAction(
                        kind=IndexActionKind(data["kind"]),
                        schema_name=data.get("schema_name"),
                        table_name=data.get("table_name"),
                        key_columns=tuple(data.get("key_columns") or ()),
                        include_columns=tuple(data.get("include_columns") or ()),
                    )
                    database_context = record.get("action_database_context")
                    if (
                        encoding_version == GENERIC_V3_ACTION_ENCODING
                        and database_context is None
                    ):
                        context_key = (
                            action.kind,
                            action.schema_name,
                            action.table_name,
                            action.key_columns,
                            action.include_columns,
                        )
                        if context_key not in context_cache:
                            context_cache[context_key] = collect_action_database_context(
                                action,
                                connection=connection,
                            )
                        database_context = context_cache[context_key]
                        record["action_database_context"] = database_context
                    record["action_features"] = encode_action(
                        action,
                        record.get("sql_text"),
                        encoding_version=encoding_version,
                        database_context=database_context,
                    )
                    record["action_encoding_version"] = encoding_version
                    target.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
    return count
