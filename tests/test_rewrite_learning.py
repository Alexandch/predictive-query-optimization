import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pqo.rewrite_learning import prepare_dataset, rewrite_features, train_experiment, main


def record(run_id="a", original="SELECT a FROM retail.t", variant="SELECT b FROM retail.t", time=5):
    samples = {"baseline": [10., 10., 10.], "variant": [float(time)] * 3}
    return {
        "schema_version": 1, "run_id": run_id, "status": "completed", "source": "desktop",
        "dataset_role": "unassigned", "database_fingerprint": "db", "original_sql": original,
        "protocol": {"repetitions": 3, "order": "alternating", "pair_warmups": 1},
        "candidates": [{"status": "completed", "equivalence": True, "rule_id": "manual",
                        "variant_sql": variant, "baseline_samples_ms": samples["baseline"],
                        "variant_samples_ms": samples["variant"], "baseline_time_ms": 10.,
                        "rewritten_time_ms": float(time), "runtime_training_eligible": True,
                        "measurements": ([{"side": side, "phase": "warmup", "time_ms": values[0]}
                                          for side, values in samples.items()] +
                                         [{"side": side, "phase": "measurement", "time_ms": samples[side][index]}
                                          for index in range(3) for side in
                                          (("baseline", "variant") if index % 2 == 0 else ("variant", "baseline"))]),
                        "estimated_plans": {side: [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 10,
                                                               "Plan Rows": 100}}]
                                            for side in samples}}],
    }


