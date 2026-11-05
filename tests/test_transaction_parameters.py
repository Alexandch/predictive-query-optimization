import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch

from sqlglot import exp, parse_one
from pqo.config import DatabaseSettings
from pqo.index_actions import IndexAction
from pqo.transaction_parameters import (
    ParameterValue, ParameterSlot, PARAMETER_TYPES, discover_transaction_parameters,
    bind_transaction_parameters, is_typed_constant,
)
from pqo.transaction_indexes import RelationIndexes, build_transaction_index_report
from pqo.transaction_planning import StatementPlan, TransactionPlanReport, collect_transaction_plans
from pqo.transaction_index_trial import LAB_DATABASE, run_transaction_index_set_trial


def university_bindings():
    return tuple(ParameterValue(number, position, kind, value) for number, position, kind, value in (
        (1, 1, "integer", "17001"), (1, 2, "integer", "50"),
        (2, 1, "integer", "350"), (2, 2, "integer", "200"),
        (3, 1, "text", "completed"), (3, 2, "integer", "17001"), (3, 3, "integer", "350")))


class TransactionParameterTests(TestCase):
    def bind(self, kind, value="", is_null=False, statement="SELECT $1"):
        return bind_transaction_parameters(f"BEGIN; {statement}; ROLLBACK;", (ParameterValue(1, 1, kind, value, is_null),))

    def test_parameters_are_per_statement_and_repeated_occurrences_share_one_value(self):
        text = "BEGIN; SELECT $1, $1; SELECT $1; ROLLBACK;"
        self.assertEqual(discover_transaction_parameters(text), (ParameterSlot(1, 1), ParameterSlot(2, 1)))
        result = bind_transaction_parameters(text, (ParameterValue(1, 1, "integer", "10"), ParameterValue(2, 1, "integer", "20")))
        self.assertEqual(result.source_sql, text)
        self.assertEqual(result.sql_text.count("CAST(10 AS pg_catalog.int4)"), 2)
        self.assertEqual(result.sql_text.count("CAST(20 AS pg_catalog.int4)"), 1)
        self.assertFalse(discover_transaction_parameters(result.sql_text))

    def test_placeholders_in_comments_strings_and_dollar_quoted_literals_are_not_values(self):
        text = "BEGIN; SELECT '$1', $$ $2 $$, $1 /* $3 */; -- $4\nROLLBACK;"
        self.assertEqual(discover_transaction_parameters(text), (ParameterSlot(1, 1),))
        result = bind_transaction_parameters(text, (ParameterValue(1, 1, "integer", "7"),))
        self.assertIn("'$1'", result.sql_text)
        self.assertIn("$2", result.sql_text)

    def test_all_supported_types_and_explicit_typed_null(self):
        values = {"integer": "-2147483648", "bigint": "9223372036854775807", "numeric": "12345.6700",
            "double precision": "1.25e4", "boolean": "true", "text": " NULL ", "date": "2026-10-31",
            "timestamp": "2026-10-31 12:34:56.123456", "timestamptz": "2026-10-31T12:34:56+03:00",
            "uuid": "550e8400-e29b-41d4-a716-446655440000"}
        for kind, value in values.items():
            with self.subTest(kind=kind):
                result = self.bind(kind, value)
                node = parse_one(result.inspection.statements[0].sql, read="postgres").expressions[0]
                self.assertTrue(is_typed_constant(node))
                self.assertIn(PARAMETER_TYPES[kind], result.sql_text)
                self.assertIn("NULL", self.bind(kind, is_null=True).sql_text)
        self.assertNotEqual(self.bind("text", "NULL").sql_text, self.bind("text", is_null=True).sql_text)
        self.assertNotEqual(self.bind("text", "").sql_text, self.bind("text", is_null=True).sql_text)

    def test_invalid_values_limits_and_sensitive_error_text(self):
        cases = (("integer", "2147483648"), ("bigint", "-9223372036854775809"), ("numeric", "NaN"),
            ("numeric", "1e9999"), ("double precision", "Infinity"), ("double precision", "1e999"),
            ("double precision", "1e-999"),
            ("boolean", "yes"), ("date", "2026-02-30"), ("date", "tomorrow"),
            ("timestamp", "2026-10-31T01:00:00Z"), ("timestamp", "2026-10-31T01:00:00.123456789"),
            ("timestamptz", "2026-10-31T01:00:00"), ("uuid", "secret-invalid-value"),
            ("text", "\x00"), ("text", "\ud800"), ("text", "x" * 4001), ("my_schema.type", "123"))
        for kind, value in cases:
            with self.subTest(kind=kind, length=len(value)), self.assertRaises(ValueError) as caught:
                self.bind(kind, value)
            self.assertNotIn("secret-invalid-value", str(caught.exception))
        with self.assertRaises(ValueError):
            self.bind("text", "not empty", is_null=True)

    def test_missing_extra_duplicate_gaps_and_unsupported_placeholders_refused_offline(self):
        for statement in ("SELECT $0", "SELECT $65", "SELECT $2", "SELECT $bad", "SELECT :name", "SELECT ?"):
            with self.subTest(statement=statement), patch("psycopg.connect") as connect, self.assertRaises(ValueError):
                bind_transaction_parameters(f"BEGIN; {statement}; COMMIT;", ())
            connect.assert_not_called()
        value = ParameterValue(1, 1, "integer", "1")
        for values in ((), (value, value), (value, replace(value, statement_number=2))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                bind_transaction_parameters("BEGIN; SELECT $1; COMMIT;", values)

    def test_order_by_keeps_cast_and_identifier_parameters_cannot_change_structure(self):
        result = self.bind("integer", "1", statement="SELECT 10 AS a, 20 AS b ORDER BY $1")
        order = parse_one(result.inspection.statements[0].sql, read="postgres").args["order"].expressions[0].this
        self.assertIsInstance(order, exp.Cast)
        with self.assertRaises(ValueError):
            self.bind("text", "university.students", statement="SELECT * FROM $1")

    def test_total_parameter_limit_and_comment_only_trivia_are_not_extra_commands(self):
        with self.assertRaises(ValueError):
            discover_transaction_parameters("BEGIN; " + "; ".join("SELECT $1, $2, $3" for _ in range(43)) + "; COMMIT;")
        result = bind_transaction_parameters("BEGIN; -- $9\nSELECT $1; -- $4\nROLLBACK; -- $8\n", (ParameterValue(1, 1, "integer", "1"),))
        self.assertEqual(len(result.inspection.statements), 1)

    def test_quote_backslash_and_injection_payload_remain_one_literal(self):
        for value in ("'); DELETE FROM university.students; --", "\\'; SELECT dangerous(); --", "кириллица 😀\n'\\$1"):
            with self.subTest(value=value):
                result = self.bind("text", value)
                self.assertEqual(len(result.inspection.statements), 1)
                self.assertFalse(result.inspection.requires_test_copy)
                self.assertEqual(result.inspection.statements[0].relation_names, ())

    def test_bound_literal_is_a_candidate_value_not_a_function_on_the_column(self):
        text = "BEGIN; SELECT enrollment_id FROM university.enrollments WHERE student_id=$1 ORDER BY registered_at; ROLLBACK;"
        result = bind_transaction_parameters(text, (ParameterValue(1, 1, "integer", "17001"),))
        plans = TransactionPlanReport(result.inspection, "source", (StatementPlan(1, "planned", "fixture"),))
        report = build_transaction_index_report(result.inspection, plans, {("university", "enrollments"):
            RelationIndexes("university", "enrollments", ("student_id", "registered_at", "enrollment_id"))})
        self.assertTrue(any(candidate.action.key_columns == ("student_id", "registered_at") for candidate in report.candidates))
        self.assertFalse(is_typed_constant(parse_one("CAST(LOWER('x') AS pg_catalog.text)", read="postgres")))
        self.assertFalse(is_typed_constant(parse_one("CAST('x' AS unsafe.custom_type)", read="postgres")))


@skipUnless(os.getenv("PQO_TRANSACTION_TRIAL_TESTS") == "1", "requires dedicated university index lab")
class TransactionParameterPostgresTests(TestCase):
    def settings(self):
        return replace(DatabaseSettings.from_env(), dbname=LAB_DATABASE, allowed_schemas=frozenset({"university"}))

    def test_text_values_survive_generator_guard_and_postgres_with_exact_semantics(self):
        import psycopg
        from datetime import date, datetime
        from decimal import Decimal
        from uuid import UUID
        from pqo.explain import _assert_read_only_query
        with psycopg.connect(**self.settings().connection_kwargs()) as connection:
            with connection.transaction(force_rollback=True):
                connection.execute("SET TRANSACTION READ ONLY")
                connection.execute("SET LOCAL standard_conforming_strings = on")
                for value in ("", "NULL", "a\\b", "\\'; DELETE FROM university.students; --", "строка 😀\n'\\$1"):
                    bound = bind_transaction_parameters("BEGIN; SELECT $1; ROLLBACK;", (ParameterValue(1, 1, "text", value),))
                    query = _assert_read_only_query(bound.inspection.statements[0].sql)
                    self.assertEqual(connection.execute(query).fetchone()[0], value)
                cases = (("integer", "-2147483648", -2147483648),
                    ("bigint", "9223372036854775807", 9223372036854775807),
                    ("numeric", "12345.6700", Decimal("12345.6700")),
                    ("double precision", "1.25e4", 12500.), ("boolean", "false", False),
                    ("date", "2026-10-31", date(2026, 10, 31)),
                    ("timestamp", "2026-10-31T12:34:56.123456", datetime(2026, 10, 31, 12, 34, 56, 123456)),
                    ("timestamptz", "2026-10-31T12:34:56+03:00", datetime.fromisoformat("2026-10-31T12:34:56+03:00")),
                    ("uuid", "550e8400-e29b-41d4-a716-446655440000", UUID("550e8400-e29b-41d4-a716-446655440000")))
                for kind, value, expected in cases:
                    bound = bind_transaction_parameters("BEGIN; SELECT $1; ROLLBACK;", (ParameterValue(1, 1, kind, value),))
                    query = _assert_read_only_query(bound.inspection.statements[0].sql)
                    self.assertEqual(connection.execute(query).fetchone()[0], expected)

    def test_parameterized_plans_and_two_index_trial_do_not_replay_dml(self):
        import psycopg
        settings = self.settings()
        source = (Path(__file__).resolve().parents[1] / "examples/transactions/university-parameters.sql").read_text(encoding="utf-8")
        bound = bind_transaction_parameters(source, university_bindings())
        def snapshot():
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                return (connection.execute("SELECT * FROM university.enrollments WHERE student_id=17001 ORDER BY enrollment_id").fetchall(),
                    connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall())
        before = snapshot()
        plans = collect_transaction_plans(bound.sql_text, settings)
        self.assertTrue(all(item.status == "planned" for item in plans.statements))
        actions = tuple(IndexAction.create("university", "enrollments", (key, "registered_at")) for key in ("student_id", "offering_id"))
        report = run_transaction_index_set_trial(bound.sql_text, replace(settings, dbname="pqo_university_unseen"),
            LAB_DATABASE, actions, test_copy_confirmed=True)
        self.assertEqual(report.status, "measured")
        self.assertEqual(report.skipped_writes, (3,))
        self.assertTrue(report.rollback_verified)
        self.assertEqual(len(report.used_index_names), 2)
        self.assertEqual(snapshot(), before)
