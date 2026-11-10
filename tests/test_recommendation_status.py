from contextlib import nullcontext
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from pqo.config import DatabaseSettings
from pqo.recommendation_status import (
    RecommendationStatusConflict,
    set_index_recommendation_status,
)


class RecommendationStatusTests(unittest.TestCase):
    def connection(self, ready=True):
        connection = MagicMock()
        connection.transaction.return_value = nullcontext()
        connection.execute.return_value.fetchone.side_effect = [(ready,), ("0", "0")]
        return connection

    def test_calls_parameterized_procedure_without_ddl_or_direct_update(self):
        connection = self.connection()
        result = set_index_recommendation_status(7, "proposed", "accepted", connection=connection)
        self.assertEqual((result.recommendation_id, result.previous_status, result.status), (7, "proposed", "accepted"))
        queries = [call.args[0] for call in connection.execute.call_args_list]
        self.assertEqual(queries[-2], "CALL pqo.set_recommendation_status(%s, %s, %s)")
        self.assertEqual(connection.execute.call_args_list[-2].args[1], (7, "proposed", "accepted"))
        self.assertFalse(any("UPDATE " in sql or "CREATE " in sql for sql in queries))
        self.assertEqual(connection.execute.call_args.args[1], ("0", "0"))
        connection.commit.assert_not_called()

    def test_invalid_inputs_do_not_connect(self):
        for args in ((True, "proposed", "accepted"), (0, "proposed", "accepted"),
                     (1.5, "proposed", "accepted"), (1, "other", "accepted"),
                     (1, "proposed", "DROP TABLE"), (1, "accepted", "accepted")):
            with self.subTest(args=args), patch("psycopg.connect") as connect:
                with self.assertRaises(ValueError):
                    set_index_recommendation_status(*args)
                connect.assert_not_called()

    def test_missing_schema_is_an_explicit_migration_not_hidden_ddl(self):
        connection = self.connection(ready=False)
        with self.assertRaisesRegex(RuntimeError, "013_recommendation_status.sql"):
            set_index_recommendation_status(7, "proposed", "accepted", connection=connection)
        self.assertEqual(connection.execute.call_count, 1)

    def test_stale_conflict_is_not_retried(self):
        connection = self.connection()
        def execute(sql, params=None):
            if sql.startswith("CALL "):
                raise psycopg.errors.ObjectNotInPrerequisiteState("stale")
            return MagicMock(fetchone=lambda: (True,))
        connection.execute.side_effect = execute
        with self.assertRaises(RecommendationStatusConflict):
            set_index_recommendation_status(7, "proposed", "accepted", connection=connection)
        self.assertEqual(sum(call.args[0].startswith("CALL ") for call in connection.execute.call_args_list), 1)

    def test_owned_connection_and_bounded_timeouts(self):
        connection = self.connection()
        settings = DatabaseSettings(statement_timeout_ms=100)
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            set_index_recommendation_status(7, "proposed", "rejected", settings)
        connect.assert_called_once_with(**settings.connection_kwargs())
        self.assertEqual(connection.execute.call_args_list[2].args[1], ("100", "50"))

    def test_server_errors_are_not_success(self):
        connection = self.connection()
        for error, expected in ((psycopg.errors.NoDataFound("missing"), ValueError),
                                (psycopg.errors.CheckViolation("invalid"), psycopg.errors.CheckViolation),
                                (psycopg.errors.QueryCanceled("timeout"), psycopg.errors.QueryCanceled)):
            def execute(sql, params=None):
                if sql.startswith("CALL "):
                    raise error
                return MagicMock(fetchone=lambda: (True,))
            connection.execute.side_effect = execute
            with self.assertRaises(expected):
                set_index_recommendation_status(7, "proposed", "accepted", connection=connection)
