import os
from dataclasses import replace
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

import psycopg

from pqo.analysis_control import AnalysisCancelled, AnalysisControl
from pqo.config import DatabaseSettings
from pqo.transaction_inspection import inspect_transaction
from pqo.transaction_planning import StatementPlan, TransactionPlanReport, collect_transaction_plans
from pqo.transaction_indexes import (
    ExistingIndex, RelationIndexes, build_transaction_index_report,
    collect_transaction_index_candidates, _load_relation_indexes, read_statements_for_index,
)
from pqo.index_actions import IndexAction


class TransactionIndexTests(TestCase):
    def setUp(self):
        self.settings = replace(DatabaseSettings(), allowed_schemas=frozenset({"university"}))
        self.students = RelationIndexes("university", "students",
            ("student_id", "campus_id", "full_name", "enrolled_at"),
            (ExistingIndex("students_pkey", ("student_id",)),))
        self.enrollments = RelationIndexes("university", "enrollments",
            ("enrollment_id", "student_id", "offering_id", "status", "registered_at"))
        self.metadata = {("university", "students"): self.students,
                         ("university", "enrollments"): self.enrollments}

    def plan(self, body):
        text = "BEGIN; " + body + "; ROLLBACK;"
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, self.settings.dbname, tuple(
            StatementPlan(item.number, "planned", "estimated", [{"Plan": {"Node Type": "Seq Scan",
                "Relation Name": "students", "Total Cost": 100}}]) for item in inspection.statements))
        return text, inspection, plans

    def report(self, body, metadata=None):
        _, inspection, plans = self.plan(body)
        return build_transaction_index_report(inspection, plans, self.metadata if metadata is None else metadata)

    def test_shared_candidates_deduplicated_and_primary_key_not_proposed_again(self):
        report = self.report("""SELECT campus_id FROM university.students WHERE student_id=1;
            SELECT full_name FROM university.students WHERE student_id=2;
            UPDATE university.students SET campus_id=7 WHERE student_id=3""")
        self.assertEqual(len({item.action for item in report.candidates}), len(report.candidates))
        plain = next(item for item in report.candidates if not item.action.include_columns)
        self.assertEqual(plain.covered_by, ("students_pkey",))
        self.assertEqual(plain.status, "covered")
        self.assertEqual([use.statement_number for use in plain.uses], [1, 2, 3])
        self.assertEqual([use.statement_number for use in plain.read_uses], [1, 2])
        self.assertEqual([(use.statement_number, use.statement_kind) for use in plain.write_lookup_uses], [(3, "update")])
        covering = next(item for item in report.candidates if item.action.include_columns == ("campus_id",))
        self.assertEqual(covering.status, "covering_review")
        self.assertEqual(covering.writes[0].changed_index_columns, ("campus_id",))
        self.assertIn("HOT", covering.writes[0].explanation)
        self.assertEqual(plain.writes[0].changed_index_columns, ())
        self.assertIn("не гарантированы", plain.writes[0].explanation)

    def test_insert_delete_and_writing_cte_are_reported_for_the_actual_target_only(self):
        report = self.report("""SELECT student_id FROM university.students WHERE campus_id=2;
            INSERT INTO university.students(student_id,campus_id) VALUES (5,2);
            WITH gone AS (DELETE FROM university.students WHERE student_id=5 RETURNING student_id) SELECT * FROM gone;
            UPDATE university.enrollments SET status='active' WHERE student_id=5""")
        item = next(c for c in report.candidates if c.action.table_name == "students" and c.action.key_columns == ("campus_id",))
        self.assertEqual([(w.statement_number, w.kind) for w in item.writes], [(2, "insert"), (3, "delete")])
        self.assertTrue(any(c.action.table_name == "enrollments" for c in report.candidates))

    def test_dml_only_key_is_not_a_select_use_in_shared_workload(self):
        text = (Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql").read_text(encoding="utf-8")
        inspection = inspect_transaction(text)
        plans = TransactionPlanReport(inspection, self.settings.dbname, tuple(
            StatementPlan(item.number, "planned", "estimated") for item in inspection.statements))
        report = build_transaction_index_report(inspection, plans, self.metadata)
        dml = next(c for c in report.candidates if c.action.key_columns == ("offering_id",) and not c.action.include_columns)
        self.assertFalse(dml.read_uses)
        self.assertEqual([use.statement_number for use in dml.write_lookup_uses], [3])
        self.assertFalse(read_statements_for_index(inspection, dml.action, self.metadata))
        read = IndexAction.create("university", "enrollments", ("student_id", "registered_at"))
        self.assertEqual(read_statements_for_index(inspection, read, self.metadata), (1, 2))

    def test_read_relevance_excludes_writing_ctes_projection_or_and_ambiguous_binding(self):
        action = IndexAction.create("university", "students", ("campus_id",))
        for body in (
            "SELECT campus_id FROM university.students WHERE student_id=1",
            "WITH x AS (DELETE FROM university.students WHERE campus_id=1 RETURNING *) SELECT * FROM x",
            "SELECT * FROM university.students WHERE campus_id=1 OR student_id=1",
            "SELECT * FROM university.students s JOIN university.students t ON true WHERE campus_id=1",
            "SELECT campus_id+1 AS campus_id FROM university.students ORDER BY campus_id",
        ):
            with self.subTest(body=body):
                self.assertFalse(read_statements_for_index(inspect_transaction(f"BEGIN; {body}; ROLLBACK;"), action, self.metadata))
        for body in (
            "SELECT * FROM university.students WHERE campus_id=1",
            "SELECT campus_id AS place FROM university.students ORDER BY place",
            "WITH x AS (SELECT * FROM university.students WHERE campus_id=1) SELECT * FROM x",
            "SELECT s.student_id FROM university.students s WHERE EXISTS (SELECT 1 FROM university.enrollments s WHERE s.offering_id=1) ORDER BY s.campus_id",
        ):
            with self.subTest(body=body):
                self.assertEqual(read_statements_for_index(inspect_transaction(f"BEGIN; {body}; ROLLBACK;"), action, self.metadata), (1,))

    def test_partial_expression_invalid_or_nondefault_indexes_do_not_cover(self):
        metadata = dict(self.metadata)
        metadata["university", "students"] = replace(self.students, indexes=(
            ExistingIndex("partial", ("campus_id",), compatible=False),
            ExistingIndex("expression", (None,), compatible=False),
            ExistingIndex("invalid", ("campus_id",), compatible=False)))
        report = self.report("SELECT student_id FROM university.students WHERE campus_id=1", metadata)
        self.assertTrue(report.candidates)
        self.assertTrue(all(not item.covered_by for item in report.candidates))

    def test_include_and_leading_prefix_coverage_are_distinguished(self):
        metadata = dict(self.metadata)
        metadata["university", "students"] = replace(self.students, indexes=(
            ExistingIndex("wide", ("campus_id", "student_id"), ("full_name",)),))
        report = self.report("SELECT full_name FROM university.students WHERE campus_id=1", metadata)
        self.assertTrue(all(item.covered_by == ("wide",) for item in report.candidates))
        other = self.report("SELECT full_name FROM university.students WHERE student_id=1", metadata)
        self.assertTrue(all(not item.covered_by for item in other.candidates))

    def test_nested_aliases_and_cte_columns_not_bound_to_unrelated_base_tables(self):
        report = self.report("""SELECT s.student_id FROM university.students s WHERE EXISTS
            (SELECT 1 FROM university.enrollments s WHERE s.offering_id=3)""")
        self.assertTrue(report.candidates)
        self.assertTrue(all(c.action.table_name == "enrollments" for c in report.candidates))
        self.assertTrue(all(c.action.key_columns == ("offering_id",) for c in report.candidates))
        cte = self.report("""WITH x AS (SELECT campus_id AS student_id FROM university.students)
            SELECT * FROM x WHERE student_id=1""")
        self.assertFalse(cte.candidates)  # No guessed CTE lineage or pushed predicates.

    def test_or_non_sargable_and_ambiguous_columns_not_combined(self):
        report = self.report("SELECT student_id FROM university.students WHERE campus_id=1 OR student_id=2")
        self.assertFalse(report.candidates)
        self.assertTrue(any("OR" in note for note in report.notes))
        self.assertFalse(self.report("SELECT student_id FROM university.students WHERE LOWER(full_name)='x'").candidates)
        ambiguous = self.report("""SELECT student_id FROM university.students s
            JOIN university.enrollments e ON true WHERE student_id=1""")
        self.assertFalse(ambiguous.candidates)
        self.assertTrue(any("неоднозначный" in note for note in ambiguous.notes))

    def test_computed_sort_alias_is_not_mistaken_for_a_base_column(self):
        report = self.report("SELECT campus_id+1 AS student_id FROM university.students ORDER BY student_id")
        self.assertFalse(report.candidates)
        direct = self.report("SELECT campus_id AS place FROM university.students ORDER BY place")
        self.assertTrue(all(c.action.key_columns == ("campus_id",) for c in direct.candidates))

    def test_equality_prefix_and_range_ordering_variants_bounded_to_three_keys(self):
        report = self.report("""SELECT enrollment_id FROM university.enrollments
            WHERE registered_at>DATE '2025-01-01' AND student_id=1 ORDER BY offering_id""")
        # DATE is a cast, deliberately outside the current simple literal rule.
        literals = self.report("""SELECT enrollment_id FROM university.enrollments
            WHERE offering_id>3 AND student_id=1 ORDER BY registered_at""")
        keys = {item.action.key_columns for item in literals.candidates}
        self.assertIn(("student_id", "offering_id", "registered_at"), keys)
        self.assertIn(("student_id", "registered_at", "offering_id"), keys)
        self.assertTrue(all(len(key) <= 3 for key in keys))
        self.assertTrue(report.candidates)

    def test_scan_evidence_does_not_conflate_same_table_name_across_schemas(self):
        text, inspection, plans = self.plan("SELECT * FROM university.students WHERE campus_id=1")
        metadata = dict(self.metadata)
        metadata["other", "students"] = replace(self.students, schema="other")
        report = build_transaction_index_report(inspection, plans, metadata)
        self.assertTrue(all(not use.observed_scan_nodes for c in report.candidates for use in c.uses))

    def test_collect_reads_catalogs_only_and_rejects_stale_sql_before_connect(self):
        text, _, plans = self.plan("SELECT campus_id FROM university.students WHERE student_id=1")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("pqo.transaction_indexes.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_indexes._load_relation_indexes", return_value=self.students):
            report = collect_transaction_index_candidates(text, self.settings, plans=plans)
        self.assertTrue(report.candidates)
        commands = [call.args[0] for call in connection.execute.call_args_list]
        self.assertTrue(all(sql.startswith(("SET ", "SELECT set_config")) for sql in commands))
        self.assertTrue(all(call.kwargs == {"force_rollback": True} for call in connection.transaction.call_args_list))
        with patch("pqo.transaction_indexes.psycopg.connect") as connect, self.assertRaises(ValueError):
            collect_transaction_index_candidates(text.replace("= 1", "= 2") + " ", replace(self.settings, dbname="other"), plans=plans)
        connect.assert_not_called()

    def test_cancel_and_no_eligible_plans_never_connect(self):
        text, inspection, plans = self.plan("SELECT 1")
        control = AnalysisControl()
        control.cancel()
        with patch("pqo.transaction_indexes.psycopg.connect") as connect, self.assertRaises(AnalysisCancelled):
            collect_transaction_index_candidates(text, self.settings, plans=plans, control=control)
        connect.assert_not_called()
        plans = replace(plans, statements=(StatementPlan(1, "error", "failed"),))
        with patch("pqo.transaction_indexes.psycopg.connect") as connect:
            report = collect_transaction_index_candidates(text, self.settings, plans=plans)
        self.assertFalse(report.candidates)
        connect.assert_not_called()

    def test_catalog_queries_preserve_index_validity_contract(self):
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (42, 100, 1024, 10, 5, 1, 2, ["a", "b"])
        connection.execute.return_value.fetchall.return_value = [("idx", 1, ["a", "b"], False)]
        metadata = _load_relation_indexes(connection, "university", "students")
        self.assertEqual(metadata.hot_updates, 2)
        self.assertFalse(metadata.indexes[0].compatible)
        sql = connection.execute.call_args_list[1].args[0]
        for expected in ("indisvalid", "indisready", "indislive", "indcheckxmin", "indpred", "indexprs", "opcdefault", "indcollation"):
            self.assertIn(expected, sql)
        self.assertNotIn("CREATE INDEX", sql)

    def test_candidate_limit_is_explicit_not_an_unbounded_cross_product(self):
        body = ";".join(f"SELECT full_name FROM university.t{i} WHERE campus_id=1" for i in range(100))
        metadata = {("university", f"t{i}"): replace(self.students, table=f"t{i}", indexes=()) for i in range(100)}
        report = self.report(body, metadata)
        self.assertEqual(len(report.candidates), 128)
        self.assertTrue(report.truncated)

    def test_metadata_error_does_not_silently_imply_no_indexes_exist(self):
        text, _, plans = self.plan("SELECT campus_id FROM university.students WHERE student_id=1")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("pqo.transaction_indexes.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_indexes._load_relation_indexes", side_effect=psycopg.errors.QueryCanceled("timeout")):
            report = collect_transaction_index_candidates(text, self.settings, plans=plans)
        self.assertFalse(report.candidates)
        self.assertTrue(any("метаданные не получены" in note for note in report.notes))

    def test_metadata_budget_and_cancellation_are_observed(self):
        text, _, plans = self.plan("SELECT campus_id FROM university.students WHERE student_id=1")
        connection = MagicMock()
        connection.__enter__.return_value = connection
        with patch("pqo.transaction_indexes.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_indexes.time.monotonic", side_effect=[0, 31]), \
             patch("pqo.transaction_indexes._load_relation_indexes") as load:
            report = collect_transaction_index_candidates(text, self.settings, plans=plans)
        load.assert_not_called()
        self.assertTrue(any("30 с" in note for note in report.notes))
        control = AnalysisControl()
        def cancel(*args):
            control.cancel()
            raise psycopg.errors.QueryCanceled("cancel")
        with patch("pqo.transaction_indexes.psycopg.connect", return_value=connection), \
             patch("pqo.transaction_indexes._load_relation_indexes", side_effect=cancel), \
             self.assertRaises(AnalysisCancelled):
            collect_transaction_index_candidates(text, self.settings, plans=plans, control=control)


@skipUnless(os.getenv("PQO_UNIVERSITY_TESTS") == "1", "requires isolated university test database")
class TransactionIndexesPostgresTests(TestCase):
    def test_shared_candidate_and_write_risk_without_data_or_index_changes(self):
        settings = replace(DatabaseSettings.from_env(), dbname="pqo_university_unseen",
                           allowed_schemas=frozenset({"university"}))
        path = Path(__file__).resolve().parents[1] / "examples/transactions/university-shared-index.sql"
        text = path.read_text(encoding="utf-8")
        def snapshot():
            with psycopg.connect(**settings.connection_kwargs()) as connection:
                return (connection.execute("SELECT * FROM university.enrollments WHERE student_id=17001 ORDER BY enrollment_id").fetchall(),
                    connection.execute("SELECT indexname,indexdef FROM pg_catalog.pg_indexes WHERE schemaname='university' ORDER BY indexname").fetchall())
        before = snapshot()
        plans = collect_transaction_plans(text, settings)
        self.assertTrue(all(plan.status == "planned" for plan in plans.statements))
        report = collect_transaction_index_candidates(text, settings, plans=plans)
        candidate = next(item for item in report.candidates if item.action.key_columns == ("student_id",) and not item.action.include_columns)
        self.assertGreaterEqual(len(candidate.uses), 3)
        self.assertEqual({write.kind for write in candidate.writes}, {"insert", "update", "delete"})
        changed_status = [item for item in report.candidates if "status" in (*item.action.key_columns, *item.action.include_columns)]
        self.assertTrue(any(write.changed_index_columns == ("status",) for item in changed_status for write in item.writes))
        self.assertEqual(before, snapshot())
