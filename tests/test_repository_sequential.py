import unittest
from dataclasses import replace
from contextlib import nullcontext

from pqo.index_actions import IndexAction
from pqo.repository import save_sequential_analysis
from pqo.sequential_recommendation import (
    SequentialCandidateCheck,
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
    def test_persists_rejected_and_accepted_trials_with_nullable_measurements(self):
        import json
        connection = _Connection()
        action = _plan().steps[0].action
        checks = (
            SequentialCandidateCheck(action, 1, 0.9, False, "statement_timeout", 12),
            SequentialCandidateCheck(action, 1, 0.8, True, None, 12, 0.0, 8192, 1.5),
        )
        save_sequential_analysis("SELECT 1", replace(_plan(), candidate_checks=checks),
                                 connection=connection)
        evidence = connection.calls[1][1][-2].obj
        evidence = json.loads(json.dumps(evidence))
        self.assertEqual(evidence["version"], 1)
        self.assertEqual(evidence["max_candidates_per_step"], 3)
        self.assertIsNone(evidence["checks"][0]["after_time_ms"])
        self.assertEqual(evidence["checks"][1]["after_time_ms"], 0)
        self.assertFalse(evidence["checks"][0]["accepted"])
        self.assertIn("CREATE INDEX", evidence["checks"][1]["proposed_ddl"])

    def test_timeout_does_not_become_exact_execution_time(self):
        connection = _Connection()
        save_sequential_analysis(
            "SELECT 1", replace(_plan(), baseline_timed_out=True),
            connection=connection,
        )
        self.assertIsNone(connection.calls[0][1][-1])
        self.assertTrue(connection.calls[1][1][-1])

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
