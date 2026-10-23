import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pqo.config import DatabaseSettings
from pqo.explain import _assert_read_only_query
from pqo.rewrite_batch import Relationship, build_cases, collect_batch, _batch_lock
from pqo.rewrite_learning import prepare_dataset
from pqo.sql_rewrite import SQLRewritePlan, generate_sql_rewrites


class RewriteBatchTests(unittest.TestCase):
    def setUp(self):
        self.relations = [Relationship("retail", "order_items", "order_id", "retail", "customer_orders", "order_id"),
                          Relationship("logistics", "tracking_events", "shipment_id", "logistics", "shipments", "shipment_id")]
        self.settings = replace(DatabaseSettings(), statement_timeout_ms=1000)

    def result(self, *args, **kwargs):
        return SQLRewritePlan(args[0], (), None, 0, 2, .05, "no_candidates", journal_run_id="run-123")

    def test_seven_rules_are_generated_and_read_only(self):
        cases = build_cases(self.relations)
        self.assertEqual(len(cases), 14)
        rules = set()
        for case in cases:
            _assert_read_only_query(case.sql)
            candidates = generate_sql_rewrites(case.sql)
            self.assertEqual(len(candidates), 1, case.template)
            rules.add(candidates[0].rule_id)
        self.assertEqual(len(rules), 7)

    def test_order_and_splits_do_not_depend_on_catalog_order(self):
        self.assertEqual(build_cases(self.relations), build_cases(list(reversed(self.relations))))
        groups = {}
        for case in build_cases(self.relations):
            groups.setdefault(case.family_id, set()).add(case.role)
        self.assertTrue(all(len(roles) == 1 for roles in groups.values()))

    def test_literal_changes_keep_same_relationship_group(self):
        small, large = build_cases(self.relations, key_upper=32), build_cases(self.relations, key_upper=512)
        self.assertEqual({case.family_id for case in small}, {case.family_id for case in large})
        self.assertNotEqual([case.case_id for case in small], [case.case_id for case in large])

    def test_limits_and_statement_timeouts_are_validated_before_execution(self):
        with tempfile.TemporaryDirectory() as directory, patch("pqo.rewrite_batch.evaluate_sql_rewrites") as execute:
            with self.assertRaises(ValueError):
                collect_batch([], self.settings, Path(directory) / "out", max_cases=0)
            with self.assertRaises(ValueError):
                collect_batch([], replace(self.settings, statement_timeout_ms=0), Path(directory) / "out")
            execute.assert_not_called()

    def test_case_limit_then_resume_without_reexecuting_completed_cases(self):
        cases = build_cases(self.relations)[:3]
        with tempfile.TemporaryDirectory() as directory, patch("pqo.rewrite_batch.evaluate_sql_rewrites", side_effect=self.result) as execute:
            output = Path(directory) / "out"
            state = collect_batch(cases, self.settings, output, max_cases=1)
            self.assertEqual(state["status"], "budget_exhausted")
            self.assertEqual(execute.call_count, 1)
            state = collect_batch(cases, self.settings, output, max_cases=3, resume=True)
            self.assertEqual(state["status"], "completed")
            self.assertEqual(execute.call_count, 3)
            self.assertTrue((output / "split.json").exists())
            self.assertEqual(execute.call_args.kwargs["minimum_baseline_time_ms"], 0)
            self.assertIn(execute.call_args.kwargs["journal_source"], {"batch_development", "batch_control"})

    def test_error_does_not_abort_remaining_cases(self):
        cases = build_cases(self.relations)[:2]
        with tempfile.TemporaryDirectory() as directory, patch("pqo.rewrite_batch.evaluate_sql_rewrites",
                                                              side_effect=[TimeoutError("timeout"), self.result(cases[1].sql)]):
            state = collect_batch(cases, self.settings, Path(directory) / "out")
            self.assertEqual([entry["status"] for entry in state["cases"]], ["failed", "completed"])
            self.assertEqual(state["cases"][0]["error"]["type"], "TimeoutError")

    def test_resume_rejects_changed_protocol_database_or_suite(self):
        cases = build_cases(self.relations)[:1]
        with tempfile.TemporaryDirectory() as directory, patch("pqo.rewrite_batch.evaluate_sql_rewrites", side_effect=self.result):
            output = Path(directory) / "out"
            collect_batch(cases, self.settings, output)
            for settings, repetitions, selected in ((replace(self.settings, dbname="other"), 3, cases),
                                                     (self.settings, 4, cases),
                                                     (self.settings, 3, [])):
                with self.assertRaisesRegex(ValueError, "configuration"):
                    collect_batch(selected, settings, output, repetitions=repetitions, resume=True)

    def test_interrupted_case_is_retried_not_marked_successful(self):
        cases = build_cases(self.relations)[:1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with patch("pqo.rewrite_batch.evaluate_sql_rewrites", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    collect_batch(cases, self.settings, output)
            with patch("pqo.rewrite_batch.evaluate_sql_rewrites", side_effect=self.result) as execute:
                state = collect_batch(cases, self.settings, output, resume=True)
                execute.assert_called_once()
                self.assertEqual(state["status"], "completed")
                self.assertEqual([entry["status"] for entry in state["cases"]], ["interrupted", "completed"])

    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                collect_batch([], self.settings, directory)

    def test_saved_suite_cannot_escape_schema_scope_or_execute_dml(self):
        case = build_cases(self.relations)[0]
        with tempfile.TemporaryDirectory() as directory, patch("pqo.rewrite_batch.evaluate_sql_rewrites") as execute:
            for sql in ("SELECT * FROM secret.records", "DELETE FROM retail.order_items"):
                with self.assertRaises(ValueError):
                    collect_batch([replace(case, sql=sql)], self.settings, Path(directory) / "out")
            execute.assert_not_called()

    def test_concurrent_resume_is_rejected_and_lock_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            with _batch_lock(directory):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with _batch_lock(directory):
                        self.fail("concurrent batch acquired the lock")
            with _batch_lock(directory):
                pass

    def test_learning_uses_curated_family_not_only_sql_shape(self):
        from test_rewrite_learning import record
        first = record(original="SELECT a FROM retail.first", variant="SELECT b FROM retail.first")
        second = record("b", original="SELECT x FROM logistics.second", variant="SELECT y FROM logistics.second")
        for run in (first, second):
            run["collection"] = {"family_id": "same-relationship"}
        with tempfile.TemporaryDirectory() as directory:
            for index, run in enumerate((first, second)):
                Path(directory, f"run-{index}.json").write_text(json.dumps(run))
            with self.assertRaisesRegex(ValueError, "leakage"):
                prepare_dataset([directory], {"development_run_ids": ["a"], "control_run_ids": ["b"]})
