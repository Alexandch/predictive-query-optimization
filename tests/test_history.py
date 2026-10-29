import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from pqo.history import load_analysis_history, HistoryRecord, format_candidate_trial_history


class HistoryTests(unittest.TestCase):
    def test_exports_evidence_to_both_formats(self):
        import csv
        import json
        import tempfile
        from pathlib import Path
        from pqo.experiments import export_history_records
        evidence = {"version": 1, "checks": [], "max_candidates_per_step": 3}
        with tempfile.TemporaryDirectory() as directory:
            for suffix in ("csv", "json"):
                path = Path(directory) / f"history.{suffix}"
                export_history_records([self.record(evidence)], path)
                if suffix == "json":
                    exported = json.loads(path.read_text(encoding="utf-8"))[0]["candidate_trial_evidence"]
                else:
                    with path.open(encoding="utf-8-sig", newline="") as stream:
                        exported = json.loads(next(csv.DictReader(stream))["candidate_trial_evidence"])
                self.assertEqual(exported, evidence)

    def record(self, evidence=None):
        return HistoryRecord(1, datetime.now(timezone.utc), "completed", "SELECT 1",
                             None, None, None, (), None, candidate_trial_evidence=evidence)

    def test_legacy_evidence_is_unknown_not_zero_trials(self):
        self.assertIn("не сохранён", format_candidate_trial_history(self.record()))
        evidence = {"version": 1, "checks": [], "max_candidates_per_step": 3}
        self.assertIn("кандидатов: 0", format_candidate_trial_history(self.record(evidence)))

    def test_null_measurement_and_zero_measurement_are_distinct(self):
        check = dict(plan_step=1, proposed_ddl="CREATE INDEX ...", accepted=False,
                     rejection_reason="statement_timeout", before_time_ms=10,
                     after_time_ms=None, predicted_q=.1, index_size_bytes=None,
                     creation_time_ms=None)
        evidence = {"version": 1, "max_candidates_per_step": 3, "checks": [check]}
        self.assertIn("не завершён", format_candidate_trial_history(self.record(evidence)))
        check["after_time_ms"] = 0
        self.assertIn("10.00 → 0.00", format_candidate_trial_history(self.record(evidence)))

    def test_history_loads_optional_evidence_without_migrating(self):
        record = self.record()
        evidence = {"version": 1, "checks": [], "max_candidates_per_step": 3}
        row = (1, record.started_at, "completed", "SELECT 1", None, None, None,
               None, None, 3, 10, 10, 0, "index_not_used", None, None, False, evidence)
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = (True, False)
        connection.execute.return_value.fetchall.return_value = [row]
        with patch("psycopg.connect") as connect:
            connect.return_value.__enter__.return_value = connection
            records = load_analysis_history()
        self.assertEqual(records[0].candidate_trial_evidence, evidence)
        queries = "\n".join(call.args[0] for call in connection.execute.call_args_list)
        self.assertIn("to_jsonb(sequential)->'candidate_trial_evidence'", queries)
        self.assertNotIn("ALTER", queries)

    def test_rejects_invalid_limit(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 10000"):
            load_analysis_history(limit=0)


if __name__ == "__main__":
    unittest.main()
