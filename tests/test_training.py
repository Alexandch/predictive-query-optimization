import math
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from pqo.training import (
    MODEL_FEATURES,
    predict_query_time,
    train_xgboost,
)


class TrainingTests(unittest.TestCase):
    def _synthetic_frame(self) -> pd.DataFrame:
        root_types = ("Seq Scan", "Index Scan", "Hash Join", "Aggregate")
        rows = []
        for index in range(160):
            template_number = index % 8
            cost = 10 + index * 1.5
            row = {
                "template_id": f"template_{template_number}",
                "sql_text": f"SELECT {index}",
                "query_length": 20 + template_number,
                "join_count": template_number % 3,
                "where_condition_count": 1 + template_number % 4,
                "subquery_count": template_number % 2,
                "has_group_by": template_number % 2 == 0,
                "has_order_by": template_number % 3 == 0,
                "has_distinct": template_number == 7,
                "table_reference_count": 1 + template_number % 3,
                "unique_table_count": 1 + template_number % 3,
                "select_expression_count": 1 + template_number % 4,
                "aggregate_function_count": template_number % 2,
                "inner_join_count": template_number % 3,
                "left_join_count": 0,
                "right_join_count": 0,
                "full_join_count": 0,
                "cross_join_count": 0,
                "estimated_startup_cost": cost * 0.2,
                "estimated_total_cost": cost,
                "estimated_plan_rows": 10 + index % 30,
                "estimated_plan_width": 16 + template_number,
                "estimated_rows_all_nodes": 20 + index % 50,
                "node_count": 1 + template_number % 5,
                "max_plan_depth": 1 + template_number % 3,
                "relation_count": 1 + template_number % 3,
                "seq_scan_count": template_number % 2,
                "index_scan_count": (template_number + 1) % 2,
                "index_only_scan_count": 0,
                "bitmap_heap_scan_count": 0,
                "hash_join_count": template_number % 2,
                "merge_join_count": 0,
                "nested_loop_count": (template_number + 1) % 2,
                "sort_node_count": template_number % 2,
                "aggregate_node_count": template_number % 2,
                "relation_row_estimate_sum": 100_000 + index * 100,
                "largest_relation_rows": 80_000 + index * 50,
                "relation_size_bytes": 10_000_000 + index * 1_000,
                "index_size_bytes": 2_000_000 + index * 100,
                "existing_index_count": 2 + template_number % 3,
                "estimated_selectivity": (10 + index % 30) / (80_000 + index * 50),
                "root_node_type": root_types[template_number % len(root_types)],
                "shared_hit_blocks": index % 20,
                "shared_read_blocks": index % 7,
                "temp_read_blocks": 0,
                "temp_written_blocks": 0,
                "actual_total_time_ms": 0.2 * cost + template_number,
            }
            rows.append(row)
        return pd.DataFrame(rows)

    def test_trains_saves_and_loads_model(self):
        frame = self._synthetic_frame()
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            dataset_path = directory / "dataset.csv"
            output_dir = directory / "model"
            frame.to_csv(dataset_path, index=False)

            metrics = train_xgboost(
                dataset_path,
                output_dir,
                random_state=7,
                tune=False,
            )
            prediction = predict_query_time(
                output_dir / "xgboost_query_time.joblib",
                frame.iloc[0][MODEL_FEATURES].to_dict(),
            )

            self.assertEqual(metrics.sample_count, 160)
            self.assertEqual(metrics.modeled_sample_count, 160)
            self.assertTrue(math.isfinite(metrics.mae_ms))
            self.assertGreaterEqual(prediction, 0)
            self.assertTrue((output_dir / "metrics.json").exists())
            self.assertTrue((output_dir / "feature_importance.json").exists())

    def test_rejects_too_small_dataset(self):
        frame = self._synthetic_frame().head(20)
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_path = Path(temp_dir) / "small.csv"
            frame.to_csv(dataset_path, index=False)

            with self.assertRaisesRegex(ValueError, "At least 100 samples"):
                train_xgboost(
                    dataset_path,
                    Path(temp_dir) / "model",
                    tune=False,
                )


if __name__ == "__main__":
    unittest.main()
