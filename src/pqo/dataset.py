"""Build flat, machine-learning-ready samples from SQL and EXPLAIN data."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import DatabaseSettings
from .explain import collect_explain
from .query_case import QueryCase
from .repository import save_analysis
from .sql_features import extract_sql_features


@dataclass(frozen=True, slots=True)
class CollectionResult:
    sql_text: str
    values: dict[str, str | int | float | bool | None]
    query_run_id: int | None


def collect_sample(
    query: str | QueryCase,
    settings: DatabaseSettings | None = None,
    persist: bool = True,
) -> CollectionResult:
    case = query if isinstance(query, QueryCase) else QueryCase("external", query)
    sql_text = case.sql_text
    sql_features = extract_sql_features(sql_text)
    explain_result = collect_explain(sql_text, settings=settings)
    values = {
        "template_id": case.template_id,
        "sql_text": sql_text.strip(),
        **sql_features.as_dict(),
        **explain_result.features.as_dict(),
    }
    query_run_id = None
    if persist:
        query_run_id = save_analysis(
            sql_text=sql_text.strip(),
            plan_json=explain_result.plan_json,
            sql_features=sql_features,
            plan_features=explain_result.features,
            settings=settings,
            template_id=case.template_id,
        )

    return CollectionResult(
        sql_text=sql_text.strip(),
        values=values,
        query_run_id=query_run_id,
    )


def collect_to_csv(
    queries: Iterable[str | QueryCase],
    output_path: str | Path,
    settings: DatabaseSettings | None = None,
    persist: bool = True,
    progress: Callable[[int, int, CollectionResult], None] | None = None,
) -> list[CollectionResult]:
    cases = [
        query if isinstance(query, QueryCase) else QueryCase("external", query)
        for query in queries
    ]
    cases = [case for case in cases if case.sql_text.strip()]
    results: list[CollectionResult] = []
    for position, case in enumerate(cases, start=1):
        result = collect_sample(case, settings=settings, persist=persist)
        results.append(result)
        if progress is not None:
            progress(position, len(cases), result)

    if not results:
        raise ValueError("At least one non-empty query is required")

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(results[0].values))
        writer.writeheader()
        writer.writerows(result.values for result in results)

    return results
