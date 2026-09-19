"""Reproducible final evaluation assembled from sealed controls and live trials."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import json
from pathlib import Path
from typing import Any

from .explain import collect_explain
from .structural_advisor import analyze_query_structure
from .structural_validation import (
    automatic_validation_supported,
    validate_structural_recommendation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class StructuralCase:
    case_id: str
    purpose: str
    sql: str
    predicted_time_ms: float | None = None


STRUCTURAL_CASES = (
    StructuralCase(
        "aggregation_having",
        "Фактическая проверка переноса неагрегатного HAVING в WHERE",
        """
        SELECT status, COUNT(*) AS flight_count
        FROM aviation.flights
        GROUP BY status
        HAVING status <> 'Cancelled' AND COUNT(*) >= 1
        """,
    ),
    StructuralCase(
        "redundant_inner_sort",
        "Фактическая проверка удаления сортировки из подзапроса",
        """
        SELECT flight_id, departure_airport
        FROM (
            SELECT flight_id, departure_airport
            FROM aviation.flights
            ORDER BY scheduled_departure
        ) AS ordered_flights
        ORDER BY flight_id
        """,
    ),
    StructuralCase(
        "materialized_summary",
        "Транзакционная проверка материализованной сводки",
        """
        SELECT f.departure_airport,
               f.arrival_airport,
               COUNT(*) AS ticket_count,
               SUM(tf.amount) AS revenue
        FROM aviation.flights AS f
        JOIN aviation.ticket_flights AS tf ON tf.flight_id = f.flight_id
        GROUP BY f.departure_airport, f.arrival_airport
        """,
        predicted_time_ms=100.0,
    ),
    StructuralCase(
        "join_diagnostic",
        "Проверка обнаружения JOIN без ON/USING",
        """
        SELECT f.flight_id, a.airport_name
        FROM aviation.flights AS f
        CROSS JOIN aviation.airports AS a
        LIMIT 100
        """,
    ),
)


SEALED_METRICS = {
    "xgboost_training": "models/xgboost/metrics.json",
    "xgboost_production_control": "models/control/xgboost_control_metrics.json",
    "xgboost_logistics_zero_shot": (
        "models/control/leave_one_database_out/logistics/xgboost_control_metrics.json"
    ),
    "xgboost_chbenchmark_control": (
        "models/control/chbenchmark/primary/xgboost_control_metrics.json"
    ),
    "dqn_training": "models/dqn/metrics.json",
    "dqn_production_control": "models/control/dqn_control_metrics.json",
    "dqn_logistics_control": (
        "models/control/negative_augmented/logistics/dqn_control_metrics.json"
    ),
    "dqn_chbenchmark_control": (
        "models/control/chbenchmark/primary/dqn_control_metrics.json"
    ),
    "sequential_dqn_control": "models/control/sequential/summary.json",
}


def load_sealed_metrics(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Load versioned control results without rerunning or contaminating them."""
    result = {}
    for name, relative_path in SEALED_METRICS.items():
        path = project_root / relative_path
        if not path.is_file():
            raise FileNotFoundError(f"Required sealed metric is missing: {path}")
        result[name] = json.loads(path.read_text(encoding="utf-8"))
    return result


def run_structural_trials(*, repetitions: int = 5) -> list[dict[str, Any]]:
    """Detect all advice and measure supported rules against the live test DB."""
    rows: list[dict[str, Any]] = []
    for case in STRUCTURAL_CASES:
        normalized_sql = "\n".join(
            line.strip() for line in case.sql.strip().splitlines()
        )
        plan = collect_explain(normalized_sql, analyze=False).plan_json
        recommendations = analyze_query_structure(
            normalized_sql,
            plan_json=plan,
            predicted_time_ms=case.predicted_time_ms,
        )
        for recommendation in recommendations:
            base = {
                "case_id": case.case_id,
                "purpose": case.purpose,
                "category": recommendation.category.value,
                "rule_id": recommendation.rule_id,
                "priority": recommendation.priority.value,
                "title": recommendation.title,
                "automatic_validation": automatic_validation_supported(
                    recommendation.rule_id
                ),
            }
            if not base["automatic_validation"]:
                rows.append(
                    {
                        **base,
                        "supported": False,
                        "equivalent": None,
                        "baseline_time_ms": None,
                        "candidate_time_ms": None,
                        "improvement_ratio": None,
                        "accepted": False,
                        "rolled_back": True,
                        "validation_method": "manual",
                        "artifact_creation_time_ms": None,
                    }
                )
                continue
            measured = validate_structural_recommendation(
                normalized_sql,
                recommendation,
                repetitions=repetitions,
            )
            rows.append({**base, **asdict(measured)})
    return rows


def build_summary(
    sealed_metrics: dict[str, Any],
    structural_results: list[dict[str, Any]],
    *,
    repetitions: int,
) -> dict[str, Any]:
    measured = [row for row in structural_results if row["supported"]]
    accepted = [row for row in measured if row["accepted"]]
    return {
        "experiment": "PQO final independent evaluation",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "training_performed": False,
        "sealed_controls_modified": False,
        "structural_repetitions": repetitions,
        "sealed_metrics": sealed_metrics,
        "structural_validation": {
            "recommendation_count": len(structural_results),
            "automatically_measured_count": len(measured),
            "accepted_count": len(accepted),
            "all_trials_rolled_back": all(
                row["rolled_back"] for row in structural_results
            ),
            "results": structural_results,
        },
        "interpretation": {
            "xgboost": (
                "Высокая точность внутри обучающих доменов не переносится без "
                "калибровки на произвольную новую схему; logistics переносится "
                "лучше внешнего CH-benCHmark."
            ),
            "dqn": (
                "Обычная DQN зависит от домена; последовательная DQN имеет "
                "статистически подтверждённое преимущество над STOP-базой."
            ),
            "structural": (
                "Совет считается подтверждённым только при эквивалентности, "
                "измеренном ускорении и полном откате пробного изменения."
            ),
        },
    }


def write_results(summary: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rows = summary["structural_validation"]["results"]
    columns = [
        "case_id",
        "category",
        "rule_id",
        "priority",
        "automatic_validation",
        "supported",
        "equivalent",
        "baseline_time_ms",
        "candidate_time_ms",
        "improvement_ratio",
        "accepted",
        "rolled_back",
        "validation_method",
        "artifact_creation_time_ms",
    ]
    with (output_dir / "structural_results.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_final_experiment(
    *,
    output_dir: Path,
    repetitions: int = 5,
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    if repetitions <= 0:
        raise ValueError("repetitions must be positive")
    sealed = load_sealed_metrics(project_root)
    structural = run_structural_trials(repetitions=repetitions)
    summary = build_summary(sealed, structural, repetitions=repetitions)
    write_results(summary, output_dir)
    return summary
