import unittest
from contextlib import nullcontext

from pqo.index_actions import IndexAction
from pqo.repository import save_sequential_analysis
from pqo.sequential_recommendation import (
    SequentialRecommendationPlan,
    SequentialRecommendationStep,
)


class _Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, existing_sql=None):
        self.existing_sql = existing_sql
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, parameters=None):
        self.calls.append((sql, parameters))
        if "INSERT INTO pqo.query_run" in sql:
            return _Result((41,))
        if "SELECT sql_text FROM pqo.query_run" in sql:
            return _Result(
                None if self.existing_sql is None else (self.existing_sql,)
            )
        if "INSERT INTO pqo.sequential_analysis (" in sql:
            return _Result((17,))
        return _Result()


def _plan():
    action = IndexAction.create("public", "orders", ("customer_id",))
    step = SequentialRecommendationStep(
        action=action,
        predicted_q=0.8,
        measured_reward=0.4,
        before_time_ms=12.0,
        after_time_ms=7.0,
        index_size_bytes=8192,
        creation_time_ms=1.5,
        used_by_postgresql=True,
    )
    return SequentialRecommendationPlan(
        steps=(step,),
        baseline_time_ms=12.0,
        final_time_ms=7.0,
        storage_budget_bytes=1024 * 1024,
        used_budget_bytes=8192,
        candidate_count=3,
        decision_threshold=0.1,
        minimum_baseline_time_ms=5.0,
        minimum_absolute_improvement_ms=1.0,
        terminal_reason="max_steps",
    )


class SequentialRepositoryTests(unittest.TestCase):
    def test_saves_new_query_plan_and_step_atomically(self):
        connection = _Connection()

        result = save_sequential_analysis(
            " SELECT * FROM public.orders ",
            _plan(),
            connection=connection,
            model_version="model.pt",
        )

        self.assertEqual(result, (41, 17))
        self.assertEqual(len(connection.calls), 3)
        self.assertIn("CREATE INDEX ON", connection.calls[2][1][6])
        self.assertEqual(connection.calls[2][1][0:2], (17, 1))

    def test_rejects_mismatched_existing_query_run(self):
        connection = _Connection(existing_sql="SELECT 1")

        with self.assertRaisesRegex(ValueError, "does not match"):
            save_sequential_analysis(
                "SELECT 2", _plan(), query_run_id=9, connection=connection
            )


if __name__ == "__main__":
    unittest.main()
