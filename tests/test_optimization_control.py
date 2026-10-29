import unittest
from dataclasses import dataclass
from pathlib import Path
import json
import tempfile
from unittest.mock import MagicMock, patch
from pqo.config import DatabaseSettings
from pqo.optimization_control import control_cases
from pqo.explain import _assert_read_only_query
from pqo.sql_rewrite import generate_sql_rewrites


class OptimizationControlTests(unittest.TestCase):
    def test_defense_examples_match_fixed_control_manifest(self):
        from sqlglot import parse_one
        root = Path(__file__).resolve().parents[1]
        cases = {case.name: case.sql for case in control_cases()}
        for filename, case in (
            ("01-logistics-index.sql", "logistics-index"),
            ("02-aviation-rewrite.sql", "aviation-rewrite"),
            ("03-retail-no-change.sql", "retail-negative"),
        ):
            sql = (root / "examples" / "defense" / filename).read_text(encoding="utf-8")
            self.assertEqual(parse_one(sql, read="postgres"), parse_one(cases[case], read="postgres"))

    def test_report_preserves_failed_cases_continues_and_refuses_overwrite(self):
        from pqo.optimization_control import run_control
        @dataclass
        class Plan:
            terminal_reason: str = "below_runtime_threshold"
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = ("testdb", "PostgreSQL test")
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"
            model.write_bytes(b"fixed-model")
            output = Path(directory) / "diagnostic"
            with patch("psycopg.connect") as connect, \
                 patch("pqo.optimization_control.control_cases", return_value=control_cases()[:2]), \
                 patch("pqo.optimization_control.index_inventory", return_value=[]), \
                 patch("pqo.optimization_control.recommend_sequential_indexes", side_effect=[RuntimeError("failed trial"), Plan()]):
                connect.return_value.__enter__.return_value = connection
                report = run_control(output, model, DatabaseSettings(), repetitions=1)
                self.assertEqual(report["status"], "completed_with_errors")
                self.assertEqual([row["status"] for row in report["results"]], ["error", "completed"])
                self.assertTrue(report["index_inventory_unchanged"])
                self.assertTrue(report["model_unchanged"])
                self.assertEqual(json.loads((output / "report.json").read_text())["status"], "completed_with_errors")
                with self.assertRaises(FileExistsError):
                    run_control(output, model, DatabaseSettings())

    def test_fixed_matrix_contains_three_scenarios_for_each_schema(self):
        cases = control_cases()
        self.assertEqual(len(cases), 9)
        self.assertEqual(len({case.name for case in cases}), 9)
        for schema in ("aviation", "retail", "logistics"):
            self.assertEqual({case.mode for case in cases if case.schema == schema},
                             {"index", "rewrite", "both"})
        for case in cases:
            _assert_read_only_query(case.sql)
            if case.mode == "rewrite":
                variants = generate_sql_rewrites(case.sql)
                self.assertTrue(any(item.rule_id.startswith("combined:") for item in variants))
            if case.mode == "both":
                self.assertFalse(generate_sql_rewrites(case.sql))
