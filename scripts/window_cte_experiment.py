"""First-prediction experiment: fresh CTE/window families, no calibration.

Only aviation/retail development data are read. Sealed controls are excluded.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from pqo.dataset import collect_sample
from pqo.model_cache import load_joblib_model
from pqo.query_case import QueryCase
from pqo.sql_features import EXTENDED_SQL_FEATURES, extract_sql_features
from pqo.training import MODEL_FEATURES, TARGET_COLUMN, train_xgboost


def workload():
    for size in (10000, 30000, 60000, 100000):
        families = {
            "train_retail_product_rank": f"""
                WITH sales AS (
                    SELECT p.category_id, oi.product_id,
                           COUNT(DISTINCT oi.order_id) AS orders,
                           SUM(oi.quantity * oi.unit_price) AS revenue
                    FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
                    JOIN retail.products p USING(product_id)
                    WHERE o.order_id <= {size} AND o.order_status <> 'cancelled'
                    GROUP BY p.category_id, oi.product_id
                ) SELECT *, DENSE_RANK() OVER(PARTITION BY category_id ORDER BY revenue DESC) AS rnk,
                            AVG(revenue) OVER(PARTITION BY category_id) AS category_avg
                  FROM sales ORDER BY revenue DESC LIMIT 300
            """,
            "train_retail_customer_running": f"""
                WITH monthly AS (
                    SELECT o.customer_id, date_trunc('month', o.ordered_at) AS month,
                           COUNT(DISTINCT oi.product_id) AS products,
                           SUM(oi.quantity * oi.unit_price) AS revenue
                    FROM retail.customer_orders o JOIN retail.order_items oi USING(order_id)
                    WHERE o.order_id <= {size} GROUP BY o.customer_id, month
                ) SELECT *, SUM(revenue) OVER(PARTITION BY customer_id ORDER BY month) AS running,
                            LAG(revenue) OVER(PARTITION BY customer_id ORDER BY month) AS previous
                  FROM monthly ORDER BY revenue DESC LIMIT 300
            """,
            "train_aviation_route_rank": f"""
                WITH routes AS (
                    SELECT f.departure_airport, f.arrival_airport,
                           COUNT(DISTINCT tf.ticket_no) AS passengers, SUM(tf.amount) AS revenue
                    FROM aviation.flights f JOIN aviation.ticket_flights tf USING(flight_id)
                    WHERE f.flight_id <= {size // 4}
                    GROUP BY f.departure_airport, f.arrival_airport
                ) SELECT *, DENSE_RANK() OVER(PARTITION BY departure_airport ORDER BY revenue DESC) AS rnk,
                            SUM(revenue) OVER(PARTITION BY departure_airport) AS total
                  FROM routes ORDER BY revenue DESC LIMIT 200
            """,
            "train_aviation_ticket_rank": f"""
                WITH tickets AS (
                    SELECT f.departure_airport, tf.ticket_no,
                           COUNT(DISTINCT f.flight_id) AS flights, SUM(tf.amount) AS revenue
                    FROM aviation.ticket_flights tf JOIN aviation.flights f USING(flight_id)
                    WHERE f.flight_id <= {size // 4}
                    GROUP BY f.departure_airport, tf.ticket_no
                ) SELECT *, ROW_NUMBER() OVER(PARTITION BY departure_airport ORDER BY revenue DESC) AS rnk,
                            AVG(revenue) OVER(PARTITION BY departure_airport) AS avg_revenue
                  FROM tickets ORDER BY revenue DESC LIMIT 300
            """,
            "validation_retail_inventory_rank": f"""
                WITH stock AS (
                    SELECT i.warehouse_id, p.category_id, i.product_id,
                           SUM(i.quantity - i.reserved_quantity) AS available
                    FROM retail.inventory i JOIN retail.products p USING(product_id)
                    WHERE i.product_id <= {size // 10}
                    GROUP BY i.warehouse_id, p.category_id, i.product_id
                ), ranked AS (
                    SELECT *, DENSE_RANK() OVER(PARTITION BY warehouse_id ORDER BY available DESC) AS rnk,
                              AVG(available) OVER(PARTITION BY category_id) AS average_stock
                    FROM stock
                ) SELECT * FROM ranked WHERE rnk <= 20 ORDER BY available DESC LIMIT 300
            """,
            "validation_aviation_monthly": f"""
                WITH monthly AS (
                    SELECT f.departure_airport, date_trunc('month', f.scheduled_departure) AS month,
                           COUNT(DISTINCT tf.ticket_no) AS passengers, SUM(tf.amount) AS revenue
                    FROM aviation.flights f JOIN aviation.ticket_flights tf USING(flight_id)
                    WHERE f.flight_id <= {size // 4} GROUP BY f.departure_airport, month
                ), changes AS (
                    SELECT *, LAG(revenue) OVER(PARTITION BY departure_airport ORDER BY month) AS prior,
                              SUM(revenue) OVER(PARTITION BY departure_airport ORDER BY month) AS running
                    FROM monthly
                ) SELECT * FROM changes WHERE revenue > 0 ORDER BY revenue DESC LIMIT 200
            """,
            "acceptance_retail_sales_stock": f"""
                WITH sales AS (
                    SELECT oi.product_id, COUNT(DISTINCT oi.order_id) AS orders,
                           SUM(oi.quantity) AS units, SUM(oi.quantity * oi.unit_price) AS revenue
                    FROM retail.order_items oi JOIN retail.customer_orders o USING(order_id)
                    WHERE o.order_id <= {size} AND o.order_status <> 'cancelled'
                    GROUP BY oi.product_id
                ), stock AS (
                    SELECT product_id, SUM(quantity - reserved_quantity) AS available,
                           COUNT(DISTINCT warehouse_id) AS warehouses
                    FROM retail.inventory GROUP BY product_id
                ), ranked AS (
                    SELECT c.category_id, p.product_id, p.product_name, COALESCE(s.revenue, 0) AS revenue,
                           COALESCE(st.available, 0) AS available,
                           DENSE_RANK() OVER(PARTITION BY c.category_id ORDER BY COALESCE(s.revenue, 0) DESC) AS rnk,
                           AVG(COALESCE(s.revenue, 0)) OVER(PARTITION BY c.category_id) AS average_revenue
                    FROM retail.products p JOIN retail.categories c USING(category_id)
                    LEFT JOIN sales s USING(product_id) LEFT JOIN stock st USING(product_id)
                    WHERE p.is_active
                ) SELECT * FROM ranked WHERE rnk <= 20 OR available = 0
                  ORDER BY category_id, rnk, available LIMIT 500
            """,
            "acceptance_aviation_capacity": f"""
                WITH revenue AS (
                    SELECT flight_id, COUNT(DISTINCT ticket_no) AS passengers, SUM(amount) AS revenue
                    FROM aviation.ticket_flights WHERE flight_id <= {size // 4} GROUP BY flight_id
                ), capacity AS (
                    SELECT aircraft_code, COUNT(*) AS seats FROM aviation.seats GROUP BY aircraft_code
                ), ranked AS (
                    SELECT f.flight_id, f.departure_airport, r.revenue,
                           r.passengers::numeric / NULLIF(c.seats, 0) AS occupancy,
                           DENSE_RANK() OVER(PARTITION BY f.departure_airport ORDER BY r.revenue DESC) AS rnk,
                           AVG(r.revenue) OVER(PARTITION BY f.departure_airport) AS average_revenue
                    FROM aviation.flights f JOIN revenue r USING(flight_id)
                    JOIN capacity c USING(aircraft_code)
                ) SELECT * FROM ranked WHERE rnk <= 10 ORDER BY revenue DESC LIMIT 300
            """,
        }
        for family, text in families.items():
            yield family.split("_", 1)[0], QueryCase(family, text.strip())


def enrich(frame):
    features = {text: extract_sql_features(text).as_dict() for text in frame.sql_text.unique()}
    for name in EXTENDED_SQL_FEATURES:
        frame[name] = frame.sql_text.map(lambda text: features[text][name])
    return frame


def evaluate(path, frame):
    model = load_joblib_model(path)
    predicted = model["pipeline"].predict(frame[model["model_features"]])
    if model["target_transform"] == "log1p":
        predicted = np.expm1(predicted)
    predicted = np.maximum(predicted, 0.001)
    actual = frame[TARGET_COLUMN].to_numpy()
    return {
        "queries": len(frame), "mae_ms": float(np.mean(np.abs(actual - predicted))),
        "median_q_error": float(np.median(np.maximum(actual, predicted) / np.maximum(np.minimum(actual, predicted), 0.001))),
        "within_20_percent": float(np.mean(np.abs(actual - predicted) <= np.maximum(actual, 0.001) * 0.2)),
    }


def aggregate(frame):
    first = {name: "first" for name in frame.columns if name not in ("template_id", "sql_text", TARGET_COLUMN)}
    return frame.groupby(["template_id", "sql_text"], as_index=False).agg({**first, TARGET_COLUMN: "median"})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/window-cte-development"))
    parser.add_argument("--reuse-measurements", action="store_true")
    parser.add_argument(
        "--baseline", type=Path,
        default=Path("models/xgboost/xgboost_query_time.joblib"),
        help="Frozen baseline weights; use the archived model to reproduce a previous comparison",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    samples_file = args.output / "measurements.csv"
    baseline = args.baseline
    initial_hash = hashlib.sha256(baseline.read_bytes()).hexdigest()
    manifest = [{"split": split, "template": case.template_id, "sql": case.sql_text} for split, case in workload()]
    manifest_path = args.output / "split-manifest.json"
    if not args.reuse_measurements:
        # Written before any measurements or model fitting.
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        rows, failures = [], []
        for number, (split, case) in enumerate(workload(), 1):
            try:
                collect_sample(case, persist=False)
                complete = [collect_sample(case, persist=False).values for _ in range(3)]
                rows.extend({**row, "split": split} for row in complete)
                print(f"{number}/{len(manifest)} {case.template_id}: {complete[-1][TARGET_COLUMN]:.1f} ms", flush=True)
            except Exception as exc:
                failures.append({"template": case.template_id, "error": str(exc)})
                print(f"SKIPPED {case.template_id}: {exc}", flush=True)
        if failures:
            (args.output / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
            raise RuntimeError("Workload incomplete; fix failures before fitting")
        with samples_file.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    fresh = enrich(pd.read_csv(samples_file))
    training = fresh[fresh.split == "train"].drop(columns="split")
    validation = aggregate(fresh[fresh.split == "validation"].drop(columns="split"))
    acceptance = aggregate(fresh[fresh.split == "acceptance"].drop(columns="split"))
    partitions = [set(frame.template_id) for frame in (training, validation, acceptance)]
    assert not any(partitions[i] & partitions[j] for i in range(3) for j in range(i))
    base = enrich(pd.read_csv("dataset/postgresql/multidomain_training.csv"))
    combined = args.output / "training.csv"
    pd.concat([base, training], ignore_index=True).to_csv(combined, index=False)
    candidates = {}
    for version in ("legacy", "sql-v2"):
        output = args.output / version
        train_xgboost(combined, output, tune=False, target_transform="identity", template_balanced=True, feature_version=version)
        candidates[version] = output / "xgboost_query_time.joblib"
    validation_results = {name: evaluate(path, validation) for name, path in candidates.items()}
    selected = min(candidates, key=lambda name: validation_results[name]["mae_ms"])
    regression = aggregate(base).sample(n=500, random_state=1023)
    report = {
        "protocol": "32 fresh SQL, 8 families; train/validation/acceptance assigned before collection; no calibration or sealed controls",
        "baseline_sha256": initial_hash,
        "train_queries": training.sql_text.nunique(),
        "validation": {"baseline": evaluate(baseline, validation), **validation_results},
        "selected_by_validation": selected,
        "acceptance": {"baseline": evaluate(baseline, acceptance), "selected": evaluate(candidates[selected], acceptance)},
        "regression_screen": {"note": "Existing data, not independent", "baseline": evaluate(baseline, regression), "selected": evaluate(candidates[selected], regression)},
        "primary_replaced": False,
    }
    assert hashlib.sha256(baseline.read_bytes()).hexdigest() == initial_hash
    (args.output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
