"""Fresh aggregation workload; never reads sealed logistics/CH controls.

Run from the repository root with the project's Python interpreter.
All measurements, candidate weights and reports go to artifacts/.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path
from statistics import median

import pandas as pd

from pqo.config import DatabaseSettings
from pqo.dataset import collect_sample
from pqo.query_case import QueryCase
from pqo.training import MODEL_FEATURES, TARGET_COLUMN, predict_query_time, train_xgboost


def workload():
    groups = {
        "region": "a.region",
        "segment": "c.segment",
        "carrier_channel": "s.carrier, o.sales_channel",
        "month_segment": "date_trunc('month', o.ordered_at), c.segment",
        # Whole grouping families reserved before collection, not used in fit.
        "region_segment": "a.region, c.segment",
        "payment_channel": "p.payment_method, o.sales_channel",
    }
    for family, grouping in groups.items():
        split = "holdout" if family in {"region_segment", "payment_channel"} else "train"
        for distinct in (False, True):
            template = f"dev_fanout_{family}_{'distinct' if distinct else 'plain'}"
            for ceiling in (10_000, 40_000, 100_000):
                aggregate = "COUNT(DISTINCT oi.product_id)" if distinct else "COUNT(oi.product_id)"
                sql = f"""
                    SELECT {grouping},
                           COUNT(DISTINCT o.order_id) AS orders,
                           {aggregate} AS products,
                           COUNT(DISTINCT p.payment_id) AS payments,
                           COUNT(DISTINCT s.shipment_id) AS shipments,
                           SUM(oi.quantity) AS units,
                           SUM(oi.quantity * oi.unit_price) AS revenue,
                           SUM(o.total_amount) AS order_value,
                           COUNT(DISTINCT s.shipment_id) FILTER
                               (WHERE s.shipment_status = 'delivered') AS delivered
                    FROM retail.customer_orders o
                    JOIN retail.customers c ON c.customer_id = o.customer_id
                    JOIN retail.addresses a ON a.address_id = o.shipping_address_id
                    JOIN retail.order_items oi ON oi.order_id = o.order_id
                    LEFT JOIN retail.payments p ON p.order_id = o.order_id
                    LEFT JOIN retail.shipments s ON s.order_id = o.order_id
                    WHERE o.order_id <= {ceiling} AND o.order_status <> 'cancelled'
                    GROUP BY {grouping}
                    HAVING COUNT(DISTINCT o.order_id) >= 2
                    ORDER BY revenue DESC LIMIT 200
                """
                yield split, QueryCase(template, sql.strip())


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evaluate(model, frame):
    errors = []
    ratios = []
    for row in frame.to_dict("records"):
        actual = float(row[TARGET_COLUMN])
        prediction = predict_query_time(model, row)
        errors.append(abs(actual - prediction))
        ratios.append(max(actual, prediction, 0.001) / max(min(actual, prediction), 0.001))
    return {"count": len(errors), "mae_ms": sum(errors) / len(errors),
            "median_q_error": median(ratios)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/aggregation-development"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--reuse-measurements", action="store_true")
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    args.output.mkdir(parents=True, exist_ok=True)
    samples_path = args.output / "measurements.csv"
    baseline = Path("models/xgboost/xgboost_query_time.joblib")
    base_path = Path("dataset/postgresql/multidomain_training.csv")
    initial_hash = file_hash(baseline)
    if not args.reuse_measurements:
        samples, failures = [], []
        cases = list(workload())
        for number, (split, case) in enumerate(cases, 1):
            try:
                collect_sample(case, persist=False)  # one warmup
                for repeat in range(args.repetitions):
                    sample = collect_sample(case, persist=False)
                    samples.append({**sample.values, "development_split": split})
                print(f"{number}/{len(cases)} {case.template_id}: {sample.values[TARGET_COLUMN]:.1f} ms", flush=True)
            except Exception as exc:
                # Incomplete measurements never become training targets.
                samples = [row for row in samples if row["sql_text"] != case.sql_text]
                failures.append({"template": case.template_id, "error": str(exc)})
                print(f"{number}/{len(cases)} skipped: {exc}", flush=True)
        if not samples:
            raise RuntimeError("No complete measurements")
        with samples_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
            writer.writeheader()
            writer.writerows(samples)
        (args.output / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")

    fresh = pd.read_csv(samples_path)
    train = fresh[fresh.development_split == "train"].drop(columns="development_split")
    holdout = fresh[fresh.development_split == "holdout"].drop(columns="development_split")
    if train.empty or holdout.empty:
        raise RuntimeError("Both independent development partitions are required")
    if set(train.sql_text) & set(holdout.sql_text) or set(train.template_id) & set(holdout.template_id):
        raise RuntimeError("Development split leaks queries or template families")
    base = pd.read_csv(base_path)
    combined = args.output / "training.csv"
    pd.concat([base, train], ignore_index=True).to_csv(combined, index=False)
    candidate_dir = args.output / "candidate"
    train_xgboost(combined, candidate_dir, tune=False, target_transform="identity", template_balanced=True)
    candidate = candidate_dir / "xgboost_query_time.joblib"
    aggregation = {name: "first" for name in MODEL_FEATURES}
    aggregation[TARGET_COLUMN] = "median"
    held = holdout.groupby(["template_id", "sql_text"], as_index=False).agg(aggregation)
    # Regression screen on an existing-data sample, explicitly not an independent control.
    regression = base.groupby(["template_id", "sql_text"], as_index=False).agg(aggregation).sample(n=min(500, len(base.drop_duplicates("sql_text"))), random_state=1022)
    report = {
        "protocol": "Fresh retail aggregation families; grouping families held out before collection; no sealed controls read",
        "baseline_sha256": initial_hash,
        "training_queries": int(train.sql_text.nunique()),
        "heldout_queries": len(held),
        "heldout_baseline": evaluate(baseline, held),
        "heldout_candidate": evaluate(candidate, held),
        "existing_data_baseline": evaluate(baseline, regression),
        "existing_data_candidate": evaluate(candidate, regression),
        "existing_data_note": "Regression screen only: the primary model has seen these data",
        "primary_model_replaced": False,
    }
    if file_hash(baseline) != initial_hash:
        raise RuntimeError("Primary model unexpectedly changed")
    (args.output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
