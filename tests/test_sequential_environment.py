import unittest
from unittest.mock import Mock, patch

from pqo.config import DatabaseSettings
from pqo.sequential_environment import (
    SequentialIndexEnvironment,
    _is_statement_timeout,
    _plan_index_names,
)


class SequentialEnvironmentTests(unittest.TestCase):
    def test_validates_budget_steps_and_warmup(self):
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(max_steps=0)
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(storage_budget_bytes=0)
        with self.assertRaises(ValueError):
            SequentialIndexEnvironment(warmup_runs=-1)

    def test_extracts_index_names_from_nested_json_plan(self):
        plan = [
            {
                "Plan": {
                    "Node Type": "Nested Loop",
                    "Plans": [
                        {"Node Type": "Index Scan", "Index Name": "pqo_seq_a"},
                        {
                            "Node Type": "Bitmap Index Scan",
                            "Index Name": "pqo_seq_b",
                        },
                    ],
                }
            }
        ]

        self.assertEqual(_plan_index_names(plan), {"pqo_seq_a", "pqo_seq_b"})

    def test_uses_configured_statement_timeout_by_default(self):
        settings = DatabaseSettings(statement_timeout_ms=45_000)

        environment = SequentialIndexEnvironment(settings)

        self.assertEqual(environment.statement_timeout_ms, 45_000)

    def test_recognizes_baseline_statement_timeout(self):
        error = RuntimeError("canceling statement due to statement timeout")
        error.sqlstate = "57014"

        self.assertTrue(_is_statement_timeout(error))
        self.assertFalse(_is_statement_timeout(RuntimeError("other")))

    def test_baseline_timeout_recovers_transaction_and_keeps_episode_usable(self):
        settings = DatabaseSettings(statement_timeout_ms=12_000)
        environment = SequentialIndexEnvironment(
            settings,
            warmup_runs=1,
            repetitions=1,
        )
        connection = Mock()
        timeout = RuntimeError("canceling statement due to statement timeout")
        timeout.sqlstate = "57014"
        environment._measure = Mock(side_effect=timeout)

        with patch(
            "pqo.sequential_environment.generate_sequential_index_actions",
            return_value=(),
        ):
            episode = environment._start_episode(
                "SELECT * FROM aviation.flights", connection
            )

        self.assertTrue(episode.state.baseline_timed_out)
        self.assertEqual(episode.state.baseline_time_ms, 12_000.0)
        self.assertEqual(episode.state.baseline_samples_ms, ())
        connection.rollback.assert_called_once_with()
        statements = [call.args[0] for call in connection.execute.call_args_list]
        self.assertIn("statement_timeout", statements[0])
        self.assertIn("lock_timeout", statements[1])
        self.assertIn("pg_advisory_xact_lock", statements[2])


if __name__ == "__main__":
    unittest.main()
