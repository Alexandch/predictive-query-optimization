import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from pqo.config import DatabaseSettings
from pqo.rewrite_journal import RewriteJournalRun, iter_journal, journal_summary, sql_identity, export_rewrite_journal
from pqo.sql_rewrite import SQLRewriteCandidate, SQLRewriteEvaluation, SQLRewritePlan, evaluate_sql_rewrites, evaluate_sql_variant


class RewriteJournalTests(unittest.TestCase):
    def make_run(self, directory, source="api"):
        return RewriteJournalRun(directory, "SELECT 1", DatabaseSettings(password="never-store-this-secret"),
                                 mode="manual", repetitions=2, thresholds={}, source=source)

    def test_failed_runtime_candidate_is_retained_and_not_given_a_zero_time(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(directory)
            run.candidates([SQLRewriteCandidate("manual", "test", "SELECT 2")])
            run.candidate_stage(0, "equivalence")
            result = SQLRewriteEvaluation(SQLRewriteCandidate("manual", "test", "SELECT 2"), False,
                                          None, None, None, False, "result_mismatch")
            run.evaluation(0, result)
            plan = SQLRewritePlan("SELECT 1", (result,), None, 0, 0, 0, "result_mismatch")
            run.finish(plan)
            saved = list(iter_journal(directory))[0]
            candidate = saved["candidates"][0]
            self.assertFalse(candidate["runtime_training_eligible"])
            self.assertIsNone(candidate["rewritten_time_ms"])
            self.assertFalse(candidate["equivalence"])
            self.assertEqual(saved["dataset_role"], "unassigned")
            self.assertNotIn("never-store-this-secret", run.path.read_text())

    def test_slow_equivalent_variant_is_valid_runtime_training_data_but_unassigned(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(directory)
            candidate = SQLRewriteCandidate("manual", "slow", "SELECT 2")
            run.candidates([candidate])
            for _ in range(2):
                run.measurement(0, "baseline", "measurement", 10.0, [{"Plan": {"Node Type": "Result"}}])
                run.measurement(0, "variant", "measurement", 20.0, [{"Plan": {"Node Type": "Result"}}])
            result = SQLRewriteEvaluation(candidate, True, 10, 20, -1, False, "improvement_too_small")
            run.evaluation(0, result)
            run.finish(SQLRewritePlan("SELECT 1", (result,), None, 0, 0, 0, "no_measured_improvement"))
            row = journal_summary(directory)[0]
            self.assertTrue(row["runtime_training_eligible"])
            self.assertFalse(row["accepted"])
            self.assertEqual(row["dataset_role"], "unassigned")
            exported = Path(directory) / "export.json"
            export_rewrite_journal(directory, exported)
            self.assertEqual(len(json.loads(exported.read_text())[0]["candidates"][0]["measurements"]), 4)

    def test_no_candidates_does_not_connect_and_can_be_logged(self):
        with tempfile.TemporaryDirectory() as directory, patch("psycopg.connect", side_effect=AssertionError("must not connect")):
            plan = evaluate_sql_rewrites("SELECT 1", journal_directory=directory)
            self.assertEqual(plan.terminal_reason, "no_candidates")
            record = list(iter_journal(directory))[0]
            self.assertEqual(record["status"], "completed")
            self.assertIsNone(record["baseline_probe"])
            self.assertFalse(record["candidates"])

    def test_invalid_manual_sql_is_logged_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory, patch("psycopg.connect", side_effect=AssertionError("must not connect")):
            with self.assertRaises(ValueError):
                evaluate_sql_variant("SELECT 1", "DELETE FROM public.orders", journal_directory=directory)
            run = list(iter_journal(directory))[0]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["error"]["stage"], "validation")

    def test_log_failure_does_not_break_a_valid_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "file"
            blocker.write_text("keep")
            plan = evaluate_sql_rewrites("SELECT 1", journal_directory=blocker)
            self.assertEqual(plan.terminal_reason, "no_candidates")
            self.assertIsNotNone(plan.journal_warning)
            self.assertEqual(blocker.read_text(), "keep")

    def test_incomplete_and_corrupt_records_are_visible_and_not_eligible(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(directory)
            run.candidates([SQLRewriteCandidate("manual", "test", "SELECT 2")])
            (Path(directory) / "run-broken.json").write_text("{broken")
            (Path(directory) / ".rewrite-incomplete.tmp").write_text("partial")
            rows = journal_summary(directory)
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["status"] for row in rows}, {"running", "unreadable"})
            self.assertFalse(any(row["runtime_training_eligible"] for row in rows))

    def test_atomic_replace_failure_preserves_previous_json(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(directory)
            previous = run.path.read_text()
            with patch("pqo.rewrite_journal.os.replace", side_effect=OSError("disk full")):
                run.stage("measurement")
            self.assertEqual(run.path.read_text(), previous)
            self.assertIsNotNone(run.warning)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_parallel_checks_have_unique_valid_files_and_smoke_source_is_test(self):
        with tempfile.TemporaryDirectory() as directory:
            with ThreadPoolExecutor(max_workers=4) as pool:
                runs = list(pool.map(lambda _: self.make_run(directory, "smoke_test"), range(8)))
            self.assertEqual(len({item.data["run_id"] for item in runs}), 8)
            self.assertEqual(len(list(iter_journal(directory))), 8)
            self.assertTrue(all(item["dataset_role"] == "test" for item in iter_journal(directory)))

    def test_identity_groups_literal_variants_and_export_is_spreadsheet_safe(self):
        first, second = sql_identity("SELECT * FROM t WHERE id=1"), sql_identity("SELECT * FROM t WHERE id=2")
        self.assertNotEqual(first["sql_hash"], second["sql_hash"])
        self.assertEqual(first["shape_hash"], second["shape_hash"])
        with tempfile.TemporaryDirectory() as directory:
            run = self.make_run(directory)
            run.candidates([SQLRewriteCandidate("manual", "bad", "=1+1")])
            destination = Path(directory) / "export.csv"
            export_rewrite_journal(directory, destination)
            with destination.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream))[0]["variant_sql"], "'=1+1")
            with self.assertRaises(ValueError):
                export_rewrite_journal(directory, run.path)