class RewriteLearningTests(unittest.TestCase):
    def prepare(self, records, manifest=None):
        with tempfile.TemporaryDirectory() as directory:
            for index, run in enumerate(records):
                Path(directory, f"run-{index}.json").write_text(json.dumps(run), encoding="utf-8")
            return prepare_dataset([directory], manifest)

    def test_unassigned_data_is_audited_but_never_silently_selected(self):
        rows, report = self.prepare([record()])
        self.assertEqual(rows, [])
        self.assertEqual(report["technical_candidates"], 1)
        self.assertEqual(report["unassigned_run_ids"], ["a"])
        self.assertFalse(report["training_ready"])

    def test_explicit_selection_preserves_slow_examples(self):
        rows, _ = self.prepare([record(time=20)], {"development_run_ids": ["a"]})
        self.assertLess(rows[0]["label"], 0)

    def test_smoke_test_cannot_be_selected_for_training(self):
        run = record()
        run.update(source="smoke_test", dataset_role="test")
        rows, report = self.prepare([run], {"development_run_ids": ["a"]})
        self.assertFalse(rows)
        self.assertEqual(report["excluded"]["protected_test_or_control"], 1)

    def test_explicit_control_cannot_be_reassigned_to_development(self):
        run = record()
        run["dataset_role"] = "control"
        self.assertFalse(self.prepare([run], {"development_run_ids": ["a"]})[0])
        self.assertEqual(len(self.prepare([run], {"control_run_ids": ["a"]})[0]), 1)

    def test_forged_eligibility_does_not_override_invalid_measurements(self):
        for field, value, reason in (("equivalence", False, "unproven_equivalence"),
                                     ("baseline_samples_ms", [0, 0, 0], "invalid_samples"),
                                     ("rewritten_time_ms", 123, "median_mismatch"),
                                     ("measurements", [], "missing_warmup")):
            with self.subTest(field=field):
                run = record()
                run["candidates"][0][field] = value
                rows, report = self.prepare([run], {"development_run_ids": ["a"]})
                self.assertFalse(rows)
                self.assertEqual(report["excluded"][reason], 1)

    def test_estimated_features_ignore_actual_times_and_buffer_fields(self):
        run = record()
        candidate = run["candidates"][0]
        expected = rewrite_features(run["original_sql"], candidate["variant_sql"], "manual", candidate["estimated_plans"])
        candidate["estimated_plans"]["baseline"][0]["Plan"].update({
            "Actual Total Time": 9999, "Shared Read Blocks": 99999})
        actual = rewrite_features(run["original_sql"], candidate["variant_sql"], "manual", candidate["estimated_plans"])
        self.assertEqual(expected, actual)
        self.assertFalse(any("actual" in key or "blocks" in key for key in actual))

    def test_literals_do_not_create_new_independent_families(self):
        first = record(original="SELECT a FROM retail.t WHERE a > 1", variant="SELECT b FROM retail.t WHERE a > 1")
        second = record("b", "SELECT a FROM retail.t WHERE a > 2", "SELECT b FROM retail.t WHERE a > 2")
        rows, report = self.prepare([first, second], {"development_run_ids": ["a", "b"]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(report["families"]["development"], 1)

    def test_variant_to_original_leakage_is_rejected_across_databases(self):
        first = record()
        second = record("b", "SELECT b FROM retail.t", "SELECT c FROM retail.t")
        second["database_fingerprint"] = "another_db"
        with self.assertRaisesRegex(ValueError, "leakage"):
            self.prepare([first, second], {"development_run_ids": ["a"], "control_run_ids": ["b"]})

    def test_repeated_clicks_collapse_to_one_row(self):
        first, second = record(), record("b", time=20)
        rows, report = self.prepare([first, second], {"development_run_ids": ["a", "b"]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_ids"], ["a", "b"])
        self.assertAlmostEqual(rows[0]["label"], 0)
        self.assertEqual(report["families"]["development"], 1)

    def test_unassigned_bridge_still_connects_related_families(self):
        first = record()
        bridge = record("bridge", "SELECT b FROM retail.t", "SELECT c FROM retail.t")
        last = record("last", "SELECT c FROM retail.t", "SELECT d FROM retail.t")
        with self.assertRaisesRegex(ValueError, "leakage"):
            self.prepare([first, bridge, last], {"development_run_ids": ["a"], "control_run_ids": ["last"]})

    def test_false_alternating_protocol_is_rejected(self):
        run = record()
        run["candidates"][0]["measurements"].reverse()
        _, report = self.prepare([run])
        self.assertEqual(report["excluded"].get("pair_order_mismatch"), 1)

    def test_selection_and_labels_are_fingerprinted(self):
        _, first = self.prepare([record()], {"development_run_ids": ["a"]})
        _, second = self.prepare([record(time=20)], {"development_run_ids": ["a"]})
        self.assertNotEqual(first["dataset_sha256"], second["dataset_sha256"])

    def test_audit_never_connects_to_database(self):
        with patch("psycopg.connect", side_effect=AssertionError("must stay offline")):
            self.assertEqual(self.prepare([record()])[1]["technical_candidates"], 1)

    def test_missing_estimates_not_replaced_with_fake_zero_costs(self):
        run = record()
        run["candidates"][0]["estimated_plans"]["baseline"] = [{"Plan": {"Node Type": "Result"}}]
        _, report = self.prepare([run])
        self.assertEqual(report["excluded"]["Incomplete estimated plan"], 1)

    def test_unknown_or_duplicate_manifest_ids_rejected(self):
        for manifest in ({"development_run_ids": ["unknown"]},
                         {"development_run_ids": ["a"], "control_run_ids": ["a"]}):
            with self.assertRaises(ValueError):
                self.prepare([record()], manifest)

    def test_conflicting_duplicate_run_ids_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.prepare([record(), record(time=20)])

    def test_corrupt_record_does_not_block_valid_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "run-invalid.json").write_text("broken")
            Path(directory, "run-ok.json").write_text(json.dumps(record()))
            _, report = prepare_dataset([directory])
            self.assertEqual(report["technical_candidates"], 1)
            self.assertEqual(report["runs"], 2)

    def test_insufficient_data_refuses_model_even_with_forged_report(self):
        rows, report = self.prepare([record()], {"development_run_ids": ["a"]})
        report["training_ready"] = True
        with self.assertRaisesRegex(ValueError, "Not enough"):
            train_experiment(rows, report)

    def test_training_has_no_automatic_activation(self):
        # Synthetic fixtures test code only, not application quality.
        rows = []
        for index in range(40):
            rows.append({"role": "development" if index < 30 else "control", "family": str(index),
                         "features": {"rule_id": "manual", "cost": index % 10},
                         "label": (index % 10) / 10, "run_ids": [str(index)]})
        artifact, metrics = train_experiment(rows, {"training_ready": True})
        self.assertFalse(metrics["automatic_activation"])
        self.assertEqual(len(artifact["provenance"]), 40)

    def test_cli_refusal_writes_report_not_model(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("sys.argv", ["rewrite-learning", "--directory", str(root),
                                    "--report", str(root / "audit.json"),
                                    "--train-output", str(root / "experiment")]):
                self.assertEqual(main(), 2)
            self.assertTrue((root / "audit.json").exists())
            self.assertFalse((root / "experiment").exists())
